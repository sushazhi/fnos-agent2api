#!/usr/bin/env python3
"""静态校验 GitHub Actions workflow —— 抓那些「推上去才炸」的错误。

CI 是这套流程的关键路径：本机不编 Rust，二进制只能从 Actions 拿。
workflow 写错了要等到推送后跑一次才知道，而一次跑要十几分钟，
所以能静态查的先在这里查掉。

检查项：
  1. YAML 可解析（缩进/tab/引号错误）
  2. job 依赖 (needs) 指向存在的 job
  3. 每个 job 的 runs-on 存在
  4. run 步骤里引用的仓库内脚本/文件确实存在
  5. 版本号读取方式与 manifest 实际格式一致
  6. artifact 名上下游一致（下载的必须在本 workflow 内被上传过）
  7. 全仓至少有一个 workflow 产出关键 artifact（linux-binaries / agent2api-fpk）
  8. `gh workflow run <文件>` 指向存在的 workflow，且该 workflow 可被手动派发
  9. cron 表达式是合法的 5 段式
 10. `${{ inputs.X }}` 的 X 必须在 workflow_dispatch / workflow_call 里声明
 11. run: 里的 shell 脚本能通过 `bash -n`（本机找不到 bash 时跳过）

 12. `uses:` 都带版本，且同一个 action 在各 workflow 间版本一致

用法:
    python tools/check_workflow.py
"""
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(PROJ, ".github", "workflows")

# 这几个 artifact 是交付物本身：全仓至少得有一个 workflow 产出它们，
# 否则用户下载不到包。（早先的版本要求**每个** workflow 文件里都有，
# 加了 check-upstream.yml 这种只做检查、不产包的 workflow 之后，
# 那条规则就不再成立了。）
REQUIRED_ARTIFACTS = ("linux-binaries", "agent2api-fpk")

try:
    import yaml
except ImportError:
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_triggers(doc):
    """取出 on: 的内容。

    PyYAML 按 YAML 1.1 解析，裸写的 `on:` 会被当成布尔 True，
    所以两种键都要认。
    """
    for key in ("on", True):
        if key in doc:
            return doc[key]
    return None


def trigger_info(trig):
    """从 on: 里提取 cron 列表、是否可派发、已声明的 input 名。"""
    crons, declared, has_dispatch = [], set(), False

    def note_dispatch(spec):
        nonlocal has_dispatch
        has_dispatch = True
        if isinstance(spec, dict):
            for key in ("inputs",):
                declared.update((spec.get(key) or {}).keys())

    if isinstance(trig, dict):
        sched = trig.get("schedule")
        for e in (sched if isinstance(sched, list) else [sched]):
            if isinstance(e, dict) and e.get("cron"):
                crons.append(str(e["cron"]))
        if "workflow_dispatch" in trig:
            note_dispatch(trig.get("workflow_dispatch"))
        # 可复用 workflow 的输入同样通过 inputs 上下文传入
        if "workflow_call" in trig:
            note_dispatch(trig.get("workflow_call"))
    elif isinstance(trig, list):
        has_dispatch = "workflow_dispatch" in trig
    elif isinstance(trig, str):
        has_dispatch = trig == "workflow_dispatch"

    return crons, has_dispatch, declared


CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-]+$")


def check_cron(expr):
    fields = expr.split()
    if len(fields) != 5:
        return f"cron 应为 5 段，实际 {len(fields)} 段: {expr!r}"
    for f in fields:
        if not CRON_FIELD.match(f):
            return f"cron 段 {f!r} 含非法字符: {expr!r}"
    return None


def collect_artifacts(jobs):
    """按 steps 结构取 upload/download-artifact 的 name，比正则扫全文可靠。"""
    uploads, downloads = {}, {}
    for jname, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for st in (job.get("steps") or []):
            if not isinstance(st, dict):
                continue
            uses = str(st.get("uses") or "")
            with_ = st.get("with") or {}
            name = with_.get("name")
            if not name:
                continue
            if "upload-artifact" in uses:
                uploads.setdefault(str(name), []).append(jname)
            elif "download-artifact" in uses:
                downloads.setdefault(str(name), []).append(jname)
    return uploads, downloads


def find_bash():
    """找一个 bash 用于 `bash -n`。

    Windows 上 bash 通常不在 PATH 里（Git for Windows 只把它放在 bin/ 下），
    所以要显式找几个常见位置；找不到就跳过这项检查而不是报错 —— 这台机器上
    shell 语法检查是加分项，不该让整个 linter 跑不起来。
    """
    exe = shutil.which("bash")
    if exe:
        return exe
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe",
                 r"C:\Program Files (x86)\Git\bin\bash.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe")):
        if os.path.exists(cand):
            return cand
    return None


def check_shell(raw, doc, wf):
    """对每个 run: 块做 `bash -n` 语法检查。

    YAML 能解析不代表 shell 能解析：引号不配对、if/fi 不匹配这类错误在 YAML
    层面完全合法，却要到 CI 跑起来才炸。这里提前抓。
    """
    bash = find_bash()
    if bash is None:
        print("  shell 语法: SKIP（本机找不到 bash）")
        return []
    problems, checked = [], 0
    for jname, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for i, st in enumerate(job.get("steps") or []):
            if not isinstance(st, dict):
                continue
            script = st.get("run")
            if not script:
                continue
            checked += 1
            # shell: 不是 bash 的步骤（如 pwsh/python）不能拿 bash 检
            shell = str(st.get("shell") or "")
            if shell and "bash" not in shell and "sh" not in shell:
                continue
            p = subprocess.run([bash, "-n", "-c", script],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if p.returncode != 0:
                label = st.get("name") or f"step{i}"
                problems.append(f"{wf}: {jname} 的步骤「{label}」shell 语法错误: "
                                f"{(p.stderr or '').strip().splitlines()[:1]}")
    print(f"  shell 语法: OK（bash -n 检查了 {checked} 个 run 块）"
          if not problems else f"  shell 语法: FAIL {len(problems)} 处")
    return problems


def check_action_pins(raw, doc, wf, seen):
    """`uses:` 必须带版本；同一个 action 在各 workflow 之间版本要一致。

    不带版本的 `uses: actions/checkout` 在 GitHub 上会被拒绝（或按浮动标签
    解析），而同一 action 在不同文件里版本不一致，会导致「A 文件能跑、
    B 文件跑不动」这种难查的差异。
    """
    problems, found = [], 0
    for jname, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for st in (job.get("steps") or []):
            if not isinstance(st, dict):
                continue
            uses = str(st.get("uses") or "")
            if not uses or uses.startswith("./") or uses.startswith("docker://"):
                continue
            found += 1
            if "@" not in uses:
                problems.append(f"{wf}: {jname} 的 `uses: {uses}` 没带版本号")
                continue
            action, ref = uses.rsplit("@", 1)
            if not ref.strip():
                problems.append(f"{wf}: {jname} 的 `uses: {uses}` 版本号为空")
                continue
            prev = seen.get(action)
            if prev and prev[0] != ref:
                problems.append(
                    f"{wf}: action '{action}' 版本是 @{ref}，"
                    f"但 {prev[1]} 里用的是 @{prev[0]} —— 请统一")
            else:
                seen[action] = (ref, wf)
    print(f"  uses 版本: OK（{found} 处引用）" if not problems
          else f"  uses 版本: FAIL {len(problems)} 处")
    return problems


def main():
    problems = []
    wfs = [f for f in sorted(os.listdir(WF_DIR)) if f.endswith((".yml", ".yaml"))]
    if not wfs:
        print(f"ERROR: {WF_DIR} 下没有 workflow", file=sys.stderr)
        sys.exit(1)

    # 先全部解析一遍：跨文件检查（gh workflow run 的目标）要用到。
    parsed = {}
    for wf in wfs:
        path = os.path.join(WF_DIR, wf)
        raw = read_text(path)
        try:
            parsed[wf] = (raw, yaml.safe_load(raw))
        except yaml.YAMLError as e:
            problems.append(f"{wf}: YAML 解析失败: {e}")

    # manifest 版本读取演练（与 workflow 无关，做一次即可）
    mf = read_text(os.path.join(PROJ, "manifest"))
    vlines = [l for l in mf.splitlines() if l.strip().startswith("version")]
    got = re.sub(r"^version\s*=\s*", "", vlines[0]).strip() if vlines else ""
    print(f"manifest version 读取演练: {got!r}")
    if not re.fullmatch(r"\d+\.\d+\.\d+(-\d+)?", got):
        problems.append(f"manifest: 版本号格式异常 {got!r}")
    print()

    all_uploads = {}
    seen_actions = {}

    for wf in wfs:
        print("=" * 66)
        print(wf)
        print("=" * 66)
        if wf not in parsed:
            print("  YAML: FAIL（见下方问题列表）\n")
            continue
        raw, doc = parsed[wf]
        print("  YAML 解析: OK")

        if not isinstance(doc, dict) or "jobs" not in doc:
            problems.append(f"{wf}: 顶层缺 jobs")
            continue

        jobs = doc["jobs"]
        if not isinstance(jobs, dict):
            problems.append(f"{wf}: jobs 不是映射")
            continue

        # 2. needs 指向存在的 job  /  3. runs-on
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
            if "runs-on" not in job:
                problems.append(f"{wf}: job {jname} 缺 runs-on")
            print(f"  job {jname:12s} runs-on={job.get('runs-on')} "
                  f"needs={job.get('needs')} steps={len(job.get('steps', []))}")

        # 4. run 步骤里引用的仓库内文件
        missing = []
        for m in re.finditer(r"(?:python3?|bash|sh)\s+([\w./\-]+\.(?:py|sh))", raw):
            ref = m.group(1)
            if ref.startswith(("/", "http")):
                continue
            if not os.path.exists(os.path.join(PROJ, ref)):
                missing.append(ref)
                problems.append(f"{wf}: 引用了不存在的脚本 {ref}")
        print("  引用的仓库内脚本: OK" if not missing
              else f"  引用的仓库内脚本: FAIL {missing}")

        # 6. artifact 名上下游一致
        uploads, downloads = collect_artifacts(jobs)
        all_uploads.update({k: wf for k in uploads})
        print(f"  上传 artifact: {sorted(uploads) or '（无）'}")
        for name, jnames in downloads.items():
            if name not in uploads:
                problems.append(
                    f"{wf}: job {jnames} 下载了 artifact '{name}'，"
                    f"但本 workflow 里没有对应的 upload（跨 workflow 取 artifact 是不可能的）")
        for name, jnames in uploads.items():
            if name in downloads:
                print(f"    {name}: 由 {jnames} 上传 → 由 {downloads[name]} 下载")

        # 8. gh workflow run <文件>
        for m in re.finditer(r"gh\s+workflow\s+run\s+([\w.\-/]+\.ya?ml)", raw):
            target = m.group(1)
            if not os.path.exists(os.path.join(WF_DIR, target)):
                problems.append(f"{wf}: `gh workflow run {target}` 指向不存在的 workflow")
                continue
            tdoc = parsed.get(target, (None, None))[1]
            if not isinstance(tdoc, dict):
                continue
            _, has_dispatch, _ = trigger_info(get_triggers(tdoc))
            if not has_dispatch:
                problems.append(
                    f"{wf}: 派发 {target}，但该 workflow 没有 workflow_dispatch 触发器，"
                    f"`gh workflow run` 会 422 失败")
            else:
                print(f"  派发目标 {target}: 存在且可手动派发 OK")
        for m in re.finditer(r"--workflow[= ]([\w.\-/]+\.ya?ml)", raw):
            target = m.group(1)
            if not os.path.exists(os.path.join(WF_DIR, target)):
                problems.append(f"{wf}: `--workflow={target}` 指向不存在的 workflow")

        # 9. cron
        crons, has_dispatch, declared = trigger_info(get_triggers(doc))
        if crons:
            print(f"  定时任务: {crons}")
        for c in crons:
            err = check_cron(c)
            if err:
                problems.append(f"{wf}: {err}")

        # 10. inputs.X 必须已声明
        used = set(re.findall(r"\$\{\{\s*inputs\.([A-Za-z_][\w\-]*)\s*\}\}", raw))
        undeclared = sorted(used - declared)
        if undeclared:
            problems.append(
                f"{wf}: 引用了未声明的 input {undeclared} —— "
                f"on.workflow_dispatch.inputs 里只声明了 {sorted(declared) or '（空）'}")
        elif used:
            print(f"  引用的 inputs: {sorted(used)}（均已声明）OK")

        # 11. shell 语法
        problems.extend(check_shell(raw, doc, wf))

        # 12. uses 版本
        problems.extend(check_action_pins(raw, doc, wf, seen_actions))

        print()

    # 7. 全仓关键 artifact
    for a in REQUIRED_ARTIFACTS:
        if a not in all_uploads:
            problems.append(f"没有任何 workflow 产出关键 artifact '{a}'")

    print("=" * 66)
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("workflow 静态检查通过")


if __name__ == "__main__":
    main()
