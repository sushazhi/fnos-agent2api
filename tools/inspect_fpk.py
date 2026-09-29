#!/usr/bin/env python3
"""独立核对生成的 fpk —— 不依赖 build.py 的自检逻辑，重新解包逐项检查。

刻意与 build.py 用不同的代码路径读包，这样「两边都说没问题」才有意义：
build.py 的 verify_fpk() 是打包者自检，这里是外部复核。

用法:
    python tools/inspect_fpk.py                    # 检查仓库根下所有 *.fpk
    python tools/inspect_fpk.py <某个.fpk>          # 只检查一个
"""
import io
import json
import os
import re
import struct
import sys
import tarfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_UI = (
    "ui/index.html", "ui/app.js", "ui/icons.js", "ui/islands/ui.js",
    "ui/css/tokens.css", "ui/config", "ui/images/icon_64.png",
    "ui/images/icon_256.png",
)
REQUIRED_ROOT = (
    "LICENSE.upstream", "UPSTREAM.txt", "ICON.PNG", "ICON_256.PNG",
    "config/privilege", "config/resource",
    "wizard/install", "wizard/uninstall", "wizard/upgrade", "wizard/config",
)


def find_fpks():
    if len(sys.argv) > 1:
        return [os.path.abspath(sys.argv[1])]
    return sorted(
        os.path.join(PROJ, n) for n in os.listdir(PROJ) if n.endswith(".fpk")
    )


def check(path):
    print("=" * 72)
    print(f"{os.path.basename(path)}  {os.path.getsize(path):,} B")
    print("=" * 72)

    problems = []
    with tarfile.open(path, "r:gz") as t:
        members = {m.name: m for m in t.getmembers()}

        print("\n[外层 tar 条目]")
        for n in sorted(members):
            m = members[n]
            print(f"  {m.mode:04o}  {m.size:>10,}  {n}")

        print("\n[包内 manifest]")
        mf = t.extractfile("manifest").read().decode("utf-8")
        for line in mf.splitlines():
            if line.strip().startswith((
                "appname", "version", "platform", "service_port", "checkport",
                "desktop_uidir", "desktop_applaunchname", "install_dep_apps",
                "ctl_stop", "os_min_version",
            )):
                print(f"  {line.strip()}")

        # 契约：不该出现 install_dep_apps（本应用不需要 Node 运行时）
        if re.search(r"(?m)^\s*install_dep_apps\s*=", mf):
            problems.append("manifest 里不该有 install_dep_apps（不需要 Node 运行时）")

        inner = tarfile.open(fileobj=io.BytesIO(t.extractfile("app.tgz").read()), mode="r:gz")
        imembers = {m.name: m for m in inner.getmembers() if m.isfile()}
        print(f"\n[app.tgz] 共 {len(imembers)} 个文件")

        print("\n  -- app/bin（ELF 架构须与包架构一致）--")
        for n in sorted(k for k in imembers if k.startswith("bin/")):
            f = imembers[n]
            head = inner.extractfile(f).read(20)
            em = struct.unpack("<H", head[18:20])[0] if len(head) >= 20 else 0
            mach = {0x3E: "x86_64", 0xB7: "AArch64"}.get(em, hex(em))
            is_elf = head[:4] == b"\x7fELF"
            if not is_elf:
                problems.append(f"{n} 不是 ELF")
            print(f"    {f.mode:04o}  {f.size:>10,}  {n}   ELF={is_elf}  {mach}")

        print("\n  -- app/ui 顶层 --")
        tops = sorted({k.split("/")[1] for k in imembers
                       if k.startswith("ui/") and len(k.split("/")) > 1})
        print(f"    {tops}")

        print("\n  -- app/ui 必需文件 --")
        for rel in REQUIRED_UI:
            f = imembers.get(rel)
            if not f:
                problems.append(f"缺少 {rel}")
            print(f"    {'OK ' if f else 'MISS'} {rel}" + (f"  {f.size:,} B" if f else ""))

        cfg = json.loads(inner.extractfile(imembers["ui/config"]).read().decode("utf-8"))
        print("\n  -- ui/config --")
        print("    " + json.dumps(cfg, ensure_ascii=False))

        html = inner.extractfile(imembers["ui/index.html"]).read().decode("utf-8", "replace")
        refs = re.findall(r'(?i)\b(?:src|href)\s*=\s*"([^"]+)"', html)
        rel_refs = [r for r in refs
                    if not re.match(r'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//|/)', r)]
        print(f"\n  -- index.html: {len(refs)} 引用, {len(rel_refs)} 个裸相对路径 --")
        print(f"    前 6 个相对: {rel_refs[:6]}")
        print(f"    含 <head>: {'<head>' in html}")
        if "<head>" not in html:
            problems.append("index.html 里没有 <head>，fngateway 的注入点会失效")

        print("\n[外层 cmd/* 权限（须 0755）]")
        for n in sorted(k for k in members if k.startswith("cmd/")):
            mode = members[n].mode
            if mode != 0o755:
                problems.append(f"{n} 权限 {mode:04o}，应为 0755")
            print(f"  {mode:04o}  {n}")

        print("\n[根文件齐备]")
        for n in REQUIRED_ROOT:
            if n not in members:
                problems.append(f"缺少 {n}")
            print(f"  {'OK ' if n in members else 'MISS'} {n}")

        if "UPSTREAM.txt" in members:
            print("\n[UPSTREAM.txt 内容]")
            print(t.extractfile("UPSTREAM.txt").read().decode("utf-8"))

    print("\n[结论]")
    if problems:
        for p in problems:
            print(f"  PROBLEM: {p}")
        return False
    print("  全部检查通过")
    return True


def main():
    fps = find_fpks()
    if not fps:
        print("没找到 .fpk。先跑 python build.py。", file=sys.stderr)
        sys.exit(1)
    ok = all(check(p) for p in fps)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
