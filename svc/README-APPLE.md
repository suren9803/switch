# Apple ID 自动取证（vendored iPASide engine）

## 运行

```bat
cd svc\src
.vendor\venv\Scripts\python -m pip install -r requirements-apple.txt
.vendor\venv\Scripts\python app.py
```

Linux / 本目录：

```bash
.vendor/venv/bin/pip install -r requirements-apple.txt
.vendor/venv/bin/python app.py
```

依赖装在 `.vendor/venv`；GSA / developerservices2 实现来自 MIT 的 iPASide Engine（见 `.vendor/NOTICE.txt`、`.vendor/IPASIDE_LICENSE.txt`）。

## 流程

1. 登录本站 → 上传游戏（可不选设备）
2. Apple ID 页填写邮箱 + 密码（或 App 专用密码）+ UDID
3. 若触发 2FA：同页输入验证码
4. 服务端自动：登录 Apple → 开发证书 CSR → 登记 UDID → App ID → mobileprovision → 写入 `data/creds/user{N}.*`
5. worker 用 zsign 签名；`/install/<jid>` 给出带短时 HMAC token 的 itms-services 链接（installd 无需 cookie）
6. Renewer 到期前用已存 Apple ID 重新取证并重签

证书 machineName 为 `SwitchPack`，避免误吊销用户自己的 iPASide / Xcode 证书。
