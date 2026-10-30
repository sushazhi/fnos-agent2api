"""从上游 tag **源码**构建时，就地改写本仓库的上游 pin（只在 CI 工作区用，不提交）。

为什么需要它
------------
build.py 的常规路径假设「二进制 + 面板都来自上游官方 Release 包」，并据此钉住两个值：

  · ``UPSTREAM_VERSION``  —— 只用于日志/溯源与 ``UPSTREAM_TAG`` 派生；
  · ``UPSTREAM_UI_SHA256`` —— 上游面板树的哈希，构建期逐架构校验，**不符即 exit 1**。

上游只打了 tag、还没发 Release 资产时（2026-10-30 的 ``v3.3.0-beta.5.1`` 就是这样），
二进制改由 CI 交叉编译，面板改从同一 tag 的 ``desktop-tauri/ui`` 取。这两件事一变，
上面两个值就必须跟着变：

  · ``UPSTREAM_UI_SHA256`` 不重算 → 构建期报「面板形态可能已变」，出不了包；
  · ``UPSTREAM_VERSION`` 不改   → 日志与 changelog 说「集成的是 beta.4」，
                                  实际编进去的是 beta.5.1，溯源直接失真。

哈希为什么要走 ``build._upstream_ui_src()``
------------------------------------------
真正的进包形态不是「源码目录原样」——``patch_sw_scope()`` 会把 PWA 的 SW scope
从根绝对路径改成 ``./``（否则经网关子路径运行时注册失败）。所以必须让 build.py
自己把补丁打完再算树哈希，和 ``verify_webui_provenance()`` 的口径完全一致。

用法
----
    python tools/rewrite_pin_for_source.py --tag v3.3.0-beta.5.1 --app-version 3.3.0-beta.5.1-1

前置：``.local-build/upstream-bundle-<arch>/ui/`` 已按官方包布局就位（CI 里由
workflow 从源码复制过去）。脚本只改文件、不下载、不打包。
"""
import argparse
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import build  # noqa: E402  （必须先把 PROJECT_DIR 放进 sys.path）


def compute_ui_hash(arches):
    """逐架构算「进包形态」的面板树哈希；两架构不一致就直接失败。"""
    hashes = {}
    for arch in arches:
        src = build._upstream_ui_src(arch)
        digest, entries = build.tree_hash(src, exclude_prefixes=())
        hashes[arch] = digest
        print(f"  面板 [{arch}]: {len(entries)} 个文件, 树哈希 {digest}（{src}）")
    if len(set(hashes.values())) != 1:
        raise SystemExit(
            "ERROR: 两架构的面板树哈希不一致 —— 说明 .local-build/upstream-bundle-<arch>/ui "
            f"不是同一份源码拷出来的: {hashes}"
        )
    return hashes["amd64"]


def rewrite_build_py(upstream_version, ui_hash):
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    text, n1 = re.subn(r'(?m)^UPSTREAM_VERSION\s*=\s*"[^"]+"',
                       f'UPSTREAM_VERSION = "{upstream_version}"', text)
    text, n2 = re.subn(r'(?m)^UPSTREAM_UI_SHA256\s*=\s*"[0-9a-f]{64}"',
                       f'UPSTREAM_UI_SHA256 = "{ui_hash}"', text)
    if n1 != 1:
        raise SystemExit(f"ERROR: build.py 里 UPSTREAM_VERSION 匹配到 {n1} 处，应为 1 处")
    if n2 != 1:
        raise SystemExit(f"ERROR: build.py 里 UPSTREAM_UI_SHA256 匹配到 {n2} 处，应为 1 处")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"  build.py: UPSTREAM_VERSION = {upstream_version}")
    print(f"  build.py: UPSTREAM_UI_SHA256 = {ui_hash}")


def rewrite_manifest(app_version, upstream_tag):
    path = os.path.join(PROJECT_DIR, "manifest")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # 说明三条硬约束：
    #   · changelog 里不能出现 ASCII 分号 —— fnpack 会在那里把整行静默截断；
    #   · 用中文分号/顿号都可以，应用中心按 HTML 渲染，<br> 是唯一换行手段；
    #   · 这一行是**整行覆盖**（与 bump_upstream.py 的行为一致），旧版说明不再累加。
    entry = (
        f"v{app_version}<br>"
        f"1.上游 agent2api {upstream_tag} 暂未发布官方 Release，本包由其 tag 源码交叉编译"
        f"（amd64 原生、arm64 交叉，ubuntu-22.04，glibc 上限 2.36）<br>"
        f"2.修复 v3.3.0-beta.5 headless 启动即 panic（there is no reactor running，"
        f"上游在该 tag 已改用 spawn_task）<br>"
        f"3.面板取自同一 tag 的 desktop-tauri/ui，构建期逐架构重算树哈希，"
        f"与官方包路径走同一套组装与自检<br>"
        f"4.本条由 GitHub Actions 按 tag 源码构建生成"
    )

    text, n1 = re.subn(r'(?m)^version\s*=\s*\S+\s*$', f'version               = {app_version}', text)
    text, n2 = re.subn(r'(?m)^changelog\s*=\s*.*$', 'changelog             = ' + entry, text)
    if n1 != 1 or n2 != 1:
        raise SystemExit(f"ERROR: manifest 改写失败（version 匹配 {n1} 处、changelog 匹配 {n2} 处）")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"  manifest: version = {app_version}")


def main():
    ap = argparse.ArgumentParser(description="从 tag 源码构建时改写上游 pin")
    ap.add_argument("--tag", required=True, help="上游 tag，含 v 前缀（如 v3.3.0-beta.5.1）")
    ap.add_argument("--app-version", required=True,
                    help="本应用版本（如 3.3.0-beta.5.1-1），写入 manifest.version")
    ap.add_argument("--arch", action="append", default=None,
                    help="参与校验的架构，可重复；默认 amd64 与 arm64 各一次")
    args = ap.parse_args()

    if not args.tag.startswith("v"):
        raise SystemExit(f"ERROR: tag 必须以 v 开头: {args.tag!r}")
    upstream_version = args.tag[1:]
    arches = args.arch or ["amd64", "arm64"]

    print(f"[1/2] 重算上游面板树哈希（{', '.join(arches)}）...")
    ui_hash = compute_ui_hash(arches)

    print("[2/2] 改写 build.py 与 manifest ...")
    rewrite_build_py(upstream_version, ui_hash)
    rewrite_manifest(args.app_version, args.tag)
    print("完成")


if __name__ == "__main__":
    main()
