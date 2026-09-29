#!/usr/bin/env python3
"""计算 app/ui 里「上游派生文件」的确定性树哈希。

用来核对仓库里提交的面板副本是否仍与上游 tag 逐字节一致。build.py 里的
UPSTREAM_UI_SHA256 就是这个值；上游改动面板时它会变，构建期会硬失败。

排除本项目自己加的 config 与 images/（桌面入口与图标，不属于上游 WebUI）。
哈希 = sha256( 逐行 "sha256  相对路径\\n"，按路径排序 )。

用法:
    python tools/ui_hash.py            # 算 app/ui
    python tools/ui_hash.py <目录>      # 算任意目录
    python tools/ui_hash.py <目录> -v   # 附带逐文件哈希
"""
import hashlib
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_PREFIXES = ("config", "images")


def tree_hash(root, exclude_prefixes=EXCLUDE_PREFIXES):
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


def main():
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else os.path.join(PROJ, "app", "ui")
    h, entries = tree_hash(root)
    print(f"root   : {root}")
    print(f"files  : {len(entries)}")
    print(f"sha256 : {h}")
    if "-v" in sys.argv[2:]:
        for rel, fh in entries:
            print(f"  {fh[:16]}  {rel}")


if __name__ == "__main__":
    main()
