#!/usr/bin/env python3
"""签名与 OTA 分发。

签名用 zsign（跨平台，一份 exe 搞定重签）。它需要两样东西：

  p12              签名证书（含私钥）
  mobileprovision  描述文件，里面必须列着目标设备的 UDID

这两样是 Apple 发给「开发者账号持有人」的凭据。平台自己的账号池模式下，它们由平台
持有；用户自备模式下，由用户上传。无论哪种，平台都不接触用户的 Apple ID 密码本身。

OTA：iOS 通过一个 itms-services:// 链接安装，链接指向 manifest.plist，plist 里再指向
真正的 IPA。整条链必须是 HTTPS，否则 iOS 直接拒绝解析。

Phase 1 凭据分层：
  - apple_ids 表：用户面向的 Apple ID + App 专用密码（自动取证属 Phase 2）
  - data/creds/user{N}.p12 + .mobileprovision：高级路径手动上传的证书文件
  - data/creds/user{N}.pass：高级路径的 p12 密码（utf-8 文本），与 apple_ids 分离

English
-------
Code signing and OTA delivery.

Signing goes through zsign, which needs two things: a .p12 certificate (with its private
key) and a .mobileprovision that lists the target device's UDID. Both belong to whoever
holds the developer account — the platform never sees the account password itself.

OTA delivery is the standard mechanism: an itms-services:// link points at a manifest.plist,
which in turn points at the IPA. Every step of that chain must be HTTPS or iOS refuses it.
"""
import os
import shutil
import subprocess
import sys
import plistlib
import hashlib
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, 'bin', 'zsign.exe')
DATA = os.path.join(HERE, 'data')
CREDS = os.path.join(DATA, 'creds')
OUTPUTS = os.path.join(HERE, 'outputs')


def ensure_dirs():
    os.makedirs(CREDS, exist_ok=True)
    os.makedirs(OUTPUTS, exist_ok=True)


# ---------------------------------------------------------------- 凭据

def cred_paths(user_id):
    """一个用户一套凭据。文件名带 user id，避免互相覆盖。"""
    ensure_dirs()
    return (os.path.join(CREDS, f'user{user_id}.p12'),
            os.path.join(CREDS, f'user{user_id}.mobileprovision'))


def p12_pass_path(user_id):
    """高级 p12 路径的密码文件：data/creds/user{N}.pass（utf-8）。"""
    ensure_dirs()
    return os.path.join(CREDS, f'user{user_id}.pass')


def has_creds(user_id):
    """磁盘上是否有可 zsign 的 p12 + mobileprovision（与是否填了 Apple ID 无关）。"""
    p12, mp = cred_paths(user_id)
    return os.path.exists(p12) and os.path.exists(mp)


def save_creds(user_id, p12_bytes, mp_bytes):
    p12, mp = cred_paths(user_id)
    with open(p12, 'wb') as f:
        f.write(p12_bytes)
    with open(mp, 'wb') as f:
        f.write(mp_bytes)
    return p12, mp


def save_p12_password(user_id, password):
    """高级路径：把 p12 密码写到 .pass 文件，不占用 apple_ids 表。"""
    path = p12_pass_path(user_id)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(password or '')
    return path


def read_p12_password(user_id):
    """优先读 .pass；没有则返回 None（调用方可再退到 apple_ids.secret，仅兼容旧数据）。"""
    path = p12_pass_path(user_id)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def zsign_password(user_id):
    """给 worker / renewer 用的 zsign 密码：.pass 优先，否则兼容旧的 apple_ids.secret。"""
    pw = read_p12_password(user_id)
    if pw is not None:
        return pw
    try:
        import models
        cred = models.get_apple_id(user_id)
        if cred and cred['secret_enc'] and has_creds(user_id):
            # 旧数据：曾把 p12 密码误存在 apple_ids；仅在已有 p12 文件时才当密码用
            secret = cred['secret_enc']
            return secret.decode('utf-8') if isinstance(secret, bytes) else str(secret)
    except Exception:
        pass
    return ''


def profile_info(mp_path):
    """从 mobileprovision 里抠出能用的信息：Team、有效期、包含的 UDID 列表。

    描述文件是 CMS 签名包裹的 plist，前后各有二进制头尾，直接找 plist 的起止标记。
    """
    raw = open(mp_path, 'rb').read()
    start = raw.find(b'<?xml')
    end = raw.rfind(b'</plist>')
    if start < 0 or end < 0:
        return None
    try:
        p = plistlib.loads(raw[start:end + len(b'</plist>')])
    except Exception:
        return None
    return {
        'name': p.get('Name'),
        'app_id': p.get('AppIDName'),
        'team': (p.get('TeamIdentifier') or [None])[0],
        'team_name': p.get('TeamName'),
        'expires': str(p.get('ExpirationDate')),
        'udids': p.get('ProvisionedDevices') or [],
        'all_devices': bool(p.get('ProvisionsAllDevices')),
    }


def udid_covered(mp_path, udid):
    """这台设备能不能装用这份描述文件签出来的包。"""
    info = profile_info(mp_path)
    if not info:
        return False
    if info['all_devices']:          # 企业证书：不限制设备
        return True
    return udid in info['udids']


# ---------------------------------------------------------------- 签名

def sign(ipa_in, ipa_out, user_id, p12_password, bundle_id=None):
    """用 zsign 重签一个 IPA。返回 (成功?, 输出)。

    bundle_id：免费账号取证时 profile 绑的是 team-scoped id，必须用 -b 改掉 IPA 原 bundle。
    """
    if not os.path.exists(BIN):
        return False, f'zsign 不存在：{BIN}'
    p12, mp = cred_paths(user_id)
    if not (os.path.exists(p12) and os.path.exists(mp)):
        return False, '这个账号还没有配置签名凭据（p12 + mobileprovision）'
    cmd = [BIN, '-k', p12, '-p', p12_password or '', '-m', mp, '-o', ipa_out]
    if bundle_id:
        cmd += ['-b', bundle_id]
    cmd.append(ipa_in)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    ok = r.returncode == 0 and os.path.exists(ipa_out)
    return ok, out[-2000:]


# ---------------------------------------------------------------- OTA

def make_manifest(ipa_url, bundle_id, version, title):
    """生成 itms-services 用的 manifest.plist。"""
    return plistlib.dumps({
        'items': [{
            'assets': [{'kind': 'software-package', 'url': ipa_url}],
            'metadata': {
                'bundle-identifier': bundle_id,
                'bundle-version': version,
                'kind': 'software',
                'title': title,
            }
        }]
    })


def ota_link(public_base, job_id):
    """给用户的安装链接。public_base 必须是 https 开头的公网地址。"""
    return (f'itms-services://?action=download-manifest'
            f'&url={public_base}/install/{job_id}/manifest.plist')


def check_https(public_base):
    if not public_base:
        return False, '没有配置公网地址（SVC_PUBLIC_URL）'
    if not public_base.startswith('https://'):
        return False, f'OTA 安装要求 HTTPS，当前配置是 {public_base}'
    return True, ''


if __name__ == '__main__':
    # 简单自检
    print('zsign exists:', os.path.exists(BIN))
    print('creds dir:', CREDS)
    if len(sys.argv) > 1 and sys.argv[1] == 'info':
        print(profile_info(sys.argv[2]))
