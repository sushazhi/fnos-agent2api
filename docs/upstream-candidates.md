# 上游候选调研：能否换一个更合适的上游？

调研日期：2026-09-30
调研范围：GitHub 公开仓库，目标是「把 WorkBuddy / CodeBuddy / Qoder / Trae 等 AI 客户端登录态转成本地 OpenAI 兼容 API 网关」，
且必须能作为飞牛 fnOS `.fpk` 的上游。

当前上游：`aimod-cc/agent2api`（v2.9.0，Rust 单二进制，无官方 Linux 产物，本仓库 CI 自编译）
既有对比基线：`caigee-cmd/cli2api`（Go 控制面 + Node worker）

> 说明：本文所有结论均来自实际抓取的 README / releases.atom / CI 配置 / LICENSE 文件。
> 抓不到的一律标注「未获取到」，不臆测。`api.github.com` REST 在调研期被限流（403），
> 元数据（star/日期）主要来自一次成功的 REST 调用与 releases.atom。

---

## 0. 结论先行

**有更好的上游，但「更好」的方向不是换掉 agent2api 的 provider 覆盖，而是换掉它的「自编译 + 面板形态被钉死」这两个维护负担。**

推荐组合（首选）：

```
宿主：router-for-me/CLIProxyAPI          —— Go 单二进制，官方直发 linux amd64/arm64
插件：mmqz/cpa-multi-plugins             —— 官方直发 linux amd64/arm64 的 .so 插件
```

覆盖：CodeBuddy / WorkBuddy（CN+Intl+Global 三区合并）、Qoder（CN+Intl）、Trae（CN Code + CN SOLO + Intl 三变体）、
ZCode（智谱 GLM 编码套餐）、MiMo（小米）。**正好命中你点名的 workbuddy + qoder，并附带 trae。**

两者都是 **纯 MIT**（比当前上游「MIT + 禁止商业用途附加条款」更干净），
两者都**官方直发 Linux 双架构产物**（不用再自己编 Rust），
插件机制把「上游协议漂移」隔离在一个 `.so` 里（不用 fork 上游）。

**唯一的硬风险**：插件 ABI 绑定宿主主版本。cpa-multi-plugins 的 `go.mod` 锁 `CLIProxyAPI/v7 v7.2.30`，
而宿主已经发到 **v8.0.4**。上 fpk 前**必须实测锁定一组可用组合**（见 §5 验证清单）。

---

## 1. 现状回顾：当前上游的痛点

| 项 | 现状 |
|---|---|
| 上游 | `aimod-cc/agent2api` v2.9.0 |
| 形态 | Rust 单二进制（`agent2api-server`） |
| Linux 产物 | **上游不发**。Release 只有 Windows NSIS + macOS dmg + Docker 镜像 |
| 本仓库 CI | 拉上游 tag 源码 → `cargo build -p agent2api-server`（amd64 原生 + arm64 交叉），冷编译约 10 分钟 |
| 面板适配 | 上游 WebUI 被 `UPSTREAM_UI_SHA256` 树哈希钉死，上游改一个字节就构建期失败，必须人工回归 |
| 发布节奏 | 极快：v2.7.3(09-27) → v2.8.0(09-28) → v2.9.0(09-29)，两天两个 minor |
| 许可 | MIT + 「使用声明」第 3 条禁止商业用途与二次分发牟利（**非纯 MIT**） |
| provider 覆盖 | 已相当全：WorkBuddy / Raccoon / CatPaw / AutoClaw / Qoder / Cline / Accio / CodeArts / Trae / ZCode |

**结论：provider 覆盖其实已经不是短板了**（Trae、CodeArts、Accio、ZCode 在 v2.8.0/v2.9.0 都补上了）。
真正的问题是：① 每次上游发版都要重编 Rust 双架构；② 面板形态漂移探针逼人工回归；③ 许可不是纯 MIT。

---

## 2. 候选对比矩阵（按推荐度排序）

| 仓库 | 形态 | Linux 双架构产物 | provider 覆盖 | 许可 | 活跃度 | 作为 fpk 上游 |
|---|---|---|---|---|---|---|
| **router-for-me/CLIProxyAPI** | Go 单二进制 | ✅ 官方直发 `linux_amd64/aarch64.tar.gz`（另有 `_no-plugin` 变体） | 本体不做国内客户端；靠插件扩展 | MIT | 53.6k★，v8.0.4 (09-29) | ⭐⭐⭐⭐⭐ 宿主 |
| **mmqz/cpa-multi-plugins** | CPA 插件（`.so`） | ✅ 官方直发 `linux-amd64/arm64.zip` | WorkBuddy/CodeBuddy(三区) + Qoder(双区) + Trae(三变体) + ZCode + MiMo | MIT | v0.12.103 (09-30)，10 天 40+ 版 | ⭐⭐⭐⭐⭐ 插件 |
| zhangdailin/API-Console | Go 单二进制 | ⚠️ 仅 `linux-amd64`，**无 arm64** | WorkBuddy / Qoder / Cline / Grok | ❌ **无 LICENSE(404)** | 未获取到 | ⭐⭐ 阻断 |
| CangShui/workbuddy-gateway | Go 单二进制（零 CGO） | ✅ 官方直发 `linux-amd64/arm64` | 仅 CodeBuddy/WorkBuddy 国内+国际 | ❌ **无 LICENSE(404)** | 未获取到 | ⭐⭐ 阻断 |
| linguo2625469/workbuddy2api-panel | Go 单二进制 | ❌ 无 Release，需自编译（有 GHCR 镜像） | 仅 CodeBuddy/WorkBuddy | MIT | 中等，单维护者 | ⭐⭐ 覆盖不足 |
| ithtelab/workbuddy-manager | Python + Next.js | ❌ 无单二进制（tar.gz 仍需 Python） | 仅 CodeBuddy（上游 workbuddy2api） | MIT | 极活跃 v1.0.76 | ⭐⭐ 需 docker.sock |
| smart-open/TraeWorkAssistant | Rust/axum | ❌ 仅 GHCR `linux/amd64`，**无 arm64** | Trae / WorkBuddy / 豆包 | MIT | v3.6.x 活跃 | ⭐⭐⭐ 缺 arm64 |
| shuishuipingan/qoder2api-hub | 纯 Python 零依赖 | ❌ 无产物，需自编译 | 仅 Qoder 双区 | MIT | v1.1.1 单维护者 | ⭐⭐ 单 provider |
| maiphucgiang/codebuddy2api | Python + React | ❌ 无二进制（GHCR 双架构镜像） | WorkBuddy / CodeBuddy | MIT（但 README 自述禁止商用，与 MIT 冲突） | 极活跃 v1.3.1 | ⭐⭐ 需 Python/Docker |
| HUIdada1/AgentHub | Electron 桌面 | ❌ 仅 Windows exe | Trae / WorkBuddy / Zcode | MIT | v1.40.0 | ⭐ 桌面端 |
| 1416277987/proxy-hub | Node.js | ❌ 无 Release | CodeBuddy/Trae/Qoder(CN) | ❌ **无 LICENSE(404)** | 单日 6 commit，已停 | ⭐ 阻断 |
| techysy/CreditDaddy | Node + 桌面 | ❌ | Qoder/WorkBuddy/ZCode | MIT | v0.3.0 | ⭐ 定位不同 |
| caigee-cmd/cli2api | Go + Node worker | ❌ 服务端无二进制（Release 只有 updater） | Qoder/WorkBuddy/Trae/Codex | MIT | v0.6.13 活跃 | ⭐⭐ 自述真账号验收未完成 |
| BoosaIDonng/workbuddy2plus-plugin | CPA 插件（`.so`） | ✅ 但仅 workbuddy.so | 仅 CodeBuddy/WorkBuddy | MIT | v2.8.4 (09-25) | ⭐⭐⭐ 可用但窄 |
| aimod-cc/agent2api（现状） | Rust 单二进制 | ❌ 上游不发，CI 自编译 | 10 家（最全） | MIT+附加限制 | v2.9.0 极快 | ⭐⭐⭐⭐ 现状 |

---

## 3. 首选方案详解：CLIProxyAPI + cpa-multi-plugins

### 3.1 为什么是它

1. **产物形态最省事**。宿主与插件都由上游 CI 直发 Linux amd64/arm64，`build.py` 从「拉源码 + cargo 交叉编译 10 分钟」
   退化成「下载两个压缩包 + 解压」，CI 的 Rust 工具链、`aarch64-linux-gnu-gcc`、glibc 断言那一整套可以拆掉。
2. **插件机制 = 漂移隔离层**。上游改协议时，改的是插件仓库（10 天发 40+ 版，跟进极快），
   而不是让本仓库 fork 上游。这正是当前 agent2api 方案缺的东西——它靠「树哈希钉死 + 人工回归」硬扛。
3. **provider 命中需求**。WorkBuddy/CodeBuddy 三区合并、Qoder 双区、Trae 三变体、ZCode、MiMo。
4. **许可更干净**。两者都是纯 MIT，没有「禁止商业用途」附加条款。
5. **无运行时依赖**。不需要 Node / Python / Docker / 桌面客户端。glibc baseline 2.17（宿主默认构建），
   飞牛 fnOS 1.2.x = Debian 12 = glibc 2.36，**向下兼容，安全**（不像当前 CI 要刻意锁 ubuntu-22.04）。
6. **插件带签到 / 配额调度 / 多账号池**，功能上比单纯转发更完整（credit-aware 调度、4 档冷却状态机、每日签到）。

### 3.2 fpk 内形态

```
app/bin/cli-proxy-api            # 宿主（Go，官方 release 直取）
app/bin/plugins/linux/<arch>/    # workbuddy.so qoder.so trae.so zcode.so mimo.so
config/config.yaml               # plugins.enabled=true + 每个插件显式 enabled
data/auth/                       # 凭据 JSON（每账号一个，0600，原子写）
data/logs/
```

- 宿主监听 8317，插件由宿主 `dlopen` 加载，**不是一个独立进程**，`cmd/main` 只需管宿主一个 PID。
- `plugins.dir` 与 `auth-dir` 必须落在 `${TRIM_PKGVAR}`，否则升级丢凭据。
- 每个插件在 `config.yaml` 里默认 **disabled**，必须显式打开（`workbuddy: { enabled: true }` 等）。

### 3.3 需要注意的适配点

- **管理面板子路径**：宿主管理面在 `/v0/management`，插件面板在 `/v0/resource/plugins/<id>/panel`，
  与当前 `fngateway` 的 HTML 改写思路同构，可以复用（`<base>` + fetch 前缀 + `Location`/`Set-Cookie` 回填）。
- **登录回调**：Trae 的 OAuth 回调强制 `http://127.0.0.1:<port>/authorize`，跨机时插件提供粘贴框
  （`/v0/resource/plugins/trae/panel` 或 `oauth_submit?cb_url=`），fpk 里要引导用户走粘贴路径。
- **时区**：插件签到按自然日 0 点重置，隐含要求宿主时区为 CN 时区。
- **插件崩溃 = 宿主崩溃**（同进程），`cmd/main` 的守护逻辑要能拉起整个宿主。

---

## 4. 明确不建议的候选（及原因）

| 候选 | 阻断原因 |
|---|---|
| `CangShui/workbuddy-gateway` | **无 LICENSE**（`LICENSE`/`LICENSE.md` 均 404），默认保留全部权利，**无再分发授权**，打包分发法律基础缺失。技术形态其实很好（零 CGO 静态双架构二进制），可惜卡在许可。 |
| `1416277987/proxy-hub` | **无 LICENSE** + 全部 6 个 commit 挤在 2026-08-21 单日、此后停更 + 4 个适配器里 3 个依赖 **Windows 桌面端私有路径**（`%APPDATA%`），在 Linux NAS 上根本不成立。 |
| `zhangdailin/API-Console` | **无 LICENSE** + **Redis 硬依赖**（多一个常驻进程）+ 仅 amd64 + `credential.key` 丢失即全部凭据不可解密。 |
| `caigee-cmd/cli2api` | 服务端**无 Linux 二进制**（Release 只有 updater）；Qoder 每账号一个 Node 进程 + 独立 HOME（约 511 MB RSS/账号）；README 自述 Qoder 国内版/WorkBuddy/Trae「真账号验收仍未完成」。 |
| 各类 `workbuddy2api` 系（linguo2625469 / ithtelab / BoosaIDonng / Sliverkiss fork） | **单一 provider**（只做腾讯 CodeBuddy/WorkBuddy），不满足「workbuddy + qoder 等多个」；且原始上游 `Sliverkiss/workbuddy2api` **已被作者删库**，供应链脆弱。 |
| `smart-open/TraeWorkAssistant` | 只有 `linux/amd64` 镜像，**无 arm64**，飞牛部分机型直接不可用。 |

---

## 5. 上 fpk 前的验证清单（按顺序）

1. **ABI 组合实测**（最关键）：插件 `go.mod` 锁 `CLIProxyAPI/v7 v7.2.30`，宿主已到 v8.0.4。
   必须下载宿主 v7.2.x 与 v8.0.4 各一份，把 `linux-amd64` 插件 `.so` 分别放入
   `plugins/linux/amd64/`，看日志是否出现 `pluginhost: plugin registered plugin_id=...`。
   **锁定一组可用组合后再写进 `build.py` 常量。**
2. **glibc 断言**：宿主默认构建要求 GLIBC 2.17 baseline，验证产物最高 GLIBC 符号 ≤ 2.36（沿用现有 CI 断言）。
3. **`_no-plugin` 变体不可用**：必须用默认构建（CGO 版）才支持 `.so` 插件，别拿 `no-plugin` 省事。
4. **双架构一致性**：arm64 的 `.so` 与 arm64 宿主同组验证（插件的 amd64/arm64 是分开编译的）。
5. **登录链路**：Qoder 设备码 / WorkBuddy state 轮询 / Trae 回调或粘贴，各跑通一次真账号。
6. **数据目录持久化**：`auth-dir` + `config.yaml` + `plugins/` 三者都映射到 `${TRIM_PKGVAR}`，升级不重建。
7. **许可合规**：确认 `LICENSE.upstream` 需要随包附带**两个**上游（宿主 + 插件）的 MIT 全文。

---

## 6. 备选路径（风险最小）

如果不想动现有已跑通的 `fnos-agent2api`，还有两条低风险路线：

**A. 并行新增一个 fpk 应用**（推荐给「先试试」）
新建 `fnos-cliproxyapi` 仓库，与 `fnos-agent2api` 并存。两者端口（8317 vs 3065）、
数据目录、生命周期完全独立，互不干扰。跑一段时间对比稳定性与覆盖率，再决定是否替换。

**B. 只把「自编译」这一步优化掉**
保留 agent2api 作上游，但在 `build.py` 里增加「优先从 Docker 镜像提取 `agent2api-server`」的分支，
把 Rust 交叉编译降级为兜底路径。收益有限（面板漂移探针与许可问题仍在），不解决根本问题。

---

## 7. 参考：本轮抓取到的关键事实

- `CLIProxyAPI` v8.0.4 Release 说明原文：
  「`CLIProxyAPI_<version>_linux_<arch>.tar.gz` is the default Linux build. It supports dynamic library plugins and
  is built against a **GLIBC 2.17 baseline**.」
- `cpa-multi-plugins` v0.12.103 资产：`linux-amd64.zip` (17.5 MB) / `linux-arm64.zip` (15.9 MB)，
  安装方式为「把 `.so` 拷到 `plugins/linux/amd64/` 并在 `config.yaml` 里逐个 `enabled: true`」，
  且标注 *Installation (verified against real CLIProxyAPI host)*。
- `agent2api` Release 资产（v2.7.1 实测）：仅 `.exe` / `.dmg` / Source code，**无 Linux 二进制**。
- 本仓库 CI 用 `ubuntu-22.04` 编、`ubuntu-24.04` 打包，注释明确：飞牛 = glibc 2.36，
  用 24.04(glibc 2.39) 编出来的二进制在真机上会 `version 'GLIBC_2.38' not found`。
