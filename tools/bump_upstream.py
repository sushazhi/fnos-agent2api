#!/usr/bin/env python3
"""检查上游 agent2api 是否有新版本；有则把「五处耦合」一次性改到位。

为什么需要这个脚本
------------------
本仓库只做飞牛适配层，上游源码不入库。上游发版很快，每次升级都要同时改动
**五个互相耦合的位置**，漏掉任何一处都会在构建期硬失败：

  1. build.py  UPSTREAM_VERSION         —— 决定 UPSTREAM_TAG 与下载地址
  2. build.py  UPSTREAM_BUNDLE_SHA256   —— 官方 Linux 服务端包的 sha256（每架构一个）
  3. build.py  UPSTREAM_UI_SHA256       —— 面板副本的树哈希（上游改一个字节就变）
  4. manifest  version                  —— 上游版本-N（预发布时为 x.y.z-pre-N），
                                            其中上游版本段必须等于 build.py 的 pin
  5. manifest  changelog                —— 版本说明（同时是 Release notes 的来源）

人工做这几步既繁琐又容易漏 —— 尤其第 2、3 项：两个哈希都得先下载上游产物、
跑完同步才知道。本脚本把「查上游 → 改 pin → 取包内 sha256 → 同步面板 →
重算哈希 → 改 manifest → 跑守卫」串成一步，供 GitHub Actions 每天自动
执行，也可以本地手动跑。

上游仓库名不在这里硬编码：一律读 build.UPSTREAM_REPO（换仓库时只改 build.py
一处。已迁过两次：2026-10-18 aimod-cc/agent2api → Agent2API/agent2api，
2026-10-24 因 Agent2API 账号消失又迁回 aimod-cc/Agent2API）。

为什么只改 pin 而不是 fork 上游
-------------------------------
README §8 与 docs/upstream-candidates.md 记录了取舍：适配层刻意**零源码改动**，
上游协议漂移被隔离在 fngateway 的反向代理改写里。所以「升级上游」在本仓库里
等价于「改几个数字 / 哈希」，而不是「合并上游 diff」。这个脚本就是那几个数字的
搬运工。

注意：二进制本身**不再由本仓库编译**。v3.1.0 起上游官方发布 Linux 服务端包
（agent2api-server-linux-<arch>.tar.gz），本仓库只钉住它的 sha256，构建时由
build.py 的 fetch_server_bundle() 下载并校验。所以这里第 2 项改的不是「编译
参数」而是「上游产物的指纹」—— 它由上游的 .sha256 资产原样搬过来。

用法
----
    python tools/bump_upstream.py --detect        # 只检查，不改任何文件
    python tools/bump_upstream.py --apply         # 检查并应用（版本取自上游）
    python tools/bump_upstream.py --apply --upstream-version 3.2.0
    python tools/bump_upstream.py --apply --force # 上游无新版本也重算一遍

在 GitHub Actions 里会把结果写进 $GITHUB_OUTPUT（update / upstream_version /
current_upstream_version / app_version / tag），供后续 job 使用。
"""
import importlib
import importlib.util
import json
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import build  # noqa: E402  （要放在 sys.path 调整之后）

# 上游 tag 形态：v2.9.0 / 2.9.0，以及**预发布** v3.2.0-beta.2。
#
# 2026-10-20 起本仓库的 pin 可能是预发布（v3.2.0-beta.2），所以 parse_ver 必须
# **能解析**预发布串 —— 否则 main() 一开头的 `cur = parse_ver(build.UPSTREAM_VERSION)`
# 会直接 SystemExit「无法解析」，连 --detect 都跑不起来。
#
# 但「能解析」不等于「会自动跟」：upstream_versions() 仍只把**正式版**放进候选集。
# 于是「pin 是预发布、上游最新正式版反而更旧」会被判为「已是最新」并早退，
# 与「预发布要不要跟由人决定」的原有约定一致；等上游发出 3.2.0 正式版，
# 3.2.0 > 3.2.0-beta.2 成立，才会照常自动跟进。
#
# 表示法：正式版 → (major, minor, patch, 1, ())；预发布 →
# (major, minor, patch, 0, ((类, 段), …))。于是 3.2.0-beta.2 < 3.2.0，
# 与 semver / PEP 440 的「预发布低于同号正式版」一致。段里纯数字按数值比、
# 其余按字符串比（beta.2 > beta.1、rc > beta）。
VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?$")

# changelog 只保留**最新一版**的说明。上游发版极快，早先的做法是每版往旧条目
# 前面插一条、最多留 4 条，但 manifest 仍会一直长下去（desc 与 changelog 都会
# 进包内 manifest，也都会显示在应用中心里），而应用中心只关心「这一版更新了
# 什么」。所以现在 bump 时**整体覆盖** changelog，不在旧条目上累加；旧版本的
# 说明留在各版本的 GitHub Release 里即可，不在 manifest 里堆。
#
# 注意：这只在「确实有新版本」时发生（版本号变了）。`--force` 且版本未变时
# 早退、不动 changelog，所以重打包不会把说明改写成通用模板。

# changelog 里分隔两个版本条目的写法（见历史 manifest：`…失败<br><br>v2.9.0-1<br>…`）。
# 只在 `<br><br>` 后面紧跟版本号时才切分，避免把条目内部的段落分隔也当成边界。
# 现在不再保留旧条目，只用来数出「覆盖前有几条」以便日志说明。
ENTRY_SPLIT_RE = re.compile(r"<br><br>(?=v\d+\.\d+\.\d+)")


def log(msg):
    """复用 build.py 的日志函数（它已经处理了 Windows 控制台的 GBK 编码问题）。"""
    build.log(msg)


def parse_ver(s):
    m = VER_RE.match((s or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1, ())
    parts = tuple(
        (0, int(seg)) if seg.isdigit() else (1, seg)
        for seg in pre.split(".")
        if seg
    )
    return (int(major), int(minor), int(patch), 0, parts)


def ver_str(v):
    base = ".".join(str(x) for x in v[:3])
    if v[3] == 1:
        return base
    return base + "-" + ".".join(str(p[1]) for p in v[4])


def api_get(path, token=None):
    url = f"https://api.github.com/{path}"
    headers = {
        "User-Agent": "fnos-agent2api-bump",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def upstream_versions(token=None):
    """列出上游所有「正式版本」号。

    tags 是权威来源 —— 本仓库升级的是「上游 tag 对应的那份产物」。
    releases/latest 只作兜底（万一上游先发 Release 后补 tag）。

    预发布（--prerelease / tag 带 -rc 之类）由 parse_ver 的严格正则挡掉：
    自动流水线只跟正式版本。
    """
    found = set()
    for page in (1, 2):
        try:
            tags = api_get(f"repos/{build.UPSTREAM_REPO}/tags?per_page=100&page={page}", token)
        except Exception as e:
            # 限流 / 网络抖动 / 仓库再次迁址（2026-10 就发生过 → 404）：
            # 不要抛原始 traceback，交给文件末尾那句「未能从上游取到任何版本号」
            # 统一报错 —— 结果一样是失败退出，但人能一眼看懂原因（在 Actions
            # 日志里尤其重要）。
            log(f"  [提示] 读取上游 tags 第 {page} 页失败: {e}")
            break
        if not isinstance(tags, list) or not tags:
            break
        for t in tags:
            v = parse_ver(t.get("name", ""))
            # 只收**正式版**：候选集里出现预发布，会让「自动跟进」越过人工决定。
            if v and v[3] == 1:
                found.add(v)
    try:
        rel = api_get(f"repos/{build.UPSTREAM_REPO}/releases/latest", token)
        v = parse_ver(rel.get("tag_name", ""))
        if v and v[3] == 1:
            found.add(v)
    except Exception as e:  # 404（仓库没有 Release）或限流都不该让检查失败
        log(f"  [提示] 读取 releases/latest 失败，忽略: {e}")
    if not found:
        raise RuntimeError(
            f"未能从上游 {build.UPSTREAM_REPO} 取到任何版本号"
            "（仓库迁址 / API 限流 / 网络？）"
        )
    return found


# ---------------------------------------------------------------------------
# build.py 的改写（UPSTREAM_VERSION / UPSTREAM_BUNDLE_SHA256 / UPSTREAM_UI_SHA256
# + 哈希上方的文件数）
# ---------------------------------------------------------------------------

def rewrite_pin(upstream_version):
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(r'(?m)^UPSTREAM_VERSION\s*=\s*"[^"]+"',
                     f'UPSTREAM_VERSION = "{upstream_version}"', text)
    if n != 1:
        raise RuntimeError(f"build.py 里 UPSTREAM_VERSION 匹配到 {n} 处，应为 1 处")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    log(f"  build.py: UPSTREAM_VERSION = {upstream_version}")


BUNDLE_SHA_RE_CACHE = {}


def _bundle_sha_re(arch):
    r"""UPSTREAM_BUNDLE_SHA256 里某个架构那一行的正则（惰性构造，按版本重用）。

    build.py 里的写法形如（键可带引号，值可带引号）：
        "amd64": "019d92bf...fb",
    正则只替换中间那 64 位十六进制，前缀与尾随逗号原样保留 —— 改完仍然**等长**，
    这对「同一整秒 + 等长 -> 旧 .pyc 被判有效」那个坑很关键（见 load_build()）。
    """
    if arch not in BUNDLE_SHA_RE_CACHE:
        BUNDLE_SHA_RE_CACHE[arch] = re.compile(
            r"(?m)^(?P<pre>\s*[\"']?" + re.escape(arch) + r"[\"']?\s*:\s*[\"']?)"
            r"(?P<h>[0-9a-f]{64})"
            r"(?P<post>[\"']?\s*,?\s*$)"
        )
    return BUNDLE_SHA_RE_CACHE[arch]


def rewrite_bundle_hashes(hashes):
    """把 build.py 里 UPSTREAM_BUNDLE_SHA256 的两个架构值改成上游给的那两个。

    hashes 形如 {"amd64": "019d…fb", "arm64": "c7cd…92"}，来自上游 Release 的
    `<asset>.sha256` 资产（内容是一行 "<hash>  <asset>"）。

    为什么必须搬这两个值而不是「让 build.py 现去查 .sha256」：钉住的哈希是本项目
    对上游产物的**独立记录**，上游重新上传资产时它必须变成构建期硬失败，而不是
    跟着上游一起漂。所以升级是一次人工/自动的「重新钉」动作，落在这一个函数里。
    """
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new = text
    for arch, want in hashes.items():
        if not re.fullmatch(r"[0-9a-f]{64}", want or ""):
            raise RuntimeError(f"{arch} 的 sha256 不是 64 位小写十六进制: {want!r}")
        hits = _bundle_sha_re(arch).findall(new)
        if len(hits) != 1:
            raise RuntimeError(
                f"build.py 里 UPSTREAM_BUNDLE_SHA256[{arch!r}] 匹配到 {len(hits)} 处，应为 1 处"
                "（字典键或取值写法变了？请同步更新 bump_upstream._bundle_sha_re）"
            )
        cur = hits[0][1]
        new = _bundle_sha_re(arch).sub(
            lambda m: m.group("pre") + want + m.group("post"), new, count=1)
        log(f"  build.py: UPSTREAM_BUNDLE_SHA256[{arch}] "
            f"{'(未变)' if cur == want else cur[:12] + '… -> ' + want[:12] + '…'}")
    if new != text:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    return hashes


def fetch_bundle_sha256(arch, upstream_tag, token=None):
    """读上游 Release 的 `<asset>.sha256` 资产，返回该架构二进制包的 sha256。

    资产内容是一行 `<hash>  <asset>`（sha256sum 格式，与 `sha256sum -c` 逐字节
    兼容）。两种取法，按顺序尝试：

      1. GitHub API `releases/tags/<tag>` 列出 assets，按名字精确匹配 ——
         带 token 时走 API 限流额度，CI 里更稳；
      2. 直接下载 `.../releases/download/<tag>/<asset>.sha256` —— 本地无 token
         或 API 限流时的兜底。

    两种都取不到、取到多行、或哈希形状不对，都**硬失败**：绝不能把「没读到」
    当成「不用改」—— 那会让 build.py 留着旧哈希，而版本号已经换了，结果是
      `--fetch-binaries` 在 CI 上必然失败（上游包 sha 不符），却看不出是这里漏了。
    """
    asset = build.UPSTREAM_BUNDLE_ASSET.format(arch=arch)
    raw = None

    # 1) API：列出 assets，按名字精确匹配，再下载 .sha256 资产本身
    try:
        rel = api_get(f"repos/{build.UPSTREAM_REPO}/releases/tags/{upstream_tag}", token)
        names = {a.get("name"): a for a in (rel.get("assets") or [])}
        sha_name = f"{asset}.sha256"
        if sha_name not in names:
            raise RuntimeError(f"Release {upstream_tag} 里没有资产 {sha_name}"
                               f"（现有: {', '.join(sorted(n for n in names if n))[:300]}）")
        url = names[sha_name].get("browser_download_url") or (
            f"https://github.com/{build.UPSTREAM_REPO}/releases/download/"
            f"{upstream_tag}/{sha_name}")
        req = urllib.request.Request(url, headers={"User-Agent": "fnos-agent2api-bump"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:
        log(f"  [提示] 经 API 取 {asset}.sha256 失败，改用直连地址: {e}")
        raw = None

    # 2) 直连：不依赖 API（本地无 token / 限流时这条路更省事）
    if raw is None:
        url = (f"https://github.com/{build.UPSTREAM_REPO}/releases/download/"
               f"{upstream_tag}/{asset}.sha256")
        req = urllib.request.Request(url, headers={"User-Agent": "fnos-agent2api-bump"})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"{asset}.sha256 是空的")
    if len(lines) > 1:
        # 多于一行时按第二列的文件名挑出本资产那一行，仍然多于一行就报错。
        matched = [ln for ln in lines if ln.split()[-1].lstrip("*") == asset]
        if len(matched) != 1:
            raise RuntimeError(f"{asset}.sha256 里有 {len(lines)} 行，且无法唯一定位 {asset}")
        lines = [matched[0]]
    parts = lines[0].split()
    digest = parts[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError(f"{asset}.sha256 第一列不是 sha256: {lines[0]!r}")
    if len(parts) > 1 and parts[-1].lstrip("*") != asset:
        raise RuntimeError(f"{asset}.sha256 指向的是 {parts[-1]!r}，不是 {asset!r}")
    return digest


def fetch_bundle_hashes(upstream_tag, token=None):
    """取两个架构的官方包 sha256：{"amd64": …, "arm64": …}。"""
    out = {}
    for arch in ("amd64", "arm64"):
        out[arch] = fetch_bundle_sha256(arch, upstream_tag, token)
        log(f"  上游 {arch} 包 sha256: {out[arch]}")
    return out


def rewrite_ui_hash(ui_hash, file_count):
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(r'(?m)^UPSTREAM_UI_SHA256\s*=\s*"[0-9a-f]{64}"',
                     f'UPSTREAM_UI_SHA256 = "{ui_hash}"', text)
    if n != 1:
        raise RuntimeError(f"build.py 里 UPSTREAM_UI_SHA256 匹配到 {n} 处，应为 1 处")
    # 哈希上方那句注释里的文件数也要跟着走，否则文档会悄悄失真。
    new, n2 = re.subn(r"(?m)^(# 上游 WebUI（[^）]*）的确定性树哈希 —— )\d+( 个文件)",
                      rf"\g<1>{file_count}\g<2>", new)
    if n2 != 1:
        raise RuntimeError(f"build.py 里树哈希注释的文件数匹配到 {n2} 处，应为 1 处")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    log(f"  build.py: UPSTREAM_UI_SHA256 = {ui_hash}  ({file_count} 个文件)")


def load_build():
    """改完 build.py 后重新载入，让常量（含由 UPSTREAM_VERSION 派生的下载地址）生效。

    这里**不能**只写 importlib.reload(build)：reload 会复用 __pycache__ 里的 .pyc，
    而 CPython 判定 .pyc 是否新鲜只看源文件 **mtime 的整秒** 和 **字节数**
    （importlib._bootstrap_external._validate_timestamp_pyc）。本脚本对 build.py 的
    **三处**改写全都等长 —— "3.1.0"→"3.2.0"、UPSTREAM_BUNDLE_SHA256 的 64 位
    十六进制→64 位十六进制、UPSTREAM_UI_SHA256 的 64 位十六进制→64 位十六进制 ——
    只要改写与上一次 import 落在同一整秒内，旧 .pyc 就被判为仍然有效，reload 拿到
    的是旧字节码：文件明明改了，模块里的常量却还是旧的。

    这在 CI 上真的发生过，而且只在 CI 上偶发：第 3 步刚打印出新的树哈希，第 4 步
    verify_webui_provenance() 却用旧常量去比新算出的哈希，报出一句看不出真因的
    「上游 WebUI 树哈希与钉住的值不一致」。两次改写之间只隔着一次下载 + 解包 + 同步，
    实测可以不到一秒，所以只有恰好同秒才命中。本地同样会中，只是要撞上时序。

    所以这里三重保险，不依赖任何时序：
      1. invalidate_caches() —— 清掉 importlib 自己的目录缓存（单独用它**不够**，
         它不校验 .pyc 是否过期，实测仍会读到旧字节码）；
      2. 删掉这个源文件对应的 .pyc —— 让缓存无处可复用；
      3. 直接 exec 源文件当前内容到已有模块的 __dict__ —— 即便缓存又以别的形式
         冒出来，读到的也一定是磁盘上的最新文本。

    （tools/check_bump_reload.py 把上面那个「同一整秒 + 等长改写」的时序确定性地
    造出来做自检：修好之前必失败，修好之后必通过。）
    """
    importlib.invalidate_caches()

    pyc = importlib.util.cache_from_source(build.__file__)
    try:
        os.remove(pyc)
    except FileNotFoundError:
        pass

    with open(build.__file__, "r", encoding="utf-8") as f:
        source = f.read()
    exec(compile(source, build.__file__, "exec"), build.__dict__)
    return build


# ---------------------------------------------------------------------------
# manifest 的改写
# ---------------------------------------------------------------------------

def read_manifest():
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        return f.read()


def write_manifest(text):
    problems = build.check_manifest_values(text)
    if problems:
        raise RuntimeError("manifest 取值会被 fnpack 截断:\n    " + "\n    ".join(problems))
    with open(os.path.join(PROJECT_DIR, "manifest"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def manifest_version(text):
    m = re.search(r"(?m)^version\s*=\s*(\S+)\s*$", text)
    if not m:
        raise RuntimeError("manifest 里读不到 version")
    return m.group(1)


MANIFEST_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s*=\s*)(?P<val>.*)$")


def set_manifest_key(text, key, value):
    """按行改写 manifest 的某个键，保留键名对齐（等号两侧空白）与行尾。

    用逐行替换而不是对整段文本做 `re.sub`：manifest 的 desc/changelog 都是超长
    单行，连续两次整段替换时第二次的偏移会失效 —— 实测会把文件末尾的换行吃掉，
    触发 check_text_hygiene 的「末尾缺换行」（该守卫只对 vendored app/ui 放行）。
    """
    lines = text.splitlines(keepends=True)
    hits = 0
    for i, line in enumerate(lines):
        m = MANIFEST_LINE_RE.match(line.rstrip("\r\n"))
        if not m or m.group("key") != key:
            continue
        eol = "\n" if line.endswith("\n") else ""
        lines[i] = f"{m.group('key')}{m.group('sep')}{value}{eol}"
        hits += 1
    if hits != 1:
        raise RuntimeError(f"manifest 里 {key} 匹配到 {hits} 行，应为 1 行")
    return "".join(lines)


def bump_manifest(upstream_tag, app_version):
    """把 manifest 的 version / changelog 改到位。

    版本号已经是目标值时**不动 changelog** —— 这样 `--force` 就是纯粹的
    「按当前版本重打一次包」，不会每次重跑都往 changelog 里塞重复条目。

    changelog **只保留最新一版**：有新版本时整段覆盖，不再把旧条目累加在后面。
    """
    text = read_manifest()
    cur = manifest_version(text)
    if cur == app_version:
        log(f"  manifest: version 已是 {app_version}，changelog 保持不变（重打包）")
        return text, False

    # 上游版本号必须跟着走：manifest.version 的 x.y.z 段是给人看的「集成了哪个
    # 上游」，与 build.py 的 pin 脱钩会得到「包名说 2.10 实际编的是 2.9」。
    new_entry = (
        f"v{app_version}<br>"
        f"1.上游 agent2api {upstream_tag} 原样集成（零源码改动），面板副本与上游 tag 逐字节一致<br>"
        f"2.重新编译 Linux 双架构二进制（amd64 / arm64），glibc 上限仍低于飞牛 fnOS 的 2.36<br>"
        f"3.本条目由 GitHub Actions 定时检查上游后自动生成"
    )
    m = re.search(r"(?m)^changelog\s*=\s*(.*)$", text)
    if not m:
        raise RuntimeError("manifest 里读不到 changelog")
    # 只统计旧条目数用于日志；旧条目一律丢弃（changelog 只保留最新一版）。
    old_entries = [e for e in ENTRY_SPLIT_RE.split(m.group(1)) if e.strip()]

    text = set_manifest_key(text, "version", app_version)
    text = set_manifest_key(text, "changelog", new_entry)
    log(f"  manifest: version {cur} -> {app_version}，changelog 覆盖为最新一版"
        f"（丢弃旧条目 {max(len(old_entries) - 1, 0)} 条）")
    return text, True


# ---------------------------------------------------------------------------
# GitHub Actions 输出
# ---------------------------------------------------------------------------

def emit_outputs(pairs):
    """把结果写给 GitHub Actions：$GITHUB_OUTPUT 供下游 job/step 取值。

    只写 key=value，不写 markdown 表格 —— workflow 里的 Summary step 已经
    负责展示（那边能同时列出「上游 tag / 应用版本 / 发布 tag」的全貌，
    而本脚本在 --detect 早期就可能退出，写两份会出现两个内容不同的表格）。
    本地运行时这两个环境变量都不存在，整个函数就是空操作。
    """
    out = os.environ.get("GITHUB_OUTPUT", "").strip()
    for k, v in pairs.items():
        log(f"  output {k}={v}")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    detect_only = "--detect" in args
    apply_changes = "--apply" in args
    force = "--force" in args
    if not detect_only and not apply_changes:
        print(__doc__)
        sys.exit(2)

    want = ""
    if "--upstream-version" in args:
        want = args[args.index("--upstream-version") + 1]
        if not parse_ver(want):
            raise SystemExit(f"ERROR: --upstream-version 不是合法版本号: {want!r}")
        want = ver_str(parse_ver(want))

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

    log("=" * 46)
    log(" 上游版本检查")
    log(f" 仓库: {build.UPSTREAM_REPO}")
    log(f" 当前 pin: {build.UPSTREAM_VERSION}")
    log("=" * 46)

    cur = parse_ver(build.UPSTREAM_VERSION)
    if cur is None:
        raise SystemExit(f"ERROR: build.py 的 UPSTREAM_VERSION 无法解析: {build.UPSTREAM_VERSION!r}")

    try:
        latest = parse_ver(want) if want else max(upstream_versions(token))
    except Exception as e:
        # 取不到上游版本就是「这次检查失败」，但不能让它以 traceback 收场：
        # Actions 日志里一个 traceback 会被当成脚本 bug，而真实原因通常是
        # API 限流（匿名 60 次/小时）或网络。job 照样非零退出（fail 是正确
        # 的 —— 绝不能在「不知道上游有没有新版」时判定「无更新」并静默跳过）。
        raise SystemExit(f"ERROR: 无法确定上游最新版本，本次检查失败: {e}")
    log(f" 上游最新: {ver_str(latest)}")

    newer = latest > cur
    update = newer or force
    target_up = ver_str(latest if newer else cur)
    upstream_tag = f"v{target_up}"
    app_version = f"{target_up}-1"
    if not newer:
        # 上游没新版本：目标版本沿用 manifest 里现有的（可能是 -2、-3 这种重打包）
        app_version = manifest_version(read_manifest())

    log(f" 判定: {'有新版本' if newer else '已是最新'}"
        f"{'（--force 仍重打一次）' if force and not newer else ''}")
    log(f" 目标: 上游 {upstream_tag} → 应用版本 {app_version}")

    emit_outputs({
        "update": "true" if update else "false",
        "newer": "true" if newer else "false",
        "upstream_version": target_up,
        "current_upstream_version": build.UPSTREAM_VERSION,
        "upstream_tag": upstream_tag,
        "app_version": app_version,
        "tag": f"v{app_version}",
    })

    if detect_only or not update:
        log("")
        log(" 未改动任何文件" if not update else " --detect：未改动任何文件")
        return

    log("")
    log("[1/6] 改 build.py 的上游 pin ...")
    if newer:
        rewrite_pin(target_up)
        load_build()
    else:
        log("  （--force：上游无新版本，pin 不变）")

    # 二进制包 sha256 跟着版本走：新版就有新包、新指纹。--force 且版本未变时
    # 也重搬一次（上游可能悄悄换了资产），这正好让「重新钉一次」变得可审计。
    log("[2/6] 取上游官方服务端包的 sha256 并写入 build.py ...")
    bundle_hashes = fetch_bundle_hashes(upstream_tag, token)
    if bundle_hashes == dict(getattr(build, "UPSTREAM_BUNDLE_SHA256", {}) or {}):
        log("  与旧哈希一致（上游包未变）")
    rewrite_bundle_hashes(bundle_hashes)
    load_build()

    log("[3/6] 拉上游源码 + 同步面板 ...")
    build.fetch_upstream()
    build.sync_webui()

    log("[4/6] 重算面板树哈希 ...")
    ui_hash, entries = build.tree_hash(
        os.path.join(PROJECT_DIR, "app", "ui"), exclude_prefixes=build.LOCAL_UI_ENTRIES)
    log(f"  新哈希 {ui_hash[:16]}…（{len(entries)} 个文件）")
    if ui_hash == build.UPSTREAM_UI_SHA256:
        log("  与旧哈希一致（上游面板未变）")
    rewrite_ui_hash(ui_hash, len(entries))
    load_build()

    log("[5/6] 面板溯源复核 + 改 manifest（version + changelog）...")
    build.verify_webui_provenance()
    text, changed = bump_manifest(upstream_tag, app_version)
    write_manifest(text)
    load_build()

    log("[6/6] 构建期守卫（不需要二进制的那几道）...")
    build.check_text_hygiene()
    build.verify_icons()
    build.check_webui()
    build.check_consistency()

    # 最后自证：常量确实落到目标值上（防止「文件改了但正则没匹配上」这种静默失配）
    if build.UPSTREAM_VERSION != target_up:
        raise SystemExit(f"ERROR: 改完后 UPSTREAM_VERSION={build.UPSTREAM_VERSION!r}"
                         f" != {target_up!r}")
    if build.UPSTREAM_TAG != upstream_tag:
        raise SystemExit(f"ERROR: 改完后 UPSTREAM_TAG={build.UPSTREAM_TAG!r}"
                         f" != {upstream_tag!r}（它应由 UPSTREAM_VERSION 派生）")
    got_bundle = dict(getattr(build, "UPSTREAM_BUNDLE_SHA256", {}) or {})
    for arch, want in bundle_hashes.items():
        if got_bundle.get(arch) != want:
            raise SystemExit(f"ERROR: 改完后 UPSTREAM_BUNDLE_SHA256[{arch}]="
                             f"{got_bundle.get(arch)!r} != {want!r}"
                             "（.pyc 缓存未失效，或正则失配）")
    if build.UPSTREAM_UI_SHA256 != ui_hash:
        raise SystemExit("ERROR: 改完后 UPSTREAM_UI_SHA256 与实测树哈希不一致")
    log("")
    log(f" 完成：上游 {upstream_tag}，应用版本 {app_version}"
        f"{'，manifest 已更新' if changed else '，manifest 版本未变'}")


if __name__ == "__main__":
    main()
