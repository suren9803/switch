# Switch 游戏出包服务（svc）

> 本文档严格依据源码（app.py / models.py / worker.py / signer.py）编写。标注【不确定】的内容在代码里找不到依据，需要人工确认。

## 1. 概述

一个 Flask 小服务：用户凭邀请码注册 → 绑定 iPhone（描述文件取 UDID）→ 上传 .nsp → 后台线程解包（nx_extract）并调用 `nx_pack.py` 套用预编好的“游戏壳”打出 IPA → 若用户上传了 p12 + mobileprovision，用 zsign 重签 → 提供下载与 OTA（itms-services）安装。另有 Renewer 线程每小时检查一次，签名超过 6 天（144 小时）的 IPA 自动重签。

- Web 与 worker 在同一进程：`app.py` 启动时起 `Worker`（打包，一次一个任务）和 `Renewer`（续签）两个线程。
- 数据库：SQLite（WAL 模式），`data/svc.db`。

## 2. 目录结构（D:\switch\svc\）

| 路径 | 说明 |
|---|---|
| app.py | Web 入口：路由、会话、上传、凭据、OTA、设备绑定 |
| models.py | SQLite 数据层：users / invites / devices / jobs / apple_ids |
| worker.py | 任务队列、解包+打包、自动续签线程 |
| signer.py | zsign 重签、mobileprovision 解析、OTA manifest |
| templates/ | 12 个页面模板 |
| static/ | 静态资源 |
| logs/ | 每个任务一个 `job<id>.log`；续签线程异常写 `job0.log` |
| data/svc.db (+ -wal, -shm) | 数据库（敏感） |
| data/secret_key | Flask 会话签名密钥，首次启动自动生成（敏感） |
| data/creds/ | 用户证书：`user<id>.p12`、`user<id>.mobileprovision`（敏感） |
| shells/ | 预编好的游戏壳：`<title_id>.zip`（目前 `0100000000010000.zip`） |
| uploads/job<id>/ | 用户上传的 NSP 与解包工作目录，任务结束（成功或失败）后自动删除 |
| outputs/job<id>.ipa | 产出的 IPA（不会自动清理） |
| bin/zsign.exe | 签名工具（signer.py 硬编码此路径） |
| bin/cloudflared.exe | 隧道工具（代码中未引用） |

## 3. 外部依赖（位于 svc 之外，**不要删除**）

worker.py 以 `D:\switch`（= svc 的上级目录）为根：

- `D:\switch\nxnative-main\`：打包引擎
  - `scripts\nx_pack.py`
  - `dm\tools\nx_extract\bin\Release\net10.0\nx_extract.exe`
- `D:\switch\prod.keys`：解包 NSP 用的密钥（缺失时每个任务都会失败：“平台缺少 prod.keys”）
- `D:\switch\shell-inner.zip`：壳的母本；`shells\` 里是副本。【不确定】代码中没有引用该文件，它与 shells 中 zip 的关系依据的是部署说明而非代码。

> 如果移动 svc 目录，必须保证上述文件仍在 svc 的**上一级**目录中。

## 4. 环境要求与安装

- Windows（signer.py 写死 `bin\zsign.exe`，nx_extract 为 .exe）
- Python 3（版本未在代码中声明【不确定】；nx_pack.py 用同一个 `sys.executable` 运行）
- Python 包：`flask`（含 werkzeug）。代码中没有 requirements.txt【不确定 nx_pack.py 自身的依赖】
- .NET 运行时：nx_extract 编译目标为 `net10.0`，需要相应运行时【根据路径推断】

```bat
pip install flask
```

## 5. 启动

```bat
cd /d D:\switch\svc
set SVC_PUBLIC_URL=https://你的公网域名
rem 可选：OTA manifest 中的 bundle id，默认 org.nxnative.0100000000010000
set SVC_BUNDLE_ID=org.nxnative.0100000000010000
python app.py
```

- 监听：`0.0.0.0:8772`（app.py 第 416 行写死，无参数可改；会对局域网开放）
- 健康检查：`GET /healthz` → `{"ok": true}`
- 首次启动自动建表；若库中没有任何管理员，会创建账号 **admin / 111111**（或将已存在的 `admin` 提升为管理员）。**请立即修改此密码**（目前没有改密页面，需要直接改数据库，见第 8 节）。
- 只单独跑打包：`python worker.py`（只有 Worker，没有 Renewer、没有 Web）。
- 不要同时启动多个 `app.py`。

环境变量（代码中只有这两个）：

| 变量 | 作用 |
|---|---|
| SVC_PUBLIC_URL | 对外 https 地址。OTA 安装页/manifest **必须**有且以 `https://` 开头；设备描述文件的回调地址也优先用它 |
| SVC_BUNDLE_ID | manifest 里的 bundle-identifier |

## 6. cloudflared 隧道与公网地址

代码里**没有**启动 cloudflared 的逻辑，也没有隧道配置文件；公网地址**唯一**的配置点是环境变量 `SVC_PUBLIC_URL`（app.py 41、317、335 行）。

临时隧道示例【命令为 cloudflared 通用用法，非代码给出】：

```bat
D:\switch\svc\bin\cloudflared.exe tunnel --url http://127.0.0.1:8772
```

它会打印一个 `https://xxxx.trycloudflare.com` 地址。每次重启临时隧道地址都会变，所以：

1. 停止 app.py；
2. `set SVC_PUBLIC_URL=https://新地址`（不要以 `/` 结尾也可，代码会去掉）；
3. 重新 `python app.py`。

固定域名需使用 cloudflared 命名隧道（`cloudflared tunnel login/create/route dns/run`），配置位于 cloudflared 自己的 config.yml【不确定：仓库中没有】。
注意：已经发出去的 OTA 链接中包含旧地址，换地址后要重新打开安装页获取新链接。

## 7. 添加新游戏壳

依据 worker.py 第 119–137 行：

1. 用 nxnative 为该游戏编出壳（在云端/外部完成，代码未包含此步骤【不确定具体流程】）。
2. 以 16 位 **title id** 命名为 `<title_id>.zip`，放到 `D:\switch\svc\shells\`，例如 `shells\0100000000010000.zip`。
3. title id 的确定顺序：解包结果中 `game\dm\shell_info.txt` 的 `GAME=` 项 → 上传时从文件名 `[0100XXXXXXXXXXXX]` 提取的值 → 基础 NSP 文件名。文件名匹配区分大小写取决于 Windows 文件系统（不区分），但建议与 shell_info 中的写法一致（通常大写）。
4. 无需重启：每个任务运行时才检查文件是否存在。
5. OTA：manifest 中的 bundle id 来自 `SVC_BUNDLE_ID`（全局一个值），版本写死为 `1.4.1`。不同游戏的壳若 bundle id 不同，OTA 可能装不上【需确认壳的实际 bundle id】。

## 8. 邀请码与管理员

- 注册必须使用邀请码；一码一人，注册时把邀请码上的 `credits`（出包次数）赋给新用户。
- 管理员登录后访问 `/admin/invites`：一次可生成 1–50 个码，每码 1–100 次额度，可写备注（≤60 字）。
- 每次上传扣 1 次额度；**任务失败不会退还**（目前代码不调用 refund_credit）。
- 手动设管理员 / 改额度（先停服务或确保无写入）：

```bat
sqlite3 data\svc.db "UPDATE users SET is_admin=1 WHERE email='someone@x.com';"
sqlite3 data\svc.db "UPDATE users SET credits=credits+10 WHERE email='someone@x.com';"
```

- 改 admin 密码（需在本机 Python 中生成哈希）：

```bat
python -c "from werkzeug.security import generate_password_hash as g;import sqlite3;c=sqlite3.connect('data/svc.db');c.execute(\"UPDATE users SET password_hash=? WHERE email='admin'\",(g('新密码'),));c.commit()"
```

## 9. 备份

数据库为 WAL 模式，最近写入可能还在 `svc.db-wal` 中。

- 推荐：先停止 `app.py`，再复制整个 `data\`（svc.db、svc.db-wal、svc.db-shm、secret_key、creds\）。
- 不停机：用 SQLite 在线备份 `sqlite3 data\svc.db ".backup D:\backup\svc.db"`；如果直接复制文件，必须**同时**复制 `svc.db`、`svc.db-wal`、`svc.db-shm`。
- 另行备份：`shells\`、`D:\switch\prod.keys`、`D:\switch\shell-inner.zip`、`D:\switch\nxnative-main\`。`outputs\` 可按需。
- 备份文件含敏感数据，需加密保存。

## 10. 日志

- `logs\job<id>.log`：解包/打包子进程的全部输出、签名结果、失败 traceback、续签记录。
- `logs\job0.log`：Renewer 线程自身的异常。
- Worker 启动/开始/结束信息打印到控制台（stdout）；Flask 访问日志也在控制台。
- 日志不会自动轮转或清理。

## 11. 绝不能外传的敏感文件

- `data\creds\`（用户 p12 证书含私钥 + 描述文件）
- `data\svc.db`（及 -wal/-shm）：含密码哈希，以及 **明文存储** 的 p12 密码（apple_ids.secret_enc 实际未加密）
- `data\secret_key`：泄露即可伪造任何用户（含管理员）的登录会话
- `D:\switch\prod.keys`
- 另外 `uploads\`、`outputs\` 含用户游戏数据，也不应共享。
