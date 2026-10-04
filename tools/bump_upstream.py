#!/usr/bin/env python3
"""检查上游 agent2api 是否有新版本；有则把「四处耦合」一次性改到位。

为什么需要这个脚本
------------------
本仓库只做飞牛适配层，上游 Rust 源码不入库。上游发版很快（v2.7.3 → v2.9.0
只用了两天），每次升级都要同时改动**四个互相耦合的位置**，漏掉任何一处都会
在构建期硬失败：

  1. build.py  UPSTREAM_VERSION    —— 决定 UPSTREAM_TAG 与源码下载地址
  2. build.py  UPSTREAM_UI_SHA256  —— 面板副本的树哈希（上游改一个字节就变）
  3. manifest  version             —— x.y.z-N，其中 x.y.z 必须等于上游版本
  4. manifest  changelog           —— 版本说明（同时是 Release notes 的来源）

人工做这四步既繁琐又容易漏 —— 尤其第 2 项：哈希得先跑完上游同步才知道。
本脚本把「查上游 → 改 pin → 同步面板 → 重算哈希 → 改 manifest → 跑守卫」
串成一步，供 GitHub Actions 每周一自动执行，也可以本地手动跑。

为什么只改 pin 而不是 fork 上游
-------------------------------
README §8 与 docs/upstream-candidates.md 记录了取舍：适配层刻意**零源码改动**，
上游协议漂移被隔离在 fngateway 的反向代理改写里。所以「升级上游」在本仓库里
等价于「改四个数字」，而不是「合并上游 diff」。这个脚本就是那四个数字的搬运工。

用法
----
    python tools/bump_upstream.py --detect        # 只检查，不改任何文件
    python tools/bump_upstream.py --apply         # 检查并应用（版本取自上游）
    python tools/bump_upstream.py --apply --upstream-version 2.10.0
    python tools/bump_upstream.py --apply --force # 上游无新版本也重算一遍

在 GitHub Actions 里会把结果写进 $GITHUB_OUTPUT（update / upstream_version /
current_upstream_version / app_version / tag），供后续 job 使用。
"""
import importlib
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

# 上游 tag 形态：v2.9.0 / 2.9.0。带后缀的（v2.9.0-beta）一律不认 ——
# 自动流水线只跟正式版本，预发布版本要不要跟由人决定。
VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

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
    return tuple(int(x) for x in m.groups()) if m else None


def ver_str(v):
    return ".".join(str(x) for x in v)


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

    tags 是权威来源：上游 Release 只挂 macOS/Windows 安装包（实测 v2.7.x~v2.9.0
    的 assets 全是 .dmg/.exe），**不发 Linux 二进制**，所以 Release 本身不能用来
    判断「源码版本」；tag 才是我们要编的那份源码。releases/latest 只作兜底
    （万一上游先发 Release 后补 tag）。
    """
    found = set()
    for page in (1, 2):
        try:
            tags = api_get(f"repos/{build.UPSTREAM_REPO}/tags?per_page=100&page={page}", token)
        except Exception as e:
            # 限流 / 网络抖动：不要抛原始 traceback，交给文件末尾那句
            # 「未能从上游取到任何版本号」统一报错 —— 结果一样是失败退出，
            # 但人能一眼看懂原因（在 Actions 日志里尤其重要）。
            log(f"  [提示] 读取上游 tags 第 {page} 页失败: {e}")
            break
        if not isinstance(tags, list) or not tags:
            break
        for t in tags:
            v = parse_ver(t.get("name", ""))
            if v:
                found.add(v)
    try:
        rel = api_get(f"repos/{build.UPSTREAM_REPO}/releases/latest", token)
        v = parse_ver(rel.get("tag_name", ""))
        if v:
            found.add(v)
    except Exception as e:  # 404（仓库没有 Release）或限流都不该让检查失败
        log(f"  [提示] 读取 releases/latest 失败，忽略: {e}")
    if not found:
        raise RuntimeError("未能从上游取到任何版本号（网络或 API 限流？）")
    return found


# ---------------------------------------------------------------------------
# build.py 的三处改写（UPSTREAM_VERSION / UPSTREAM_UI_SHA256 / 哈希上方的文件数）
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


def rewrite_ui_hash(ui_hash, file_count):
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(r'(?m)^UPSTREAM_UI_SHA256\s*=\s*"[0-9a-f]{64}"',
                     f'UPSTREAM_UI_SHA256 = "{ui_hash}"', text)
    if n != 1:
        raise RuntimeError(f"build.py 里 UPSTREAM_UI_SHA256 匹配到 {n} 处，应为 1 处")
    # 哈希上方那句注释里的文件数也要跟着走，否则文档会悄悄失真。
    new, n2 = re.subn(r"(?m)^(# 上游 WebUI（desktop-tauri/ui/）的确定性树哈希 —— )\d+( 个文件)",
                      rf"\g<1>{file_count}\g<2>", new)
    if n2 != 1:
        raise RuntimeError(f"build.py 里树哈希注释的文件数匹配到 {n2} 处，应为 1 处")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    log(f"  build.py: UPSTREAM_UI_SHA256 = {ui_hash}  ({file_count} 个文件)")


def load_build():
    """改完 build.py 后重新载入，让常量（含由 UPSTREAM_VERSION 派生的下载地址）生效。"""
    importlib.reload(build)
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

    log("[2/6] 拉上游源码 + 同步面板 ...")
    build.fetch_upstream()
    build.sync_webui()

    log("[3/6] 重算面板树哈希 ...")
    ui_hash, entries = build.tree_hash(
        os.path.join(PROJECT_DIR, "app", "ui"), exclude_prefixes=build.LOCAL_UI_ENTRIES)
    log(f"  新哈希 {ui_hash[:16]}…（{len(entries)} 个文件）")
    if ui_hash == build.UPSTREAM_UI_SHA256:
        log("  与旧哈希一致（上游面板未变）")
    rewrite_ui_hash(ui_hash, len(entries))
    load_build()

    log("[4/6] 面板溯源复核 ...")
    build.verify_webui_provenance()

    log("[5/6] 改 manifest（version + changelog）...")
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
    if build.UPSTREAM_UI_SHA256 != ui_hash:
        raise SystemExit("ERROR: 改完后 UPSTREAM_UI_SHA256 与实测树哈希不一致")
    log("")
    log(f" 完成：上游 {upstream_tag}，应用版本 {app_version}"
        f"{'，manifest 已更新' if changed else '，manifest 版本未变'}")


if __name__ == "__main__":
    main()
