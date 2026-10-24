# Switch Packing Service / Switch 出包服务

把用户自己的 Switch 游戏文件（NSP），在服务器上跑完整条流水线——解包、改写、
翻译着色器、打包、签名、OTA 安装——最后给他一个点一下就能装的 app。

Take a player's own Switch game files (NSP) and run the whole pipeline on a server:
extract, rewrite, translate shaders, pack, sign, deliver over OTA — ending with an app
they can install with one tap.

---

## 这是什么 / What this is

一个可以直接跑起来的 Web 服务，把 [nxnative](../README.md) 的打包能力包装成产品：
账号体系、设备绑定、任务队列、进度回传、签名、安装链接、到期续签。

A runnable web service that turns [nxnative](../README.md)'s packing engine into a
product: accounts, device binding, a job queue, live progress, code signing, install
links and certificate renewal.

**它不提供任何游戏内容。** 用户上传的必须是他自己从主机导出的文件；出包完成后原始
文件立即删除，只保留给他生成的那个 IPA。

**It ships no game content.** What users upload must be dumped from their own console;
the originals are deleted right after packing, and only the resulting IPA is kept.

---

## 完整链路 / The full pipeline

```
用户上传 NSP                     user uploads NSP
        │
        ▼
nx_extract 解包                   extract → ExeFS/ + RomFS/ + control.nacp + icon.jpg
        │                         （prod.keys 在这里用掉，全局通用，每台机器一样）
        ▼
patch_nso.py 改写程序              rewrite the code modules for Apple/ARM64
        │                         main / sdk / subsdk0 三个模块，逐指令改写
        ▼
guest_dylib.py + sign_adhoc.py   写成一个 Mach-O 动态库并打 adhoc 签名
        │                         iOS 要求每个嵌入的 Mach-O 都有签名节
        ▼
shader_split.py 拆 shader         从 RomFS 里扫出全部 shader（SMO 是 27984 个）
        │
        ▼
dmsh_build.py + spirv-cross       Maxwell 机器码 → SPIR-V → Metal Shading Language
        │                         nometal 模式：MSL 源码直接进包，手机上首次启动时编译
        ▼
pack_ipa.py 打包                  app + shader + RomFS → 一个 IPA
        │
        ▼
zsign 签名                       用 p12 + mobileprovision 重签
        │
        ▼
OTA 安装                         itms-services:// 链接，手机上点一下装
        │
        ▼
定时续签                          证书 7 天到期，提前一天自动重签
```

---

## 目录结构 / Layout

| 文件 | 职责 | Responsibility |
|---|---|---|
| `app.py` | Web 入口：路由、会话、上传、OTA | Web entry: routes, sessions, upload, OTA |
| `models.py` | 数据层：用户 / 设备 / 任务 / 邀请码 / 凭据 | Data layer: users, devices, jobs, invites, creds |
| `worker.py` | 任务队列、打包执行、自动续签线程 | Job queue, pack execution, auto-renew thread |
| `signer.py` | zsign 调用、OTA manifest 生成 | zsign wrapper, OTA manifest |
| `apple_provision.py` | Apple ID → 证书 + 描述文件（走 GSA + developerservices2） | Apple ID → cert + profile via GSA |
| `templates/` | 页面模板 | Page templates |

---

## 跑起来 / Running it

### 1. 依赖 / Requirements

```bash
# Python 3.9+
pip install flask requests lz4 capstone fonttools zstandard

# .NET SDK（翻译器 dmsh_xlate 需要）
winget install Microsoft.DotNet.SDK.10

# spirv-cross（Windows 用 MSYS2 拿，版本要和壳构建时一致）
winget install MSYS2.MSYS2
C:\msys64\usr\bin\pacman.exe -S --needed mingw-w64-x86_64-spirv-cross
```

### 2. 三样外部东西 / Three external pieces

这些东西不在仓库里，仓库也不该带：

**These are not in the repo, and must not be:**

| 东西 | 放哪 | 说明 |
|---|---|---|
| `prod.keys` | `D:\switch\prod.keys` | 解包用。全机型通用。**绝对不能提交** |
| 预编好的壳 | `svc/shells/<title id>.zip` | 每个游戏一个，用 `dm/ios/tools/shell_build.sh` 在 macOS 上编 |
| `zsign.exe` | `svc/bin/zsign.exe` | [Releases](https://github.com/zhlynn/zsign/releases) 下 Windows 版 |

### 3. 启动 / Start

```bash
cd svc
python app.py                 # http://127.0.0.1:8772
```

首次启动会自动建库、建 `admin / 111111`。**上线前务必改掉这个密码。**

First launch creates the database and an `admin / 111111` account. **Change it before
exposing the service.**

### 4. 外网访问 / Public access

iOS 装描述文件、OTA 安装都要求 HTTPS，本机 `127.0.0.1` 不行。开发阶段用
Cloudflare 快速隧道：

```bash
cloudflared tunnel --url http://127.0.0.1:8772 --protocol http2 --edge-ip-version 4
```

拿到 `https://xxx.trycloudflare.com` 后，带环境变量重启：

```bash
set SVC_PUBLIC_URL=https://xxx.trycloudflare.com
python app.py
```

国内网络下必须加 `--protocol http2 --edge-ip-version 4`：QUIC(UDP) 和 IPv6 经常连不上，
不定会表现为隧道时通时断，用户侧看到 Cloudflare 的 1033 错误页。

---

## 环境变量 / Environment

| 变量 | 作用 |
|---|---|
| `SVC_PUBLIC_URL` | 对外地址，OTA 链接和描述文件回传地址都用它 |
| `SVC_BUNDLE_ID` | OTA manifest 里的 bundle id |

---

## 已知限制 / Known limitations

**描述文件自动拿 UDID 在新 iOS 上不可靠。** `/device/profile.mobileconfig` 生成的是
未签名的 `Profile Service` 描述文件，iOS 26+ 越来越倾向于拒绝安装。当前的可行路径是
让用户用第三方工具（如 udid.tech）查到 UDID，再填进平台的手动表单。

**The auto-UDID profile is unreliable on recent iOS.** `/device/profile.mobileconfig`
serves an unsigned `Profile Service` profile, and iOS 26+ increasingly refuses those.
The working path today: the user looks up the UDID with a third-party tool and pastes it
into the manual form.

**壳要按游戏预编。** 每个 Title ID 需要一份在 macOS 上编出来的壳；没有壳的游戏会在
上传后被明确拒绝，不会给你一个跑不起来的包。

**Shells must be built per game.** Each title ID needs a shell compiled on macOS. Games
without one are rejected with a clear message rather than producing a broken package.

---

## 安全须知 / Security notes

- `data/` 里存着用户的签名凭据（p12、mobileprovision）和数据库，**不要提交、不要外传**
- 平台自己用的 `prod.keys` 只能放在服务端，绝不能随包发给用户
- `admin / 111111` 只是开发默认值，公网部署前必须改
- 这份代码默认给本地/内网用；要做多租户公网服务，凭据应当换 KMS 托管、p12 换成人手不可读的存储

---

## 许可 / License

服务层代码随主仓库，MIT。`apple_provision.py` 依赖的 iPASide 引擎是独立的 MIT 项目：
<https://github.com/pwnapplehat/iPASide>，以 `.vendor/` 方式引入，不随本仓库分发。

Service code follows the main repository, MIT. The iPASide engine that
`apple_provision.py` builds on is a separate MIT project, vendored under `.vendor/` and
not redistributed here.
