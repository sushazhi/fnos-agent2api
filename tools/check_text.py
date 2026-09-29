#!/usr/bin/env python3
"""检查仓库里文本文件的编码与换行 —— 专门防「Windows 上好好的，Linux 上炸」。

飞牛侧对这两件事很敏感：
  * cmd/* 与 wizard/* 必须是 LF。CRLF 会让 shebang 与命令解析失效，
    表现为「装完启动不了」，而本地用编辑器看完全正常。
  * 所有文本文件必须是 UTF-8（无 BOM）。manifest/wizard 里的中文若有 BOM，
    fnpack 解析出的第一个键名会带 \ufeff 前缀而静默失配。

用法:
    python tools/check_text.py           # 检查仓库（跳过 .git / .local-build）
    python tools/check_text.py -v        # 逐文件打印
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".local-build", "__pycache__", "node_modules", "tools"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".fpk", ".gz", ".tgz",
              ".zip", ".exe", ".so", ".dylib", ".db", ".woff", ".woff2"}

# 这些子目录在 Linux 上执行，必须严格 LF
MUST_BE_LF = ("cmd/", "wizard/")
MUST_BE_EXEC = ("cmd/",)

# 上游 WebUI 的逐字节副本。只对它查编码（BOM / 非法 UTF-8），
# **不**查末尾换行与 CRLF —— 上游有些文件本来就没有末尾换行，
# 「修正」它们会改变字节、打破 build.py 里的 UPSTREAM_UI_SHA256 溯源。
VENDORED = ("app/ui/",)


def iter_files():
    for dirpath, dirnames, filenames in os.walk(PROJ):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            yield os.path.join(dirpath, fn)


def main():
    verbose = "-v" in sys.argv
    problems = []
    checked = 0

    for path in iter_files():
        rel = os.path.relpath(path, PROJ).replace(os.sep, "/")
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXT:
            continue
        with open(path, "rb") as f:
            b = f.read()
        if b"\x00" in b[:8192]:
            continue  # 看起来是二进制，跳过
        checked += 1

        notes = []
        if b[:3] == b"\xef\xbb\xbf":
            problems.append(f"{rel}: 有 UTF-8 BOM")
            notes.append("BOM!")
        try:
            b.decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append(f"{rel}: 不是合法 UTF-8 ({e})")
            notes.append("NOT-UTF8")
            continue

        crlf = b.count(b"\r\n")
        vendored = rel.startswith(VENDORED)
        if crlf and rel.startswith(MUST_BE_LF):
            problems.append(f"{rel}: 有 {crlf} 个 CRLF（该目录必须 LF）")
            notes.append(f"CRLF={crlf}")
        if b and not b.endswith(b"\n") and not vendored:
            problems.append(f"{rel}: 末尾缺换行")
            notes.append("no-EOL")

        if rel.startswith(MUST_BE_EXEC) and os.name != "nt":
            if not (os.stat(path).st_mode & 0o111):
                problems.append(f"{rel}: 缺少可执行位")

        if verbose:
            print(f"  {rel:52s} {len(b):>8,} B  LF={b.count(chr(10).encode())}  CRLF={crlf}"
                  + ("  <- " + ",".join(notes) if notes else ""))

    print(f"检查了 {checked} 个文本文件")
    if problems:
        print(f"\n发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("编码 / 换行 / 末尾换行 / 可执行位：全部通过")


if __name__ == "__main__":
    main()
