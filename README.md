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
  改写成带前缀；紧随 `<head>` 之后注入 `<base href="/app/agent2api/">` 与两段
  脚本 —— 一段把面板要的密钥种进 `localStorage['agent2api.webKey']`，一段补
  `fetch`/XHR/`WebSocket` 等运行时 API 的前缀。

**为什么还要 `<base>`（这一条是踩出来的）。** 桌面入口的 `url` 是
`/app/agent2api`，**不带尾斜杠**，于是文档 URL 就是
`https://host/app/agent2api`。这种文档 URL 下，任何**裸相对引用**都会被浏览器
按「上一级目录」解析：

```
new URL('assets/providers/workbuddy.png', 'https://host/app/agent2api')
  → https://host/app/assets/providers/workbuddy.png     ← agent2api 这一段被吃掉了
```

`RewriteHTML` 只改写**静态 HTML 里**的引用，救不了这些引用 —— 它们全部是运行期
由面板 JS 拼出来的：`preset-providers.js` 的 `iconOf()` 返回
`assets/providers/<file>.png`，反代列表的图标则编译在 `islands/ui.js` 的
`PROVIDER_ICONS` 里。表现就是**「添加账号 → 反代」里各个模型的图标全部不显示**
（只剩首字母兜底徽章）。

修法是双保险，两处都在 `fngateway` 内、不碰上游产物：

1. 响应里注入 `<base href="{前缀}/">`，把文档基址钉回前缀之下 ——
   与 `fnos-logmanager`（`ServeIndexWithBase`）、`nas`（`injectPrefix`）、
   `deepseek.harness`（`fnGatewayBridgeScript`）四处做法一致。
2. 桥接脚本解析相对引用时用 `P + '/'` 作基准（`RESOLVE_BASE`），
   而不是 `window.location.href`。**这一步不是可有可无的防御**：加了 `<base>`
   之后浏览器已经能正确解析裸相对引用，但桥接脚本仍会把这个 URL 按无斜杠的
   文档 URL 解析、再补一次前缀，亲手把它改成 `/app/agent2api/app/assets/...`
   的双重前缀 404。两者必须一起改。

> 这个缺陷在本地 `devcheck` 里**看不出来**：`devcheck` 打印的地址带尾斜杠
> （`…/app/agent2api/`），带斜杠的文档 URL 解析是正常的。只有模拟飞牛桌面入口
> 那种不带斜杠的地址才会复现。`devcheck` 现在会把这一点打在日志里。

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
│   ├── check-upstream.yml      # 每周一查上游；有新版就改 pin+manifest 并派发构建
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
│   └── *_test.go               # 37 个测试
├── app/ui/                     # 上游 WebUI（71 文件）+ config + images/
├── tools/                      # 本地开发辅助脚本（不参与打包）
│   ├── make_icons.py           # 从一张主图生成四个图标文件
│   ├── ui_hash.py              # 算/核对上游 WebUI 树哈希
│   ├── bump_upstream.py        # 查上游新版本 → 改四处 pin + manifest（CI 定时用）
│   ├── inspect_fpk.py          # 独立复核生成的 fpk（与 build.py 不同的代码路径）
│   ├── check_workflow.py       # 静态校验 CI workflow（needs/runs-on/artifact/cron/语法）
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

# 维护：查上游新版本并改齐 pin/manifest（CI 定时跑的同一个脚本，见 §3.5）
python tools\bump_upstream.py --detect             # 只看，不改文件
python tools\bump_upstream.py --apply              # 有新版就改到位
```

`--sync-upstream` 只做「刷新 app/ui 并核对」，不继续打包 —— 它是个独立维护
步骤，改完的 `app/ui` 要提交进仓库，这样离线打包也能出完整包。

### 3.3 CI 构建

两条 workflow，分工明确：

| workflow | 触发 | 干什么 |
| --- | --- | --- |
| [`check-upstream.yml`](.github/workflows/check-upstream.yml) | 每周一 03:23 UTC 定时（也可手动） | 查上游有没有新版；有就改 pin+manifest、提交 `main`、打 tag、派发构建 |
| [`build-and-release.yml`](.github/workflows/build-and-release.yml) | `v*` tag / 被派发 / 手动 | 编 Linux 二进制 → 出 fpk → 发 Release（含 `SHA256SUMS.txt`） |

**自动升级（默认路径）**：上游发新版后，最迟一周（下个周一），`check-upstream.yml` 会
自动走完「改四处 pin → 同步面板 → 提交 → 打 tag → 构建 → 发 Release」。
平时无事发生时它只是一次几秒的空转，不会重复发版（幂等：提交后 pin 已等于
上游最新版，下次判定「无更新」直接跳过）。

**为什么是「派发」而不是「push tag 自动触发」**：GitHub 有一条防递归规则 ——
用内置 `GITHUB_TOKEN` 推的 tag **不会触发**其它 workflow 运行，只有
`workflow_dispatch` / `repository_dispatch` 两个事件例外。所以如果让定时任务
去 push tag 等构建自己开跑，tag 会推上去但构建永远不开始；而那时 pin 已经
提交进 `main`，下次检查会判定「无更新」，这个版本就**永远不会有 Release**。
改成显式 `gh workflow run --ref <tag>` 派发，构建逻辑仍然只有一份。

**手动跑 `build-and-release.yml`**：

- `ref` 选 **tag** → 构建 + 发 Release（等价于 tag 触发）
- `ref` 选 **分支** → 只构建，产物走 artifact，不发 Release；
  勾上 `force_release` 才会发（tag 名取 `manifest` 里的版本号，不存在则自动创建）
- 重发某个版本：`ref` 填对应 tag 即可（同名 Release 会被替换，不会报 already exists）

**runner 为什么是 `ubuntu-22.04` 而不是 `ubuntu-24.04`** —— glibc 兼容性：

| runner | glibc | 结论 |
| --- | --- | --- |
| `ubuntu-22.04` | 2.35 | ✅ 低于飞牛的 2.36，安全 |
| `ubuntu-24.04` | 2.39 | ❌ 可能报 `version 'GLIBC_2.38' not found` |

二进制在哪个 glibc 上编，就要求目标机 glibc ≥ 那个版本。飞牛 fnOS 1.2.x 基于
Debian 12 (bookworm) = glibc 2.36。这个坑**不会在构建期报错，只在真机启动时
炸**，所以 workflow 里有一道 `readelf`/`objdump` 断言，把「runner 镜像悄悄
升级」这种漂移变成构建期硬失败。（`check-upstream.yml` 只跑 Python 与 git，
不产二进制，所以没有这个约束，跑在 `ubuntu-24.04` 上无所谓。）

编出来的二进制要落到 `.local-build/bin/` 才能打包，命名固定：

```
fngateway-linux-amd64          fngateway-linux-arm64
agent2api-server-linux-amd64   agent2api-server-linux-arm64
```

从 CI 的 artifact 下载后放进该目录，`python build.py` 即可离线打包。

**fpk 不是逐字节可复现的** —— `fnpack` 自己写 tar 元数据（mtime/uid），gzip 层
也带时间，包内 `manifest` 的 `checksum` 字段随之改变。所以本地重打同一个
commit 与 CI 的产物哈希天然对不上，**这不是 bug**。已逐文件比对确认：
`app.tgz` 内 78 个文件的内容哈希完全一致，差异只在打包层。
`UPSTREAM.txt` 的构建时间戳尊重 `SOURCE_DATE_EPOCH`（CI 喂的是提交时间），
所以同一 commit 重跑时那一行不再漂。

### 3.4 构建期守卫

都是「编译能过、真机才炸」的静默缺陷，所以做成硬失败：

| 守卫 | 防的是什么 |
| --- | --- |
| `check_text_hygiene` | CRLF 破坏 `cmd/*` 的 shebang；BOM 让 fnpack 解析出带 `\ufeff` 的键名而静默失配 |
| `check_manifest_values` | fnpack 把 manifest 取值里的 `;` 当终止符，之后内容**静默丢弃** |
| `verify_icons` | 图标缺失/尺寸错 → 桌面图标空白 |
| `check_webui`（图标） | `ui/assets/providers/*.png` 缺失或索引引用的图标不存在 →「添加账号」里模型图标空白。**两个索引写法不同**（反代在 `islands/ui.js` 里写完整路径 `assets/providers/x.png`；预置在 `preset-providers.js` 里只写 `icon: 'x.png'`，由 `iconOf()` 运行期拼目录），故配了两条正则 —— 实测只配一条时 20 个预置图标会全部漏检 |
| `check_webui` | 上游 WebUI 缺文件（面板白屏）或资源引用变成根绝对路径（改写失效） |
| `verify_webui_provenance` | 面板形态漂移（上游改了 HTML/localStorage 键/fetch 形态） |
| `verify_upstream_version` | tag 被移动 / 镜像给了旧包 → 版本号对不上 |
| `check_consistency` | 前缀 / socket / 端口 / 入口 / 运行用户在四处文件里不一致 |
| `check_binaries` | 二进制缺失、不是 ELF、架构不符 |
| CI `Verify binaries` | 二进制架构不符、glibc 超过飞牛的 2.36、**以及替身二进制**（无 `rustc` 指纹或体积 < 3 MB） |
| `verify_fpk` | 打包后重新解包核对：ELF 架构、可执行位、`cmd/*` 权限、包内 manifest 逐值一致 |
| `check_workflow.py` | workflow 静态错误：`needs` 指向不存在的 job、缺 `runs-on`、引用不存在的脚本、下载了本 workflow 没上传的 artifact、非法 cron、未声明的 `inputs.X`、`uses` 缺版本号或跨文件版本不一致、`bash -n` 语法错误、派发目标不可手动触发 |

`fngateway` 自身还有 37 个 Go 测试：

```powershell
cd fngateway; go vet ./...; go test ./...
```

独立复核（不依赖 `build.py` 的自检，重新解包逐项检查）：

```powershell
python tools\inspect_fpk.py
python tools\check_workflow.py
python tools\check_text.py
```

### 3.5 换图标 / 升级上游版本

**换图标**：把主图（建议 1024×1024 PNG）放到任意位置，然后

```powershell
python tools\make_icons.py 主图.png
```

它会按中心方形裁剪 + LANCZOS 缩放，写出 `ICON.PNG`、`ICON_256.PNG`、
`app/ui/images/icon_64.png`、`app/ui/images/icon_256.png` 四个文件。

**升级上游**：正常情况下**不用手动做** —— `check-upstream.yml` 每周一自动查、
自动改、自动发包（见 §3.3）。需要立刻升级或想先在本地看一眼时：

```powershell
# 只看上游有没有新版，不改任何文件（只读，安全）
python tools\bump_upstream.py --detect

# 有新版就改到位：build.py 的两处 pin + manifest 的 version/changelog
python tools\bump_upstream.py --apply

# 上游已是最新时也重打一次当前版本（验证流水线用）
python tools\bump_upstream.py --apply --force

# 指定版本（比如上游最新是 2.10.0，想先跳到 2.9.5 验证）
python tools\bump_upstream.py --apply --upstream-version 2.9.5
```

脚本会**一次性改齐四处耦合值**，漏改任何一处都会让 `build.py` 硬失败：

| 位置 | 内容 |
| --- | --- |
| `build.py` `UPSTREAM_VERSION` | 驱动 `UPSTREAM_TAG` 与下载 URL |
| `build.py` `UPSTREAM_UI_SHA256` | 面板树哈希（含注释里的文件数） |
| `manifest` `version` | 格式 `x.y.z-N` |
| `manifest` `changelog` | 覆盖为最新一版（只留本版说明，不累加旧条目） |

改完还会自己跑一遍不需要二进制的构建期守卫（卫生/图标/面板/契约），
不过就非零退出、不留半成品。若上游改了面板，`sync_webui` 会把新面板写进
`app/ui`，树哈希随之变化 —— 这一步是自动的，不再需要人工抄哈希。

> 只跑 `python build.py --sync-upstream` 仍然可用：它只做「刷新 `app/ui`
> 并核对溯源」，不碰 pin 和 manifest。改完的 `app/ui` 要提交进仓库，
> 这样离线打包也能出完整包。

---

## 4. 安装与升级

### 4.1 下载现成的包（推荐）

已构建好的包挂在 Release 上，按机器架构选一个：

- <https://github.com/sushazhi/fnos-agent2api/releases/latest>

| 架构 | 适用机型 | 包大小 |
| --- | --- | --- |
| `arm64` | 飞牛 ARM 机型（如瑞芯微/ARM 主板） | ≈ 8.0 MB |
| `amd64` | x86_64 机型 | ≈ 8.5 MB |

下载后校验（可选，但建议）：

```bash
sha256sum -c SHA256SUMS.txt
```

### 4.2 安装

把 `agent2api-<版本>-<arch>.fpk` 装进飞牛应用中心即可。安装向导会说明提供商
范围、下游端口地址与凭据风险。

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
  —— 后续修「模型图标不显示」时新增 3 个测试（`TestBaseTag`、
  `TestBridgeScriptResolvesRelativeToPrefix`，以及
  `TestProxyHTMLRewriteAndInject` 里新增的 `<base>` 注入顺序断言），
  当前共 **37 个，全部通过**（`go vet` 亦干净）
- 图标守卫做了正反两向验证：正常树上报「磁盘 30 个，被引用 30 个，缺失 0 个」；
  把 `openai.png` 临时改名后，`--check` 如期以 exit 1 失败并点名该文件
- 根因用 Node 实测复现（无尾斜杠文档 URL）：
  `new URL('assets/providers/openai.png','http://host/app/agent2api')`
  → `http://host/app/assets/providers/openai.png`（`agent2api` 段被吃掉），
  加尾斜杠后才落到 `.../app/agent2api/assets/providers/openai.png`
- 上游 WebUI 溯源哈希与上游 tag 逐字节一致（`5ecf43c0…`，71 个文件）
- 打包管线端到端跑通：组装 → `fnpack build` → 解包自检，amd64 与 arm64
  两个包均通过；随后用 `tools/inspect_fpk.py` 独立复核了包内 ELF 架构、
  `cmd/*` 权限、`ui/config` 内容与 `UPSTREAM.txt` 文案
- 各构建期守卫逐个做负例注入（篡改面板字节 / 写错前缀 / manifest 塞分号 /
  删图标 / 抽掉二进制），均如期硬失败
- 自动升级脚本 `tools/bump_upstream.py` 端到端验证：`--detect` 只读、不改文件；
  `--apply` 六个步骤全过（改 pin → 同步面板 → 重算树哈希 → 溯源复核 → 改
  manifest → 跑守卫），改完的 `manifest` 差异精确到只有 `version` 与
  `changelog` 两处，`build.py` 与 `app/ui` 逐字节未变；`--apply --force`
  在上游无新版时正确跳过 changelog 追加（不产生重复条目）
- workflow 静态检查 `tools/check_workflow.py` 做了 12 项负例注入
  （派发不存在的 workflow / 派发目标不可手动触发 / 未声明的 `inputs.X` /
  非法 cron / 引用不存在的脚本 / 下载未上传的 artifact / 关键 artifact 缺失 /
  `uses` 缺版本号 / 跨文件版本不一致），全部如期检出；两个 workflow 里
  20 个 `run` 块通过 `bash -n` 语法检查

**已通过 CI 验证**（GitHub Actions，`ubuntu-22.04` 编 / `ubuntu-24.04` 打包）：

- 真实上游二进制产出成功。`agent2api-server-linux-{amd64,arm64}` 分别为
  10,297,816 B / 9,052,672 B，`rustc` 指纹存在、无 Go 占位标记
- glibc 上限实测 **2.34**（< 飞牛的 2.36）；`NEEDED` 只有
  `libgcc_s.so.1` / `libm.so.6` / `libc.so.6`，无 `RUNPATH`
- Release `v2.9.0-1` 已发布，两个 fpk 与 `SHA256SUMS.txt` 可匿名下载，
  校验和逐项核对一致
- Rust 依赖缓存命中生效（350 MB，第二次运行恢复后重编时间明显下降）

**尚未验证**：

- ⚠️ **自动升级链路的首次真实运行**。`check-upstream.yml` 的每一步都在本地
  验证过（脚本 `--detect`/`--apply`/`--force` 三种模式、workflow 静态检查、
  shell 语法、`gh workflow run` 的目标可派发），但**「定时触发 → 提交 →
  打 tag → 派发 → 构建 → 发 Release」这条完整链路还没有被上游真实的新版本
  跑通过** —— 需要等上游发一次新版才能确认。已知的、无法在本地验证的点：
  Actions 对 `secrets.GITHUB_TOKEN` 的 `actions: write` 授权是否允许
  `gh workflow run`（若被拒，退路是改用 `repository_dispatch` + 一个 PAT）。
- ⚠️ **真机安装**。以上全部是构建期结论，**尚未在真实飞牛设备上跑起来**。
  待验证项：socket 路径与权限、`X-Trim-*` 管理员闸门、面板经网关后的渲染与
  交互、下游 `:3065/v1` 的局域网可达性。
  —— 「添加账号 → 反代」模型图标的修复已在本机通过 `go test`（37 个）与
  `python build.py --check`，但**图标在真机上是否真的显示出来，仍需实机确认**；
  这是该 bug 的最终验收。
- ⚠️ **上游升级后需回归**：图标守卫依赖 `preset-providers.js` 的
  `icon: 'x.png'` 写法与 `islands/ui.js` 的完整路径字面量。上游若改写法，
  两条正则可能同时失效 —— 已加「解析出的图标数不得少于磁盘数一半」的兜底断言
  把它变成构建期失败，但升级上游时仍应人工看一眼日志里的图标计数。
  **自动化把这件事的风险放大了**：以前升级是人手动跑的，会顺带看一眼日志；
  现在没人看了。兜底断言能把「漏检」变成「构建失败」，但断言本身失效
  （两条正则同时不匹配任何图标）时仍会静默通过 —— 这是已知的残余风险。
- 真机首次安装曾暴露过一次 `exit status 1`：当时 `.local-build/bin/` 里放的是
  Go 占位程序（1.5 MB、静态、**能通过全部 glibc/架构检查**），不是真 Rust
  二进制。现已加两道断言（`rustc` 指纹 + 体积下限）拦住这类替身。
  该次真机日志本身有价值：**飞牛的安装/启动/网关/停止链路是通的**，
  失败只发生在上游二进制那一步。
