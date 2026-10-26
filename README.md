# fnos-agent2api

把上游 [aimod-cc/Agent2API](https://github.com/aimod-cc/Agent2API) 打包成飞牛 fnOS 原生应用（`.fpk`），以统一网关接入桌面。上游是 Rust 单文件静态二进制，自带 Web 控制台，内置 17 家 AI 客户端提供商适配（工作台/IDE 类客户端，另支持自定义），把客户端登录态复用成标准 OpenAI 兼容接口。

- 本仓库只含飞牛适配层；上游源码与 WebUI 都不入库，面板构建时从官方包注入。
- 当前版本 `3.3.0-beta.4-1`（约定「上游版本-N」）。个人自用项目；上游许可是 **AGPL-3.0**（自 v3.3.0-beta.2 起，此前为 MIT + 使用声明），见 §5。
- 上游仓库迁过两次：`aimod-cc/agent2api` →（2026-10-18）`Agent2API/agent2api` →（2026-10-24，因 Agent2API 账号消失）**迁回 `aimod-cc/Agent2API`**，现已可达，CI 定时跟进重新可用。⚠️ 但面板不入库，**干净克隆仍必须先让官方包落地**（§2.2）。

## 1. 架构

```
浏览器 ──https──> 飞牛统一网关 ──unix socket──> fngateway ──tcp 127.0.0.1:<随机>──> agent2api-server
（仅管理员）        /app/agent2api            （本仓库）      （内核分配）              （上游二进制）

OpenAI SDK ──http──> http://<飞牛IP>:3065/v1 ─────────────────────────────────────> agent2api-server
```

| 入口 | 路径 | 给谁用 | 鉴权 |
| --- | --- | --- | --- |
| 统一网关 | `/app/agent2api` | 桌面 Web 控制台 | 飞牛登录态 + 仅管理员 |
| 下游端口 | `:3065/v1` | 任意 OpenAI 兼容客户端 | `Authorization: Bearer <apikey>` |

### 1.1 fngateway 做什么

上游只监听 TCP、不认 Unix socket，而飞牛要求应用监听 `${TRIM_APPDEST}/<app>.sock`，所以外面套一层自己持 socket 的代理（**上游源码零改动**，跟进上游时不必重放 patch）。它还负责：

1. **密钥注入**：首次启动在 `${TRIM_PKGVAR}/apikey` 生成随机密钥（0600），经 `AGENT2API_PROXY_API_KEY` 传给子进程。密钥进 `active_api_keys()` 后面板登录闸门永不启用、从不签 Cookie —— 无需二次注册账号，也没有「网关剥 `Set-Cookie` 导致掉线」的问题；密钥只在服务端。
2. **面板子路径改写**（§1.2）。
3. **生命周期与日志**：轮转日志、优雅停机、清理残留 socket。

### 1.2 子路径适配

上游面板按根路径写（`fetch('/api/...')`），而 `location.pathname` 是 `[LegacyUnforgeable]` 无法在 JS 侧覆盖，只能服务端改写：

- **读方向**：剥前缀转发，响应里 `Location` 补前缀，`Set-Cookie` 的 `Path` 收窄到 `/app/agent2api/`。
- **写方向**：HTML 里裸相对路径补前缀；`<head>` 后注入 `<base href="/app/agent2api/">` 和两段脚本（种密钥到 `localStorage['agent2api.webKey']`、给 `fetch`/XHR/`WebSocket` 补前缀）。

**`<base>` 与桥接脚本的相对基准必须一起改。** 桌面入口 url 不带尾斜杠，裸相对引用会被解析到上一级（`assets/x.png` → `/app/assets/x.png`），表现为「添加账号 → 反代」模型图标全不显示；反之只加 `<base>` 而脚本仍以文档 URL 为基准，会形成 `/app/agent2api/app/...` 双重前缀。`devcheck` 打印带尾斜杠的地址，复现不了此坑。

上游面板无客户端路由，所以不需要在压缩产物里打正则锚点。三处新适配面：i18n 语言包（依赖 `<base>`）、手机端 `/m`（3.2.0 起面板已不再写根绝对路径，`build.py` 的白名单仍保留 `/m` 以防上游回退）、PWA —— manifest 由 `RewriteManifest()` 字段级改写（只改以单个 `/` 开头的路径，相对路径不动）；SW 的 `scope:"/"` 由 `patch_sw_scope()` 改成 `"./"` 并有守卫。⚠️ 加 `Service-Worker-Allowed` 头**无效**，绕不过「scope 必须在脚本目录之下」。

面板形态被钉死：`UPSTREAM_UI_SHA256`（v3.3.0-beta.4 = 218 文件，`bf07ea4a…`，口径为**打完补丁后**的树）一变构建期就失败，逼人工确认；校验对象是解包目录，amd64 / arm64 各校一次。

## 2. 构建

### 2.1 前置条件与命令

不需要 Rust / WSL / Docker —— 上游自己发官方 Linux 服务端包，CI 不再交叉编译。只需 Go ≥ 1.27（纯标准库）、Python ≥ 3.8、`fnpack`（自动下载）。glibc：上游要 ≥ 2.34，飞牛 2.36，满足。

```powershell
pwsh -File fngateway\build.ps1 -Version 3.3.0-beta.4-1   # 出 fngateway 双架构
python build.py --fetch-binaries                 # 落盘官方包（面板不入库，必须先跑）
python build.py --check                          # 全量预检（不联网、不打包）
python build.py [--arch arm64] [--version X]     # 出 fpk
python build.py --sync-upstream [--materialize-ui]  # 解包+核对溯源；需要人工 diff 才写 app/ui
python tools\bump_upstream.py --detect|--apply   # 改齐四处 pin + manifest
cd fngateway; go vet ./...; go test ./...        # 37 个测试
```

辅助：`inspect_fpk.py`、`check_workflow.py`、`check_bump_reload.py`、`check_text.py`、`make_icons.py`。

### 2.2 面板注入

`app/ui/` 只留 `config` 与 `images/`，213 个面板文件不进仓库；`stage_ui()` 组装时从 `.local-build/upstream-bundle-<arch>/ui/` 拷入（先跑 `patch_sw_scope`）。好处是仓库/stage/fpk 三份副本不再漂移，代价是**上游改面板不再出现在 `git diff` 里**，只靠树哈希探针 + 解包目录 `diff -rq` 发现。缺失官方包时 `build.py` fail-fast，不会产出错包，但也就打不出包。

### 2.3 CI

`check-upstream.yml` 每天 03:23 UTC 查新版 → 改 pin+manifest → 打 tag → `gh workflow run` **派发**构建；`build-and-release.yml` 取包（校验 sha256）→ 编 fngateway → 出 fpk → 发 Release。用派发而非 push tag，是因为 `GITHUB_TOKEN` 推的 tag 不会触发其它 workflow，否则版本永远没有 Release。二进制 job 留在 `ubuntu-22.04`（glibc 2.35 < 2.36，24.04 的 2.39 会炸），并用 `readelf` 断言上游包 glibc 不超 2.36。fpk 不可逐字节复现（`fnpack` tar 元数据带 mtime/uid），本地与 CI 哈希不同不是 bug。

### 2.4 构建期守卫

「编译能过、真机才炸」的静默缺陷一律做成硬失败：文本卫生（CRLF/BOM）、manifest 取值里的 `;`、图标缺失、面板缺文件/根绝对路径/提供商图标缺失、面板树哈希漂移、上游版本不符、官方包与 bin sidecar 溯源不符、四处契约不一致、二进制架构与替身检测、打包后解包复核（ELF/可执行位/权限/manifest 逐值）、workflow 静态错误、`bump_upstream.py` 改完仍读到旧常量（CPython 只按 mtime 整秒 + 字节数判 `.pyc`）。

## 3. 安装与使用

下载 <https://github.com/sushazhi/fnos-agent2api/releases/latest>（`v3.3.0-beta.4-1`：amd64 13,659,132 B / arm64 12,955,546 B，校验值见同页 `SHA256SUMS.txt`），装进应用中心即可。`./cmd/main start|stop|status`。升级保留数据与 `apikey`（幂等）；卸载向导默认保留数据；⚠️ 从 v2.9 / v3.1.0 / 3.2.0-beta.2 升级的数据兼容未实测，先备份 `${TRIM_PKGVAR}`（含 `-wal`/`-shm`）。

控制台点图标即用，仅管理员可见、无需单独登录；面板的「检查更新」在飞牛上不可用，升级走应用中心。下游接入用独立端口（⚠️ 别指向 `/app/agent2api`，飞牛 1.2.0604+ 会拦截非 ticket 的 `Authorization` 头）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://192.168.0.2:3065/v1", api_key="<cat /var/apps/agent2api/var/apikey>")
```

数据与日志都在 `/var/apps/agent2api/var/`：`data/agent2api.db`（+ `-wal`/`-shm`，SQLite）、`apikey`、`home/`、`panel.log`/`agent2api.log`/`main.log`（8 MB 轮转）。

## 4. 安全设计

- `run-as=package` 以 `agent2api` 用户运行，不用 root；socket 建在 `${TRIM_APPDEST}` 并清理残留。
- 密钥只在服务端流转，浏览器侧只有占位掩码，全程无 `Set-Cookie`。
- 下游端口绑 `0.0.0.0:3065`（与上游默认一致，绑回环则局域网连不上）；内部回环端口由内核分配。
- 刻意不设 `AGENT2API_ADMIN_USER`/`ADMIN_PASSWORD`/`ALLOW_NO_KEY`/`PANEL_PORT`，避免面板登录或 fail-open；`AGENT2API_SELF_UPDATE` 未设即 fail-closed，面板内原地更新不可用。
- ⚠️ 上游以**明文**存 accessToken/refreshToken 于 SQLite（上游设计）。别在共享设备上用，别提交该文件。卸载脚本先确认 `DATA_DIR != "/"`。

## 5. 许可

本仓库自己的代码（`fngateway/`、`cmd/`、`config/`、`wizard/`、`manifest`、`build.py`、图标）为 **MIT**；随包内置的上游二进制与面板是上游作品，**自 v3.3.0-beta.2 起为 AGPL-3.0**（此前是 MIT + 使用声明）。所以：本仓库以 MIT 发布，而**分发物（.fpk）整体受 AGPL-3.0 约束**。

- 包内已附 AGPL 全文（`LICENSE.upstream`）与 `UPSTREAM.txt`（写明 Corresponding Source 的获取位置，以及本包对上游**唯一**的产物层改动：`ui/m/mobile.js` 里 SW 注册 scope 由 `/` 改 `./`，1 字节）。
- ⚠️ **公开发布 fpk 属于 AGPL 意义上的「分发」**：使用者应能按 §13 取得对应源码，源码地址已随包给出（上游仓库 + 该 tag 的 tarball）。上游面板不可改动（改了就破坏树哈希探针），所以**面板界面内没有额外的源码入口**；如需严格满足 §13 的「面向网络使用者」，可在 fngateway 侧补一行说明（本轮未做，见 §6）。

## 6. 验证状态

**v3.3.0-beta.4-1 发布产物（CI，`build-and-release.yml` run #12，2026-10-26，权威指纹）**：`agent2api-3.3.0-beta.4-1-amd64.fpk` 13,659,132 B（sha256 `a5d3a17390988896f147ee34aa6bc814b8ea47790d341935133741d6874c23e4`）/ `arm64.fpk` 12,955,546 B（`0632f469f8542c095d22598a71b9e4104c4bdd839f62ee6c842c3606cb2a3591`），同页附 `SHA256SUMS.txt`。这是 2026-10-20 那次 refactor 之后**第一次跑通的 CI 构建**。

**同版本本地复现（Windows 构建机，无真机）**：产物 amd64 13,658,746 B（`349f5158…`）—— 与 CI 那份相差 386 B，符合 §2.3 记录的「fnpack 的 tar 元数据与 gzip 层每次不同，fpk 不可逐字节复现」。内层 `app.tgz` 225 个文件，其中 `app/ui/` 221（面板 218 + `config` + 2 图标）、i18n 域文件 101（`manifest.js` + 5 语 × 20 域）；官方包 amd64 10,414,609 B（`675210b9…`）/ arm64 10,038,256 B（`1430c208…`），**本地实算 = 上游 `.sha256` 资产 = GitHub API digest 三方一致**；包内二进制 amd64 15,478,576 B / arm64 14,049,112 B（beta.3 为 13,140,208 / 11,951,592），ELF 架构正确（`0x3E` / `0xB7`），最高 GLIBC 2.34，内嵌版本串 `3.3.0-beta.4`；面板 218 文件（相对 beta.3 **新增 7 / 删除 2 / 修改 109**，补丁后树哈希 `bf07ea4a3dbe472ffa1e786f9cea0aab8930e50174b46dfcd04f50e557d9ec70`，逐架构各校一次）。`build.py --check` 全绿，打包后**独立解包复核**通过（包内 manifest `version=3.3.0-beta.4-1`、`UPSTREAM.txt` 上游版本 `v3.3.0-beta.4`、`ui/m/mobile.js` 的 SW scope 为 `./`、两二进制 755 且内嵌版本串正确）。

⚠️ 本轮顺带修掉两处**只在干净检出/CI 上才暴露**的问题：① `.local-build/bin/` 里的网关二进制原是 `-Version 3.1.0-2`（Go 1.27.1）编的旧产物 —— 代码未变（比最后一个 `.go` 源文件新），但版本串陈旧，启动日志与 `--version` 会报 `3.1.0-2`；已按 CI 同口径重编 `-X main.version=3.3.0-beta.4-1`（Go 1.27.3，`CGO_ENABLED=0`），amd64 7,700,640 B / arm64 7,143,584 B。② `--fetch-binaries` 里的 `check_binaries()` 会硬失败于「缺少产物 fngateway-linux-*」，而网关是**本仓库自己编**的、干净检出时还不存在（CI 的 binaries job 正是「先 fetch、后 build fngateway」）—— 于是 2026-10-20 起 CI 每次都停在这一步，README §2.1 让干净克隆先跑 `--fetch-binaries` 的用法同样打不出来；已改为 `check_binaries(arches, need_gateway=False)`，打包路径 [6/6] 仍要求网关在位。

**网关改写面复核结论：无需改网关源码。** 与上一版同构 —— `fetch('/api/...')` 根绝对仍是 **5** 处（同一批登录页调用）、无裸相对 `api/…` / `./…` / 绝对 `http(s)://` `fetch`、面板内硬编码 `/app/agent2api` **0** 处、`navigationLiteralRe` 命中仍是唯一 **1** 处（`m/mobile.js` 的 `/login?next=%2Fm`）、`agent2api.*` 键 0 新增、图标 44/44、i18n 5 语 × **20** 域（新增的 `i18n/*/routing.js` 五语齐备）。变化仅在量上：`/api/` 字面量 174 → **222**（新页面带来更多调用，形态未变，仍由网关的 fetch 前缀桥接覆盖）；`index.html` 资源引用 51 → **50**，根绝对引用仍是 **0**。

⚠️ **已知问题**（上游 3.2.0 起、3.3.0-beta.4 仍未改）：面板登出写成三目 `window.location.href = e ? "login.html" : "/login"`，而 `navigationLiteralRe` 要求 `=` 后**紧跟**字面量，匹配不到 → 经由 `/app/agent2api` 登出会跳到站点根而非应用内登录页（不影响功能）。修法：产物层把该分支改成裸相对，或网关正则补三目分支 + 单测。

⚠️ **未验证**：真机安装与运行（socket、管理员闸门、面板渲染、`:3065/v1` 局域网可达、图标真机显示）；新面板交互与手机端 PWA 离线；v2.9 / v3.1.0 / 3.2.0-beta.2 → 当前版本的数据迁移；应用中心是否接受预发布版本号；自动升级链路首次真实运行；图标守卫与 `rustc` 指纹拦截的残余风险；**AGPL §13 的完整性**（包内已给源码地址，但面向网络使用者的界面内没有源码入口，见 §5）。

历史结论（v3.1.0 / v2.9 轮）的版本无关部分仍成立：守卫负例注入全部如期失败、`bump_upstream.py` 端到端只改 `version` + `changelog`、打包管线双架构跑通。原始数据见 `.local-build/scan330b4.py`、`build_beta4_amd64.log`、`verify_artifact.py`（本轮 beta.4），以及 `.local-build/recon330/recon.md`（beta.3 轮）、`.local-build/scout320/recon.md`、`.local-build/verify330/`。
