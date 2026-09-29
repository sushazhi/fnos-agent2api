# fnos-agent2api

把上游 [aimod-cc/agent2api](https://github.com/aimod-cc/agent2api) 打包成飞牛
fnOS 的原生应用（`.fpk`），以**统一网关**方式接入桌面。

上游是 Rust 单文件静态二进制，自带官方 Web 控制台，内置 7 个提供商适配
（WorkBuddy / 小浣熊 Raccoon / CatPaw / AutoClaw / Qoder / Cline Free /
Cline Pass），把客户端登录态复用成标准 OpenAI 兼容接口。

本仓库**不含上游源码**，只提供飞牛适配层 + 上游 WebUI 的一份副本：

```
fngateway/     Go 反向代理：持有 Unix socket、注入网关密钥、改面板子路径
cmd/           飞牛生命周期脚本（install / upgrade / config / uninstall）
config/        privilege（run-as=package）、resource
wizard/        安装/升级/卸载/配置向导文案
app/ui/        上游 WebUI（71 个文件）+ 本项目加的桌面入口 config 与图标
manifest       应用元信息
build.py       打包脚本（本地与 CI 共用同一条路径）
.github/       CI：编 Linux 二进制 + 出 fpk + 发 Release
```

> 这是个人自用项目。上游 LICENSE 是 MIT 正文 + 一份「使用声明」，其中第 3 条
> 禁止商业用途与二次分发牟利（个人学习、研究、自用不受限）。详见 §8。

---

## 1. 架构

```
浏览器 ──https──> 飞牛统一网关 ──unix socket──> fngateway ──tcp 127.0.0.1:<随机>──> agent2api-server
（仅管理员）        /app/agent2api            （本仓库）      （内核分配）              （上游二进制）

OpenAI SDK ──http──> http://<飞牛IP>:3065/v1 ─────────────────────────────────────> agent2api-server
（任意客户端）        （独立下游端口，不经统一网关）
```

一个应用，两条入口，互不干扰：

| 入口 | 路径 / 端口 | 给谁用 | 鉴权 |
| --- | --- | --- | --- |
| 统一网关 | `/app/agent2api` | 飞牛桌面里的 Web 控制台 | 飞牛登录态 + 仅管理员 |
| 下游端口 | `http://<飞牛IP>:3065/v1` | 任意 OpenAI 兼容客户端 | `Authorization: Bearer <网关密钥>` |

### 1.1 为什么还要 fngateway 这一层

上游 `agent2api-server` 只监听 TCP 端口，不认 Unix socket，而飞牛统一网关要求
应用监听 `${TRIM_APPDEST}/<appname>.sock`。有两条路：改上游源码加 socket 支持，
或者在外面套一层自己持有 socket。

这里选了后者，理由是**上游源码零改动** —— 上游发布节奏很快（v2.7 → v2.9 期间
密集迭代），一旦本地打了 patch，每次跟进上游都要重放冲突。`fngateway` 反过来
把上游当黑盒：它自己监听 socket，再代理到内核分配的回环端口上的子进程。

除了 socket，`fngateway` 还负责三件上游不管的事：

1. **网关密钥注入。** 上游的管理面板有自己的登录体系（注册管理员 + 会话
   Cookie）。飞牛这边不希望用户在应用里再注册一套账号，所以由 `fngateway`
   在 `${TRIM_PKGVAR}/apikey` 生成 32 字节随机密钥（0600），通过
   `AGENT2API_PROXY_API_KEY` 传给子进程。密钥进了 `active_api_keys()`，
   面板的登录闸门就**永远不启用**，也就**从不下发 Cookie** —— 没有 Cookie
   就没有「飞牛网关剥离 `Set-Cookie` 导致登录后掉线」那类问题。
   密钥只存在于服务端，浏览器拿不到。
2. **面板子路径改写。** 见 §1.2。
3. **生命周期与日志。** 轮转日志、优雅停机（转发 SIGTERM，等子进程退出）、
   清掉残留 socket。

### 1.2 面板的子路径适配（为什么 agent2api 比 cli2api 省事）

飞牛把应用挂在 `/app/agent2api` 下，而上游面板是**按根路径 `/` 写的**：
`fetch('/api/panel/status')`、`href="css/tokens.css"`。不改写就会全部 404。

这里有一条关键约束：Chromium 里 `Location` 的 `pathname` 等属性是
`[LegacyUnforgeable]`，**运行期 JS 无法覆盖 `location.pathname`**。所以纯前端
方案走不通，必须在服务端把响应改写掉。`fngateway` 做的是：

- **读方向**：剥掉 `/app/agent2api` 前缀再转给子进程；把响应里的
  `Location` 补回前缀；把 `Set-Cookie` 的 `Path` 从 `/` 收窄到
  `/app/agent2api/`（子路径迁移），否则 Cookie 越界且会被同域其它应用覆盖。
- **写方向**：HTML 里 `src` / `href` / `action` / `poster` 的**裸相对路径**
  改写成带前缀；紧随 `<head>` 之后注入两段脚本 —— 一段把面板要的密钥种进
  `localStorage['agent2api.webKey']`，一段补 `fetch`/XHR/`WebSocket` 等
  运行时 API 的前缀。

之所以说 agent2api 比 cli2api 省事：上游面板**没有客户端路由**（全仓库
`ui/` 下 `location.pathname` / `pushState` / `basename` 出现 0 次，翻页是内存里
一个 `currentPage` 变量），所以不需要像 cli2api 那样在压缩产物里做正则锚点
打补丁、也不怕上游改压缩方式导致锚点失配。

**上游 WebUI 的形态被钉住了。** `build.py` 记着 `app/ui` 里上游派生文件的
确定性树哈希（`UPSTREAM_UI_SHA256`），上游一旦改动面板就构建期失败，逼人工确认
「形态是否还是 fngateway 能改写的那一种」。同理，`check_webui()` 会拒绝出现根
绝对路径资源引用的 `index.html`。

---

## 2. 目录结构

```
fnos-agent2api/
├── manifest                    # 应用元信息（appname/version/service_port/入口）
├── build.py                    # 打包脚本（本地与 CI 共用）
├── LICENSE                     # 本项目（适配层）的许可
├── LICENSE.upstream            # 上游 agent2api 的完整许可（随仓库提交，要进包）
├── ICON.PNG / ICON_256.PNG     # 桌面图标（64 / 256）
├── .github/workflows/
│   └── build-and-release.yml   # 编二进制 + 出 fpk + 发 Release
├── config/
│   ├── privilege               # run-as=package, username/groupname=agent2api
│   └── resource                # {}
├── cmd/                        # 飞牛生命周期脚本（必须 LF + 可执行位）
│   ├── install_init / install_callback
│   ├── upgrade_init / upgrade_callback
│   ├── config_init  / config_callback
│   ├── uninstall_init / uninstall_callback
│   └── main                    # start / stop / status
├── wizard/                     # install / upgrade / uninstall / config 向导
├── fngateway/                  # Go 适配层（纯标准库，CGO_ENABLED=0）
│   ├── main.go                 # socket、密钥、子进程、日志、信号
│   ├── rotate.go               # 轮转日志 writer
│   ├── build.ps1               # 本地交叉编译（Windows 直出 linux 双架构）
│   ├── internal/gateway/
│   │   ├── gateway.go          # 前缀剥离/回填、HTML 改写、Cookie Path 改写
│   │   ├── bridge.go           # 注入面板的 seed + 运行时 API 补丁脚本
│   │   └── gate.go             # 管理员闸门
│   └── *_test.go               # 34 个测试
├── app/ui/                     # 上游 WebUI（71 文件）+ config + images/
├── tools/                      # 本地开发辅助脚本（不参与打包）
│   ├── make_icons.py           # 从一张主图生成四个图标文件
│   ├── ui_hash.py              # 算/核对上游 WebUI 树哈希
│   ├── inspect_fpk.py          # 独立复核生成的 fpk（与 build.py 不同的代码路径）
│   └── check_text.py           # 编码/换行/可执行位检查
└── .local-build/               # 中间产物（gitignore）
    ├── bin/                    # 放 CI 产出的 4 个 Linux 二进制
    ├── tools/fnpack(.exe)
    └── upstream-src/           # --sync-upstream 拉的上游源码
```

---

## 3. 构建

### 3.1 前置条件

**本机不需要 Rust 工具链，也不需要 WSL / Docker。** 上游二进制的编译交给
GitHub Actions（见 §3.3）—— 这是刻意的：Rust 交叉编译在 Windows 上要装
交叉工具链或开 WSL，而 CI 上就是一条 `apt-get` + `cargo build`。

本机只需要：

| 用途 | 依赖 |
| --- | --- |
| `fngateway` 交叉编译 | Go ≥ 1.27（纯标准库，`CGO_ENABLED=0` 即静态） |
| 打包 | Python ≥ 3.8 |
| 打包 | `fnpack`（`build.py` 会自动下载到 `.local-build/tools/`） |

```powershell
# 编 fngateway 的 linux 双架构（Windows 上直接出 ELF）
pwsh -File fngateway\build.ps1 -Version 2.9.0-1
# 产物：.local-build\bin\fngateway-linux-{amd64,arm64}
```

### 3.2 命令

```powershell
# 全量预检（不联网、不打包）：图标 / WebUI / 溯源 / 契约 / 二进制
python build.py --check

# 打包（amd64 + arm64 各一个 fpk；需 .local-build/bin 里已有 4 个二进制）
python build.py
python build.py --arch arm64        # 只出 arm64
python build.py --version 2.9.0-2   # 覆盖版本号

# 维护：拉上游 tag 源码，把 desktop-tauri/ui 同步进 app/ui 并核对溯源
python build.py --sync-upstream
python build.py --sync-upstream --force-upstream   # 忽略缓存重下
```

`--sync-upstream` 只做「刷新 app/ui 并核对」，不继续打包 —— 它是个独立维护
步骤，改完的 `app/ui` 要提交进仓库，这样离线打包也能出完整包。

### 3.3 CI 构建（推荐）

推 `v*` tag 触发 [`.github/workflows/build-and-release.yml`](.github/workflows/build-and-release.yml)：
先编 Linux 二进制，再打包并发布 Release（含两个 fpk 与 `SHA256SUMS.txt`）。
也可以 `workflow_dispatch` 手动跑，产物走 artifact、不发 Release。

**runner 为什么是 `ubuntu-22.04` 而不是 `ubuntu-24.04`** —— glibc 兼容性：

| runner | glibc | 结论 |
| --- | --- | --- |
| `ubuntu-22.04` | 2.35 | ✅ 低于飞牛的 2.36，安全 |
| `ubuntu-24.04` | 2.39 | ❌ 可能报 `version 'GLIBC_2.38' not found` |

二进制在哪个 glibc 上编，就要求目标机 glibc ≥ 那个版本。飞牛 fnOS 1.2.x 基于
Debian 12 (bookworm) = glibc 2.36。这个坑**不会在构建期报错，只在真机启动时
炸**，所以 workflow 里有一道 `readelf`/`objdump` 断言，把「runner 镜像悄悄
升级」这种漂移变成构建期硬失败。

编出来的二进制要落到 `.local-build/bin/` 才能打包，命名固定：

```
fngateway-linux-amd64          fngateway-linux-arm64
agent2api-server-linux-amd64   agent2api-server-linux-arm64
```

从 CI 的 artifact 下载后放进该目录，`python build.py` 即可离线打包。

### 3.4 构建期守卫

都是「编译能过、真机才炸」的静默缺陷，所以做成硬失败：

| 守卫 | 防的是什么 |
| --- | --- |
| `check_text_hygiene` | CRLF 破坏 `cmd/*` 的 shebang；BOM 让 fnpack 解析出带 `\ufeff` 的键名而静默失配 |
| `check_manifest_values` | fnpack 把 manifest 取值里的 `;` 当终止符，之后内容**静默丢弃** |
| `verify_icons` | 图标缺失/尺寸错 → 桌面图标空白 |
| `check_webui` | 上游 WebUI 缺文件（面板白屏）或资源引用变成根绝对路径（改写失效） |
| `verify_webui_provenance` | 面板形态漂移（上游改了 HTML/localStorage 键/fetch 形态） |
| `verify_upstream_version` | tag 被移动 / 镜像给了旧包 → 版本号对不上 |
| `check_consistency` | 前缀 / socket / 端口 / 入口 / 运行用户在四处文件里不一致 |
| `check_binaries` | 二进制缺失、不是 ELF、架构不符 |
| `verify_fpk` | 打包后重新解包核对：ELF 架构、可执行位、`cmd/*` 权限、包内 manifest 逐值一致 |

`fngateway` 自身还有 34 个 Go 测试：

```powershell
cd fngateway; go vet ./...; go test ./...
```

独立复核（不依赖 `build.py` 的自检，重新解包逐项检查）：

```powershell
python tools\inspect_fpk.py
python tools\check_text.py
```

### 3.5 换图标 / 升级上游版本

**换图标**：把主图（建议 1024×1024 PNG）放到任意位置，然后

```powershell
python tools\make_icons.py 主图.png
```

它会按中心方形裁剪 + LANCZOS 缩放，写出 `ICON.PNG`、`ICON_256.PNG`、
`app/ui/images/icon_64.png`、`app/ui/images/icon_256.png` 四个文件。

**升级上游**：改 `build.py` 顶部的 `UPSTREAM_VERSION` / `UPSTREAM_TAG`，
跑 `python build.py --sync-upstream`。若上游改了面板，`verify_webui_provenance`
会失败并打印新旧哈希 —— 人工确认改写逻辑仍然成立后，把新哈希写回
`UPSTREAM_UI_SHA256`。

---

## 4. 安装与升级

把 `agent2api-<版本>-<arch>.fpk` 装进飞牛应用中心即可（amd64 与 arm64 各一个
包，按机器架构选）。安装向导会说明提供商范围、下游端口地址与凭据风险。

命令行管理：

```bash
# 生命周期（TRIM_* 由飞牛注入，需在应用上下文里执行）
./cmd/main start | stop | status      # status: 0=运行中 3=未运行 1=参数错
```

- **升级**：数据（`data/agent2api.db`）与网关密钥（`apikey`）都会保留，
  升级脚本是幂等的，不会重建账号数据。
- **卸载**：向导里有 `wizard_keep_data` 选项，默认**保留**数据。

---

## 5. 使用

### 5.1 控制台（管理员）

飞牛桌面点应用图标，走统一网关 `/app/agent2api`，**仅管理员可见可访问**
（`allUsers: false`）。面板**不需要单独登录** —— 网关密钥由后端注入，浏览器
侧只拿到一个占位值。

面板里可以：加账号（扫码/设备码登录上游客户端）、管模型、发 API 密钥、看
运行日志与请求记录、定时任务、对话测试台。

> **注意**：控制台自带的「检查更新」在飞牛上不可用（没有宿主机 updater
> 进程），属预期降级 —— 升级请用应用中心。

### 5.2 下游接入（外部客户端）

标准 OpenAI 兼容接口在**独立端口**：

```
http://<飞牛IP>:3065/v1
```

网关密钥在 `${TRIM_PKGVAR}/apikey`（root/应用属主可读）：

```bash
cat /var/apps/agent2api/var/apikey
```

客户端配置（以 OpenAI SDK 为例）：

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://192.168.0.2:3065/v1",
    api_key="<apikey 内容>",
)
```

> **为什么不把外部客户端指到 `/app/agent2api`？** 飞牛 1.2.0604+ 会拦截非
> 飞牛 ticket 的 `Authorization` 头，外部客户端的 `Bearer` 会被吃掉。统一网关
> 那条路径只给桌面面板用。

### 5.3 数据与日志位置

全部在 `${TRIM_PKGVAR}`（即 `/var/apps/agent2api/var/`）：

| 路径 | 内容 |
| --- | --- |
| `data/agent2api.db` | 账号、日志、请求统计、调试报文、脱敏词表、网关配置（SQLite） |
| `data/agent2api.db-wal` / `-shm` | WAL 附属文件，**备份时要一起拷** |
| `apikey` | 网关密钥（0600，首次启动自动生成） |
| `home/` | 子进程的 `HOME`（0700） |
| `panel.log` | `fngateway` 自己的日志（8 MB 轮转） |
| `agent2api.log` | 上游子进程的 stdout/stderr（8 MB 轮转） |
| `main.log` | 飞牛生命周期脚本日志 |

---

## 6. 权限与安全设计

- **最小权限**：`config/privilege` 声明 `run-as=package`，以 `agent2api`
  用户/组运行，不用 root。
- **网关密钥只走服务端**：`fngateway` 生成 → 环境变量传子进程 → 面板里只显示
  掩码。密钥从不下发给浏览器，所以「前端能看到密钥」这条路不存在。
- **无面板登录 = 无 Cookie**：密钥进了 `active_api_keys()`，面板登录闸门不启用，
  全程不下发 `Set-Cookie`。即便将来启用面板登录，`fngateway` 的
  `RewriteCookie` 也已把 `Path` 收窄到应用子路径。
- **socket 权限最小化**：socket 建在 `${TRIM_APPDEST}`，启动前与停止时都清理
  残留（残留会让新进程 bind 失败）。
- **下游端口绑定所有网卡**：`agent2api-server` 监听 `0.0.0.0:3065`（不是
  `127.0.0.1`）—— 只绑回环会让局域网客户端连不上；子进程内部用的回环端口由
  内核分配、不对外暴露。
- **上游配置不外泄**：`fngateway` 刻意**不**设置 `AGENT2API_ADMIN_USER` /
  `AGENT2API_ADMIN_PASSWORD` / `AGENT2API_ALLOW_NO_KEY` / `AGENT2API_PANEL_PORT`，
  避免意外打开面板登录或 fail-open。
- **凭据明文**：上游把 accessToken/refreshToken 以**明文**存在
  `data/agent2api.db`。这是上游的既有设计（其 LICENSE 第 4 条有说明），本适配
  层没有改变它。不要在多人共享的设备上使用，也不要提交/分享该文件。
- **路径安全**：卸载脚本先确认 `DATA_DIR != "/"` 才递归删除。

---

## 7. 与 fnos-cli2api 的关系

两者是同一套飞牛适配思路的两个实例，适配层同源、内核不同：

| | fnos-cli2api | fnos-agent2api（本仓库） |
| --- | --- | --- |
| 上游 | caigee-cmd/cli2api | aimod-cc/agent2api |
| 上游形态 | Go 控制面 + Node worker | Rust 单文件二进制 |
| 运行时依赖 | 需要应用中心 Node.js 运行时 | 无（静态链接，仅 glibc） |
| 包体积 | ~95 MB（含 Node 与 CLI bundle） | 二进制约 10 MB / 9 MB（amd64/arm64） |
| 每账号开销 | 一个 Node 进程 + 独立 HOME（Qoder 账号约 511 MB RSS） | 无独立进程 |
| 面板适配 | 需要在压缩产物里做正则锚点打补丁 | 无客户端路由，只需服务端改写 |
| 上游漂移敏感度 | 高（钉死 Qoder CLI 版本 + 6 处压缩串） | 低（零源码改动） |
| 账号凭据 | 应用数据目录 | 应用数据目录（明文，上游设计） |

选 agent2api 的直接收益是**运行时依赖与内存**：不再需要 Node 运行时、不再有
每账号一份的 Node 进程，包体积从 ~95 MB 降到十几 MB。

需要留意的是提供商覆盖不同：cli2api 在你这台环境里实测通过的是 Qoder
Intl/CN + WorkBuddy Intl/CN + Trae CN；agent2api 内置的是 WorkBuddy /
小浣熊 / CatPaw / AutoClaw / Qoder / Cline Free / Cline Pass。其中
**WorkBuddy、Qoder、Cline 是设备码轮询登录**，在只有浏览器的场景下最顺；
AutoClaw / CatPaw 走 loopback 回调（要求浏览器与网关同机）；小浣熊用自定义
scheme `office-raccoon://` 回调，只能靠粘贴凭据。

---

## 8. 上游与许可

本仓库自己的代码（`fngateway/`、`cmd/`、`config/`、`wizard/`、`manifest`、
`build.py`、图标与桌面入口）以 **MIT** 发布，见 [`LICENSE`](LICENSE)。

上游 agent2api 有**自己的许可**，随仓库提交在
[`LICENSE.upstream`](LICENSE.upstream)，并会被 `build.py` 原样放进 fpk 包内：
MIT 条款正文**外加**一份「使用声明」，其中第 3 条禁止商业用途与二次分发牟利。

完整授权 = MIT 条款 + 使用声明，两者冲突时以更严格的一方为准。就本项目而言：
使用声明第 3 条明确「个人学习、研究、自用，以及不以牟利为目的的分享与交流，
不受本条限制」，所以自用打包与分发不受该条约束。

上游：<https://github.com/aimod-cc/agent2api>（`Copyright (c) 2026 aimod-cc`）

---

## 9. 已验证 / 未验证

**已在本地验证**（Windows）：

- `fngateway`：`go vet` 干净，`go test ./...` 34 个测试全过
  （含 HTML 改写、Cookie Path 改写、密钥生成与复用、前缀剥离/回填）
- 上游 WebUI 溯源哈希与上游 tag 逐字节一致（`5ecf43c0…`，71 个文件）
- 打包管线端到端跑通（用等价架构的 ELF 占位充当上游二进制）：
  组装 → `fnpack build` → 解包自检，amd64 与 arm64 两个包均通过；
  随后用 `tools/inspect_fpk.py` 独立复核了包内 ELF 架构、`cmd/*` 权限、
  `ui/config` 内容与 `UPSTREAM.txt` 文案
- 各构建期守卫逐个做负例注入（篡改面板字节 / 写错前缀 / manifest 塞分号 /
  删图标 / 抽掉二进制），均如期硬失败

**尚未验证**：

- ⚠️ **真机安装**。以上全部是本地构建期结论，**没有在真实飞牛设备上装过**。
  待验证项：socket 路径与权限、`X-Trim-*` 管理员闸门、面板经网关后的渲染与
  交互、下游 `:3065/v1` 的局域网可达性。
- ⚠️ **真实上游二进制尚未产出**。本仓库不提交二进制，需先跑一次 CI（§3.3）
  才能拿到真正的 `agent2api-server-linux-*`；在那之前 `python build.py`
  会在二进制预检处停下并提示去 Actions 取产物。上游二进制的体积与 glibc
  要求由 CI 里的断言负责（须 ≤ 2.36）。
