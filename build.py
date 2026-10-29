#!/usr/bin/env python3
"""build.py — fnos-agent2api 打包脚本（跨平台）

上游 aimod-cc/Agent2API（Rust 单二进制 + 自托管面板，许可 AGPL-3.0）**原样使用**；
本仓库只提供飞牛适配层（fngateway / cmd / config / wizard / manifest / 图标 /
上游 WebUI 的注入），不改上游源码 —— 唯一的产物层改动是补丁 SW scope 那 1 字节。

产物: agent2api-<manifest 版本>-<arch>.fpk（每架构一个包，装哪台给哪个）

与 D:\\fnos 下成熟项目一致的工程约定:
  * 中间产物全部落 .local-build/（已 gitignore）: fnpack / 编译产物 / stage
  * fnpack 自动下载（官方 static2.fnnas.com，1.2.3）
  * Windows 上 fnpack 产出 0666 → 打包后重建 tar，重写可执行位
  * fnpack 只打包它 schema 认识的根文件 → 补回 UPSTREAM.txt
  * manifest 取值中的 ASCII 分号会被 fnpack 静默截断 → 构建期硬失败

与 fnos-cli2api 的差异（上游形态不同，校验项随之不同）:
  * 无 Node worker / 无 Qoder CLI bundle / 无 sharp 原生库 → 无随包 npm 依赖
  * **二进制与面板都取自上游官方 Release 资产**（agent2api-server-linux-<arch>
    .tar.gz，见 UPSTREAM_BUNDLE_SHA256）：v3.1.0 起上游自己发布 Linux 服务端包，
    本仓库不再需要 Rust 工具链、不再自编译。本机/CI 都不需要 WSL/Docker。
  * 上游 WebUI（desktop-tauri/ui，v3.3.0-beta.4 为 218 个文件）不再入库，构建时
    从官方包解包目录注入 app/ui/
  * 额外校验: 面板入口文件齐备、i18n 五语词典齐全、PWA 文件在、面板资源引用是
    裸相对路径（fngateway 的 HTML 改写依赖这一形态；一旦上游改成绝对路径，网关
    会静默失效）、上游二进制的 glibc 要求 ≤ 飞牛上限、防替身二进制

用法:
    python build.py                    # 打包（amd64 + arm64 各一个包）
    python build.py --arch arm64       # 只出 arm64 包
    python build.py --check            # 只跑全部预检，不打包（不联网）
    python build.py --fetch-binaries   # 只从上游官方包取二进制到 .local-build/bin/
    python build.py --sync-upstream    # 从上游官方包同步面板进 app/ui
    python build.py --sync-upstream-src  # 改从上游 tag 源码同步面板（核对用）
    python build.py --version 3.3.0-beta.4-1  # 覆盖版本号

上游内容怎么进来:
    * app/ui/（面板）**不在仓库里**（2026-10-20 起），只有本项目自己的
      `config` 与 `images/`；213 个面板文件由 stage_ui() 在组装时从官方包解包
      目录注入，`verify_webui_provenance()` 按钉住的树哈希逐架构核对。
      所以**干净克隆必须先 `--fetch-binaries`** 把官方包落到 .local-build/。
    * agent2api-server 的 Linux 二进制由 `--fetch-binaries` 从同一个官方包取，
      sha256 与 build.py 里钉住的值逐一核对。CI 走的就是这条路
      （见 .github/workflows/build-and-release.yml）。
    * 官方包里的 ui/ 与 tag 源码的 desktop-tauri/ui/ 实测逐字节一致；需要自己
      核一遍时用 `--sync-upstream-src`。
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(PROJECT_DIR, ".local-build")
STAGE_DIR = os.path.join(BUILD_DIR, "stage")
BIN_CACHE = os.path.join(BUILD_DIR, "bin")
SRC_DIR = os.path.join(BUILD_DIR, "upstream-src")

APP_NAME = "agent2api"

# --- 上游（版本与 manifest 的 x.y.z-N 对应）---
#
# 上游仓库迁过两次，**换仓库时只改这一个常量**（tools/bump_upstream.py 与两条
# workflow 都从这里读，不各自硬编码）：
#   2026-10-18  aimod-cc/agent2api   →  Agent2API/agent2api（当时旧地址 404）
#   2026-10-24  Agent2API/agent2api  →  aimod-cc/Agent2API（**迁回原仓库**）
#
# 最后一次迁移的实测依据（2026-10-24）：`Agent2API/agent2api` 与 `Agent2API`
# 账号本身都是 404（账号已消失，同一时刻 api.github.com 对 torvalds/linux 返回
# 200，故非本机网络问题）；而 `aimod-cc/Agent2API` 可达 —— 它是同一账号下
# 2026-09-11 创建的原仓库（仓库名大写为 `Agent2API`，与小写 `agent2api` 是同一个
# 仓库，GitHub 对仓库名大小写不敏感），1360 star，release 线一直延伸到今天。
#
# ⚠️ 这次迁回**不是**「只改个 URL」：新仓库的 tag 里**没有** v3.1.0 与
# v3.2.0-beta.2（那两个只存在于已消失的 Agent2API 账号下，是本仓库上一轮 pin 的
# 对象）。继续 pin 它们，`--fetch-binaries` 与 `verify_upstream_version` 都会
# 404，所以 pin 必须换成新仓库真实存在的 release。
#
# 2026-10-26：pin 跟到上游 **预发布** v3.3.0-beta.4（该仓库当前最新 release，
# 上一个 pin 是 v3.3.0-beta.3）。
# 自动流水线跟不跟预发布，取决于**pin 自己所在的频道**（tools/bump_upstream.py
# 的 upstream_versions(allow_prerelease=cur[3] == 0)）：pin 是正式版就只在正式版
# 里挑候选，pin 是预发布就让预发布也进候选集。所以站在这条预发布线上，上游发
# 每一版 beta 都能被定时任务看见；上游一旦发出 3.3.0 正式版，会自动升过去，
# 频道随之收回正式版。
# （2026-10-29 之前是写死「只跟正式版」：pin 挪到 beta 线后，正式版线还停在
#  v3.0.8，而 3.0.8 < 3.3.0-beta.4 恒成立，于是每天判定「已是最新」，
#  beta.5 根本进不了候选集 —— 就是「Actions 检查不到上游更新」的真因。）
UPSTREAM_REPO = "aimod-cc/Agent2API"
UPSTREAM_VERSION = "3.3.0-beta.4"
UPSTREAM_TAG = f"v{UPSTREAM_VERSION}"

# --- GitHub 加速源（实测选型，2026-10-18）--------------------------------
# 候选池当时测了 5 个：直连、v4.gh-proxy.org、gh-proxy.com、ghfast.top、
# gh.dpik.top。结论与踩过的坑都记在这里，免得下次有人「顺手加个镜像」。
#
# 1. **直连不要放**。本机实测：大资产连接 21s 直接失败，GitHub API 虽能通
#    （0.79s）但大文件直连质量极差（qbittorrent 那边实测掉到 0.14 MB/s）。
#    把它放在链首只会让每次构建先白等一次超时。
# 2. **v4.gh-proxy.org 与 gh-proxy.com 是同一套服务的两个门**，不是互相备份：
#    两者对同一资产返回的内容哈希完全相同，且回源标头
#    `x-served-by: cache-iad-kiad7000091-IAD, cache-den-kden1300065-DEN`
#    一字不差 —— 只域名与证书不同（前者 GTS、后者 Let's Encrypt）。
#    所以链里**只留一个**，留 v4：实测 3.15 MB/s（gh-proxy.com 3.34 MB/s，
#    差距在误差内），且 API 通道同样通（1.65s）。
# 3. **ghfast.top 不要放**。长连接会卡死：实测 2MB / 131s（0.02 MB/s），
#    50s 硬截止都没下完 3MB；API 通道也直接 HTTPError。
# 4. **gh.dpik.top 是唯一真正异构的第二来源**（独立 IP、独立 CDN），
#    实测 8.83MB / 4.4s（1.90 MB/s）、内容哈希一致，故留作兜底。
#    注意它**只在下载通道可用**：对 api.github.com 实测 3/3 超时，所以
#    不要把 API 也往它这里代理（见 tools/bump_upstream.py 的说明）。
GH_PROXY = "https://v4.gh-proxy.org/"
GH_PROXY_FALLBACK = "https://gh.dpik.top/"

# 上游源码 tarball 的下载地址（codeload 直连 + 加速源兜底）。
# 只取 tag，不取 master —— 上游发布节奏很快，master 随时可能漂。
UPSTREAM_URLS = (
    f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/refs/tags/{UPSTREAM_TAG}",
    GH_PROXY + f"https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.tar.gz",
    GH_PROXY_FALLBACK + f"https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.tar.gz",
)

# --- 上游官方 Linux 服务端包（二进制 + 面板的真实来源）---
#
# 为什么不再自编译：v3.1.0 起上游**自己发布 Linux 服务端包**（Release 资产
# agent2api-server-linux-<arch>.tar.gz + 同名 .sha256），包内是
#   根下 agent2api-server（headless 二进制）+ ui/（面板）+ install.sh /
#   agent2api.service / agent2api.env.example。
# 这正好是本项目需要的两个东西，于是「拉源码 → 装 Rust → 交叉编译」整条路
# 都可以删掉：CI 快得多，也不再依赖上游 Cargo 结构与工具链版本。
#
# sha256 在这里**钉死**（而不是只信 Release 的 .sha256 资产）：上游资产可以
# 被重新上传，钉住本地值才能让「上游悄悄换了二进制」变成构建期硬失败。
# 升级上游时由 tools/bump_upstream.py 连同版本号一起改写。
UPSTREAM_BUNDLE_ASSET = "agent2api-server-linux-{arch}.tar.gz"
UPSTREAM_BUNDLE_SHA256 = {
    "amd64": "675210b97cbaf1ae7504b509a20749bc925aaa0113009f2a270935448c344221",
    "arm64": "1430c20870601f9946e73c6eed9f4259a6c17638363251298a217337c640d12c",
}
# 包内二进制名（解包后即叫这个名，无需改名）与包内 UI 目录名。
UPSTREAM_BUNDLE_BIN = "agent2api-server"
UPSTREAM_BUNDLE_UI = "ui"

# 上游二进制在 .local-build/bin/ 里的溯源 sidecar 后缀（由 fetch_server_bundle()
# 在解包并校验官方包 sha256 之后写出，check_binaries() 复查）。见
# _upstream_bin_provenance() 的说明：没有它，「面板同步完直接打包」会把上一版
# 的二进制打进另一种架构的包，而所有守卫全绿。
UPSTREAM_BIN_SIDECAR_EXT = ".sha256"


def bundle_urls(arch):
    """官方服务端包的下载地址（GitHub 直连 + 两个加速源兜底）。

    加速源选型依据见文件头部 GH_PROXY 处的实测记录：v4 为主（与
    gh-proxy.com 同源，只留一个）、gh.dpik.top 是唯一异构兜底。
    直连保留在链首：它在**部分**网络下可用（实测有 1.18 MB/s 的轮次），
    且不依赖第三方；失败时由后续两个加速源接住。
    """
    asset = UPSTREAM_BUNDLE_ASSET.format(arch=arch)
    direct = f"https://github.com/{UPSTREAM_REPO}/releases/download/{UPSTREAM_TAG}/{asset}"
    return (
        direct,
        GH_PROXY + direct,
        GH_PROXY_FALLBACK + direct,
    )


# 上游 WebUI（desktop-tauri/ui/）的确定性树哈希 —— 218 个文件，排除本项目自己
# 加的 config / images/。算法见 tree_hash()。
#
# 这是一个**漂移探针**：取上游面板后按此哈希核对，本地也核对已提交的副本。
# 上游若改动面板（哪怕只改一个字节），这里立刻构建期失败，逼我们人工确认
# 「面板形态是否还是 fngateway 能改写的那一种」，而不是等到真机上白屏才发现。
UPSTREAM_UI_SHA256 = "bf07ea4a3dbe472ffa1e786f9cea0aab8930e50174b46dfcd04f50e557d9ec70"

# app/ui 下属于本项目、**不属于**上游 WebUI 的条目（同步时不能被上游覆盖）。
#
# 2026-10-20 起上游面板**不再提交进仓库**：`app/ui/` 里只留这两个本项目条目，
# 213 个上游面板文件改为构建时从官方包解包目录**注入**（见 stage_ui()）。
# 仓库因此只存“本项目自己的 UI 资产”，上游面板的真相唯一来源是官方包
# （sha256 钉在 UPSTREAM_BUNDLE_SHA256），避免出现「仓库副本 / stage / fpk」
# 三份互相漂移的状态。
LOCAL_UI_ENTRIES = ("config", "images")

# --- 与飞牛侧的契约（build.py 里集中一份，用 check_consistency() 与各文件比对）---
GATEWAY_PREFIX = "/app/agent2api"
SOCKET_NAME = "agent2api.sock"
# 下游端口取上游 agent2api 的默认端口（AGENT2API_PROXY_PORT 默认 3065），
# 客户端可照抄上游文档。
SERVICE_PORT = 3065
# 上游二进制名（fngateway 按 runtime.GOARCH 拼接）
UPSTREAM_BIN = "agent2api-server-linux-{arch}"
GATEWAY_BIN = "fngateway-linux-{arch}"

# --- 随包上游 WebUI ---
# 上游仓库 desktop-tauri/ui/ 下的全部文件（v3.3.0-beta.4：218 个）原样进 app/ui/。
# 必需文件缺失不会导致打包失败，只会让面板白屏或某个功能区失效 —— 属于
# 「装完才发现」的静默缺陷。
#
# v3.1.0 相对 v2.9.0 的面板变化（每一项都在这里钉一个必需文件）：
#   · i18n 五语（zh-CN / zh-Hant / en / ja / ko / pt-BR）：i18n.js + i18n/*.js
#     由 index.html 在 head 里**动态注入**，缺一个语言就只能在那种语言下白屏。
#   · 手机端 /m：m/mobile.js + m/mobile.css + m/manifest.webmanifest。
#   · PWA：manifest.webmanifest + sw.js（手机端的 Service Worker）。
#   · 新页面样式：css/page-agent.css / page-assistant.css / page-checkin.css /
#     page-gateway.css / mobile.css。
#
# v3.2.0-beta.2 核对结果（2026-10-20，实测）：上面 15 个上游条目**全部仍在**，
# 列表无需增删；面板从 208 涨到 213 个文件。注意 i18n 目录从「六语」变成
# 「五语」—— 新面板的 `zh-CN` 不再是 i18n/ 下的词典目录（简体中文改由基准文案
# 承担），所以这里**不能**把 zh-CN 加进 UI_REQUIRED，否则构建会误报缺文件。
#
# v3.3.0-beta.3 复核结果（2026-10-24，实测）：16 项里 15 项在（`ui/config` 是
# 本项目自己的条目，不在上游包里）；面板仍是 213 个文件，相对 3.2.0-beta.2
# **新增 0 / 删除 0 / 修改 59**（纯内容迭代：i18n 39、css 6、islands 2、m 2、
# 根 js 8、html 2），无新增顶层文件、无新增提供商图标（44 个全未变）。
#
# v3.3.0-beta.4 复核结果（2026-10-26，实测）：16 项里仍是 15 项在（`ui/config` 是
# 本项目自己的条目）；面板涨到 218 个文件（相对 beta.3 新增 7 / 删除 2 / 修改 109），
# 新增的是 css/page-routing{,-trace}.css 与 5 种语言的 i18n/*/routing.js，删掉的是
# titlebar.js、tooltip.js。每个语言的词典域因此从 19 涨到 20 —— 语言集仍是五语
# （无 zh-CN），而 i18n 校验是从目录列表动态推导 locale，所以**列表无需增删**。
UI_REQUIRED = (
    "ui/index.html",
    "ui/login.html",
    "ui/app.js",
    "ui/icons.js",
    "ui/i18n.js",
    "ui/i18n/manifest.js",
    "ui/i18n/pt-BR/common.js",
    "ui/islands/ui.js",
    "ui/css/tokens.css",
    "ui/css/layout.css",
    "ui/css/mobile.css",
    "ui/manifest.webmanifest",
    "ui/sw.js",
    "ui/m/mobile.js",
    "ui/m/mobile.css",
    "ui/config",
)

FNPACK_BASE = "https://static2.fnnas.com/fnpack/fnpack-1.2.3"
FNPACK_VER = "1.2.3"

# 上游 HTML 里资源引用的形态。fngateway 的 htmlAttrRe 依赖**裸相对路径**
# （src="islands/ui.js"，不带前导 / 或 ./）。上游若改成 /islands/ui.js，
# 改写逻辑仍然工作，但若改成绝对 http(s) 或 CDN 前缀，网关就静默失效了。
BARE_ASSET_RE = re.compile(r'(?i)\b(?:src|href)\s*=\s*"([^"]+)"')
ABSOLUTE_ASSET_RE = re.compile(r'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)')

# 面板里**允许存在**的根绝对路径引用（白名单，不是放行改写失效）。
#
# v3.1.0 的 index.html 里有一处 `<link rel="alternate" href="/m">` 指向手机端
# 入口 —— 上游是按「面板挂在站点根」写的。网关侧由 fngateway 的 Prefix.
# shouldRewrite() 负责给它补前缀（它改写的是**不带**前缀的裸相对/根绝对路径），
# 所以这里不能当成「上游改成绝对路径 → 网关静默失效」来报错，否则会误伤。
# v3.2.0-beta.2 实测仍然只有这一处根绝对路径（52 个资源引用里唯一一个），白名单不变。
#
# v3.3.0-beta.3 实测（2026-10-24）：那一处**没了** —— 手机端链接改成非根绝对写法，
# index.html 的资源引用 52 → 51 个、根绝对路径由 1 降到 **0**。这只会让本白名单
# 空转（_unexpected_root_abs 返回空集即通过），**不是**「改写失效」，故白名单保留：
# 上游若把 `/m` 写回去，这里仍按「已登记、由网关补前缀」处理。
#
# v3.3.0-beta.4 实测（2026-10-26）：根绝对引用仍是 **0**（index.html 资源引用
# 51 → 50），白名单继续空转，`_unexpected_root_abs()` 返回空集。
ROOT_ABS_ALLOWED = ("/m",)


def _unexpected_root_abs(refs):
    """从 HTML 资源引用里挑出「站内根绝对路径」中**未登记**的那些。

    返回 (全部根绝对路径, 未登记的那些)，两处调用方（check_webui 与
    verify_fpk）共用这一个判据，避免口径漂移。

    刻意**不**用 ABSOLUTE_ASSET_RE 做前置过滤：那个正则是
    `^(?:[a-zA-Z][a-zA-Z0-9+.\\-]*:|//)`，作用是排除「带 scheme 的绝对地址」
    与「协议相对 //host/x」，它**不匹配以 / 开头的站内路径** —— 拿它筛
    startswith("/") 会得到空集（这一点已经踩过一次：守卫因此恒不生效）。
    正确判据只有一条：以 `/` 开头、但**不是** `//host`（协议相对）。
    """
    root_abs = []
    for raw in refs:
        v = raw.strip()
        if not v.startswith("/") or v.startswith("//"):
            continue
        root_abs.append(v)
    unexpected = sorted({v for v in root_abs if v not in ROOT_ABS_ALLOWED})
    return root_abs, unexpected

# 上游手机端 Service Worker 的注册调用里、`scope` 的字面量。
#
# 只认「serviceWorker.register(...) 的实参对象里、scope 的值是根绝对路径 '/'」
# 这一种写法；引号双/单都吃。**不**跨 `}` 匹配是刻意的：实参对象里若出现嵌套
# 对象（`{scope:'/',foo:{...}}`），`[^}]*?` 会在内层 `}` 处断开而漏改，
# 所以改成「按 url 字面量定位，再在实参文本里找 scope」（见 patch_sw_scope）。
SW_REGISTER_RE = re.compile(
    r"""serviceWorker\s*\.\s*register\s*\(\s*(['"])sw\.js\1\s*,"""
)
SW_SCOPE_LITERAL_RE = re.compile(r"""scope\s*:\s*(['"])/\1""")

# --- 面板模型图标 ---
# 「添加账号」里的提供商图标全部是**裸相对路径**（预置列表走
# preset-providers.js 的 iconOf() → `assets/providers/<file>.png`；反代列表
# 走 islands/ui.js 里编译进去的 PROVIDER_ICONS），靠网关注入的
# <base href="{前缀}/"> 才落到位。图标文件缺失不会让安装失败，只会让图标变成
# 空白或首字母兜底 —— 又一个「装完才发现」的静默缺陷，所以这里显式核对。
UI_PROVIDER_DIR = "ui/assets/providers"
# 两个索引的写法不同，必须各配一条正则，否则会漏检一半（实测：只配下面第一条时
# 只能匹配到 islands/ui.js 的反代图标，preset-providers.js 的预置图标全部漏掉
# —— 因为后者写的是 `icon: 'openai.png'`，**没有**目录前缀）。
#   反代（islands/ui.js）      : "assets/providers/openai.png"（完整路径字面量）
#   预置（preset-providers.js）: icon: 'openai.png'（iconOf() 再拼目录）
ICON_REF_RE = re.compile(r'assets/providers/([A-Za-z0-9._\-]+\.png)')
ICON_DECL_RE = re.compile(r"""icon\s*:\s*['"]([A-Za-z0-9._\-]+\.png)['"]""")

# 二进制预检：上游官方包实测体积（amd64 13.75 MB / arm64 13.1 MB 量级）。
# 下限取 8 MB —— 真实 Rust 产物远高于它，而占位/替身二进制（Go 假程序之类）
# 只有 1~2 MB 且**能通过全部 ELF 与 glibc 检查**，必须靠体积 + rustc 标记拦。
UPSTREAM_BIN_MIN_BYTES = 8 * 1024 * 1024
# 飞牛 fnOS 1.2.x = Debian 12 (bookworm) = glibc 2.36。二进制引用的 GLIBC_2.x
# 符号版本不得高于它，否则真机上 "version `GLIBC_2.38' not found" 启动即炸，
# 而这在构建期完全不报错。
MAX_GLIBC = (2, 36)

# 文本卫生检查跳过的扩展名（二进制文件不该按文本规则检查）
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".fpk", ".gz", ".tgz",
               ".zip", ".exe", ".so", ".dylib", ".db", ".woff", ".woff2"}


def log(msg):
    """输出一行日志。

    Windows 控制台默认 GBK，直接 write 含 emoji 的字符串会抛 UnicodeEncodeError
    而中断构建（构建其实已经成功，只是打不出这一行）。替换掉不可编码字符。
    """
    try:
        sys.stdout.write(msg + "\n")
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stdout.flush()


def read_manifest_version():
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip()
    return ""


# fnpack 的 manifest 解析器把 ASCII 分号 `;` 当成取值终止符：值里第一个 `;`
# 之后的内容被静默丢弃（实测 fnpack 1.2.3，见 D:\\fnos 下项目的真实事故：
# changelog 里写了 `&lt;标识&gt;`，2672 字符只剩 1092 字符进包）。
# 危害是完全静默，因此这里做成构建期硬失败。
MANIFEST_VALUE_TERMINATORS = (";",)


def manifest_values(text):
    out = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def check_manifest_values(text):
    problems = []
    for key, val in manifest_values(text).items():
        for term in MANIFEST_VALUE_TERMINATORS:
            i = val.find(term)
            if i >= 0:
                problems.append(
                    f"{key}: 取值里第 {i} 个字符是 {term!r}，fnpack 会在此处截断 —— "
                    f"包内只会保留前 {i} 个字符（当前共 {len(val)} 个）。"
                    f"请改写该值（例如去掉 HTML 实体 &lt; &gt; &amp;）"
                )
    return problems


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------

# 图标随仓库提交，构建期不下载也不重采样（设计资产、许可清晰、不依赖外部图源），
# 只校验齐备与尺寸 —— 缺失只会让桌面图标空白，属于「装完才发现」的静默缺陷。
ICON_TARGETS = (
    ("ICON.PNG", 64, 64),
    ("ICON_256.PNG", 256, 256),
    ("app/ui/images/icon_64.png", 64, 64),
    ("app/ui/images/icon_256.png", 256, 256),
)


def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(26)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    if head[12:16] != b"IHDR":
        raise ValueError("缺少 IHDR")
    return struct.unpack(">II", head[16:24])


def verify_icons():
    for rel, w, h in ICON_TARGETS:
        p = os.path.join(PROJECT_DIR, *rel.split("/"))
        if not os.path.exists(p):
            log(f"  ERROR: 缺少图标 {rel}（图标随仓库提交，请补齐后再构建）")
            sys.exit(1)
        try:
            gw, gh = _png_size(p)
        except Exception as e:
            log(f"  ERROR: 图标 {rel} 无法解析: {e}")
            sys.exit(1)
        if (gw, gh) != (w, h):
            log(f"  ERROR: 图标 {rel} 尺寸应为 {w}x{h}，实际 {gw}x{gh}")
            sys.exit(1)
        log(f"  {rel}: {gw}x{gh}")


# ---------------------------------------------------------------------------
# 文本卫生（编码 / 换行）
# ---------------------------------------------------------------------------

# 必须在 Linux 上以 LF 执行、且带可执行位的目录
EXEC_DIRS = ("cmd",)
LF_DIRS = ("cmd", "wizard")
# 上游 WebUI 的逐字节副本：只查编码，不查末尾换行（见 tools/check_text.py 注释）
VENDORED_PREFIX = "app/ui/"


def check_text_hygiene():
    """拒绝会「本地正常、飞牛上炸」的文本问题。

    CRLF 会破坏 cmd/* 的 shebang；BOM 会让 fnpack 解析出的第一个键名带上
    \\ufeff 前缀而静默失配。两者在 Windows 编辑器里都看不出来。
    """
    problems = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(PROJECT_DIR):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", ".local-build", "__pycache__", "tools"}]
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, PROJECT_DIR).replace(os.sep, "/")
            if os.path.splitext(rel)[1].lower() in BINARY_EXTS:
                continue
            with open(full, "rb") as f:
                b = f.read()
            if b"\x00" in b[:8192]:
                continue
            checked += 1
            if b[:3] == b"\xef\xbb\xbf":
                problems.append(f"{rel}: 有 UTF-8 BOM")
            try:
                b.decode("utf-8")
            except UnicodeDecodeError as e:
                problems.append(f"{rel}: 不是合法 UTF-8 ({e})")
                continue
            if b.count(b"\r\n") and rel.split("/")[0] in LF_DIRS:
                problems.append(f"{rel}: 有 CRLF（{rel.split('/')[0]}/ 必须 LF）")
            if b and not b.endswith(b"\n") and not rel.startswith(VENDORED_PREFIX):
                problems.append(f"{rel}: 末尾缺换行")

    if problems:
        log(f"  ERROR: 文本卫生检查未通过（{len(problems)} 项）：")
        for p in problems[:20]:
            log(f"    - {p}")
        if len(problems) > 20:
            log(f"    ... 另有 {len(problems) - 20} 项")
        sys.exit(1)
    log(f"  编码/换行检查通过（{checked} 个文本文件）")


# ---------------------------------------------------------------------------
# 上游 WebUI 预检
# ---------------------------------------------------------------------------

def check_webui():
    """校验随包的上游 WebUI 齐备，且资源引用形态仍是我们适配的那一种。

    两条都是静默缺陷：缺文件 → 面板白屏；引用形态变了 → fngateway 的
    子路径改写失效（面板能打开但样式/脚本 404）。

    2026-10-20 起检查对象是**官方包解包目录里的 ui/**（_upstream_ui_src()），
    不再读仓库里的 app/ui —— 后者已只保留本项目自己的 config 与 images。
    """
    ui_root = _upstream_ui_src()
    problems = []
    for rel in UI_REQUIRED:
        # UI_REQUIRED 里只有 ui/config 是本项目本地条目，其余全来自上游面板
        if rel == "ui/config":
            p = os.path.join(PROJECT_DIR, "app", "ui", "config")
        else:
            p = os.path.join(ui_root, *rel.split("/")[1:])
        if not os.path.exists(p):
            problems.append(f"缺少 app/{rel}（上游 desktop-tauri/ui 未完整）")

    index = os.path.join(ui_root, "index.html")
    if os.path.exists(index):
        with open(index, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        refs = BARE_ASSET_RE.findall(html)
        if not refs:
            problems.append("app/ui/index.html 里找不到任何 src/href 资源引用"
                            "（上游结构变化，请复核 fngateway 的 HTML 改写）")
        # 外链（http/https/CDN）是允许的；但根绝对路径 /xxx 会让网关改写失效。
        # v3.1.0 有一处例外：<link rel="alternate" href="/m">（手机端入口），
        # 已登记进 ROOT_ABS_ALLOWED —— 它由网关补前缀，不算失效信号。
        #
        # ⚠️ 这里曾经写成「先过 ABSOLUTE_ASSET_RE 再筛 startswith('/')」，
        # 而 ABSOLUTE_ASSET_RE = ^(?:[a-zA-Z]...:|//) **不匹配以 / 开头的串**
        # （它排除的是协议与协议相对 //），于是 root_abs 恒为空集、
        # 这道守卫在任何输入下都不生效。现在与 verify_fpk() 共用
        # _unexpected_root_abs()，两边口径强制一致。
        root_abs, unexpected = _unexpected_root_abs(refs)
        if unexpected:
            problems.append(
                "app/ui/index.html 出现未登记的根绝对路径资源引用: "
                + ", ".join(unexpected[:5])
                + " —— fngateway 只改写相对路径与已登记项，请复核 gateway.go 的 htmlAttrRe"
            )
        log(f"  index.html: {len(refs)} 个资源引用"
            f"（根绝对路径 {len(root_abs)} 个，未登记 {len(unexpected)} 个）")

    # 面板自带的 PWA 文件：sw.js 与 manifest.webmanifest 由浏览器**按固定路径**
    # 请求（不在 index.html 的资源引用里），缺了不会白屏，只会让手机端
    # 「添加到主屏幕」与离线壳静默失效。
    for rel in ("ui/manifest.webmanifest", "ui/sw.js"):
        p = os.path.join(ui_root, *rel.split("/")[1:])
        if not os.path.exists(p):
            problems.append(f"缺少 app/{rel}（手机端 PWA 会静默失效）")

    # i18n 词典：index.html 在 head 里按语言动态注入 i18n/<locale>/<domain>.js。
    # 六个语言目录必须齐全，且每个语言都必须有 index.html 会注入的那些 domain ——
    # 少一个 domain 会在那个语言下抛 ReferenceError，表现是整页脚本停摆。
    i18n_dir = os.path.join(ui_root, "i18n")
    if not os.path.isdir(i18n_dir):
        problems.append("缺少 app/ui/i18n/（面板在国际化语言下会白屏）")
    else:
        locales = sorted(d for d in os.listdir(i18n_dir)
                         if os.path.isdir(os.path.join(i18n_dir, d)))
        if not locales:
            problems.append("app/ui/i18n/ 下没有任何语言目录")
        else:
            domains = sorted(
                os.path.splitext(n)[0]
                for n in os.listdir(os.path.join(i18n_dir, locales[0]))
                if n.endswith(".js")
            )
            for loc in locales:
                missing = [d for d in domains
                           if not os.path.exists(os.path.join(i18n_dir, loc, d + ".js"))]
                if missing:
                    problems.append(f"i18n/{loc}/ 缺少词典: {', '.join(missing[:5])}")
            log(f"  i18n: {len(locales)} 种语言（{', '.join(locales)}），"
                f"{len(domains)} 个词典域")

    # 模型图标：目录必须在，且两个索引里引用到的每个图标都真实存在。
    prov_dir = os.path.join(ui_root, "assets", "providers")
    if not os.path.isdir(prov_dir):
        problems.append("缺少 app/ui/assets/providers/（「添加账号」里的模型图标"
                        "会全部空白，反代与预置两处都受影响）")
    else:
        on_disk = {n for n in os.listdir(prov_dir) if n.lower().endswith(".png")}
        referenced = set()
        # 两个索引写法不同，两条正则都要跑（见 ICON_DECL_RE 上方注释）。
        for rel in ("ui/preset-providers.js", "ui/islands/ui.js"):
            p = os.path.join(ui_root, *rel.split("/")[1:])
            if not os.path.exists(p):
                problems.append(f"缺少 app/{rel}（模型图标索引未随包）")
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            referenced |= set(ICON_REF_RE.findall(text))
            referenced |= set(ICON_DECL_RE.findall(text))
        # 预置图标是 iconOf() 运行期拼出的，兜底再核对一遍「声明数」是否够。
        # 若上游改了 icon 的声明写法，两条正则可能同时失效，那时 referenced
        # 会异常变小 —— 这里断言它不能少于磁盘图标数的一半，避免守卫静默失效。
        if len(referenced) < len(on_disk) // 2:
            problems.append(
                f"模型图标索引只解析出 {len(referenced)} 个（磁盘有 {len(on_disk)} 个），"
                "疑似上游改了 icon 声明写法 —— 请复核 build.py 的 ICON_REF_RE / ICON_DECL_RE"
            )
        missing = sorted(referenced - on_disk)
        if missing:
            problems.append("模型图标缺失（「添加账号」里对应项会空白）: "
                            + ", ".join(missing[:8]))
        log(f"  模型图标: 磁盘 {len(on_disk)} 个，被引用 {len(referenced)} 个，"
            f"缺失 {len(missing)} 个")

    if problems:
        log("  ERROR: 上游 WebUI 预检未通过：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 上游源码与 WebUI 的同步 / 溯源
# ---------------------------------------------------------------------------

def tree_hash(root, exclude_prefixes=()):
    """确定性树哈希: sha256( 逐行 "sha256  相对路径\\n"，按路径排序 )。

    只对文件内容与相对路径做哈希 —— mtime、权限、目录顺序都不参与，
    所以上游重新打包一次（时间戳全变）不会误报漂移。
    """
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(rel == p or rel.startswith(p + "/") for p in exclude_prefixes):
                continue
            with open(full, "rb") as f:
                entries.append((rel, hashlib.sha256(f.read()).hexdigest()))
    entries.sort()
    lines = "".join(f"{h}  {rel}\n" for rel, h in entries)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest(), entries


def _sha256_file(path):
    """文件的 sha256（分块读，避免把 14 MB 二进制一次性塞进内存两次）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, out_file, desc, force=False):
    """带缓存的下载。失败返回 False（由调用方决定是否换镜像）。"""
    if not force and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
        log(f"  复用已下载的 {desc}")
        return True
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    tmp = out_file + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fnos-agent2api-build"})
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        log(f"  下载失败: {e}")
        log(f"    地址: {url}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        log("  下载为空")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, out_file)
    log(f"  已下载 {desc}  ({os.path.getsize(out_file):,} B)")
    return True


def fetch_server_bundle(arch, force=False):
    """取上游官方 Linux 服务端包（tag 固定），校验钉住的 sha256，解包并归位二进制。

    返回解包目录（包内布局：``agent2api-server`` + ``ui/`` + 安装脚本等）。
    二进制会被复制成 ``.local-build/bin/agent2api-server-linux-<arch>``，
    与 fngateway 的 ``startChild()`` / build.py ``bin_paths()`` 的约定一致。

    sha256 **不符即硬失败**，不换镜像重试：镜像给错包、上游重新上传了资产、
    或者我们自己的 pin 过期，都应当让人看见，而不是被一次「重试成功」掩盖。
    """
    if arch not in UPSTREAM_BUNDLE_SHA256:
        raise SystemExit(f"未知架构 {arch!r}（没有钉住的官方包 sha256）")
    want = UPSTREAM_BUNDLE_SHA256[arch]
    asset = UPSTREAM_BUNDLE_ASSET.format(arch=arch)
    tarball = os.path.join(BUILD_DIR, asset)
    desc = f"上游官方包 {asset}"
    os.makedirs(BUILD_DIR, exist_ok=True)

    urls = bundle_urls(arch)
    if not download(urls[0], tarball, desc, force=force):
        ok = False
        for alt in urls[1:]:
            log(f"  换镜像重试: {alt}")
            if download(alt, tarball, desc, force=True):
                ok = True
                break
        if not ok:
            log("  ERROR: 无法获取上游官方服务端包。可手动下载后放到:")
            log(f"      {tarball}")
            log(f"      {urls[0]}")
            sys.exit(1)

    with open(tarball, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != want:
        log("  ERROR: 上游官方包的 sha256 与钉住的值不一致 —— 拒绝使用。")
        log(f"    包:   {tarball}")
        log(f"    期望: {want}")
        log(f"    实际: {got}")
        log("    上游重新上传过资产，或镜像给了别的包。请人工核对后更新")
        log("    build.py 的 UPSTREAM_BUNDLE_SHA256（tools/bump_upstream.py 会做这件事）。")
        # 删掉这个包，避免下次运行时被 download() 的缓存逻辑复用。
        try:
            os.remove(tarball)
        except OSError:
            pass
        sys.exit(1)
    log(f"  sha256 校验通过: {got}")

    out_dir = os.path.join(BUILD_DIR, f"upstream-bundle-{arch}")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    with tarfile.open(tarball, "r:gz") as t:
        # 包内是扁平布局（根下 agent2api-server / ui/ / install.sh ...），不剥层。
        # filter="data"：Python 3.12+ 的参数，3.14 起成为默认值；显式传来避免
        # 「将来默认行为变化 + DeprecationWarning」。上游包内没有绝对路径/设备文件。
        try:
            t.extractall(out_dir, filter="data")
        except TypeError:
            t.extractall(out_dir)

    src_bin = os.path.join(out_dir, UPSTREAM_BUNDLE_BIN)
    if not os.path.isfile(src_bin):
        log(f"  ERROR: 官方包里找不到二进制 {UPSTREAM_BUNDLE_BIN}: {out_dir}")
        sys.exit(1)
    if not os.path.isdir(os.path.join(out_dir, UPSTREAM_BUNDLE_UI)):
        log(f"  ERROR: 官方包里找不到面板目录 {UPSTREAM_BUNDLE_UI}/: {out_dir}")
        sys.exit(1)

    os.makedirs(BIN_CACHE, exist_ok=True)
    dest = os.path.join(BIN_CACHE, UPSTREAM_BIN.format(arch=arch))
    shutil.copyfile(src_bin, dest)
    os.chmod(dest, 0o755)
    # 溯源 sidecar：把「这个二进制来自刚校验过 sha256 的官方包」这件事钉在文件旁边。
    #
    # 为什么需要它（2026-10-20 实测踩到的静默缺陷）：`--sync-upstream` 只取 amd64
    # 一个包，`verify_fpk()` 又**不比对**上游二进制的来源，于是「同步完面板直接
    # python build.py」会让 arm64 包装进上一版的二进制，而所有守卫全绿。sidecar
    # 由**已验证的包**生成（自维护，不需要人工 pin 新哈希），check_binaries() 拿它
    # 复查 .local-build/bin 里的产物；CI 的 binaries job 上传 .local-build/bin/*，
    # 所以 sidecar 会随 artifact 进到 package job。
    bin_sha = _sha256_file(dest)
    with open(dest + UPSTREAM_BIN_SIDECAR_EXT, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{bin_sha}  {os.path.basename(dest)}\n")
    log(f"  二进制就位: {dest}  ({os.path.getsize(dest):,} B, sha256 {bin_sha[:16]}…)")
    log(f"  溯源 sidecar: {os.path.basename(dest)}{UPSTREAM_BIN_SIDECAR_EXT}")
    log(f"  包解包到: {out_dir}")
    return out_dir


def fetch_upstream(force=False):
    """取上游源码 tarball（tag 固定），解到 .local-build/upstream-src/。

    v3.1.0 起二进制与面板都来自官方服务端包（fetch_server_bundle），源码
    只在需要核对「面板与源码同 tag」时才拉。这里保留是为了：
      · verify_upstream_version() 用 Cargo.toml 断言 tag 没被移动；
      · 上游包结构将来有变时，还有一份源码可查。
    """
    os.makedirs(BUILD_DIR, exist_ok=True)
    tarball = os.path.join(BUILD_DIR, f"agent2api-{UPSTREAM_TAG}.tar.gz")

    if not download(UPSTREAM_URLS[0], tarball, f"上游 {UPSTREAM_TAG} 源码", force=force):
        ok = False
        for alt in UPSTREAM_URLS[1:]:
            log(f"  换镜像重试: {alt}")
            if download(alt, tarball, f"上游 {UPSTREAM_TAG} 源码", force=True):
                ok = True
                break
        if not ok:
            log("  ERROR: 无法获取上游源码。可手动下载后放到:")
            log(f"      {tarball}")
            log(f"      {UPSTREAM_URLS[0]}")
            sys.exit(1)

    if os.path.isdir(SRC_DIR):
        shutil.rmtree(SRC_DIR)
    with tarfile.open(tarball, "r:gz") as t:
        # tarball 顶层是 agent2api-<version>/，剥掉一层
        names = t.getnames()
        tops = {n.split("/")[0] for n in names}
        if len(tops) != 1:
            log(f"  ERROR: 上游 tarball 顶层目录不唯一: {sorted(tops)}")
            sys.exit(1)
        strip = tops.pop() + "/"
        members = [m for m in t.getmembers() if m.name.startswith(strip)]
        for m in members:
            m.name = m.name[len(strip):]
        members = [m for m in members if m.name]
        # filter="data" 是 Python 3.12+ 的参数，且 3.14 起成为默认值。
        # 显式传它可避免「将来默认行为变化 + DeprecationWarning」——
        # 上游 tarball 里没有绝对路径/设备文件，data 过滤不会丢东西。
        try:
            t.extractall(SRC_DIR, members=members, filter="data")
        except TypeError:
            t.extractall(SRC_DIR, members=members)

    # 审计线索：把 tarball 的哈希打进日志。上游 tarball 的字节会随打包时间变化
    # （gzip 时间戳），所以这里**不**做成硬失败，只留痕；真正的漂移探针是
    # 官方包的 UPSTREAM_BUNDLE_SHA256 与 verify_webui_provenance() 的 UI 树哈希。
    with open(tarball, "rb") as f:
        th = hashlib.sha256(f.read()).hexdigest()
    log(f"  上游源码就位: {SRC_DIR}")
    log(f"  tarball sha256: {th}")
    verify_upstream_version()

def verify_upstream_version():
    """核对拉到的上游源码确实是 manifest 声明的那个版本。

    防的是「tag 被移动 / tarball 来自别的 ref / 镜像给了缓存旧包」——
    这类问题不会让编译失败，只会静默产出一个版本号对不上的包。

    v3.1.0 起网关本体是独立 crate ``desktop-tauri/src-tauri/server``（与 Tauri
    桌面壳分开，headless 二进制由它产出），版本号在那里断言。
    """
    cargo = os.path.join(SRC_DIR, "desktop-tauri", "src-tauri", "server", "Cargo.toml")
    if not os.path.exists(cargo):
        log(f"  ERROR: 上游源码里找不到 {cargo}")
        log(f"    （上游源码结构可能已变；源码取自 {UPSTREAM_TAG}）")
        sys.exit(1)
    with open(cargo, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    got = m.group(1) if m else None
    if got != UPSTREAM_VERSION:
        log(f"  ERROR: 上游 agent2api-server 版本 {got!r} != 期望 {UPSTREAM_VERSION!r}")
        log(f"    （源码取自 {UPSTREAM_TAG}；tag 被移动或镜像给了旧包时会出现）")
        sys.exit(1)
    log(f"  上游版本核对通过: agent2api-server {got}")


def patch_sw_scope(dst):
    """把上游手机端 Service Worker 的注册 scope 从 `"/"` 改成 `"./"`。

    为什么必须改（v3.1.0 实测结论）：`m/mobile.js` 里写的是
    `navigator.serviceWorker.register("sw.js", { scope: "/" })`。脚本实际位于
    `{前缀}/sw.js`，而 SW 的 scope 上限是「脚本所在目录」——
    `"/"` 超出了 `{前缀}/`，浏览器直接抛
    `SecurityError: ... scope ('/') is not under the max scope allowed`，
    注册失败 → 手机端 PWA 的离线壳静默失效（不白屏，只降级）。

    实测过「给 sw.js 响应加 `Service-Worker-Allowed` 头」这条路**不通**：
    该头只能把上限放宽到头里给的值（`{前缀}/`），而 `/` 仍在其上，照样被拒。
    唯一真解就是让 scope 用相对值 `"./"`（解析成脚本目录，即 `{前缀}/`）。

    为什么改得动：upstream 的 `sw.js` 预缓存列表 SHELL 全部是 `./` 相对路径
    （相对 SW 自身 URL 解析），`/m`、`/m.html` 的处理也只针对手机端 —— 也就是说
    上游本来就是按「可藏在子路径下」写的，唯一的绊脚石就是这个写死的 `"/"`。

    为什么在这里改而不是改网关：这属于**产物层**的适配（与 `<base>` 注入同一类
    思路），网关只在运行时改响应正文，改不到 `register()` 的实参语义；而且在
    同步时就改好，网关无需为此加一条特例。

    只替换**注册调用里的那个字面量**，不做全局替换：`sw.js` 自己的注释与其它
    `"/"` 用法必须原样保留。找不到目标时只告警不失败 —— 上游改了写法时，应当
    在 check_webui() 里被显式检出，而不是让同步过程猜。

    v3.2.0-beta.2 实测：写法变成
    `serviceWorker.register("sw.js",{scope:"/",updateViaCache:"none"}`，即实参对象里
    多了一个 `updateViaCache`。这不影响本函数 —— SW_REGISTER_RE 只认到
    `register("sw.js",` 为止（靠那个逗号定位，不跨 `}` 匹配），SW_SCOPE_LITERAL_RE
    再在实参文本里找 `scope:"/"`，所以照样改得动（实测 n=1）。

    v3.3.0-beta.3 实测（2026-10-24）：注册写法与 3.2.0-beta.2 **完全同形**，
    仍然命中且只命中 1 处（`m/mobile.js`），补丁后 +1 字节。

    v3.3.0-beta.4 实测（2026-10-26）：写法仍未变，同样命中且只命中 1 处，
    补丁后 `scope:"./"`（`ui/m/mobile.js` 里 `serviceWorker.register` 仅 1 次）。
    """
    target = os.path.join(dst, "m", "mobile.js")
    if not os.path.isfile(target):
        log("  [警告] 缺少 ui/m/mobile.js，跳过 SW scope 修正")
        return
    with open(target, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = SW_SCOPE_RE.subn(r'\1"./"', text)
    if n == 0:
        if "./" in text and "serviceWorker.register" in text:
            log("  SW scope: 已是相对 scope，无需修正")
        else:
            log("  [警告] ui/m/mobile.js 里没找到 register(\"sw.js\",{scope:\"/\"}) 形态，"
                "未做 SW scope 修正；请复核该文件是否改了注册写法")
        return
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    log(f"  SW scope: 已把 {n} 处 scope:\"/\" 改为 scope:\"./\"（子路径下可注册）")


# SW 注册里的 scope 字面量：`serviceWorker.register("sw.js", { ... scope: "/" ... })`
# 只匹配**注册调用实参里的那一处**，且把前缀（`serviceWorker.register("sw.js",{`
# 直到 scope 键之前的内容）整段吃掉再回填，避免误伤 sw.js 注释或其它 "/" 用法。
# 捕获组 1 = 前缀（原样保留），替换成 `\1"./"`。
SW_SCOPE_RE = re.compile(
    r'(serviceWorker\.register\(\s*["\']sw\.js["\']\s*,\s*\{[^}]*?scope\s*:\s*)"/"'
)


def verify_sw_scope():
    """预检：确认**官方包面板**的 SW 注册用的是相对 scope（子路径部署的前提）。

    对象是 _upstream_ui_src() 打完补丁的那份（与进包形态一致），不是仓库 app/ui。
    """
    target = os.path.join(_upstream_ui_src(), "m", "mobile.js")
    if not os.path.isfile(target):
        return  # 缺文件由 check_webui() 报，这里不重复
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if SW_SCOPE_RE.search(text):
        log("  ERROR: ui/m/mobile.js 仍用 scope:\"/\" 注册 Service Worker ——")
        log("    经 /app/agent2api 子路径部署时浏览器会抛 SecurityError"
            "（scope 不在脚本目录之下），手机端离线壳静默失效。")
        log("    请跑 python build.py --sync-upstream 重新同步（会改写成 './'）。")
        sys.exit(1)
    log("  SW scope: 相对 scope，子路径下可注册")


def _sync_ui_tree(src, dst, origin):
    """把一份上游面板目录同步进 app/ui/（本项目自加的条目不动）。

    ⚠️ 2026-10-20 起这条路径**不再是默认构建路径**：上游面板已移出仓库，
    正常构建走 stage_ui() 直接注入。本函数保留给两种情况：
      1. 需要把面板落到工作区做人工 diff / 排查时（--materialize-ui）；
      2. 旧仓库（仍提交了 app/ui 上游面板）的兼容。
    注意它写的是**工作区**，被 .gitignore 的 app/ui 上游条目不会回到 git 里。
    """
    if not os.path.isdir(src):
        log(f"  ERROR: 找不到上游面板目录: {src}（来源: {origin}）")
        sys.exit(1)

    removed = []
    for name in os.listdir(dst):
        if name in LOCAL_UI_ENTRIES:
            continue
        p = os.path.join(dst, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        removed.append(name)

    copied = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            shutil.copy2(os.path.join(dirpath, fn), os.path.join(target_dir, fn))
            copied += 1
    log(f"  上游 WebUI 同步完成（来源: {origin}）: 移除 {len(removed)} 个旧条目, "
        f"写入 {copied} 个文件")

    patch_sw_scope(dst)
    return copied


def sync_webui(arches=("amd64", "arm64")):
    """把上游面板同步进 app/ui/（本项目自加的条目不动）。

    v3.1.0 起**面板与二进制同出自官方服务端包**：那个包里的 ui/ 与 tag 源码的
    desktop-tauri/ui/ 实测逐字节一致（v3.1.0 时 208 文件全同、v3.2.0-beta.2 时
    213 文件全同），所以用包里的更省一次
    十几 MB 的源码下载，也保证「装进包的面板」与「编进包的二进制」来自同一次
    上游发布。

    没有网络、或只想要源码那份时，用 sync_webui_from_source()。
    """
    dst = os.path.join(PROJECT_DIR, "app", "ui")
    total = 0
    for arch in (arches or ("amd64",)):
        out_dir = fetch_server_bundle(arch)
        src = os.path.join(out_dir, UPSTREAM_BUNDLE_UI)
        total = _sync_ui_tree(src, dst, f"官方包 {UPSTREAM_BUNDLE_ASSET.format(arch=arch)}")
        break  # 两个架构的包内含同一份 ui/，同步一次即可
    return total


def sync_webui_from_source():
    """从上游 tag **源码**同步面板（desktop-tauri/ui → app/ui）。"""
    src = os.path.join(SRC_DIR, "desktop-tauri", "ui")
    dst = os.path.join(PROJECT_DIR, "app", "ui")
    return _sync_ui_tree(src, dst, f"源码 {UPSTREAM_TAG}")


def verify_webui_provenance(arches=("amd64", "arm64")):
    """核对**上游面板**（官方包解包目录里的 ui/）的树哈希与钉住值一致。

    2026-10-20 起仓库不再存上游面板：`app/ui/` 只留本项目自己的 `config` 与
    `images/`，213 个面板文件由 assemble() 从官方包解包目录注入（见 stage_ui）。
    所以溯源对象从「仓库副本」变成「官方包里的 ui/ + 唯一的产物层改写
    patch_sw_scope」，这正是装进 .fpk 的那份面板的形状，钉住的
    UPSTREAM_UI_SHA256 直接对得上。

    **逐架构都要校验**（2026-10-20 修）：panel 是按架构各解一份包，而
    patch_sw_scope 只改 m/mobile.js 一处。早先这里只校验 amd64，导致 arm64 的
    解包目录长期停在「未打补丁」状态（树哈希 d388d79f…）却照样一路绿灯 ——
    出包时 assemble("arm64") 会临时补上，所以包是对的，但「校验」实际退化成了
    「抽查」，谁也看不出 arm64 那份曾经不是进包形态。逐架构各算一遍才是全检。

    哈希只能证明「已知条目没被改」，**证明不了「没有多出不该有的条目」** ——
    树哈希把实际文件列表算进去，混进外来文件会让哈希变化从而报错，但报错信息
    只会说「哈希不一致」，读的人容易误判成「上游面板变了」而去悄悄更新哈希。
    """
    for arch in arches:
        src = _upstream_ui_src(arch)
        got, entries = tree_hash(src, exclude_prefixes=())
        log(f"  上游面板 [{arch}]: {len(entries)} 个文件, 树哈希 {got[:16]}…（{src}）")
        if got != UPSTREAM_UI_SHA256:
            log(f"  ERROR: 上游面板 [{arch}] 树哈希与钉住的值不一致（面板形态可能已变）。")
            log(f"    期望: {UPSTREAM_UI_SHA256}")
            log(f"    实际: {got}")
            log("    若确认是上游改了面板：先核对 fngateway 的子路径改写是否仍适用")
            log("    （HTML 资源引用形态 / localStorage 键名 / fetch 调用形态），再更新")
            log("    build.py 里的 UPSTREAM_UI_SHA256。")
            sys.exit(1)
    log("  上游面板溯源校验通过（逐架构）")


def _upstream_ui_src(arch="amd64", force=False):
    """上游面板的**唯一真相来源**：官方包解包目录里的 ui/（已打 SW scope 补丁）。

    两个架构的包内含同一份 ui/（实测逐字节相同），但**各自解包一份**，所以这里
    按 arch 取、按 arch 打补丁 —— 否则 arm64 那份会长期停在未打补丁的原样，
    溯源校验从「全检」退化成「只查 amd64」的抽查。

    面板已移出仓库（2026-10-20），所以这条路径是构建的**硬前置**：解包目录与
    本地 tar.gz 都不在时，会尝试下载官方包（校验钉住的 sha256）。上游仓库当前
    是 404，换一台干净机器必须先把官方包落地（`--fetch-binaries`）才能构建 ——
    这属于 fail-fast：取不到就报错退出，绝不会开出一个缺面板的包。
    """
    src = os.path.join(BUILD_DIR, f"upstream-bundle-{arch}", UPSTREAM_BUNDLE_UI)
    if not os.path.isdir(src):
        log(f"  面板缓存缺失（{arch}），尝试从官方包解包 ...")
        # fetch_server_bundle() 在「下载不到官方包」时自己就 sys.exit(1)，
        # 所以必须先接住 SystemExit 再打印本函数的指引 —— 否则下面那段
        # 「请先跑 --fetch-binaries」在**最常见的失败路径**上永远不可达
        # （2026-10-20 实测：上游 404 时退出点是 fetch_server_bundle 内部，
        #   用户看不到任何关于「面板已移出仓库、必须先把包落地」的说明）。
        try:
            fetch_server_bundle(arch, force=force)
        except SystemExit as e:
            log("  ERROR: 面板缓存缺失，且无法从上游获取官方包。")
            log("    上游面板已移出仓库（2026-10-20 起），构建必须能拿到官方包：")
            log(f"      1) 把官方包 {UPSTREAM_BUNDLE_ASSET.format(arch=arch)} 放到 {BUILD_DIR}\\")
            log("      2) 或先跑 python build.py --fetch-binaries（需上游 Release 可达）")
            log("    本机之所以能出包，是因为 .local-build/ 里已有官方包缓存。")
            raise SystemExit(e.code if isinstance(e.code, int) and e.code else 1)
    if not os.path.isdir(src):
        log(f"  ERROR: 官方包解包目录里找不到面板: {src}")
        log(f"    面板已移出仓库，构建必须能拿到官方包。请先跑：")
        log(f"      python build.py --fetch-binaries")
        log(f"    并把官方包 agent2api-server-linux-{arch}.tar.gz 放到 "
            f"{BUILD_DIR}\\（或确认已有网络与可用的上游 Release）")
        sys.exit(1)
    # 解包目录是「刚解出来的上游原样」：patch_sw_scope 是幂等的（已是 ./ 就跳过），
    # 每次校验前重打一次，保证算的就是最终进包形态。
    patch_sw_scope(src)
    return src


def stage_ui(arch, dst_ui):
    """把「上游面板 + 本项目本地条目」组装到 stage 的 app/ui/。

    顺序有讲究：
      1. 先注入上游面板（来自官方包解包目录，已打 SW scope 补丁）；
      2. 再叠加 LOCAL_UI_ENTRIES（config / images）—— 它们盖在最上层，
         且不会被上游覆盖（这也保证了「仓库里只有这两样东西」这个事实成立）。
    """
    src = _upstream_ui_src(arch)
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst_ui if rel == "." else os.path.join(dst_ui, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            shutil.copy2(os.path.join(dirpath, fn), os.path.join(target_dir, fn))
            copied += 1
    log(f"    + 注入上游面板: {copied} 个文件（来源 {src}）")

    local_ui = os.path.join(PROJECT_DIR, "app", "ui")
    for name in LOCAL_UI_ENTRIES:
        src_local = os.path.join(local_ui, name)
        if not os.path.exists(src_local):
            log(f"  ERROR: 缺少本项目本地 UI 条目 app/ui/{name}")
            sys.exit(1)
        copy_tree(src_local, os.path.join(dst_ui, name))
    return copied


def _upstream_ui_top_names(arch=None):
    """上游面板树的顶层条目名（取自官方包解包目录）。

    只用于**报错时点名**与 `verify_fpk()` 的白名单核对，不参与溯源判定 ——
    判定始终是钉住的树哈希（verify_webui_provenance）。

    arch 给了就优先读该架构的解包目录；两架构的面板实测逐字节相同，所以
    缺该架构时回落到另一个架构 / 源码目录均可。
    """
    cands = []
    if arch:
        cands.append(os.path.join(BUILD_DIR, f"upstream-bundle-{arch}", UPSTREAM_BUNDLE_UI))
    cands += [
        os.path.join(BUILD_DIR, "upstream-bundle-amd64", UPSTREAM_BUNDLE_UI),
        os.path.join(BUILD_DIR, "upstream-bundle-arm64", UPSTREAM_BUNDLE_UI),
        os.path.join(SRC_DIR, "desktop-tauri", "ui"),
    ]
    for cand in cands:
        if os.path.isdir(cand):
            return set(os.listdir(cand))
    return set()


# ---------------------------------------------------------------------------
# 一致性校验（四处契约同一份值）
# ---------------------------------------------------------------------------

def check_consistency():
    """比对 fngateway / app/ui/config / cmd/main / manifest 四处契约。

    这些值任意一处写错都不会编译失败，只会表现为「桌面图标点不开」「socket
    找不到」「下游端口不通」，是这类网关应用最常见的低级事故。
    """
    problems = []
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        mf = manifest_values(f.read())
    with open(os.path.join(PROJECT_DIR, "cmd", "main"), "r", encoding="utf-8") as f:
        cmd_main = f.read()
    with open(os.path.join(PROJECT_DIR, "fngateway", "main.go"), "r", encoding="utf-8") as f:
        gw = f.read()
    with open(os.path.join(PROJECT_DIR, "app", "ui", "config"), "r", encoding="utf-8") as f:
        entry_cfg = json.load(f)
    with open(os.path.join(PROJECT_DIR, "config", "privilege"), "r", encoding="utf-8") as f:
        privilege = json.load(f)

    def gw_str(name):
        m = re.search(rf'{name}\s*=\s*"([^"]+)"', gw)
        return m.group(1) if m else None

    def gw_int(name):
        m = re.search(rf'{name}\s*=\s*(\d+)', gw)
        return int(m.group(1)) if m else None

    # 1) appname / 运行用户
    appname = mf.get("appname", "")
    if privilege.get("username") != appname or privilege.get("groupname") != appname:
        problems.append("config/privilege 的 username/groupname 必须等于 manifest.appname")
    m = re.search(r'^APP_NAME="([^"]+)"', cmd_main, re.M)
    if not m or m.group(1) != appname:
        problems.append("cmd/main 的 APP_NAME 与 manifest.appname 不一致")

    # 2) 网关前缀 / socket / 下游端口
    if gw_str("gatewayPrefix") != GATEWAY_PREFIX:
        problems.append(f"fngateway 的 gatewayPrefix 应为 {GATEWAY_PREFIX}")
    if gw_int("defaultPort") != SERVICE_PORT:
        problems.append(f"fngateway 的 defaultPort 应为 {SERVICE_PORT}")
    if mf.get("service_port") != str(SERVICE_PORT):
        problems.append(f"manifest.service_port 应为 {SERVICE_PORT}")
    if mf.get("checkport") != "true":
        problems.append("manifest.checkport 应为 true（端口冲突时应用中心能提示）")
    if not re.search(r'filepath\.Join\(appDest,\s*"%s"\)' % re.escape(SOCKET_NAME), gw):
        problems.append(f"fngateway 未监听 {SOCKET_NAME}")
    if '${TRIM_APPDEST}/${APP_NAME}.sock' not in cmd_main:
        problems.append("cmd/main 的 socket 路径必须写成 ${TRIM_APPDEST}/${APP_NAME}.sock")

    # 3) 桌面入口
    uidir = mf.get("desktop_uidir", "")
    if uidir != "ui":
        problems.append("manifest.desktop_uidir 应为 ui（与 stage/app/ui 对应）")
    launch = mf.get("desktop_applaunchname", "")
    urls = entry_cfg.get(".url") or {}
    if launch not in urls:
        problems.append(f"manifest.desktop_applaunchname={launch!r} 在 app/ui/config 里没有对应入口")
    for entry_id, entry in urls.items():
        if entry.get("gatewayPrefix") != GATEWAY_PREFIX:
            problems.append(f"入口 {entry_id} 的 gatewayPrefix 应为 {GATEWAY_PREFIX}")
        if entry.get("gatewaySocket") != SOCKET_NAME:
            problems.append(f"入口 {entry_id} 的 gatewaySocket 应为 {SOCKET_NAME}")
        if entry.get("url") != GATEWAY_PREFIX:
            problems.append(f"入口 {entry_id} 的 url 应为 {GATEWAY_PREFIX}")
        if entry.get("allUsers") is not False:
            problems.append(f"入口 {entry_id} 必须 allUsers=false（控制台只对管理员开放）")
        icon = entry.get("icon", "")
        if "{0}" not in icon:
            problems.append(f"入口 {entry_id} 的 icon 应含 {{0}} 占位（图标按尺寸选择）")

    # 4) 不应声明 Node 运行时依赖（上游为静态二进制，加了会白装一个 Node）
    dep_apps = [d.strip() for d in mf.get("install_dep_apps", "").split(",") if d.strip()]
    if dep_apps:
        problems.append(
            f"manifest.install_dep_apps 应为空（上游 agent2api 不依赖 Node 运行时），"
            f"当前声明了: {', '.join(dep_apps)}"
        )
    # cmd/* 里不应残留 nodejs 运行时探测
    for name in os.listdir(os.path.join(PROJECT_DIR, "cmd")):
        p = os.path.join(PROJECT_DIR, "cmd", name)
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if "nodejs_v" in f.read():
                problems.append(f"cmd/{name} 仍引用 nodejs 运行时（本应用不需要）")

    # 5) 端口读取来源（不要硬编码 SERVICE_PORT 到脚本里）
    if "TRIM_SERVICE_PORT" not in gw:
        problems.append("fngateway 必须优先读取 TRIM_SERVICE_PORT")

    # 6) 子进程二进制名必须与打包进 app/bin 的名字一致
    if "agent2api-server-linux-" not in gw:
        problems.append("fngateway 未按 agent2api-server-linux-<arch> 查找上游二进制")
    for a in ("amd64", "arm64"):
        if f"fngateway-linux-{a}" not in cmd_main:
            problems.append(f"cmd/main 未按架构选择 fngateway-linux-{a}")

    if problems:
        log("  ERROR: 契约不一致，已中止：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)
    log("  契约一致性通过（前缀/socket/端口/入口/运行用户/二进制名）")


# ---------------------------------------------------------------------------
# 二进制预检
# ---------------------------------------------------------------------------

ELF_MACHINE = {"amd64": 0x3E, "arm64": 0xB7}


def bin_paths(arch):
    return (
        os.path.join(BIN_CACHE, GATEWAY_BIN.format(arch=arch)),
        os.path.join(BIN_CACHE, UPSTREAM_BIN.format(arch=arch)),
    )


def _elf_check(path, arch):
    """返回 (ok, 说明)。校验是 ELF 且架构相符。"""
    with open(path, "rb") as f:
        head = f.read(20)
    if head[:4] != b"\x7fELF":
        return False, "不是 ELF 文件"
    em = struct.unpack("<H", head[18:20])[0]
    if em != ELF_MACHINE[arch]:
        got = {0x3E: "x86_64", 0xB7: "AArch64"}.get(em, hex(em))
        return False, f"架构不符（期望 {arch}，实际 {got}）"
    return True, "OK"


# 动态链接器要求的 GLIBC_2.x 符号版本：读 .dynstr 里的 "GLIBC_2.<n>" 字样。
# 这是 .gnu.version_r 判定所需的版本号的来源，所以「见过的最高的那个」就是
# 目标机 glibc 的下限。
GLIBC_SYM_RE = re.compile(rb"GLIBC_2\.(\d+)")


def max_required_glibc(path):
    """返回二进制引用到的最高 GLIBC 次版本号（如 2.34 → 34）；没有则 None。"""
    with open(path, "rb") as f:
        data = f.read()
    vers = {int(m.group(1)) for m in GLIBC_SYM_RE.finditer(data)}
    return max(vers) if vers else None


def _upstream_bin_check(path):
    """上游二进制的额外校验：防替身 + glibc 兼容。返回问题列表。"""
    problems = []
    size = os.path.getsize(path)
    name = os.path.basename(path)
    # 防「替身二进制」混进包：本地无 Rust 时曾用 Go 占位程序顶上，它 1.5 MB、
    # 静态无依赖，**能通过全部 ELF 与 glibc 检查**，装上真机才 exit status 1。
    # Rust 会把自己的编译器版本写进二进制，拿它当指纹 + 一条体积下限，双保险。
    with open(path, "rb") as f:
        head = f.read()
    if b"rustc" not in head:
        problems.append(f"{name}: 未见 rustc 标记，疑似占位/替身二进制")
    if size < UPSTREAM_BIN_MIN_BYTES:
        problems.append(
            f"{name}: 仅 {size:,} B，远小于上游官方包里的真实产物"
            f"（下限 {UPSTREAM_BIN_MIN_BYTES:,} B）"
        )
    # glibc：飞牛 1.2.x = Debian 12 = glibc 2.36。超了不会在构建期报错，
    # 只在真机启动时 "version `GLIBC_2.xx' not found"。
    want_major, want_minor = MAX_GLIBC
    got = max_required_glibc(path)
    if got is not None and got > want_minor:
        problems.append(
            f"{name}: 需要 GLIBC_{want_major}.{got}，高于飞牛上限 "
            f"{want_major}.{want_minor} —— 装上会启动即失败"
        )
    return problems, got


def _gateway_freshness(path):
    """网关二进制是否由当前 fngateway 源码编出。返回问题说明或 None。

    为什么必须查：``app/bin/fngateway`` 是**预编译缓存**（BIN_CACHE），
    build.py 只 ELF 预检、从不重编。源码改了而缓存没重编时，打出来的包
    仍然是旧网关 —— 构建全绿、装完却缺修复。实测踩过一次：v3.1.0 包里
    的网关是 Sep 29 的产物，不含 Oct 18 才写进 gateway.go 的 ``<base>``
    注入与 bridge.go 的 ``RESOLVE_BASE``，于是「添加账号 → 反代」模型图标
    仍按裸相对路径解析到 ``/app/assets/providers/*.png`` 而全部 404
    （源码、README、单测全都对，唯独装到真机上的二进制是旧的）。

    这跟上游二进制的「溯源哈希」不是一回事：那条保证「上游没被篡改」，
    这条保证「网关是自己新编的」。
    """
    name = os.path.basename(path)
    problems = []
    # 1) 字节级指纹：这两个字面量是图标修复的必要施工痕迹，缺了就没修。
    with open(path, "rb") as f:
        blob = f.read()
    for marker in (b"<base href=", b"RESOLVE_BASE"):
        if marker not in blob:
            problems.append(
                f"{name}: 缺少网关标记 {marker.decode()} —— 这个二进制不是由"
                f"当前 fngateway 源码编译的（旧缓存），装上去模型图标会 404。"
                f"请重编：cd fngateway && GOOS=linux GOARCH=<arch> go build "
                f"-o ../.local-build/bin/fngateway-linux-<arch> ."
            )
    # 2) 时间戳兜底：源码比产物新 ⇒ 大概率是「改了源码没重编」。
    bin_mtime = os.path.getmtime(path)
    src_root = os.path.join(PROJECT_DIR, "fngateway")
    newest, newest_f = 0.0, ""
    for root, _dirs, files in os.walk(src_root):
        for fn in files:
            if not fn.endswith(".go") or fn.endswith("_test.go"):
                continue
            fp = os.path.join(root, fn)
            m = os.path.getmtime(fp)
            if m > newest:
                newest, newest_f = m, fp
    if newest > bin_mtime:
        problems.append(
            f"{name}: 产物比源码旧（{time.strftime('%m-%d %H:%M', time.localtime(bin_mtime))}"
            f" < {time.strftime('%m-%d %H:%M', time.localtime(newest))}"
            f" {os.path.relpath(newest_f, PROJECT_DIR)}）—— 改了网关源码但没重编二进制"
        )
    return problems or None


def _upstream_bin_provenance(path):
    """核对 .local-build/bin 里的上游二进制确实是「钉住的那个官方包」里那一个。

    为什么必须查（2026-10-20 实测）：`--sync-upstream` 只解包 **amd64** 一个官方
    包（两个架构的 ui/ 相同，同步一次即可），而 `verify_fpk()` 不比对上游二进制
    的来源。于是「同步完面板 → python build.py」这条最常见的复用路径，会让
    **arm64 包装进上一版的上游二进制**，而 ELF/架构/rustc/体积/glibc 五项检查
    全部通过 —— 装到真机上表现为「另一套面板配旧后端」。

    判据是文件旁边的 sidecar（`fetch_server_bundle()` 在校验官方包 sha256 之后
    写出，见 UPSTREAM_BIN_SIDECAR_EXT）。它是**自维护**的：上游换包 → 包 sha256
    pin 变了 → 重新解包时 sidecar 跟着重写，不需要再人工钉一个额外哈希。

    sidecar 不存在时只警告不失败：正常流程里它一定在（本地跑过
    `--fetch-binaries`，CI 里由 binaries job 随 artifact 上传），但把「缺 sidecar」
    当成硬失败会让「只用别人给的两个二进制、不联网」的用法彻底不可用。
    """
    sidecar = path + UPSTREAM_BIN_SIDECAR_EXT
    name = os.path.basename(path)
    if not os.path.exists(sidecar):
        log(f"  [警告] {name}: 无溯源 sidecar（{os.path.basename(sidecar)}），"
            f"无法确认它来自钉住的官方包。请跑到过 --fetch-binaries 的机器上重取")
        return []
    with open(sidecar, "r", encoding="utf-8", errors="replace") as f:
        parts = f.read().split()
    want = parts[0].strip().lower() if parts else ""
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        return [f"{name}: sidecar {os.path.basename(sidecar)} 里不是 sha256: {want!r}"]
    got = _sha256_file(path)
    if got != want:
        return [
            f"{name}: sha256 与溯源 sidecar 不一致 —— 这个二进制不是钉住的官方包"
            f"（{UPSTREAM_TAG} 的 {UPSTREAM_BUNDLE_ASSET.format(arch='<arch>')}）里那一个。"
            f"期望 {want}，实际 {got}。"
            f"常见真因：同步过面板但没重新跑 --fetch-binaries，于是打进了上一版的二进制。"
        ]
    return []


def check_binaries(arches, need_gateway=True):
    """二进制预检。need_gateway=False 时只查上游二进制（见 --fetch-binaries 的用法）。

    为什么要这个开关：网关二进制是**本仓库自己编**的，干净检出 / CI 的 binaries
    job 里在取上游包的那一刻它还不存在（CI 就是「先 fetch、后 build fngateway」）。
    早先这里无条件要求两者都在位，于是 `--fetch-binaries` 在干净检出上必然失败，
    而且报错还让用户「先跑 --fetch-binaries」—— 2026-10-20 起 CI 每次构建都停在
    这一步（本地因为 .local-build/bin/ 留着上一次的网关，反而看不出问题）。
    """
    problems = []
    for arch in arches:
        paths = bin_paths(arch) if need_gateway else bin_paths(arch)[1:]
        for p in paths:
            name = os.path.basename(p)
            if not os.path.exists(p):
                hint = ("请先由 GitHub Actions 构建，或本地跑 "
                        "python build.py --fetch-binaries 从上游官方包取")
                problems.append(f"缺少产物 {name} —— {hint}")
                continue
            ok, why = _elf_check(p, arch)
            size = os.path.getsize(p)
            if not ok:
                problems.append(f"{name}: {why}")
                continue
            is_upstream = name.startswith(UPSTREAM_BIN.split("-")[0]) and "server" in name
            if is_upstream:
                extra, glibc = _upstream_bin_check(p)
                problems.extend(extra)
                problems.extend(_upstream_bin_provenance(p))
                glibc_txt = f"GLIBC≤2.{glibc}" if glibc is not None else "无 glibc 依赖"
                log(f"  {name}: {size:,} B  {arch}  ELF  {glibc_txt}")
            else:
                stale = _gateway_freshness(p)
                problems.extend(stale or [])
                log(f"  {name}: {size:,} B  {arch}  ELF"
                    + ("  源码标记齐全" if not stale else "  ← 与源码不符"))
    if problems:
        log("  ERROR: 二进制预检未通过：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def copy_tree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def assemble(arch, version):
    log(f"  组装应用包 ({arch}) ...")
    # 每次从零重建 stage，避免上一架构的残留混入。
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.makedirs(os.path.join(STAGE_DIR, "app"), exist_ok=True)

    for sub in ("cmd", "config", "wizard"):
        copy_tree(os.path.join(PROJECT_DIR, sub), os.path.join(STAGE_DIR, sub))

    # app/bin: fngateway（唯一被 cmd/main 管理的进程）+ 上游 agent2api-server（子进程）
    bin_dir = os.path.join(STAGE_DIR, "app", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    for src in bin_paths(arch):
        shutil.copy2(src, os.path.join(bin_dir, os.path.basename(src)))
        log(f"    + app/bin/{os.path.basename(src)}  ({os.path.getsize(src):,} B)")

    # app/ui: 上游 WebUI（构建时注入）+ 桌面入口 config + 图标
    #
    # 2026-10-20 起仓库不再提交上游面板（app/ui 只有 config 与 images），
    # 213 个面板文件由 stage_ui() 从官方包解包目录注入，然后才叠加本地条目。
    # 这样「装进包的面板」与「钉住哈希校验的面板」是同一个来源，仓库里也不会
    # 出现一份会与上游漂移的副本。
    dst_ui = os.path.join(STAGE_DIR, "app", "ui")
    os.makedirs(dst_ui, exist_ok=True)
    stage_ui(arch, dst_ui)
    ui_files = sum(len(fs) for _, _, fs in os.walk(dst_ui))
    log(f"    + app/ui/  ({ui_files} 个文件，上游 WebUI + 桌面入口)")

    for f in ("ICON.PNG", "ICON_256.PNG"):
        src = os.path.join(PROJECT_DIR, f)
        if not os.path.exists(src):
            log(f"  ERROR: 缺少图标 {f}（图标随仓库提交，请补齐后再构建）")
            sys.exit(1)
        shutil.copy2(src, os.path.join(STAGE_DIR, f))

    # manifest: 写回版本，并把 platform 落到**本架构**（每个 fpk 只装一个架构的
    # 二进制，声明成 all 会让另一种架构也能装上一个跑不起来的包）。
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        mf = f.read()
    mf = re.sub(r"(?m)^version\s*=.*", f"version = {version}", mf)
    mf = re.sub(r"(?m)^platform\s*=.*", f"platform = {'x86' if arch == 'amd64' else 'arm'}", mf)

    problems = check_manifest_values(mf)
    if problems:
        log("  ERROR: manifest 取值会被 fnpack 截断，已中止：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)
    with open(os.path.join(STAGE_DIR, "manifest"), "w", encoding="utf-8") as f:
        f.write(mf)

    # 上游署名与许可。**2026-10-24 起上游是 AGPL-3.0**（v3.3.0-beta.2 之前是
    # MIT + 使用声明），所以这里按 AGPL 的义务写：随包附许可全文（同目录
    # LICENSE.upstream）+ 指明 Corresponding Source 的获取位置。
    # 时间戳尊重 SOURCE_DATE_EPOCH（可复现构建惯例）：设了就固定，没设才用当前时间。
    # CI 里同一个 commit 的两次打包因此得到逐字节相同的 UPSTREAM.txt。
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if epoch.isdigit():
        build_time = datetime.datetime.fromtimestamp(
            int(epoch), datetime.timezone.utc
        ).isoformat(timespec="seconds")
    else:
        build_time = datetime.datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(STAGE_DIR, "UPSTREAM.txt"), "w", encoding="utf-8") as f:
        f.write(
            "本应用内置上游 agent2api（agent2api-server）。\n\n"
            f"上游项目: https://github.com/{UPSTREAM_REPO}\n"
            f"上游版本: {UPSTREAM_TAG}\n"
            "上游许可: GNU Affero General Public License v3.0 (AGPL-3.0)\n"
            "上游版权: Copyright (c) 2026 aimod-cc\n"
            f"二进制来源: 上游官方 Release 资产 {UPSTREAM_BUNDLE_ASSET.format(arch='<arch>')}\n"
            f"（sha256 已钉在 build.py 的 UPSTREAM_BUNDLE_SHA256，取用后校验）\n"
            f"构建时间: {build_time}\n\n"
            "上游许可全文见同目录 LICENSE.upstream。\n"
            "对应源码（Corresponding Source）可自上游仓库获取：\n"
            f"  · 仓库: https://github.com/{UPSTREAM_REPO}\n"
            f"  · 该版本: https://github.com/{UPSTREAM_REPO}/tree/{UPSTREAM_TAG}\n"
            f"  · 源码包: https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.tar.gz\n\n"
            "本包对上游的唯一改动：面板 ui/m/mobile.js 里 Service Worker 注册的\n"
            "scope 由 \"/\" 改为 \"./\"（1 字节），因为本应用挂在 /app/agent2api 子路径下，\n"
            "原值会让浏览器抛 SecurityError。改动脚本见本项目 build.py 的 patch_sw_scope()，\n"
            "据此可准确重建该文件；除此之外上游二进制与面板逐字节原样。\n\n"
            "飞牛适配层（fngateway 反向代理、生命周期脚本、桌面入口）是本项目独立新增的\n"
            "组件，以 MIT 发布，不含上游代码。\n\n"
            "AGPL-3.0 第 13 条：以网络方式与本程序交互的用户，可获得其对应源码 ——\n"
            "上述上游仓库地址即源码入口。\n"
        )
    lic = os.path.join(PROJECT_DIR, "LICENSE.upstream")
    if os.path.exists(lic):
        copy_tree(lic, os.path.join(STAGE_DIR, "LICENSE.upstream"))
    else:
        log("  [警告] 缺少 LICENSE.upstream，包内将不含上游许可全文；"
            "AGPL-3.0 的再分发义务未完成，请补齐后再发布")


# ---------------------------------------------------------------------------
# 打包与权限修正
# ---------------------------------------------------------------------------

def fnpack_platform_arch():
    """官方 fnpack 的平台文件名规则（Linux arm64 的文件名是 arm，不是 arm64）。

    2026-09 实测: fnpack-1.2.3-linux-arm 返回 200，-linux-arm64 返回 404。
    （官方文档正文写的是 arm64，以实际文件名为准。）
    """
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows", "amd64"
    if s.startswith("darwin"):
        m = platform.machine().lower()
        return "darwin", "arm64" if m in ("aarch64", "arm64") else "amd64"
    m = platform.machine().lower()
    return "linux", "arm" if m in ("aarch64", "arm64") else "amd64"


def get_fnpack():
    is_win = platform.system().lower().startswith("win")
    name = "fnpack.exe" if is_win else "fnpack"

    tools = os.path.join(BUILD_DIR, "tools")
    for cand in (os.path.join(tools, name), os.path.join(tools, "fnpack")):
        if os.path.exists(cand):
            return cand

    found = shutil.which("fnpack")
    if found:
        return found

    plat, arch = fnpack_platform_arch()
    url = f"{FNPACK_BASE}-{plat}-{arch}"
    os.makedirs(tools, exist_ok=True)
    dest = os.path.join(tools, name)
    log(f"  本地无 fnpack，自动下载 {plat}-{arch} ...")
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        log(f"  ERROR: 无法获取 fnpack: {e}")
        log("  可手动下载后放到 .local-build/tools/fnpack 或加入 PATH：")
        log(f"      {url}")
        sys.exit(1)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        log("  ERROR: fnpack 下载失败或为空")
        sys.exit(1)
    if not is_win:
        os.chmod(dest, 0o755)
    return dest


NEED_EXEC_OUTER = re.compile(r"^cmd/[^/]+$")
# app.tgz 内需要可执行位的文件（fnpack 在 Windows 上产出 0666）:
#   bin/fngateway-linux-*        网关（由 cmd/main 直接执行）
#   bin/agent2api-server-linux-* 上游服务（由 fngateway spawn）
INNER_EXEC_PATTERNS = (
    re.compile(r"^bin/[^/]+$"),
)


def _fix_tar(tar_bytes, outer_re, inner_patterns=None, extra=None):
    """重建 tar: 给匹配项设置 0755；对 app.tgz 递归处理；补齐 extra 文件。

    extra 为 [(归档内路径, 磁盘源文件)]: fnpack 只打包它 schema 认识的条目，
    根目录的署名/许可文件会被丢掉，必须在这里补回去。
    """
    src = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")
    out_buf = io.BytesIO()
    dst = tarfile.open(fileobj=out_buf, mode="w:", format=tarfile.USTAR_FORMAT)
    present = set()
    for m in src.getmembers():
        present.add(m.name.strip("/"))
        if m.isreg():
            data = src.extractfile(m).read()
            if m.name == "app.tgz" and inner_patterns is not None:
                inner = gzip.decompress(data)
                fixed = _fix_tar(inner, None, inner_patterns)
                data = gzip.compress(fixed, compresslevel=6)
            elif outer_re is not None and outer_re.match(m.name):
                m.mode = 0o755
            elif inner_patterns and any(p.search(m.name) for p in inner_patterns):
                m.mode = 0o755
            ti = tarfile.TarInfo(m.name)
            ti.mode = m.mode
            ti.size = len(data)
            ti.mtime = m.mtime
            ti.type = m.type
            dst.addfile(ti, io.BytesIO(data))
        else:
            dst.addfile(m)

    for arcname, srcpath in (extra or []):
        if arcname in present or not os.path.exists(srcpath):
            continue
        with open(srcpath, "rb") as f:
            data = f.read()
        ti = tarfile.TarInfo(arcname)
        ti.mode = 0o644
        ti.size = len(data)
        ti.mtime = int(os.path.getmtime(srcpath))
        ti.type = tarfile.REGTYPE
        dst.addfile(ti, io.BytesIO(data))
        present.add(arcname)

    dst.close()
    return out_buf.getvalue()


def fix_fpk_modes(fpk_path):
    """Windows 上 fnpack 产出 0666，需补可执行位，并补回被丢弃的署名/许可文件。"""
    with gzip.open(fpk_path, "rb") as f:
        raw = f.read()
    extra = [
        ("LICENSE.upstream", os.path.join(STAGE_DIR, "LICENSE.upstream")),
        ("UPSTREAM.txt", os.path.join(STAGE_DIR, "UPSTREAM.txt")),
    ]
    fixed = _fix_tar(raw, NEED_EXEC_OUTER, INNER_EXEC_PATTERNS, extra=extra)
    with gzip.open(fpk_path, "wb") as f:
        f.write(fixed)


def verify_fpk(fpk_path, arch):
    """打包后自检，返回 (ok, 消息列表)。"""
    msgs = []
    ok = True
    elf_machine = ELF_MACHINE[arch]

    with tarfile.open(fpk_path, "r:gz") as t:
        names = set(t.getnames())
        for need in ("manifest", "app.tgz", "cmd/main", "cmd/install_init",
                     "config/privilege", "config/resource", "wizard/install",
                     "ICON.PNG", "ICON_256.PNG", "UPSTREAM.txt"):
            if need not in names:
                msgs.append(f"缺失 {need}")
                ok = False

        bad = [m.name for m in t.getmembers()
               if m.isfile() and m.name.startswith("cmd/") and not (m.mode & 0o111)]
        if bad:
            msgs.append(f"cmd/* 不可执行: {', '.join(bad)}")
            ok = False

        inner = None
        inner_names = set()
        inner_files = {}
        if "app.tgz" in names:
            inner = tarfile.open(fileobj=io.BytesIO(t.extractfile("app.tgz").read()), mode="r:gz")
            inner_names = set(inner.getnames())
            inner_files = {m.name: m for m in inner.getmembers() if m.isfile()}

        if inner is None:
            msgs.append("app.tgz 无法解析")
            return False, msgs

        def read_inner(name):
            return inner.extractfile(inner_files[name]).read()

        # 二进制: 存在、ELF、架构相符、可执行
        for binname in (GATEWAY_BIN.format(arch=arch), UPSTREAM_BIN.format(arch=arch)):
            binname = f"bin/{binname}"
            if binname not in inner_files:
                msgs.append(f"缺失 {binname}")
                ok = False
                continue
            head = read_inner(binname)[:20]
            if head[:4] != b"\x7fELF":
                msgs.append(f"{binname} 不是 ELF")
                ok = False
                continue
            em = struct.unpack("<H", head[18:20])[0]
            if em != elf_machine:
                got = {0x3E: "x86_64", 0xB7: "AArch64"}.get(em, hex(em))
                msgs.append(f"{binname} 架构不符: 期望 {arch}, 实际 {got}")
                ok = False
            if not (inner_files[binname].mode & 0o111):
                msgs.append(f"{binname} 不可执行（应用起不来）")
                ok = False

        # 上游 WebUI: 入口文件必须在包内（缺了面板白屏）
        for rel in UI_REQUIRED:
            if rel not in inner_names:
                msgs.append(f"缺失 {rel}（上游 WebUI 未随包，面板会白屏）")
                ok = False

        # 模型图标目录：缺了不会安装失败，只会让「添加账号」里的图标空白。
        if not any(n.startswith(UI_PROVIDER_DIR + "/") for n in inner_names):
            msgs.append(f"app.tgz 内缺少 {UI_PROVIDER_DIR}/（模型图标会全部空白）")
            ok = False

        # 面板资源引用形态：fngateway 依赖裸相对路径
        if "ui/index.html" in inner_files:
            # 先查「包里有没有不该有的 ui 条目」：与 stage_ui() 同一判据
            # （上游面板顶层 + LOCAL_UI_ENTRIES 之外的东西一律不许进包）。
            # 2026-10-20 起这份白名单直接取自**官方包解包目录**（包内面板的
            # 真实来源），不再依赖仓库里那份副本。
            top = _upstream_ui_top_names(arch)
            if top:
                stray = sorted({n.split("/")[1] for n in inner_names
                                if n.startswith("ui/") and n.count("/") >= 1
                                and len(n.split("/")) > 1
                                and n.split("/")[1]
                                and n.split("/")[1] not in top
                                and n.split("/")[1] not in LOCAL_UI_ENTRIES})
                if stray:
                    msgs.append("app/ui 内混入不属于上游面板的条目（会随包发布）: "
                                + ", ".join(stray[:8]))
                    ok = False
            html = read_inner("ui/index.html").decode("utf-8", "replace")
            refs = BARE_ASSET_RE.findall(html)
            # 与 check_webui() 同一套判据（_unexpected_root_abs），不各写一份：
            # ROOT_ABS_ALLOWED 里登记的项（v3.1.0 的
            # <link rel="alternate" href="/m">）由网关补前缀，不算「改写失效」。
            _, root_abs = _unexpected_root_abs(refs)
            if root_abs:
                msgs.append("ui/index.html 出现未登记的根绝对路径资源引用，fngateway 改写会失效: "
                            + ", ".join(root_abs[:5]))
                ok = False
            # 子路径改写是否真的会被触发：至少要有相对引用
            if not any(not ABSOLUTE_ASSET_RE.match(r.strip()) for r in refs):
                msgs.append("ui/index.html 没有任何相对资源引用，fngateway 的 HTML 改写无用武之地")
                ok = False

        # 桌面入口图标（缺失不会安装失败，只会让桌面图标空白）
        if "ui/config" not in inner_names:
            msgs.append("缺失 ui/config（桌面入口未定义，飞牛桌面不会出现图标）")
            ok = False
        else:
            try:
                cfg = json.loads(read_inner("ui/config").decode("utf-8"))
            except Exception as e:
                msgs.append(f"ui/config 不是合法 JSON: {e}")
                cfg = None
            for entry_id, entry in ((cfg or {}).get(".url") or {}).items():
                icon = (entry or {}).get("icon", "")
                if "{0}" not in icon:
                    continue
                for size in (64, 256):
                    want = "ui/" + icon.replace("{0}", str(size))
                    if want not in inner_names:
                        msgs.append(f"桌面入口 {entry_id} 的图标缺失: {want}")
                        ok = False

        # 包内 manifest 必须与 stage 逐值一致（fnpack 会静默截断含 ';' 的取值）
        try:
            packed_vals = manifest_values(t.extractfile("manifest").read().decode("utf-8"))
            with open(os.path.join(STAGE_DIR, "manifest"), "r", encoding="utf-8") as sf:
                stage_vals = manifest_values(sf.read())
            for key, want in stage_vals.items():
                got = packed_vals.get(key)
                if got != want:
                    msgs.append(f"包内 manifest 的 {key} 与 stage 不一致"
                                f"（{len(want)} 字符 → "
                                f"{'缺失' if got is None else str(len(got)) + ' 字符'}）")
                    ok = False
        except Exception as e:
            msgs.append(f"manifest 取值比对失败: {e}")
            ok = False

    return ok, msgs


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="fnOS 应用统一打包脚本")
    ap.add_argument("--arch", "-a", default="all", choices=["all", "amd64", "arm64"],
                    help="目标架构（每个架构出一个 fpk）")
    ap.add_argument("--version", "-v", default="", help="版本号（默认读 manifest）")
    ap.add_argument("--check", action="store_true",
                    help="只跑全部预检，不打包（不联网）")
    ap.add_argument("--sync-upstream", action="store_true",
                    help="取上游官方包并解包到 .local-build/（面板与二进制都来自它）；"
                         "加 --materialize-ui 才把面板落到工作区 app/ui 供人工 diff")
    ap.add_argument("--materialize-ui", action="store_true",
                    help="与 --sync-upstream 同用：把上游面板落到工作区 app/ui/。"
                         "默认不落盘 —— 上游面板已移出仓库，只存在于 .local-build/ 里")
    ap.add_argument("--sync-upstream-src", action="store_true",
                    help="从上游 tag 源码同步 desktop-tauri/ui 进 app/ui（需联网，"
                         "用于核对官方包与源码面板一致）")
    ap.add_argument("--fetch-binaries", action="store_true",
                    help="只从上游官方服务端包取两个架构的 agent2api-server 到 "
                         ".local-build/bin/（需联网，不打包）；不要求网关二进制已在位，"
                         "干净检出可直接跑")
    ap.add_argument("--force-upstream", action="store_true",
                    help="与 --sync-upstream / --fetch-binaries 同用：忽略本地缓存重新下载")
    args = ap.parse_args()

    version = args.version.strip() or read_manifest_version()
    if not version:
        log("ERROR: 无法确定版本号")
        sys.exit(1)
    arches = ["amd64", "arm64"] if args.arch == "all" else [args.arch]

    log("=" * 46)
    log(f" {APP_NAME} 构建")
    log(f" 版本: {version}   架构: {', '.join(arches)}")
    log(f" 上游: {UPSTREAM_REPO} {UPSTREAM_TAG}")
    log(f" 平台: {platform.system()}")
    log("=" * 46)

    if args.fetch_binaries:
        # 维护/CI 操作：只取二进制，不碰 app/ui、不打包。
        log("[fetch] 从上游官方服务端包取二进制 ...")
        for a in arches:
            log(f"  -- {a} --")
            fetch_server_bundle(a, force=args.force_upstream)
        # 干净检出时网关还没编（CI 的 binaries job 就是「先 fetch、后 build
        # fngateway」），所以这一步只查刚取回的上游二进制；网关的检查留给打包前的
        # [6/6]，那里它必须已经在位。
        log("[fetch] 二进制预检（只查上游二进制；网关在 [6/6] 才查）...")
        check_binaries(arches, need_gateway=False)
        log("")
        log("二进制就位（.local-build/bin/），可以打包了")
        return

    if args.sync_upstream or args.sync_upstream_src:
        # 维护操作：同步面板 → 核对溯源 → 退出（此时通常还没有二进制）。
        if args.sync_upstream_src:
            log("[sync] 拉取上游源码 ...")
            fetch_upstream(force=args.force_upstream)
            log("[sync] 从源码同步上游 WebUI（落到工作区 app/ui，供人工 diff）...")
            sync_webui_from_source()
        else:
            log("[sync] 取上游官方包并解包（面板的唯一来源）...")
            for a in arches:
                fetch_server_bundle(a, force=args.force_upstream)
            if args.materialize_ui:
                log("[sync] 把面板落到工作区 app/ui（仅供人工 diff；已被 .gitignore）...")
                sync_webui(arches)
        log("[sync] 文本卫生（编码/换行）...")
        check_text_hygiene()
        log("[sync] 校验图标 + 上游 WebUI ...")
        verify_icons()
        ui_root = _upstream_ui_src()
        check_webui()
        verify_sw_scope()
        log("[sync] 上游 WebUI 溯源 ...")
        got, entries = tree_hash(ui_root, exclude_prefixes=())
        log(f"  上游面板树哈希: {got}  ({len(entries)} 个文件，来源 {ui_root})")
        if got == UPSTREAM_UI_SHA256:
            log("  与 build.py 钉住的值一致")
        else:
            log("  与 build.py 钉住的值不一致 —— 这是**新上游面板**，请人工确认")
            log("  fngateway 的子路径改写仍然适用（HTML 资源引用形态 / 注入点），")
            log("  再把 build.py 的 UPSTREAM_UI_SHA256 更新为上面的值。")
        log("")
        log("上游包已就位（面板在 .local-build/upstream-bundle-<arch>/ui/，"
            "构建时自动注入，无需提交进仓库）")
        return

    log("[1/6] 上游面板来自官方包解包目录（构建时注入；不读仓库 app/ui）")
    log("[2/6] 文本卫生（编码/换行）...")
    check_text_hygiene()
    log("[3/6] 校验图标 + 上游 WebUI ...")
    verify_icons()
    check_webui()
    verify_sw_scope()

    log("[4/6] 上游 WebUI 溯源 ...")
    verify_webui_provenance()

    log("[5/6] 契约一致性 ...")
    check_consistency()

    log("[6/6] 二进制预检 ...")
    check_binaries(arches)

    if args.check:
        log("")
        log("预检全部通过（--check 模式，未打包）")
        return

    fnpack = get_fnpack()

    for a in arches:
        log(f"[打包] 组装 + fnpack ({a}) ...")
        assemble(a, version)

        raw = os.path.join(STAGE_DIR, f"{APP_NAME}.fpk")
        if os.path.exists(raw):
            os.remove(raw)
        log(f"  fnpack build ({a}) ...")
        proc = subprocess.run([fnpack, "build", "-d", "."], cwd=STAGE_DIR, capture_output=True)
        out_text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        if not os.path.exists(raw):
            log(f"  ERROR: fnpack 失败\n{out_text[:1500]}")
            sys.exit(1)

        before = None
        with tarfile.open(raw, "r:gz") as t:
            for m in t.getmembers():
                if m.name == "cmd/install_init":
                    before = m.mode
                    break
        fix_fpk_modes(raw)
        with tarfile.open(raw, "r:gz") as t:
            for m in t.getmembers():
                if m.name == "cmd/install_init":
                    log(f"  cmd/install_init 权限: {before:o} -> {m.mode:o}")
                    break

        ok, msgs = verify_fpk(raw, a)
        if not ok:
            for m in msgs:
                log(f"  [阻断] {m}")
            log("  自检未通过")
            sys.exit(1)

        final = os.path.join(PROJECT_DIR, f"{APP_NAME}-{version}-{a}.fpk")
        shutil.move(raw, final)
        size = os.path.getsize(final) / 1024 / 1024
        log(f"  ✅ {os.path.basename(final)}  ({size:.1f} MB)  自检通过")

    log("")
    log("=" * 46)
    log(" 构建完成")
    log("=" * 46)


if __name__ == "__main__":
    main()
