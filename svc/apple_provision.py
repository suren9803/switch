#!/usr/bin/env python3
"""Apple ID → development cert + mobileprovision (Phase 1 full path).

Wraps the vendored iPASide engine (MIT, .vendor/ipaside_engine) which implements:
  - GrandSlam (GSA) login + 2FA (trusted device / SMS)
  - Offline anisette headers
  - developerservices2.apple.com provisioning (cert, device, App ID, profile)

Outputs land in data/creds/user{N}.{p12,mobileprovision,pass,bundle.json} so the
existing zsign worker / renewer path keeps working.

Run the service with .vendor/venv/bin/python (see requirements-apple.txt), or this
module will try to prepend the venv site-packages automatically.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
VENDOR = HERE / '.vendor'
ENGINE_ROOT = VENDOR / 'ipaside_engine'
CREDS = HERE / 'data' / 'creds'
APPLE_ROOT = HERE / 'data' / 'apple'

# Per-user lock so two requests for the same uid don't clobber pending 2FA / anisette.
_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Free-account profiles bind a team-scoped bundle id; games ship as org.nxnative.<title>.
DEFAULT_BASE_BUNDLE = os.environ.get('SVC_BUNDLE_ID', 'org.nxnative.0100000000010000')
# Distinct from iPASide's machine name so we do not revoke the user's iPASide cert.
MACHINE_NAME = 'SwitchPack'
P12_PASSWORD = 'SwitchPack'


def _bootstrap_vendor() -> None:
    """Ensure vendored engine + venv packages are importable."""
    # Windows: venv/Lib/site-packages ; POSIX: venv/lib/pythonX.Y/site-packages
    for cand in (
        VENDOR / 'venv' / 'Lib' / 'site-packages',
        VENDOR / 'venv' / 'lib' / 'site-packages',
    ):
        if cand.is_dir():
            s = str(cand)
            if s not in sys.path:
                sys.path.insert(0, s)
    venv_lib = VENDOR / 'venv' / 'lib'
    if venv_lib.exists():
        for pth in venv_lib.glob('python*/site-packages'):
            s = str(pth)
            if s not in sys.path:
                sys.path.insert(0, s)
    root = str(VENDOR)
    if root not in sys.path:
        sys.path.insert(0, root)
    # Subprocess helpers use `python -m ipaside_engine...` and need this.
    cur = os.environ.get('PYTHONPATH', '')
    parts = [x for x in cur.split(os.pathsep) if x]
    if root not in parts:
        parts.insert(0, root)
    os.environ['PYTHONPATH'] = os.pathsep.join(parts)


_bootstrap_vendor()


def _uid_lock(uid: int) -> threading.Lock:
    with _LOCKS_GUARD:
        if uid not in _LOCKS:
            _LOCKS[uid] = threading.Lock()
        return _LOCKS[uid]


def _user_data_dir(uid: int) -> Path:
    path = APPLE_ROOT / f'user{uid}'
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _engine_for(uid: int):
    """Point iPASide paths at this user's data dir and set our cert machine name."""
    _bootstrap_vendor()
    import ipaside_engine.paths as paths
    import ipaside_engine.provision as provision
    import ipaside_engine.signing as signing

    data = _user_data_dir(uid)
    orig_data_dir = paths.data_dir
    orig_machine = getattr(provision, 'CERTIFICATE_MACHINE_NAME', None)
    orig_p12_pw = signing.P12_PASSWORD

    def _data_dir() -> Path:
        data.mkdir(parents=True, exist_ok=True)
        return data

    paths.data_dir = _data_dir  # type: ignore[assignment]
    provision.CERTIFICATE_MACHINE_NAME = MACHINE_NAME
    signing.P12_PASSWORD = P12_PASSWORD
    try:
        yield
    finally:
        paths.data_dir = orig_data_dir  # type: ignore[assignment]
        if orig_machine is not None:
            provision.CERTIFICATE_MACHINE_NAME = orig_machine
        signing.P12_PASSWORD = orig_p12_pw


def _cred_bundle_path(uid: int) -> Path:
    CREDS.mkdir(parents=True, exist_ok=True)
    return CREDS / f'user{uid}.bundle.json'


def install_status(uid: int) -> dict[str, Any]:
    """Whether this user already has a GSA session / pending 2FA."""
    with _uid_lock(uid):
        with _engine_for(uid):
            from ipaside_engine import gsa
            st = gsa.status()
            pending = gsa._load_pending()  # noqa: SLF001 — need method for UI
            out = dict(st)
            if pending:
                out['pending_2fa'] = {
                    'email': pending.get('email'),
                    'method': pending.get('method'),
                }
            return out


def begin_login(uid: int, email: str, password: str) -> dict[str, Any]:
    """Start Apple ID login. May return status=2fa_required."""
    email = (email or '').strip()
    if not email or not password:
        raise ValueError('需要 Apple ID 和密码')
    with _uid_lock(uid):
        with _engine_for(uid):
            from ipaside_engine import gsa
            return gsa.begin_login(email, password)


def complete_2fa(uid: int, email: str, password: str, code: str) -> dict[str, Any]:
    """Submit 2FA / SMS code and finish login."""
    code = (code or '').strip().replace(' ', '')
    if not code:
        raise ValueError('需要验证码')
    with _uid_lock(uid):
        with _engine_for(uid):
            from ipaside_engine import gsa
            return gsa.complete_2fa(email, password, code)


def resend_2fa(uid: int) -> dict[str, Any]:
    """Ask Apple to push/SMS another verification code for pending 2FA."""
    with _uid_lock(uid):
        with _engine_for(uid):
            from ipaside_engine import gsa
            return gsa.resend_2fa()


def provision_signing(
    uid: int,
    udid: str,
    *,
    bundle_base: str | None = None,
    app_name: str = 'SwitchGame',
    device_name: str = 'iPhone',
    on_step: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Mint cert + register UDID + App ID + profile; copy into data/creds for zsign.

    Requires a completed GSA session for this uid (begin_login / complete_2fa).
    """
    udid = (udid or '').strip().replace('-', '')
    if len(udid) < 25:
        raise ValueError('UDID 无效，请粘贴完整的 25/40 位设备标识')

    step = on_step or (lambda _m: None)
    base = bundle_base or DEFAULT_BASE_BUNDLE

    with _uid_lock(uid):
        with _engine_for(uid):
            from ipaside_engine import gsa, provision, signing
            from ipaside_engine import developer

            session = gsa.load_session()
            teams = developer.list_teams()
            if not teams:
                raise RuntimeError('这个 Apple ID 没有开发者团队（免费账号通常会自动创建）')
            team_id = teams[0]['teamId']
            gsa.remember_team(session.get('email') or '', team_id)

            # Free accounts cannot register real App Store bundle ids.
            bundle_id = provision.team_scoped_bundle_id(base)

            bundle = provision.ensure_signing_assets(
                bundle_id=bundle_id,
                udid=udid,
                app_name=app_name,
                device_name=device_name,
                on_step=step,
            )

            # Copy into the layout signer/worker already use.
            import signer
            p12_bytes = Path(bundle['p12_path']).read_bytes()
            mp_bytes = Path(bundle['profile_path']).read_bytes()
            signer.save_creds(uid, p12_bytes, mp_bytes)
            # Prefer our fixed password (we may have overridden signing.P12_PASSWORD).
            pw = bundle.get('p12_password') or P12_PASSWORD
            signer.save_p12_password(uid, pw)

            meta = {
                'team_id': bundle.get('team_id'),
                'team_name': bundle.get('team_name'),
                'account': bundle.get('account') or session.get('email'),
                'bundle_id': bundle.get('bundle_id'),
                'udid': udid,
                'certificate_serial': bundle.get('certificate_serial'),
                'p12_password': pw,
            }
            _cred_bundle_path(uid).write_text(json.dumps(meta, indent=2), encoding='utf-8')
            return meta


def ensure_provisioned(
    uid: int,
    udid: str,
    *,
    email: str | None = None,
    password: str | None = None,
    force: bool = False,
    on_step: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Ensure p12+profile exist. Re-login with stored Apple ID if session missing.

    If force=True, always re-provision (renewer path).
    """
    import signer
    meta_path = _cred_bundle_path(uid)
    if not force and signer.has_creds(uid) and meta_path.exists():
        return json.loads(meta_path.read_text(encoding='utf-8'))

    st = install_status(uid)
    if not st.get('authenticated'):
        if not email or not password:
            raise RuntimeError('Apple 会话已过期，需要重新登录 Apple ID')
        result = begin_login(uid, email, password)
        if result.get('status') == '2fa_required':
            raise TwoFactorRequired(result.get('method') or 'trusteddevice', email)
        if result.get('status') != 'authenticated':
            raise RuntimeError(f"Apple 登录失败：{result!r}")

    if not udid:
        raise ValueError('缺少 UDID，无法向 Apple 注册设备')
    return provision_signing(uid, udid, on_step=on_step)


class TwoFactorRequired(Exception):
    def __init__(self, method: str, email: str):
        self.method = method
        self.email = email
        super().__init__(f'需要两步验证（{method}）')


def load_bundle_meta(uid: int) -> dict[str, Any] | None:
    p = _cred_bundle_path(uid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None
