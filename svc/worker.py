#!/usr/bin/env python3
"""任务队列与打包执行。

一个 worker 线程轮询 queued 任务，解包 NSP → 调 nx_pack.py 出 IPA，全程把阶段和进度
写回数据库，前端轮询那个接口就能显示进度条。

签名：若磁盘已有 p12（signer.has_creds）则直接 zsign；否则若有 Apple ID + UDID，
调用 apple_provision 向 Apple 取证后再签。Renewer 到期时同样用存好的 Apple ID 重新取证并重签。

English
-------
Job queue and the packing pipeline itself.

One worker thread polls for queued jobs and runs each one end to end: extract the NSP with
nx_extract, call nx_pack.py to rewrite / translate / pack, then hand the IPA to signer.py.
Progress is derived by matching the child process's own output lines against a stage table,
so the percentage reflects real work rather than a guess.

A second thread (Renewer) re-signs finished packages before their 7-day certificate
expires; the OTA link and the output path stay the same, so the user just reinstalls over
the old copy and keeps their saves.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

import models
import signer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                   # D:\switch
REPO = os.path.join(ROOT, 'nxnative-main')
KEYS = os.path.join(ROOT, 'prod.keys')                         # 平台自用的 prod.keys
NX_EXTRACT = os.path.join(REPO, 'dm', 'tools', 'nx_extract', 'bin', 'Release', 'net10.0', 'nx_extract.exe')
NX_PACK = os.path.join(REPO, 'scripts', 'nx_pack.py')
SHELLS = os.path.join(HERE, 'shells')                          # <title_id>.zip，每个游戏一个预编好的壳
UPLOADS = os.path.join(HERE, 'uploads')
OUTPUTS = os.path.join(HERE, 'outputs')
LOGS = os.path.join(HERE, 'logs')
PY = sys.executable

STAGE_MAP = [
    ('unpacking the shell',        '准备壳',        5),
    ('rewriting',                  '改写游戏程序',  12),
    ('writing Frameworks',         '生成动态库',    18),
    ('ad-hoc signing',             '签名框架',      20),
    ('splitting the shaders',      '拆分 shader',   25),
    ('translating shaders',        '翻译 shader',   30),
    ('installing RomFS',           '写入游戏数据',  92),
    ('packing',                    '打包 IPA',      95),
    ('done:',                      '完成',          100),
]


def log(job_id, text):
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, f'job{job_id}.log'), 'a', encoding='utf-8') as f:
        f.write(text.rstrip() + '\n')


def parse_stage(line):
    """把一行输出换算成 (阶段名, 进度)。识别不了就返回 (None, None)。"""
    low = line.lower()
    for needle, stage, pct in STAGE_MAP:
        if needle.lower() in low:
            return stage, pct
    return None, None


def run_streamed(cmd, job_id, env=None, cwd=None, on_progress=None):
    """跑一个子进程，逐行转发输出，回调进度。返回 (returncode, tail)。"""
    e = dict(os.environ)
    e.setdefault('PYTHONUTF8', '1')
    e.update(env or {})
    tail = []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding='utf-8', errors='replace', bufsize=1, env=e, cwd=cwd) as p:
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
            log(job_id, line)
            stage, pct = parse_stage(line)
            if stage and on_progress:
                on_progress(stage, pct, line)
        p.wait()
    return p.returncode, '\n'.join(tail)


def _resolve_udid(job):
    """从 job.device_id 或已存 bundle meta / 用户设备列表拿 UDID。"""
    if job['device_id']:
        with models.connect() as c:
            row = c.execute('SELECT udid FROM devices WHERE id = ?', (job['device_id'],)).fetchone()
            if row and row['udid']:
                return row['udid']
    try:
        import apple_provision as ap
        meta = ap.load_bundle_meta(job['user_id'])
        if meta and meta.get('udid'):
            return meta['udid']
    except Exception:
        pass
    devices = models.devices_of(job['user_id'])
    if devices:
        return devices[0]['udid']
    return None


def _ensure_creds_for_sign(job, jid, force=False):
    """保证 signer.has_creds；必要时用 Apple ID 自动取证。返回 (ok, bundle_id_or_None, err_msg)."""
    uid = job['user_id']
    if signer.has_creds(uid) and not force:
        bundle_id = None
        try:
            import apple_provision as ap
            meta = ap.load_bundle_meta(uid)
            if meta:
                bundle_id = meta.get('bundle_id')
        except Exception:
            pass
        return True, bundle_id, ''

    if not models.has_apple_id(uid):
        return False, None, '未配置 Apple ID，也没有手动上传的 p12'

    udid = _resolve_udid(job)
    if not udid:
        return False, None, '已保存 Apple ID，但缺少 UDID（请在 Apple ID 页粘贴 UDID 或绑定设备）'

    row = models.get_apple_id(uid)
    email = row['email']
    pw = row['secret_enc'].decode('utf-8') if row['secret_enc'] else ''

    def on_step(msg):
        models.update_job(jid, stage='取证', progress=97, message=msg[:200])
        log(jid, f'[provision] {msg}')

    try:
        import apple_provision as ap
        meta = ap.ensure_provisioned(
            uid, udid, email=email, password=pw, force=force, on_step=on_step)
        return True, meta.get('bundle_id'), ''
    except Exception as ex:
        # TwoFactorRequired：worker 无法交互，留给用户在网页完成
        name = type(ex).__name__
        return False, None, f'自动取证失败（{name}）：{ex}'


def _sign_ipa(job, jid, out_ipa, size_gb, force_provision=False):
    """取证（如需）+ zsign。成功则 mark_signed。"""
    ok_creds, bundle_id, err = _ensure_creds_for_sign(job, jid, force=force_provision)
    if not ok_creds:
        if models.has_apple_id(job['user_id']):
            models.update_job(
                jid, message=(
                    f'{size_gb:.2f} GB，打包完成但未签名：{err}。'
                    '请到 Apple ID 页完成验证 / 填写 UDID 后重新取证。'
                ))
        else:
            models.update_job(
                jid, message=f'{size_gb:.2f} GB，未配置 Apple ID / 证书，只能下载不能装')
        return False

    models.update_job(jid, stage='签名', progress=99, message='用你的证书重签 IPA')
    signed = out_ipa.replace('.ipa', '-signed.ipa')
    pw = signer.zsign_password(job['user_id'])
    ok, detail = signer.sign(out_ipa, signed, job['user_id'], pw, bundle_id=bundle_id)
    log(jid, f'[sign] ok={ok} bundle={bundle_id}\n{detail}')
    if ok:
        os.replace(signed, out_ipa)
        models.update_job(jid, message=f'{size_gb:.2f} GB，已签名，可以安装',
                          progress=100, stage='完成')
        models.mark_signed(jid)
        return True
    models.update_job(jid, stage='签名失败', message=detail[:400])
    return False


def execute(job):
    """执行一个任务：解包 → 出包 → 签名。任何异常都落到 jobs.message 上。"""
    jid = job['id']
    work = os.path.join(UPLOADS, f'job{jid}')
    os.makedirs(work, exist_ok=True)
    os.makedirs(OUTPUTS, exist_ok=True)

    def progress(stage, pct, line=None):
        models.update_job(jid, stage=stage, progress=pct, message=(line or stage)[:200])

    try:
        # --- 0. 前置检查
        if not os.path.exists(KEYS):
            raise RuntimeError(f'平台缺少 prod.keys：{KEYS}')
        nsps = [f for f in os.listdir(work) if f.lower().endswith('.nsp')]
        if not nsps:
            raise RuntimeError('上传目录里没有 .nsp 文件')
        base = [f for f in nsps if '][v0]' in f or '_v0.' in f.lower()]
        upd = [f for f in nsps if '][v' in f and f not in base]
        base_nsp = os.path.join(work, (base or nsps)[0])
        upd_nsp = os.path.join(work, upd[0]) if upd else '-'

        # --- 1. 解包
        models.update_job(jid, stage='解包 NSP', progress=2, message='读取游戏文件')
        game_dir = os.path.join(work, 'game')
        os.makedirs(game_dir, exist_ok=True)
        rc, tail = run_streamed([NX_EXTRACT, KEYS, base_nsp, upd_nsp, game_dir],
                                jid, on_progress=progress)
        if rc != 0:
            raise RuntimeError(f'解包失败（exit {rc}）\n{tail}')
        if not os.path.isdir(os.path.join(game_dir, 'ExeFS')):
            raise RuntimeError(f'解包后没有 ExeFS：{tail}')

        # --- 2. 找这个游戏的壳
        title_file = os.path.join(game_dir, 'dm', 'shell_info.txt')
        info = {}
        if os.path.exists(title_file):
            for line in open(title_file, encoding='utf-8'):
                if '=' in line:
                    k, v = line.split('=', 1)
                    info[k.strip()] = v.strip()
        title_id = info.get('GAME') or job['title_id']
        if not title_id:
            m = re.search(r'\[([0-9A-Fa-f]{16})\]', os.path.basename(base_nsp))
            title_id = m.group(1) if m else None
        shell = os.path.join(SHELLS, f'{title_id}.zip') if title_id else None
        if not shell or not os.path.exists(shell):
            raise RuntimeError(
                f'暂不支持该游戏（编号 {title_id or "未知"}）。'
                f'目前可出包：Super Mario Odyssey。'
            )

        # --- 3. 出包
        out_ipa = os.path.join(OUTPUTS, f'job{jid}.ipa')
        models.update_job(jid, stage='出包', progress=10, message='开始打包')
        rc, tail = run_streamed([PY, NX_PACK, shell, game_dir, '-o', out_ipa, '-w', os.path.join(work, '.nxpack')],
                                jid, env={'DM_GAME': title_id, 'GAME': title_id}, on_progress=progress)
        if rc != 0 or not os.path.exists(out_ipa):
            raise RuntimeError(f'打包失败（exit {rc}）\n{tail}')

        size = os.path.getsize(out_ipa) / 2 ** 30
        models.update_job(jid, state='done', stage='完成', progress=100,
                          message=f'{size:.2f} GB，可以下载了', ipa_path=out_ipa,
                          finished_at=models.now())
        log(jid, f'[done] {out_ipa} {size:.2f} GB')

        # --- 4. 签名
        _sign_ipa(job, jid, out_ipa, size)

    except Exception as ex:
        log(jid, '[failed] ' + traceback.format_exc())
        models.update_job(jid, state='failed', stage='失败', message=str(ex)[:600],
                          finished_at=models.now())
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Renewer(threading.Thread):
    """证书 7 天到期，到期前一天自动重签一遍。

    优先用已有 p12；若取证会话仍可用或库里有 Apple ID，则 force 重新向 Apple 拉一份
    新 profile 再签。产物路径与 OTA 链接不变。
    """

    def __init__(self, interval=3600):
        super().__init__(daemon=True, name='renewer')
        self.stop_flag = threading.Event()
        self.interval = interval

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception:
                log(0, '[renewer] ' + traceback.format_exc())
            self.stop_flag.wait(self.interval)

    def tick(self):
        due = models.jobs_due_for_renew()
        for job in due:
            jid = job['id']
            ipa = job['ipa_path']
            if not ipa or not os.path.exists(ipa):
                continue
            log(jid, f'[renew] 证书即将到期，重新签名（第 {job["renew_count"] or 0} 次续签）')
            size = os.path.getsize(ipa) / 2 ** 30
            # force_provision：尽量用 Apple ID 换新 profile（免费账号 7 天到期）
            ok = _sign_ipa(job, jid, ipa, size, force_provision=True)
            if ok:
                models.update_job(jid, message='证书已自动续签，重新安装一次即可继续使用')
                log(jid, '[renew] ok')
            else:
                log(jid, '[renew] failed (see job message)')

    def stop(self):
        self.stop_flag.set()


class Worker(threading.Thread):
    """单线程 worker：一次跑一个任务。并发靠加进程，不靠加线程（打包本来就吃满 CPU）。"""

    def __init__(self):
        super().__init__(daemon=True, name='packer')
        self.stop_flag = threading.Event()

    def run(self):
        n = models.requeue_stale()
        if n:
            print(f'[worker] 把 {n} 个中断的任务放回队列')
        while not self.stop_flag.is_set():
            job = models.next_queued()
            if not job:
                self.stop_flag.wait(2)
                continue
            if not models.claim_job(job['id']):
                continue
            print(f"[worker] 开始任务 {job['id']}")
            execute(job)
            print(f"[worker] 任务 {job['id']} 结束")

    def stop(self):
        self.stop_flag.set()


if __name__ == '__main__':
    models.init()
    w = Worker()
    w.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
