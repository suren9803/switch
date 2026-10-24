#!/usr/bin/env python3
"""数据层：用户、设备、任务。

单文件 SQLite，schema 在一个地方定义。所有写操作显式 commit，读操作走连接池无关的
短连接——这个规模下（几百用户）比任何 ORM 都简单可靠。

English
-------
Data layer: users, invitations, devices, jobs and signing credentials.

One SQLite file (WAL mode), schema declared in one place with idempotent migrations for
columns added later. Every mutation commits explicitly; reads use short-lived connections.
At a few hundred users this is simpler and more reliable than any ORM.
"""
import os
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, 'data', 'svc.db')

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    last_login    TEXT,
    plan          TEXT    NOT NULL DEFAULT 'trial',
    credits       INTEGER NOT NULL DEFAULT 1,     -- 可出包次数
    is_admin      INTEGER NOT NULL DEFAULT 0
);

-- 邀请码：只有持码的人能注册。一码一人，用掉即失效。
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at    TEXT,
    note       TEXT,
    credits    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    udid      TEXT    NOT NULL UNIQUE,
    name      TEXT,
    model     TEXT,
    ios       TEXT,
    bound_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    game_dir    TEXT    NOT NULL,                 -- 解包后的工作目录
    nsp_name    TEXT,
    title_id    TEXT,
    game_name   TEXT,
    state       TEXT    NOT NULL DEFAULT 'uploading', -- uploading/queued/running/done/failed
    stage       TEXT,                              -- 当前阶段（解包/改写/翻译/打包）
    progress    INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    ipa_path    TEXT,
    created_at  TEXT    NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_user  ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

-- 用户提供的 Apple ID：email + App 专用密码（secret_enc）。
-- Phase 1：仅保存，供向导判定「已填 Apple ID」；真正用 Apple 开发者接口取证
-- （anisette + developerservices2）属于 Phase 2。
-- 注意：高级路径上传 .p12 时，p12 密码写在 data/creds/user{N}.pass，不再占用本表。
CREATE TABLE IF NOT EXISTS apple_ids (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email      TEXT    NOT NULL,
    secret_enc BLOB,                              -- App 专用密码（本地部署明文存）
    team_id    TEXT,
    created_at TEXT    NOT NULL,
    valid      INTEGER NOT NULL DEFAULT 1
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    return c


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        # 轻量迁移：老库上补新列，重复执行无副作用
        cols = [r['name'] for r in c.execute('PRAGMA table_info(jobs)')]
        for col, typ in (('signed_at', 'TEXT'), ('renew_count', 'INTEGER DEFAULT 0')):
            if col not in cols:
                c.execute(f'ALTER TABLE jobs ADD COLUMN {col} {typ}')
        ucols = [r['name'] for r in c.execute('PRAGMA table_info(users)')]
        if 'is_admin' not in ucols:
            c.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')


def ensure_admin(email='admin', password='111111'):
    """保证至少有一个管理员。首次启动时创建 admin / 111111，之后只在完全没有管理员时补。"""
    from werkzeug.security import generate_password_hash
    with connect() as c:
        n = c.execute('SELECT COUNT(*) c FROM users WHERE is_admin = 1').fetchone()['c']
        if n:
            return None
        row = c.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if row:
            c.execute('UPDATE users SET is_admin = 1 WHERE id = ?', (row['id'],))
            return ('promoted', email)
        c.execute('INSERT INTO users (email, password_hash, created_at, plan, credits, is_admin) '
                  'VALUES (?,?,?,?,?,1)',
                  (email, generate_password_hash(password), now(), 'admin', 999))
        return ('created', email)


# ---------------------------------------------------------------- 邀请码

def make_invite(admin_id, note=None, credits=1, n=1):
    """生成 n 个一次性邀请码，返回列表。"""
    import secrets
    out = []
    with connect() as c:
        for _ in range(n):
            while True:
                # 去掉容易看错的 0/O/1/I，长度 10
                alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
                code = ''.join(secrets.choice(alphabet) for _ in range(10))
                if not c.execute('SELECT 1 FROM invites WHERE code = ?', (code,)).fetchone():
                    break
            c.execute('INSERT INTO invites (code, created_by, created_at, note, credits) VALUES (?,?,?,?,?)',
                      (code, admin_id, now(), note, credits))
            out.append(code)
    return out


def invite_by_code(code):
    with connect() as c:
        return c.execute('SELECT * FROM invites WHERE code = ?', (code.strip().upper(),)).fetchone()


def redeem_invite(code, user_id):
    """核销邀请码：必须是存在且未使用的。返回是否成功。"""
    with connect() as c:
        cur = c.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
                        (user_id, now(), code.strip().upper()))
        return cur.rowcount == 1


def list_invites(limit=100):
    with connect() as c:
        return c.execute('''
            SELECT i.*, u.email AS used_email
            FROM invites i LEFT JOIN users u ON u.id = i.used_by
            ORDER BY i.created_at DESC LIMIT ?''', (limit,)).fetchall()


def now():
    return datetime.utcnow().isoformat(timespec='seconds')


def later(days):
    return (datetime.utcnow() + timedelta(days=days)).isoformat(timespec='seconds')


# ---------------------------------------------------------------- users

def create_user(email, password_hash, credits=1, plan='trial'):
    with connect() as c:
        cur = c.execute(
            'INSERT INTO users (email, password_hash, created_at, plan, credits) VALUES (?,?,?,?,?)',
            (email, password_hash, now(), plan, credits))
        return cur.lastrowid


def user_by_email(email):
    with connect() as c:
        return c.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()


def user_by_id(uid):
    with connect() as c:
        return c.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()


def touch_login(uid):
    with connect() as c:
        c.execute('UPDATE users SET last_login = ? WHERE id = ?', (now(), uid))


def spend_credit(uid):
    """扣一次出包额度，返回是否成功（并发安全：只有 credits > 0 才扣）。"""
    with connect() as c:
        cur = c.execute('UPDATE users SET credits = credits - 1 WHERE id = ? AND credits > 0', (uid,))
        return cur.rowcount == 1


def refund_credit(uid):
    with connect() as c:
        c.execute('UPDATE users SET credits = credits + 1 WHERE id = ?', (uid,))


# ---------------------------------------------------------------- devices

def bind_device(uid, udid, name=None, model=None, ios=None):
    """绑定一台设备。已存在则更新描述信息，返回设备 id。"""
    with connect() as c:
        row = c.execute('SELECT id FROM devices WHERE udid = ?', (udid,)).fetchone()
        if row:
            c.execute('UPDATE devices SET name = COALESCE(?, name), model = COALESCE(?, model), '
                      'ios = COALESCE(?, ios) WHERE id = ?', (name, model, ios, row['id']))
            return row['id']
        cur = c.execute(
            'INSERT INTO devices (user_id, udid, name, model, ios, bound_at) VALUES (?,?,?,?,?,?)',
            (uid, udid, name, model, ios, now()))
        return cur.lastrowid


def devices_of(uid):
    with connect() as c:
        return c.execute('SELECT * FROM devices WHERE user_id = ? ORDER BY bound_at DESC', (uid,)).fetchall()


def device_by_id(did, uid):
    with connect() as c:
        return c.execute('SELECT * FROM devices WHERE id = ? AND user_id = ?', (did, uid)).fetchone()


# ---------------------------------------------------------------- jobs

def create_job(uid, device_id, game_dir, nsp_name, title_id=None, game_name=None,
             state='uploading'):
    """创建设备可选：device_id 可为 None（Phase 1 上传不再强制绑设备）。

    默认 state=uploading：文件还在传，worker 不会认领；上传完成后再改为 queued。
    """
    stage = '上传中' if state == 'uploading' else None
    message = '等待上传文件' if state == 'uploading' else None
    with connect() as c:
        cur = c.execute(
            'INSERT INTO jobs (user_id, device_id, game_dir, nsp_name, title_id, game_name, '
            'state, stage, message, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (uid, device_id, game_dir, nsp_name, title_id, game_name, state, stage, message, now()))
        return cur.lastrowid


def job_by_id(jid):
    with connect() as c:
        return c.execute('SELECT * FROM jobs WHERE id = ?', (jid,)).fetchone()


def jobs_of(uid, limit=30):
    with connect() as c:
        return c.execute('SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
                         (uid, limit)).fetchall()


def latest_done_job(uid):
    """最近一次成功出包的任务（含未签名），供向导第 3 步落地。"""
    with connect() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND state = 'done' "
            "ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()


def update_job(jid, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with connect() as c:
        c.execute(f'UPDATE jobs SET {cols} WHERE id = ?', (*fields.values(), jid))


def next_queued():
    """只取 queued；uploading 绝不可被 worker 认领。"""
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at LIMIT 1").fetchone()


def active_job(uid):
    """用户还没跑完的任务（含上传中）。登录 / 重开页面时直接跳回这里。"""
    with connect() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND state IN ('uploading','queued','running') "
            "ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()


def claim_job(jid):
    """把任务从 queued 抢到 running，只有一个 worker 能抢到。永不认领 uploading。"""
    with connect() as c:
        cur = c.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ? AND state = 'queued'",
                        (now(), jid))
        return cur.rowcount == 1


def requeue_stale():
    """进程重启后，把卡在 running 的任务放回队列。"""
    with connect() as c:
        cur = c.execute("UPDATE jobs SET state = 'queued', progress = 0, stage = NULL, "
                        "message = '上一次运行被中断，已重新排队' WHERE state = 'running'")
        return cur.rowcount


# ---------------------------------------------------------------- apple_ids（Apple ID 签名凭据）

def save_apple_id(uid, email, password, team_id=None):
    """保存用户的 Apple ID + App 专用密码。

    Phase 1 只存库，供向导「已填 Apple ID」判定与 Phase 2 自动取证。
    不要用本函数存 p12 密码——高级 p12 路径请用 signer.save_p12_password。
    """
    with connect() as c:
        c.execute('DELETE FROM apple_ids WHERE user_id = ?', (uid,))
        secret = (password or '').encode('utf-8') if password is not None else None
        c.execute('INSERT INTO apple_ids (user_id, email, secret_enc, team_id, created_at, valid) '
                  'VALUES (?,?,?,?,?,1)',
                  (uid, email, secret, team_id, now()))


def get_apple_id(uid):
    with connect() as c:
        return c.execute('SELECT * FROM apple_ids WHERE user_id = ? AND valid = 1', (uid,)).fetchone()


def has_apple_id(uid):
    """向导第 2 步完成条件之一：有真实 Apple 邮箱 + 非空密钥。"""
    row = get_apple_id(uid)
    if not row:
        return False
    email = (row['email'] or '').strip()
    secret = row['secret_enc']
    if not email or '@' not in email:
        return False
    if not secret:
        return False
    if isinstance(secret, bytes) and not secret.strip():
        return False
    return True


def drop_apple_id(uid):
    with connect() as c:
        c.execute('DELETE FROM apple_ids WHERE user_id = ?', (uid,))


# ---------------------------------------------------------------- 续签

def jobs_due_for_renew(hours=6 * 24):
    """已出包、已签名、且签名时间超过阈值的任务——证书 7 天到期，留一天余量。"""
    with connect() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE state = 'done' AND signed_at IS NOT NULL "
            "AND signed_at < datetime('now', ?) ORDER BY signed_at",
            (f'-{hours} hours',)).fetchall()


def mark_signed(jid):
    with connect() as c:
        c.execute("UPDATE jobs SET signed_at = ? , renew_count = COALESCE(renew_count,0) + 1 WHERE id = ?",
                  (now(), jid))
