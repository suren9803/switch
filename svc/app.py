#!/usr/bin/env python3
"""Switch 游戏出包服务 —— Web 入口。

    # 建议用带 Apple 依赖的 venv：
    .vendor/venv/bin/pip install -r requirements-apple.txt
    .vendor/venv/bin/python app.py        # http://127.0.0.1:8772

账号体系、设备绑定、上传出包、进度查询、IPA 下载在这一层；真正的打包在 worker.py
里，由同一个进程起一个线程跑。开发阶段单进程最省事，要扩就再加 worker 进程。

向导（登录后三步）：上传游戏 → Apple ID → 安装到手机。
Apple ID 路径走 apple_provision（GSA + developerservices2），产出 p12/profile 供 zsign。

English
-------
Web entry point for the packing service.

Routes cover the whole user journey: invite-only registration, login with server-side
sessions, device binding (either a Profile Service .mobileconfig or a manually pasted
UDID), NSP upload, job submission, live progress polling, IPA download and OTA install.

Uploads land in uploads/job<N>/ and are deleted when the job ends. Only the finished IPA
in outputs/ survives. The pack worker and the auto-renewer run as daemon threads inside
this same process: at this scale a separate queue is more moving parts than it is worth.
"""
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import models
import signer
import worker as packer

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(HERE, 'uploads')
os.makedirs(UPLOADS, exist_ok=True)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PASSWORD_MIN = 8
MAX_UPLOAD = 8 * 1024 ** 3          # 8 GB：一个游戏本体 + 更新档
OTA_TOKEN_TTL = 2 * 60 * 60         # 安装清单 / 下载链接有效 2 小时

# 向导三步（登录在轨外）
STEP_UPLOAD = 1
STEP_APPLE = 2
STEP_INSTALL = 3
STEP_META = (
    (STEP_UPLOAD, '上传游戏', 'upload'),
    (STEP_APPLE, '获取 UDID', 'bind_device'),
    (STEP_INSTALL, '安装到手机', None),  # URL 取决于 active / latest done job
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD
app.config['UPLOAD_CHUNK'] = 8 * 1024 * 1024
# 服务跑在 cloudflared / nginx 这类反向代理后面：X-Forwarded-Proto 告诉 Flask 真实协议，
# 否则 request.host_url 会一直是 http，而 iOS 装描述文件、OTA 安装都要求 https。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def public_base():
    """对外地址：优先用配置里的公网 URL，没有才退回请求头。"""
    return (os.environ.get('SVC_PUBLIC_URL', '').rstrip('/') or request.host_url.rstrip('/'))


def _secret():
    p = os.path.join(HERE, 'data', 'secret_key')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        return open(p, 'rb').read()
    k = secrets.token_bytes(32)
    open(p, 'wb').write(k)
    return k


app.secret_key = _secret()

# worker 线程：同一个进程里跑，开发阶段够用
_worker = None
_renewer = None


def start_worker():
    global _worker, _renewer
    if _worker is None or not _worker.is_alive():
        _worker = packer.Worker()
        _worker.start()
    if _renewer is None or not _renewer.is_alive():
        _renewer = packer.Renewer()
        _renewer.start()


# ---------------------------------------------------------------- 会话

def current_user():
    uid = session.get('uid')
    return models.user_by_id(uid) if uid else None


def login_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not current_user():
            flash('先登录', 'warn')
            return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return inner


def _has_apple(uid):
    """向导第 2 步完成：填了 Apple ID，或高级路径已有 p12 文件。"""
    return models.has_apple_id(uid) or signer.has_creds(uid)


def user_progress(u):
    """当前用户在向导里的进度。

    规则：
      if not jobs and not active → UPLOAD
      elif not has_apple → APPLE
      elif active or latest_done → INSTALL
      else → UPLOAD（可再传）
    有 active_job 且非 uploading 时强制 INSTALL；uploading 时 UPLOAD 为 current。
    """
    if not u:
        return None
    uid = u['id']
    devices = models.devices_of(uid)
    has_device = len(devices) >= 1
    has_apple = _has_apple(uid)
    has_creds = signer.has_creds(uid)
    active = models.active_job(uid)
    jobs = models.jobs_of(uid, limit=1)
    latest_done = models.latest_done_job(uid)
    has_job_ever = bool(jobs)

    if active:
        # 上传中仍算第 1 步；排队/出包才进第 3 步
        if active['state'] == 'uploading':
            step = STEP_UPLOAD
        else:
            step = STEP_INSTALL
    elif not has_job_ever:
        step = STEP_UPLOAD
    elif not has_device:
        step = STEP_APPLE  # 步骤 2：获取 UDID
    elif latest_done:
        step = STEP_INSTALL
    else:
        step = STEP_UPLOAD

    steps = []
    for n, name, endpoint in STEP_META:
        if n < step:
            status = 'done'
        elif n == step:
            status = 'current'
        else:
            status = 'locked'
        # 上传步：一旦有过任务就标 done（除非当前就是 UPLOAD）
        if n == STEP_UPLOAD and has_job_ever and step != STEP_UPLOAD:
            status = 'done'
        if n == STEP_APPLE and has_device and step != STEP_APPLE:
            status = 'done'
        if n == STEP_INSTALL and active:
            url = url_for('job_view', jid=active['id'])
        elif n == STEP_INSTALL and latest_done:
            url = url_for('job_view', jid=latest_done['id'])
        elif endpoint:
            url = url_for(endpoint)
        else:
            url = None
        steps.append({
            'n': n,
            'name': name,
            'status': status,
            'url': url,
            'clickable': status == 'done' or status == 'current',
        })
    return {
        'step': step,
        'has_device': has_device,
        'has_apple': has_apple,
        'has_creds': has_creds,
        'has_apple_id': models.has_apple_id(uid),
        'active_job': active,
        'latest_done': latest_done,
        'credits': (u['credits'] if 'credits' in u.keys() else 0) or 0,
        'device_count': len(devices),
        'steps': steps,
    }


def wizard_url(u):
    """登录后 / 首页落到的第一个未完成步骤（有活跃任务则进任务页）。"""
    p = user_progress(u)
    if not p:
        return url_for('login')
    if p['active_job']:
        return url_for('job_view', jid=p['active_job']['id'])
    if p['step'] == STEP_INSTALL and p.get('latest_done'):
        return url_for('job_view', jid=p['latest_done']['id'])
    return {
        STEP_UPLOAD: url_for('upload'),
        STEP_APPLE: url_for('bind_device'),
        STEP_INSTALL: url_for('upload'),
    }[p['step']]


def gate_steps(need_apple=False, need_signed=False):
    """若前置步骤未完成，flash 并跳到第一个未完成步。上传 / Apple ID 互不强制。"""
    u = current_user()
    p = user_progress(u)
    if need_apple and not p['has_apple']:
        flash('请先填写 Apple ID（或上传证书）', 'warn')
        return redirect(url_for('creds'))
    if need_signed:
        # 安装页额外检查在路由内做
        pass
    return None


@app.context_processor
def inject():
    u = current_user()
    p = user_progress(u) if u else None
    # 任务 / 安装页：默认第 3 步为当前；若仍在 uploading，则第 1 步为当前
    if p and request.endpoint in ('job_view', 'ota_page'):
        job_state = None
        jid = (request.view_args or {}).get('jid')
        if jid is not None:
            job = models.job_by_id(jid)
            if job:
                job_state = job['state']
        p = dict(p)
        if job_state == 'uploading':
            p['step'] = STEP_UPLOAD
            steps = []
            for s in p['steps']:
                s = dict(s)
                if s['n'] == STEP_UPLOAD:
                    s['status'] = 'current'
                    s['clickable'] = True
                    s['url'] = url_for('upload')
                elif s['n'] == STEP_APPLE:
                    s['status'] = 'done' if p.get('has_apple') else 'locked'
                    s['clickable'] = s['status'] == 'done'
                else:
                    s['status'] = 'locked'
                    s['clickable'] = False
                    if jid is not None:
                        s['url'] = url_for('job_view', jid=jid)
                steps.append(s)
            p['steps'] = steps
        else:
            p['step'] = STEP_INSTALL
            steps = []
            for s in p['steps']:
                s = dict(s)
                if s['n'] < STEP_INSTALL:
                    s['status'] = 'done'
                    s['clickable'] = True
                elif s['n'] == STEP_INSTALL:
                    s['status'] = 'current'
                    s['clickable'] = True
                    if not s.get('url') and jid is not None:
                        s['url'] = url_for('job_view', jid=jid)
                steps.append(s)
            p['steps'] = steps
    return {'user': u, 'progress': p}


# ---------------------------------------------------------------- OTA 短时令牌（installd 无 cookie）

def make_ota_token(jid, ttl=OTA_TOKEN_TTL):
    exp = int(time.time()) + ttl
    msg = f'{jid}:{exp}'.encode()
    sig = hmac.new(app.secret_key if isinstance(app.secret_key, bytes)
                   else str(app.secret_key).encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f'{exp}.{sig}'


def verify_ota_token(jid, token):
    if not token or '.' not in token:
        return False
    exp_s, sig = token.split('.', 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    msg = f'{jid}:{exp}'.encode()
    expect = hmac.new(app.secret_key if isinstance(app.secret_key, bytes)
                      else str(app.secret_key).encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


def _job_access_ok(job, jid):
    """会话用户匹配，或带有效 OTA token（给 iOS installd）。"""
    token = request.args.get('t') or request.headers.get('X-OTA-Token')
    if token and verify_ota_token(jid, token):
        return True
    u = current_user()
    return bool(u and job and job['user_id'] == u['id'])


# ---------------------------------------------------------------- 页面

@app.route('/')
def index():
    u = current_user()
    if u:
        return redirect(wizard_url(u))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        pw = request.form.get('password') or ''
        pw2 = request.form.get('password2') or ''
        code = (request.form.get('invite') or '').strip().upper()
        inv = models.invite_by_code(code) if code else None
        if not code:
            flash('需要邀请码', 'err')
        elif not inv:
            flash('邀请码不存在', 'err')
        elif inv['used_by']:
            flash('这个邀请码已经被用过了', 'err')
        elif not EMAIL_RE.match(email):
            flash('邮箱格式不对', 'err')
        elif len(pw) < PASSWORD_MIN:
            flash(f'密码至少 {PASSWORD_MIN} 位', 'err')
        elif pw != pw2:
            flash('两次输入不一致', 'err')
        elif models.user_by_email(email):
            flash('这个邮箱已经注册过', 'err')
        else:
            uid = models.create_user(email, generate_password_hash(pw), credits=inv['credits'])
            if not models.redeem_invite(code, uid):
                flash('邀请码刚刚被用掉了，换一个', 'err')
            else:
                session['uid'] = uid
                flash('注册成功', 'ok')
                return redirect(wizard_url(current_user()))
    return render_template('register.html')


def admin_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        u = current_user()
        if not u or not u['is_admin']:
            abort(403)
        return fn(*a, **kw)
    return inner


@app.route('/admin/invites', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_invites():
    u = current_user()
    if request.method == 'POST':
        n = max(1, min(50, int(request.form.get('count') or 1)))
        credits = max(1, min(100, int(request.form.get('credits') or 1)))
        note = (request.form.get('note') or '').strip()[:60]
        codes = models.make_invite(u['id'], note=note, credits=credits, n=n)
        flash('生成了 ' + '、'.join(codes), 'ok')
        return redirect(url_for('admin_invites'))
    return render_template('admin_invites.html', invites=models.list_invites())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        pw = request.form.get('password') or ''
        row = models.user_by_email(email)
        if row and check_password_hash(row['password_hash'], pw):
            session['uid'] = row['id']
            models.touch_login(row['id'])
            nxt = request.args.get('next', '')
            if nxt.startswith('/'):
                return redirect(nxt)
            return redirect(wizard_url(current_user()))
        flash('邮箱或密码不对', 'err')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    """任务向导首页：跳到第一个未完成步骤（有活跃任务则进任务页）。"""
    return redirect(wizard_url(current_user()))


# ---------------------------------------------------------------- 上传出包

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    u = current_user()
    # 上传不要求 Apple ID / 设备；有进行中的任务时 GET 回任务页
    active = models.active_job(u['id'])
    if active and request.method == 'GET':
        return redirect(url_for('job_view', jid=active['id']))

    devices = models.devices_of(u['id'])
    if request.method == 'POST':
        # 非 JS 回退：先建 uploading 任务、落盘，再改 queued（同样避免 worker 抢未写完的文件）
        device_id = request.form.get('device') or ''
        dev = models.device_by_id(device_id, u['id']) if device_id else None

        files = [f for f in request.files.getlist('nsp') if f and f.filename]
        if not files:
            flash('选一个 .nsp 文件', 'err')
            return redirect(url_for('upload'))
        if not all(f.filename.lower().endswith('.nsp') for f in files):
            flash('只接受 .nsp 文件', 'err')
            return redirect(url_for('upload'))

        if not models.spend_credit(u['id']):
            flash('出包次数用完了', 'err')
            return redirect(url_for('dashboard'))

        jid = models.create_job(u['id'], dev['id'] if dev else None, '', files[0].filename,
                                state='uploading')
        work = os.path.join(UPLOADS, f'job{jid}')
        os.makedirs(work, exist_ok=True)
        models.update_job(jid, game_dir=work, stage='上传中', progress=0, message='正在保存文件')
        try:
            saved, title_id = _save_nsp_files(work, files)
            fields = dict(state='queued', game_dir=work, nsp_name=', '.join(saved),
                          stage='排队中', progress=0, message='上传完成，等待出包')
            if title_id:
                fields['title_id'] = title_id
            models.update_job(jid, **fields)
        except Exception as ex:
            models.update_job(jid, state='failed', stage='失败',
                              message=f'上传失败：{ex}'[:600], finished_at=models.now())
            models.refund_credit(u['id'])
            flash(f'上传失败：{ex}', 'err')
            return redirect(url_for('upload'))
        return redirect(url_for('job_view', jid=jid))
    ctx = _apple_context(u['id'])
    return render_template('upload.html', ready=True, **ctx)


def _save_nsp_files(work, files):
    """把 multipart 里的 .nsp 安全地写到 job 目录。返回 (saved_names, title_id_or_None)。"""
    saved = []
    title_id = None
    for f in files:
        name = os.path.basename(f.filename).replace('\\', '_').replace('/', '_')
        if not name.lower().endswith('.nsp') or not name.strip('.'):
            raise ValueError('只接受 .nsp 文件')
        path = os.path.join(work, name)
        # 禁止路径穿越：最终路径必须仍在 work 下
        if os.path.commonpath([os.path.abspath(work), os.path.abspath(path)]) != os.path.abspath(work):
            raise ValueError('非法文件名')
        f.save(path)
        saved.append(name)
        if title_id is None:
            m = re.search(r'\[([0-9A-Fa-f]{16})\]', name)
            if m:
                title_id = m.group(1)
    if not saved:
        raise ValueError('选一个 .nsp 文件')
    return saved, title_id


@app.route('/api/upload/start', methods=['POST'])
@login_required
def api_upload_start():
    """XHR：先扣额度、建 uploading 任务，再返回上传地址。"""
    u = current_user()
    device_id = request.form.get('device') or ''
    if request.is_json:
        device_id = (request.get_json(silent=True) or {}).get('device') or device_id
    dev = models.device_by_id(device_id, u['id']) if device_id else None

    if not models.spend_credit(u['id']):
        return jsonify({'ok': False, 'error': '出包次数用完了'}), 400

    jid = models.create_job(u['id'], dev['id'] if dev else None, '', None, state='uploading')
    work = os.path.join(UPLOADS, f'job{jid}')
    os.makedirs(work, exist_ok=True)
    models.update_job(jid, game_dir=work, stage='上传中', progress=0, message='等待上传文件')
    return jsonify({
        'ok': True,
        'jid': jid,
        'upload_url': url_for('api_upload_file', jid=jid),
    })


@app.route('/api/upload/<int:jid>', methods=['POST'])
@login_required
def api_upload_file(jid):
    """XHR：接收 nsp，写完后才改 queued，避免 worker 碰正在写的文件。"""
    u = current_user()
    job = models.job_by_id(jid)
    if not job or job['user_id'] != u['id']:
        abort(404)
    if job['state'] != 'uploading':
        return jsonify({'ok': False, 'error': '任务不在上传状态'}), 400

    work = job['game_dir'] or os.path.join(UPLOADS, f'job{jid}')
    os.makedirs(work, exist_ok=True)
    files = [f for f in request.files.getlist('nsp') if f and f.filename]
    try:
        if not files:
            raise ValueError('选一个 .nsp 文件')
        if not all(f.filename.lower().endswith('.nsp') for f in files):
            raise ValueError('只接受 .nsp 文件')
        models.update_job(jid, stage='上传中', message='正在保存文件')
        saved, title_id = _save_nsp_files(work, files)
        fields = dict(state='queued', game_dir=work, nsp_name=', '.join(saved),
                      stage='排队中', progress=0, message='上传完成，等待出包')
        if title_id:
            fields['title_id'] = title_id
        models.update_job(jid, **fields)
        return jsonify({'ok': True, 'jid': jid, 'redirect': url_for('job_view', jid=jid)})
    except Exception as ex:
        models.update_job(jid, state='failed', stage='失败',
                          message=f'上传失败：{ex}'[:600], finished_at=models.now())
        models.refund_credit(u['id'])
        return jsonify({'ok': False, 'error': str(ex)}), 400


@app.route('/job/<int:jid>')
@login_required
def job_view(jid):
    job = models.job_by_id(jid)
    if not job or job['user_id'] != current_user()['id']:
        abort(404)
    u = current_user()
    try:
        signed = bool(job['signed_at'])
    except (KeyError, IndexError):
        signed = False
    ctx = _apple_context(u['id'])
    return render_template('job.html', job=job, signed=signed, **ctx)


@app.route('/api/job/<int:jid>')
@login_required
def job_api(jid):
    job = models.job_by_id(jid)
    if not job or job['user_id'] != current_user()['id']:
        abort(404)
    data = {k: job[k] for k in ('id', 'state', 'stage', 'progress', 'message', 'ipa_path')}
    try:
        data['signed'] = bool(job['signed_at'])
    except (KeyError, IndexError):
        data['signed'] = False
    st = _apple_status_json(current_user()['id'])
    data['has_apple'] = st['has_apple']
    data['has_creds'] = st['has_creds']
    data['has_apple_id'] = st['has_apple_id']
    data['needs_2fa'] = st['needs_2fa']
    data['apple_email'] = st.get('email')
    return jsonify(data)


@app.route('/download/<int:jid>')
def download(jid):
    job = models.job_by_id(jid)
    if not job:
        abort(404)
    if not _job_access_ok(job, jid):
        # 未登录且无 token：要求登录
        if not current_user():
            flash('先登录', 'warn')
            return redirect(url_for('login', next=request.path))
        abort(404)
    if job['state'] != 'done' or not job['ipa_path'] or not os.path.exists(job['ipa_path']):
        abort(404)
    return send_file(job['ipa_path'], as_attachment=True,
                     download_name=f"{job['game_name'] or 'game'}.ipa")


# ---------------------------------------------------------------- Apple ID 上下文 / JSON API

def _apple_context(uid):
    """供模板内嵌 Apple ID 面板、以及 /api/creds 状态。"""
    apple = models.get_apple_id(uid)
    info = None
    if signer.has_creds(uid):
        try:
            info = signer.profile_info(signer.cred_paths(uid)[1])
        except Exception:
            info = None
    devices = models.devices_of(uid)
    pending_2fa = session.get('apple_2fa')
    provision_meta = None
    st = {}
    try:
        import apple_provision
        provision_meta = apple_provision.load_bundle_meta(uid)
        st = apple_provision.install_status(uid) or {}
        if st.get('pending_2fa') and not pending_2fa:
            pending_2fa = st['pending_2fa']
    except Exception:
        pass
    # No saved Apple ID and no engine pending → drop stale browser 2FA session
    # so the user can switch accounts after we clear apple_ids.
    if not apple and not (st or {}).get('pending_2fa'):
        if pending_2fa:
            session.pop('apple_2fa', None)
            session.pop('apple_pw_tmp', None)
            pending_2fa = None
    return {
        'apple': apple,
        'info': info,
        'devices': devices,
        'pending_2fa': pending_2fa,
        'provision_meta': provision_meta,
        'apple_session': st if isinstance(st, dict) else {},
        'has_apple': _has_apple(uid),
        'has_creds': signer.has_creds(uid),
        'has_apple_id': models.has_apple_id(uid),
    }


def _apple_status_json(uid):
    ctx = _apple_context(uid)
    apple = ctx['apple']
    pending = ctx['pending_2fa']
    info = ctx['info']
    return {
        'ok': True,
        'has_apple': ctx['has_apple'],
        'has_creds': ctx['has_creds'],
        'has_apple_id': ctx['has_apple_id'],
        'email': (apple['email'] if apple else None),
        'needs_2fa': bool(pending),
        'pending_2fa': pending,
        'has_profile': bool(info),
        'profile_name': (info or {}).get('name') if info else None,
        'message': None,
    }


def _api_creds_handle(uid, action, data):
    """处理内嵌面板的 Apple ID 提交。data 为 form/json 字典。返回 (status_code, dict)。"""
    devices = models.devices_of(uid)
    pending_2fa = session.get('apple_2fa')
    email_in = (data.get('apple_email') or '').strip()
    pw_in = data.get('apple_password') or ''
    udid_in = (data.get('udid') or '').strip()
    code_in = (data.get('apple_code') or '').strip()

    def status(extra=None, http=200, **kw):
        body = _apple_status_json(uid)
        if extra:
            body.update(extra)
        body.update(kw)
        return http, body

    if action == 'apple_reset':
        session.pop('apple_2fa', None)
        session.pop('apple_pw_tmp', None)
        return status(needs_2fa=False, pending_2fa=None, message='已退出当前 Apple 登录，可填写新账号')

    if action == 'apple_2fa_resend':
        if not pending_2fa:
            return status(ok=False, error='当前没有待验证的登录，请先填写 Apple ID', http=400)
        try:
            import apple_provision as ap
            result = ap.resend_2fa(uid)
        except Exception as ex:
            return status(ok=False, error=f'重发失败：{ex}', http=400)
        method = (result or {}).get('method') or pending_2fa.get('method') or 'trusteddevice'
        session['apple_2fa'] = {
            'email': pending_2fa.get('email'),
            'method': method,
        }
        if (result or {}).get('note') == 'too_many_codes_sent':
            msg = '苹果暂时限流，不会再发新短信。请直接填写刚才已收到的验证码'
        elif method == 'sms':
            msg = '已请求重发短信验证码，请查看手机短信（含垃圾箱）'
        else:
            msg = '已请求重发，请查看苹果设备上的验证码弹窗'
        return status(needs_2fa=True, pending_2fa=session['apple_2fa'],
                      method=method, message=msg)

    if action == 'apple_2fa':
        email = (pending_2fa or {}).get('email') or email_in
        pw = session.get('apple_pw_tmp') or ''
        if not email or not pw:
            session.pop('apple_2fa', None)
            session.pop('apple_pw_tmp', None)
            return status(ok=False, error='登录状态已过期，请重新填写 Apple ID', http=400)
        try:
            import apple_provision as ap
            result = ap.complete_2fa(uid, email, pw, code_in)
        except Exception as ex:
            return status(ok=False, error=f'验证失败：{ex}', http=400)
        if result.get('status') != 'authenticated':
            return status(ok=False, error='验证未完成，请重试', http=400)
        models.save_apple_id(uid, email, pw, team_id=None)
        session.pop('apple_2fa', None)
        udid = udid_in or _pick_udid(uid, devices)
        if udid:
            try:
                meta = ap.provision_signing(uid, udid)
                models.save_apple_id(uid, email, pw, team_id=meta.get('team_id'))
                session.pop('apple_pw_tmp', None)
                return status(message='Apple ID 已验证，证书已自动获取')
            except Exception as ex:
                session.pop('apple_pw_tmp', None)
                return status(ok=True, warn=True,
                              message=f'登录成功，但取证失败：{ex}。可填写 UDID 后重新取证')
        session.pop('apple_pw_tmp', None)
        return status(message='登录成功。请填写 UDID 后点「重新取证」')

    if action == 'reprovision':
        udid = udid_in or _pick_udid(uid, devices)
        if not udid:
            return status(ok=False, error='请填写 UDID（或先绑定设备）', http=400)
        row = models.get_apple_id(uid)
        pw = (row['secret_enc'].decode('utf-8') if row and row['secret_enc'] else None)
        try:
            import apple_provision as ap
            email = row['email'] if row else None
            meta = ap.ensure_provisioned(uid, udid, email=email, password=pw, force=True)
            if row:
                models.save_apple_id(
                    uid, row['email'],
                    row['secret_enc'].decode('utf-8') if row['secret_enc'] else '',
                    team_id=meta.get('team_id'))
            return status(message='证书已更新')
        except Exception as ex:
            try:
                from apple_provision import TwoFactorRequired
                if isinstance(ex, TwoFactorRequired):
                    session['apple_2fa'] = {'email': ex.email, 'method': ex.method}
                    session['apple_pw_tmp'] = pw or ''
                    return status(needs_2fa=True,
                                  pending_2fa=session['apple_2fa'],
                                  message='需要两步验证码')
            except Exception:
                pass
            return status(ok=False, error=f'取证失败：{ex}', http=400)

    email = email_in
    pw = pw_in
    udid = udid_in or _pick_udid(uid, devices)
    if not EMAIL_RE.match(email):
        return status(ok=False, error='Apple ID 邮箱格式不对', http=400)
    if not pw:
        return status(ok=False, error='请填写密码或 App 专用密码', http=400)
    models.save_apple_id(uid, email, pw, team_id=None)
    if udid_in and len(udid_in.replace('-', '')) >= 25:
        try:
            models.bind_device(uid, udid_in.replace('-', ''), name='手动登记')
        except Exception:
            pass
    try:
        import apple_provision as ap
        result = ap.begin_login(uid, email, pw)
    except Exception as ex:
        return status(ok=False, error=f'Apple 登录失败：{ex}', http=400)

    if result.get('status') == '2fa_required':
        method = result.get('method') or 'trusteddevice'
        session['apple_2fa'] = {
            'email': email,
            'method': method,
            'phone_last2': result.get('phone_last2'),
        }
        session['apple_pw_tmp'] = pw
        if method == 'sms':
            last = result.get('phone_last2')
            tip = f'请查看手机短信（尾号 {last}）里的 6 位验证码' if last else '请查看手机短信里的 6 位验证码'
        else:
            tip = '请查看苹果设备上的 6 位验证码'
        if result.get('note') == 'too_many_codes_sent':
            tip = '苹果已发过验证码且暂时限流，请使用刚才短信里的验证码（不必再等新短信）'
        return status(needs_2fa=True, pending_2fa=session['apple_2fa'],
                      method=method, phone_last2=result.get('phone_last2'),
                      message=tip)

    if result.get('status') != 'authenticated':
        return status(ok=False, error='Apple 登录未完成', http=400)

    if not udid:
        return status(message='Apple 已登录。请填写手机 UDID 后点「重新取证」')
    try:
        meta = ap.provision_signing(uid, udid)
        models.save_apple_id(uid, email, pw, team_id=meta.get('team_id'))
        return status(message='Apple ID 已保存，开发者证书已自动获取')
    except Exception as ex:
        return status(ok=True, warn=True, message=f'登录成功，取证失败：{ex}')


@app.route('/api/creds', methods=['GET', 'POST'])
@login_required
def api_creds():
    """内嵌面板用：不整页跳转，避免打断上传 XHR。"""
    u = current_user()
    uid = u['id']
    if request.method == 'GET':
        return jsonify(_apple_status_json(uid))
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()
    action = data.get('action') or 'apple'
    http, body = _api_creds_handle(uid, action, data)
    return jsonify(body), http


# ---------------------------------------------------------------- Apple ID / 签名凭据

@app.route('/creds', methods=['GET', 'POST'])
@login_required
def creds():
    """Apple ID 签名：登录 GSA →（可能 2FA）→ 用 UDID 向 Apple 取证 → 落盘 p12/profile。"""
    u = current_user()
    uid = u['id']
    apple = models.get_apple_id(uid)
    info = None
    if signer.has_creds(uid):
        info = signer.profile_info(signer.cred_paths(uid)[1])
    devices = models.devices_of(uid)
    pending_2fa = session.get('apple_2fa')  # {email, method}
    provision_meta = None
    try:
        import apple_provision
        provision_meta = apple_provision.load_bundle_meta(uid)
        st = apple_provision.install_status(uid)
        if st.get('pending_2fa') and not pending_2fa:
            pending_2fa = st['pending_2fa']
    except Exception:
        apple_provision = None  # type: ignore
        st = {}

    if request.method == 'POST':
        action = request.form.get('action') or 'apple'

        # —— 高级：手动上传 p12 + mobileprovision ——
        if action == 'advanced_p12':
            p12 = request.files.get('p12')
            mp = request.files.get('mobileprovision')
            pw = request.form.get('p12_password') or ''
            if not p12 or not mp:
                flash('p12 和 mobileprovision 都要选', 'err')
                return redirect(url_for('creds'))
            if not p12.filename.lower().endswith('.p12'):
                flash('证书文件要是 .p12', 'err')
                return redirect(url_for('creds'))
            signer.save_creds(uid, p12.read(), mp.read())
            signer.save_p12_password(uid, pw)
            info2 = signer.profile_info(signer.cred_paths(uid)[1])
            flash('高级证书已保存', 'ok')
            # 不覆盖已填的 Apple ID
            return redirect(url_for('creds'))

        # —— 提交 2FA 验证码 ——
        if action == 'apple_2fa':
            email = (pending_2fa or {}).get('email') or (request.form.get('apple_email') or '').strip()
            pw = session.get('apple_pw_tmp') or ''
            code = (request.form.get('apple_code') or '').strip()
            if not email or not pw:
                flash('登录状态已过期，请重新填写 Apple ID', 'err')
                session.pop('apple_2fa', None)
                session.pop('apple_pw_tmp', None)
                return redirect(url_for('creds'))
            try:
                import apple_provision as ap
                result = ap.complete_2fa(uid, email, pw, code)
            except Exception as ex:
                flash(f'验证失败：{ex}', 'err')
                return redirect(url_for('creds'))
            if result.get('status') != 'authenticated':
                flash('验证未完成，请重试', 'err')
                return redirect(url_for('creds'))
            models.save_apple_id(uid, email, pw, team_id=None)
            session.pop('apple_2fa', None)
            # 保留密码到 session 仅到取证完成
            udid = (request.form.get('udid') or '').strip() or _pick_udid(uid, devices)
            if udid:
                try:
                    meta = ap.provision_signing(uid, udid)
                    models.save_apple_id(uid, email, pw, team_id=meta.get('team_id'))
                    session.pop('apple_pw_tmp', None)
                    flash('Apple ID 已验证，证书已自动获取', 'ok')
                    return redirect(url_for('upload') if not models.jobs_of(uid, 1)
                                    else url_for('job_view', jid=models.jobs_of(uid, 1)[0]['id']))
                except Exception as ex:
                    session.pop('apple_pw_tmp', None)
                    flash(f'登录成功，但取证失败：{ex}。可填写 UDID 后点「重新取证」', 'warn')
                    return redirect(url_for('creds'))
            session.pop('apple_pw_tmp', None)
            flash('登录成功。请填写 UDID 后点「重新取证」', 'ok')
            return redirect(url_for('creds'))

        # —— 仅重新取证（已有会话 / 已存 Apple ID）——
        if action == 'reprovision':
            udid = (request.form.get('udid') or '').strip() or _pick_udid(uid, devices)
            if not udid:
                flash('请填写 UDID（或先绑定设备）', 'err')
                return redirect(url_for('creds'))
            try:
                import apple_provision as ap
                row = models.get_apple_id(uid)
                email = row['email'] if row else None
                pw = (row['secret_enc'].decode('utf-8')
                      if row and row['secret_enc'] else None)
                meta = ap.ensure_provisioned(uid, udid, email=email, password=pw, force=True)
                if row:
                    models.save_apple_id(uid, row['email'],
                                         row['secret_enc'].decode('utf-8') if row['secret_enc'] else '',
                                         team_id=meta.get('team_id'))
                flash('证书已更新', 'ok')
            except Exception as ex:
                from apple_provision import TwoFactorRequired
                if isinstance(ex, TwoFactorRequired):
                    session['apple_2fa'] = {'email': ex.email, 'method': ex.method}
                    session['apple_pw_tmp'] = pw or ''
                    flash('需要两步验证码', 'warn')
                else:
                    flash(f'取证失败：{ex}', 'err')
            return redirect(url_for('creds'))

        # —— 主路径：提交 Apple ID ——
        email = (request.form.get('apple_email') or '').strip()
        pw = request.form.get('apple_password') or ''
        udid = (request.form.get('udid') or '').strip() or _pick_udid(uid, devices)
        note_udid = (request.form.get('udid') or '').strip()
        if not EMAIL_RE.match(email):
            flash('Apple ID 邮箱格式不对', 'err')
            return redirect(url_for('creds'))
        if not pw:
            flash('请填写密码或 App 专用密码', 'err')
            return redirect(url_for('creds'))
        # 先落库（即使后续 2FA / 取证失败，向导也能记住）
        models.save_apple_id(uid, email, pw, team_id=None)
        if note_udid and len(note_udid.replace('-', '')) >= 25:
            # 可选：把粘贴的 UDID 登记到 devices，方便下次
            try:
                models.bind_device(uid, note_udid.replace('-', ''), name='手动登记')
            except Exception:
                pass
        try:
            import apple_provision as ap
            result = ap.begin_login(uid, email, pw)
        except Exception as ex:
            flash(f'Apple 登录失败：{ex}', 'err')
            return redirect(url_for('creds'))

        if result.get('status') == '2fa_required':
            session['apple_2fa'] = {'email': email, 'method': result.get('method') or 'trusteddevice'}
            session['apple_pw_tmp'] = pw  # 完成 2FA 需要再登一次
            flash('请输入发到你受信任设备 / 短信的验证码', 'warn')
            return redirect(url_for('creds'))

        if result.get('status') != 'authenticated':
            flash('Apple 登录未完成', 'err')
            return redirect(url_for('creds'))

        if not udid:
            flash('Apple 已登录。请填写手机 UDID 后点「重新取证」', 'ok')
            return redirect(url_for('creds'))
        try:
            meta = ap.provision_signing(uid, udid)
            models.save_apple_id(uid, email, pw, team_id=meta.get('team_id'))
            flash('Apple ID 已保存，开发者证书已自动获取', 'ok')
            jobs = models.jobs_of(uid, 1)
            if jobs:
                return redirect(url_for('job_view', jid=jobs[0]['id']))
            return redirect(url_for('upload'))
        except Exception as ex:
            flash(f'登录成功，取证失败：{ex}', 'warn')
            return redirect(url_for('creds'))

    return render_template(
        'creds.html',
        info=info,
        apple=apple,
        devices=devices,
        pending_2fa=pending_2fa,
        provision_meta=provision_meta,
        apple_session=st if isinstance(st, dict) else {},
    )


def _pick_udid(uid, devices=None):
    devices = devices if devices is not None else models.devices_of(uid)
    if devices:
        return devices[0]['udid']
    meta = None
    try:
        import apple_provision as ap
        meta = ap.load_bundle_meta(uid)
    except Exception:
        pass
    return (meta or {}).get('udid') or ''


@app.route('/install/<int:jid>/manifest.plist')
def ota_manifest(jid):
    """itms-services 拉取的安装清单。iOS 要求它和 IPA 都走 HTTPS；可用 ?t= 短时令牌。"""
    job = models.job_by_id(jid)
    if not job:
        abort(404)
    if not _job_access_ok(job, jid):
        abort(404)
    signed_at = None
    try:
        signed_at = job['signed_at']
    except (KeyError, IndexError):
        pass
    if not signed_at and not signer.has_creds(job['user_id']):
        # 未签名的包装了也会闪退；仍允许清单，但前端会提示
        pass
    base = os.environ.get('SVC_PUBLIC_URL', '').rstrip('/')
    ok, why = signer.check_https(base)
    if not ok:
        abort(400, why)
    token = request.args.get('t') or make_ota_token(jid)
    ipa_url = f'{base}/download/{jid}?t={token}'
    # Prefer provisioned bundle id when available
    bundle_id = os.environ.get('SVC_BUNDLE_ID', 'org.nxnative.0100000000010000')
    try:
        import apple_provision as ap
        meta = ap.load_bundle_meta(job['user_id'])
        if meta and meta.get('bundle_id'):
            bundle_id = meta['bundle_id']
    except Exception:
        pass
    body = signer.make_manifest(ipa_url, bundle_id, '1.4.1',
                                job['game_name'] or job['nsp_name'] or 'Switch 游戏')
    return app.response_class(body, mimetype='text/xml')


@app.route('/install/<int:jid>')
@login_required
def ota_page(jid):
    job = models.job_by_id(jid)
    if not job or job['user_id'] != current_user()['id']:
        abort(404)
    u = current_user()
    has_apple = _has_apple(u['id'])
    try:
        signed = bool(job['signed_at'])
    except (KeyError, IndexError):
        signed = False
    if not has_apple:
        flash('请先填写 Apple ID', 'warn')
        return redirect(url_for('creds'))
    if not signed:
        flash('包尚未签名，暂不能 OTA 安装。请确认已取证，或等 worker 签完。', 'warn')
        return redirect(url_for('job_view', jid=jid))

    base = os.environ.get('SVC_PUBLIC_URL', '').rstrip('/')
    ok, why = signer.check_https(base)
    token = make_ota_token(jid)
    link = None
    if ok:
        # Tokenized manifest URL so installd needs no Safari cookies
        manifest = f'{base}/install/{jid}/manifest.plist?t={token}'
        link = f'itms-services://?action=download-manifest&url={manifest}'
    return render_template('install.html', job=job, link=link, reason=why, base=base,
                           signed=signed, token=token)


# ---------------------------------------------------------------- 设备绑定（可选助手，不在主轨）

@app.route('/device')
@login_required
def bind_device():
    return render_template('device.html',
                           udid_token=session.get('udid_token') or _ensure_token(),
                           public_url=public_base(),
                           devices=models.devices_of(current_user()['id']))


def _ensure_token():
    t = session.get('udid_token')
    if not t:
        t = uuid.uuid4().hex
        session['udid_token'] = t
    return t


@app.route('/device/profile.mobileconfig')
@login_required
def device_profile():
    """下发描述文件：装完之后 iOS 会把 UDID 等信息 POST 回 callback。"""
    t = _ensure_token()
    host = public_base()
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <dict>
    <key>URL</key><string>{host}/device/udid</string>
    <key>DeviceAttributes</key>
    <array><string>UDID</string><string>PRODUCT</string><string>VERSION</string><string>DEVICE_NAME</string></array>
  </dict>
  <key>PayloadOrganization</key><string>Switch 出包服务</string>
  <key>PayloadDisplayName</key><string>获取本机设备标识</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>PayloadUUID</key><string>{t}</string>
  <key>PayloadIdentifier</key><string>svc.udid.{t}</string>
  <key>PayloadType</key><string>Profile Service</string>
  <key>PayloadDescription</key><string>安装后会把本机型号与设备标识回传，用于给你的设备签名。</string>
</dict>
</plist>"""
    return app.response_class(body, mimetype='application/x-apple-aspen-config')


@app.route('/device/udid', methods=['POST'])
@login_required
def device_udid():
    """iOS 装完描述文件后，把设备属性 POST 到这里。"""
    data = request.form.get('data') or request.form.get('DATA')
    if not data:
        return 'no data', 400
    import plistlib
    try:
        p = plistlib.loads(data.encode() if isinstance(data, str) else data)
    except Exception:
        return 'bad plist', 400
    udid = p.get('UDID')
    if not udid:
        return 'no udid', 400
    models.bind_device(current_user()['id'], udid,
                       name=p.get('DEVICE_NAME'), model=p.get('PRODUCT'), ios=p.get('VERSION'))
    return render_template('device_done.html')


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})


if __name__ == '__main__':
    models.init()
    r = models.ensure_admin('admin', '111111')
    if r:
        print(f'[admin] {"已创建" if r[0] == "created" else "已提升为管理员"}: {r[1]}')
    start_worker()
    app.run(host='0.0.0.0', port=8772, debug=False, threaded=True)
