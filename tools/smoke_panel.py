#!/usr/bin/env python3
"""面板/服务端装配冒烟：这个二进制能不能把**这份面板**服务起来。

为什么单独做这一步
------------------
`build.py` 的预检只看文件树（引用是否闭合、i18n 是否齐、图标是否在），
`binaries job` 的冒烟只看二进制自己（`/health` 200、没有 reactor panic）——
两者都覆盖不到「把这份 ui/ 交给这个二进制，入口页和它引用的资源是不是真的
能取到」。而这正是「面板能打包、装上去半残」的中间地带。

面板从 2026-10-20 起不再进仓库：`app/ui/` 只有本项目的 `config` 与
`images/`，上游面板由 `build.stage_ui()` 在构建时注入。所以**直接拿
`--ui app/ui` 跑本脚本一定 404**（那不是一个完整面板，三条入口页都不存在）。
默认行为因此是「现场按进包形态装配」：上游面板
（`.local-build/upstream-bundle-<arch>/ui/`）+ 本项目条目装进临时目录，再交给
二进制 —— 与 .fpk 里的那份同形。

本脚本在 CI 的 package job 里跑（那里二进制与上游面板都已就位）：

  1. 装配（或使用 `--ui` 指定的）完整面板目录，先静态确认三个入口页都在；
  2. 用 `AGENT2API_UI_DIR` 指向它启动上游二进制；
  3. `/health` 必须就绪；
  4. `/`、`/login.html`、`/m.html` 必须 200；
  5. 入口页里**每个站内相对引用**（css/js/图标/i18n 词典…）都必须 200 ——
     这一条直接对应「子路径部署下 404 白屏」的历史事故；
  6. `/api/panel/status` 不能 404（401/403 说明路由在，只是没登录）。

它**不**验证旧面板 + 新服务端的业务语义（接口字段/流程变化）—— 那需要真机
或浏览器级测试，属于另一层。这里只回答一个能确定性回答的问题：装配有没有
把「页面要的东西」服务出来。

用法：
    # 默认：现场装配进包形态面板（CI 的 package job 走这条）
    python3 tools/smoke_panel.py \\
        --bin .local-build/bin/agent2api-server-linux-amd64

    # 已有完整面板目录（例如手工解包出来的 ui/）时直接指定
    python3 tools/smoke_panel.py --bin <bin> --ui /path/to/ui
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build  # noqa: E402  （复用 _local_asset_refs / stage_ui，口径与构建一致）

# 入口页：(URL 路径, 面板目录里的文件名)。前者进 HTTP 请求，后者做静态预检。
ENTRY_PAGES = (
    ("/", "index.html"),
    ("/login.html", "login.html"),
    ("/m.html", "m.html"),
)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url, timeout=5.0):
    """返回 (status, body_bytes)。连接失败返回 (0, b"")。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except Exception:
        return 0, b""


def wait_health(base, proc, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http_get(base + "/health", timeout=2)
        if status == 200:
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def stage_packaged_ui(dst):
    """按进包形态装配面板：上游面板 + 本项目 app/ui 本地条目。"""
    print("  未指定 --ui：现场装配进包形态面板（上游面板 + 本项目条目）...")
    build.stage_ui("amd64", dst)


def check_entry_files(ui):
    """静态确认三个入口页都在。

    这一步是**诊断**，不是冗余检查：入口页缺失时二进制只会回 404，而 404
    读起来像「服务端坏了」，实际是「给它的根本不是完整面板」。先在这里说清楚。
    """
    missing = [name for _, name in ENTRY_PAGES
               if not os.path.isfile(os.path.join(ui, name))]
    if not missing:
        return True
    print(f"ERROR: 这不是一份完整面板（缺 {', '.join(missing)}）: {ui}")
    print("  仓库里的 app/ui/ 只有本项目条目（config、images/），上游面板由")
    print("  build.stage_ui() 在构建时注入 —— 直接把它交给二进制必然白屏。")
    print("  省略 --ui 让本脚本现场装配，或先跑：")
    print("    python build.py --sync-upstream --materialize-ui")
    print("  再把 --ui 指向 app/ui。")
    return False


def run(args, tmp):
    if args.ui:
        ui = os.path.abspath(args.ui)
        if not os.path.isdir(ui):
            print(f"ERROR: 找不到面板目录: {ui}")
            return 1
    else:
        ui = os.path.join(tmp, "ui")
        stage_packaged_ui(ui)

    if not check_entry_files(ui):
        return 1
    file_count = sum(len(files) for _, _, files in os.walk(ui))
    print(f"  面板目录: {ui}（{file_count} 个文件）")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = os.path.join(tmp, "server.log")
    log = open(log_path, "wb")

    env = os.environ.copy()
    env.update({
        "AGENT2API_PROXY_HOME": os.path.join(tmp, "data"),
        "AGENT2API_HOST": "127.0.0.1",
        "AGENT2API_PROXY_PORT": str(port),
        "AGENT2API_UI_DIR": ui,
        "AGENT2API_ALLOW_NO_KEY": "1",
        "AGENT2API_CAPTCHA_ENABLED": "0",
        "HOME": os.path.join(tmp, "home"),
    })
    os.makedirs(env["AGENT2API_PROXY_HOME"], exist_ok=True)
    os.makedirs(env["HOME"], exist_ok=True)

    proc = subprocess.Popen([os.path.abspath(args.bin)], cwd=tmp, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    problems = []
    try:
        if not wait_health(base, proc):
            problems.append("/health 25 秒内未就绪（进程可能已退出）")
        else:
            # 1) 入口页
            pages = {}
            for page, _ in ENTRY_PAGES:
                status, body = http_get(base + page)
                pages[page] = body
                if status != 200 or not body:
                    problems.append(f"{page}: HTTP {status or '连接失败'}"
                                    f"（入口页取不到，面板直接白屏）")
            # 2) 入口页引用的每个站内相对资源
            checked = 0
            missing = []
            for page, body in pages.items():
                if not body:
                    continue
                html = body.decode("utf-8", "replace")
                for ref in build._local_asset_refs(html):
                    checked += 1
                    status, _ = http_get(base + "/" + ref.lstrip("/"), timeout=4)
                    if status != 200:
                        missing.append(f"{ref} (HTTP {status or '连接失败'}, 被 {page} 引用)")
            if missing:
                problems.append("入口页引用的资源取不到: " + ", ".join(sorted(set(missing))[:8]))
            # 3) 面板 API 的路由存在性（不校验业务语义：401/403 也算在）
            status, _ = http_get(base + "/api/panel/status")
            if status == 404:
                problems.append("/api/panel/status: 404（面板启动要用的路由不存在）")
            print(f"  入口页 3 个，静态引用 {checked} 个，/api/panel/status -> {status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()

    if problems:
        print("ERROR: 面板/服务端装配冒烟未通过：")
        for p in problems:
            print(f"  - {p}")
        print(f"  服务端日志（{log_path}，尾部 40 行）：")
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-40:]:
                print("    " + line.rstrip())
        return 1
    print("装配冒烟通过：入口页与引用资源均可取，status 路由存在")
    return 0


def main():
    ap = argparse.ArgumentParser(description="面板/服务端装配冒烟")
    ap.add_argument("--bin", required=True, help="上游 agent2api-server 二进制")
    ap.add_argument("--ui",
                    help="完整面板目录；省略则用 build.stage_ui() 现场装配进包形态")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = ap.parse_args()

    if not os.path.isfile(args.bin):
        print(f"ERROR: 找不到二进制: {args.bin}")
        return 1

    tmp = tempfile.mkdtemp(prefix="panel-smoke-")
    try:
        code = run(args, tmp)
    except BaseException:
        # 装配/启动阶段的意外（含 build.stage_ui 的 sys.exit）保留现场，便于排查。
        print(f"  临时目录保留: {tmp}")
        raise
    # 失败时同样保留：服务端日志是唯一能说明「为什么 404」的证据（与旧行为一致）。
    if code == 0 and not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"  临时目录保留: {tmp}")
    return code


if __name__ == "__main__":
    sys.exit(main())
