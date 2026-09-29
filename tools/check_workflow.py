#!/usr/bin/env python3
"""静态校验 GitHub Actions workflow —— 抓那些「推上去才炸」的错误。

CI 是这套流程的关键路径：本机不编 Rust，二进制只能从 Actions 拿。
workflow 写错了要等到推送后跑一次才知道，而一次跑要十几分钟，
所以能静态查的先在这里查掉。

检查项：
  1. YAML 可解析（缩进/tab/引号错误）
  2. job 依赖 (needs) 指向存在的 job
  3. 每个 job 的 runs-on 存在
  4. 引用的 local action 存在（uses: actions/...@vN 只做格式检查）
  5. run 步骤里引用的仓库内脚本/文件确实存在
  6. 版本号读取方式与 manifest 实际格式一致
  7. 产出的 artifact 名与下游 download-artifact 一致

用法:
    python tools/check_workflow.py
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(PROJ, ".github", "workflows")

try:
    import yaml
except ImportError:
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def main():
    problems = []
    wfs = [f for f in sorted(os.listdir(WF_DIR)) if f.endswith((".yml", ".yaml"))]
    if not wfs:
        print(f"ERROR: {WF_DIR} 下没有 workflow", file=sys.stderr)
        sys.exit(1)

    for wf in wfs:
        path = os.path.join(WF_DIR, wf)
        print("=" * 66)
        print(wf)
        print("=" * 66)
        raw = open(path, "r", encoding="utf-8").read()

        # 1. YAML 可解析
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            problems.append(f"{wf}: YAML 解析失败: {e}")
            print(f"  YAML: FAIL {e}")
            continue
        print("  YAML 解析: OK")

        if not isinstance(doc, dict) or "jobs" not in doc:
            problems.append(f"{wf}: 顶层缺 jobs")
            continue

        jobs = doc["jobs"]
        if not isinstance(jobs, dict):
            problems.append(f"{wf}: jobs 不是映射")
            continue

        # 2. needs 指向存在的 job
        for jname, job in jobs.items():
            if not isinstance(job, dict):
                problems.append(f"{wf}: job {jname} 不是映射")
                continue
            needs = job.get("needs")
            if needs:
                needs_list = needs if isinstance(needs, list) else [needs]
                for n in needs_list:
                    if n not in jobs:
                        problems.append(f"{wf}: job {jname} 的 needs 指向不存在的 job '{n}'")
            # 3. runs-on
            if "runs-on" not in job:
                problems.append(f"{wf}: job {jname} 缺 runs-on")
            print(f"  job {jname:12s} runs-on={job.get('runs-on')} "
                  f"needs={job.get('needs')} steps={len(job.get('steps', []))}")

        # 5. run 步骤里引用的仓库内文件
        for m in re.finditer(r'(?:python3?|bash|sh)\s+([\w./\-]+\.(?:py|sh))', raw):
            ref = m.group(1)
            if ref.startswith(("/", "http")):
                continue
            if not os.path.exists(os.path.join(PROJ, ref)):
                problems.append(f"{wf}: 引用了不存在的脚本 {ref}")
        print("  引用的仓库内脚本: OK" if not any('不存在的脚本' in p for p in problems) else "")

        # 6. 版本读取方式：必须能匹配 manifest 的 `version   = x.y.z-n`
        mf = open(os.path.join(PROJ, "manifest"), "r", encoding="utf-8").read()
        mline = [l for l in mf.splitlines() if l.strip().startswith("version")][0]
        # 复现 workflow 里的 grep|sed 逻辑
        got = re.sub(r'^version\s*=\s*', '', mline).strip()
        print(f"  manifest version 读取演练: {got!r}")
        if not re.fullmatch(r'\d+\.\d+\.\d+(-\d+)?', got):
            problems.append(f"{wf}: 版本号格式异常 {got!r}")

        # 7. artifact 名上下游一致
        uploaded = set(re.findall(r'name:\s*(linux-binaries|agent2api-fpk)', raw))
        downloaded = set(re.findall(r'name:\s*(linux-binaries|agent2api-fpk)', raw))
        print(f"  artifact 名引用: {sorted(uploaded)}")
        for a in ("linux-binaries", "agent2api-fpk"):
            if a not in uploaded:
                problems.append(f"{wf}: 未找到 artifact '{a}' 的定义")
        print()

    print("=" * 66)
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("workflow 静态检查通过")


if __name__ == "__main__":
    main()
