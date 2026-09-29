#!/usr/bin/env python3
"""build.py — fnos-agent2api 打包脚本（跨平台）

上游 aimod-cc/agent2api（Rust 单文件静态二进制）**原样使用**；本仓库只提供飞牛
适配层（fngateway / cmd / config / wizard / manifest / 图标 / 上游 WebUI），
不改上游源码。

产物: agent2api-<manifest 版本>-<arch>.fpk（每架构一个包，装哪台给哪个）

与 D:\\fnos 下成熟项目一致的工程约定:
  * 中间产物全部落 .local-build/（已 gitignore）: fnpack / 编译产物 / stage
  * fnpack 自动下载（官方 static2.fnnas.com，1.2.3）
  * Windows 上 fnpack 产出 0666 → 打包后重建 tar，重写可执行位
  * fnpack 只打包它 schema 认识的根文件 → 补回 UPSTREAM.txt
  * manifest 取值中的 ASCII 分号会被 fnpack 静默截断 → 构建期硬失败

与 fnos-cli2api 的差异（上游形态不同，校验项随之不同）:
  * 无 Node worker / 无 Qoder CLI bundle / 无 sharp 原生库 → 无随包 npm 依赖
  * 上游二进制由 GitHub Actions（ubuntu-24.04）编译后放进 .local-build/bin；
    本机不需要 Rust 工具链，也不需要 WSL/Docker
  * 上游 WebUI（desktop-tauri/ui，71 个文件）直接随包进 app/ui/
  * 额外校验: 上游 WebUI 的入口文件齐备、面板资源引用是裸相对路径（fngateway 的
    HTML 改写依赖这一形态；一旦上游改成绝对路径，网关会静默失效）

用法:
    python build.py                    # 打包（amd64 + arm64 各一个包）
    python build.py --arch arm64       # 只出 arm64 包
    python build.py --check            # 只跑全部预检，不打包（不联网）
    python build.py --sync-upstream    # 拉上游 tag 源码并同步 desktop-tauri/ui 进 app/ui
    python build.py --version 2.9.0-2  # 覆盖版本号

上游源码怎么进来:
    * app/ui/（面板）**随仓库提交**，打包不联网也能出包；
      `--sync-upstream` 用上游 tag 刷新它，随后由 verify_webui_provenance()
      按钉住的树哈希核对。
    * agent2api-server 的 Linux 二进制由 GitHub Actions 编译（见
      .github/workflows/build-and-release.yml），本机不需要 Rust 工具链。
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
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(PROJECT_DIR, ".local-build")
STAGE_DIR = os.path.join(BUILD_DIR, "stage")
BIN_CACHE = os.path.join(BUILD_DIR, "bin")
SRC_DIR = os.path.join(BUILD_DIR, "upstream-src")

APP_NAME = "agent2api"

# --- 上游（版本与 manifest 的 2.9.0-N 对应）---
UPSTREAM_REPO = "aimod-cc/agent2api"
UPSTREAM_VERSION = "2.9.0"
UPSTREAM_TAG = f"v{UPSTREAM_VERSION}"

# 上游源码 tarball 的下载地址（codeload 直连 + 两个 GitHub 镜像兜底）。
# 只取 tag，不取 master —— 上游发布节奏很快，master 随时可能漂。
UPSTREAM_URLS = (
    f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/refs/tags/{UPSTREAM_TAG}",
    f"https://gh-proxy.com/https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.tar.gz",
    f"https://ghfast.top/https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.tar.gz",
)

# 上游 WebUI（desktop-tauri/ui/）的确定性树哈希 —— 71 个文件，排除本项目自己
# 加的 config / images/。算法见 tree_hash()。
#
# 这是一个**漂移探针**：CI 从上游 tag 取源码后按此哈希核对，本地也核对已提交的
# 副本。上游若改动面板（哪怕只改一个字节），这里立刻构建期失败，逼我们人工确认
# 「面板形态是否还是 fngateway 能改写的那一种」，而不是等到真机上白屏才发现。
UPSTREAM_UI_SHA256 = "5ecf43c0eefde6f7d42bc2c3eca5ad48fe86f5c373c9d2dfcd8efa6d4579c653"

# app/ui 下属于本项目、**不属于**上游 WebUI 的条目（同步时不能被上游覆盖）。
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
# 上游仓库 desktop-tauri/ui/ 下的全部文件（71 个，约 2.0 MB）原样进 app/ui/。
# 必需文件缺失不会导致打包失败，只会让面板白屏 —— 属于「装完才发现」的静默缺陷。
UI_REQUIRED = (
    "ui/index.html",
    "ui/app.js",
    "ui/icons.js",
    "ui/islands/ui.js",
    "ui/css/tokens.css",
    "ui/config",
)

FNPACK_BASE = "https://static2.fnnas.com/fnpack/fnpack-1.2.3"
FNPACK_VER = "1.2.3"

# 上游 HTML 里资源引用的形态。fngateway 的 htmlAttrRe 依赖**裸相对路径**
# （src="islands/ui.js"，不带前导 / 或 ./）。上游若改成 /islands/ui.js，
# 改写逻辑仍然工作，但若改成绝对 http(s) 或 CDN 前缀，网关就静默失效了。
BARE_ASSET_RE = re.compile(r'(?i)\b(?:src|href)\s*=\s*"([^"]+)"')
ABSOLUTE_ASSET_RE = re.compile(r'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)')

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
    """
    problems = []
    for rel in UI_REQUIRED:
        p = os.path.join(PROJECT_DIR, "app", *rel.split("/"))
        if not os.path.exists(p):
            problems.append(f"缺少 app/{rel}（上游 desktop-tauri/ui 未完整复制）")

    index = os.path.join(PROJECT_DIR, "app", "ui", "index.html")
    if os.path.exists(index):
        with open(index, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        refs = BARE_ASSET_RE.findall(html)
        if not refs:
            problems.append("app/ui/index.html 里找不到任何 src/href 资源引用"
                            "（上游结构变化，请复核 fngateway 的 HTML 改写）")
        absolute = [r for r in refs if ABSOLUTE_ASSET_RE.match(r.strip())]
        # 外链（http/https/CDN）是允许的；但根绝对路径 /xxx 会让网关改写失效。
        root_abs = [r for r in absolute if r.strip().startswith("/")]
        if root_abs:
            problems.append(
                "app/ui/index.html 出现根绝对路径资源引用: "
                + ", ".join(sorted(set(root_abs))[:5])
                + " —— fngateway 只改写相对路径，请复核 gateway.go 的 htmlAttrRe"
            )
        log(f"  index.html: {len(refs)} 个资源引用（根绝对路径 {len(root_abs)} 个）")

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


def fetch_upstream(force=False):
    """取上游源码 tarball（tag 固定），解到 .local-build/upstream-src/。"""
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
    # verify_webui_provenance() 的 UI 树哈希 + 下面的版本号断言。
    with open(tarball, "rb") as f:
        th = hashlib.sha256(f.read()).hexdigest()
    log(f"  上游源码就位: {SRC_DIR}")
    log(f"  tarball sha256: {th}")
    verify_upstream_version()

def verify_upstream_version():
    """核对拉到的上游源码确实是 manifest 声明的那个版本。

    防的是「tag 被移动 / tarball 来自别的 ref / 镜像给了缓存旧包」——
    这类问题不会让编译失败，只会静默产出一个版本号对不上的包。
    """
    cargo = os.path.join(SRC_DIR, "desktop-tauri", "src-tauri", "server", "Cargo.toml")
    if not os.path.exists(cargo):
        log(f"  ERROR: 上游源码里找不到 {cargo}")
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


def sync_webui():
    """把上游 desktop-tauri/ui/ 同步进 app/ui/（本项目自加的条目不动）。"""
    src = os.path.join(SRC_DIR, "desktop-tauri", "ui")
    if not os.path.isdir(src):
        log(f"  ERROR: 上游源码里找不到 desktop-tauri/ui: {src}")
        sys.exit(1)
    dst = os.path.join(PROJECT_DIR, "app", "ui")

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
    log(f"  上游 WebUI 同步完成: 移除 {len(removed)} 个旧条目, 写入 {copied} 个文件")


def verify_webui_provenance():
    """核对 app/ui 里的上游派生文件与钉住的哈希一致。"""
    dst = os.path.join(PROJECT_DIR, "app", "ui")
    got, entries = tree_hash(dst, exclude_prefixes=LOCAL_UI_ENTRIES)
    log(f"  app/ui 上游派生文件: {len(entries)} 个, 树哈希 {got[:16]}…")
    if got != UPSTREAM_UI_SHA256:
        log("  ERROR: 上游 WebUI 树哈希与钉住的值不一致（面板形态可能已变）。")
        log(f"    期望: {UPSTREAM_UI_SHA256}")
        log(f"    实际: {got}")
        log("    请人工确认上游改动是否影响 fngateway 的子路径改写（HTML 资源引用、")
        log("    localStorage 键名、fetch 调用形态），确认无误后更新 build.py 里的")
        log("    UPSTREAM_UI_SHA256。")
        sys.exit(1)
    log("  上游 WebUI 溯源校验通过")


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


def check_binaries(arches):
    problems = []
    for arch in arches:
        for p in bin_paths(arch):
            name = os.path.basename(p)
            if not os.path.exists(p):
                problems.append(
                    f"缺少编译产物 {name} —— 请先由 GitHub Actions 构建并放到 "
                    f".local-build/bin/（本机不需要 Rust 工具链）"
                )
                continue
            ok, why = _elf_check(p, arch)
            size = os.path.getsize(p)
            if not ok:
                problems.append(f"{name}: {why}")
            else:
                log(f"  {name}: {size:,} B  {arch}  ELF")
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

    # app/ui: 上游 WebUI（原样）+ 桌面入口 config + 图标
    copy_tree(os.path.join(PROJECT_DIR, "app", "ui"), os.path.join(STAGE_DIR, "app", "ui"))
    ui_files = sum(len(fs) for _, _, fs in os.walk(os.path.join(STAGE_DIR, "app", "ui")))
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

    # 上游署名与许可（MIT 要求随二进制附带许可全文）
    with open(os.path.join(STAGE_DIR, "UPSTREAM.txt"), "w", encoding="utf-8") as f:
        f.write(
            "本应用内置上游 agent2api（agent2api-server）。\n\n"
            f"上游项目: https://github.com/{UPSTREAM_REPO}\n"
            f"上游版本: {UPSTREAM_TAG}\n"
            "上游许可: MIT License 正文，文件末尾附有「使用声明」\n"
            "上游版权: Copyright (c) 2026 aimod-cc\n"
            f"构建时间: {datetime.datetime.now().isoformat(timespec='seconds')}\n\n"
            "依据 MIT 许可，源码与二进制形式的再分发均保留原始版权声明与许可声明。\n"
            "上游许可全文见同目录 LICENSE.upstream；源码可自 "
            f"https://github.com/{UPSTREAM_REPO} 获取。\n\n"
            "本包未修改上游源码：飞牛适配层（fngateway 反向代理、生命周期脚本、\n"
            "桌面入口）是本项目独立新增的组件，不含上游代码。\n\n"
            "注意：上游 LICENSE 除 MIT 条款外另附「使用声明」，其中限制商业用途。\n"
            "本打包项目仅供个人在自有飞牛设备上自用。\n"
        )
    lic = os.path.join(PROJECT_DIR, "LICENSE.upstream")
    if os.path.exists(lic):
        copy_tree(lic, os.path.join(STAGE_DIR, "LICENSE.upstream"))
    else:
        log("  [警告] 缺少 LICENSE.upstream，包内将不含上游许可全文；"
            "MIT 再分发义务未完成，请补齐后再发布")


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

        # 面板资源引用形态：fngateway 依赖裸相对路径
        if "ui/index.html" in inner_files:
            html = read_inner("ui/index.html").decode("utf-8", "replace")
            refs = BARE_ASSET_RE.findall(html)
            root_abs = [r for r in refs if r.strip().startswith("/")]
            if root_abs:
                msgs.append("ui/index.html 出现根绝对路径资源引用，fngateway 改写会失效: "
                            + ", ".join(sorted(set(root_abs))[:5]))
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
                    help="拉取上游 tag 源码并把 desktop-tauri/ui 同步进 app/ui（需联网）")
    ap.add_argument("--force-upstream", action="store_true",
                    help="与 --sync-upstream 同用：忽略本地缓存重新下载")
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

    if args.sync_upstream:
        # 维护操作：拉上游 tag → 同步 desktop-tauri/ui 进 app/ui → 核对溯源 → 退出。
        # 不继续打包（此时通常还没有二进制）。
        log("[sync] 拉取上游源码 ...")
        fetch_upstream(force=args.force_upstream)
        log("[sync] 同步上游 WebUI ...")
        sync_webui()
        log("[sync] 文本卫生（编码/换行）...")
        check_text_hygiene()
        log("[sync] 校验图标 + 上游 WebUI ...")
        verify_icons()
        check_webui()
        log("[sync] 上游 WebUI 溯源 ...")
        verify_webui_provenance()
        log("")
        log("上游同步完成（app/ui 已刷新，请提交改动）")
        return

    log("[1/6] 跳过上游同步（用仓库内已提交的 app/ui；加 --sync-upstream 可刷新）")
    log("[2/6] 文本卫生（编码/换行）...")
    check_text_hygiene()
    log("[3/6] 校验图标 + 上游 WebUI ...")
    verify_icons()
    check_webui()

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
