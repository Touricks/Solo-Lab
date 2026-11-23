#!/usr/bin/env python3
"""Initialise a project directory for the agent organisation (the plugin's init step).

    python3.12 -I scripts/setup.py announce --target DIR [--name NAME | --task task.json] [--sequences ...]
    python3.12 -I scripts/setup.py init --target DIR --task task.json [--sequences product,alg,...] [--apply]
    python3.12 -I scripts/setup.py login [--wait]
    python3.12 -I scripts/setup.py env --target DIR [--apply]

What the project should look like is setup_core.py's. The checks are check.py (precheck, check), the binding
to the Feishu space is space.py (feishu, template), the list of sessions to open is pool.py open.

announce says what an initialisation is about to create: the project space and the knowledge
space by name, the knowledge-base nodes, the first research task, the sessions and the files in
the project directory. It reads local files only and writes nothing.

init renders agents, project settings (permissions and hooks), CLAUDE.md, org/,
packages/, inbox/, spec/ and a copy of the plugin's scripts and rules into
.claude/solo-lab/. Without --apply it only previews. Existing files are never
overwritten: a different version goes next to them as <file>.new. Running it
twice with the same inputs writes nothing.

Every role runs as a desktop session the researcher can see (the session pool): init adds
the pool hooks, .claude/solo-lab/pool-plan.json and the draft of a first research-task
sheet. A task file may say "mode": "pool"; any other mode is refused.

login starts a device-code login of the command-line tool whose login is gone and prints a
QR code and a link for the researcher; --wait reads the login back. No device code, password
or token is printed. env writes the links of the project space and the knowledge space into
the project's .env and touches no other line of it.
Python 3.12 standard library only (1.0.12: until then 3.9, the python3 macOS ships).
"""
import argparse
import datetime
import difflib
import json
import os
import re
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import setup_core  # noqa: E402

def load_task(path):
    return setup_core.load_task_and_seeds(path)[0]


def project_dir(path):
    """The directory to initialise, as an absolute path: not the home directory, not the root."""
    target = os.path.abspath(path)
    if os.path.realpath(target) in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(os.sep)):
        raise setup_core.SetupError("不在家目录或根目录里初始化：换一个只放这个项目的目录")
    return target


# what init writes into the project directory, by top-level name
LOCAL_TOPS = ("CLAUDE.md", ".claude/", "feishu/", "inbox/", "org/", "packages/", "runs/", "spec/")


def announce(target, name=None, task_path=None, seq_arg=None):
    """`setup.py announce`: what the researcher's opening sentence lets this initialisation create,
    said once before anything is made. The counts come from the rules and the pool plan, the names
    from the task file when there is one, else from the project's name. Reads local files only."""
    spec = orgspec.OrgSpec()
    known = os.path.join(target, "spec", "task.json")
    again = not task_path and os.path.isfile(known)
    if task_path:
        task = load_task(task_path)
    elif again:                                    # initialised before: the settings it was initialised with
        task = load_task(known)
        seq_arg = seq_arg or ",".join(setup_core.enabled_sequences(target) or [])
    else:
        setup_core.refuse_other_runtime(target)
        name = (name or "").strip()
        if not name:                               # a directory's name is no project's name: it may not follow any rule
            raise setup_core.SetupError("还没有项目名：先问研究员这个项目叫什么，再用 --name 传进来。研究员只说了飞书项目空间或知识空间的名字的，"
                             "就用那个名字。它会用作项目名、两个空间的名字和会话标题的前缀，不拿目录名代替")
        task = setup_core.parse_task({"name": name, "description": "待立项访谈补充", "feishu": {"meegle_space": name}})[0]
    setup_core.refuse_other_runtime(target)
    seqs = setup_core.pick_sequences(spec, seq_arg)
    policy = setup_core.build_policy(spec, task, seqs)
    setup_core.settle_later(task, seqs)
    ids = setup_core.load_ids(target)
    plan = setup_core.pool_plan(spec, task, policy, seqs, ids)
    later = "、".join(spec.term("seq", s) for s in task["pool"]["later"])
    rounds = len(plan["rounds"])
    asks = []
    if rounds == 2:
        asks.append("会话分 2 轮开，第 1 轮开完回一句“%s”" % setup_core.RESUME_WORDS)
    elif rounds > 2:
        asks.append("会话分 %d 轮开，每两轮之间回一句“%s”，共 %d 次" % (rounds, setup_core.RESUME_WORDS, rounds - 1))
    asks += ["飞书命令行的登录失效时扫码", "应用弹出确认框时点一下"]
    print("把下面的清单原样发给研究员，一个字不改；发出去以后再运行下一条命令。")
    print("")
    print("\n".join([
        "将要新建（已经有的直接沿用，不重复建）：",
        "- 飞书项目空间 1 个：%s，用模板创建" % setup_core.space_names(task, ids)[0],
        "- 飞书知识空间 1 个：%s" % setup_core.space_names(task, ids)[1],
        "- 知识库节点 %d 个" % len(spec.node_keys(seqs)),
        "- 研究任务 1 条和它的主文档",
        "- 桌面会话 %d 个%s，侧栏分区 1 个：%s" % (plan["total"], "（%s只开 +1）" % later if later else "", plan["label"]),
        "- 项目目录 %s 里的 %s，以及 .env 里的两行链接" % (target, "、".join(LOCAL_TOPS)),
        "中途要你做的：%s。" % "；".join(asks),
        "权限模式：本会话用 auto 模式时，开池不用你点。用 bypass 模式时，应用每开一个会话都要你点一次确认，这个池共 %d 次。" % plan["total"],
        "新建以外的动作（删除、覆盖、改已有的对象、改分享权限）会先停下问你。"]))
    if again:
        print("这个目录已经初始化过：名字和会话数照它的 spec/task.json。")
    return 0


LABELS = {"create": "新建", "same": "相同，跳过", "chmod": "补可执行权限", "update": "更新（上次生成后没人改过）",
          "new": "本地改过，新版本写 .new", "paired": "和改过的那个文件配套，新版本也写 .new",
          "pending": "内容不同，.new 已存在", "kept": "已有，研究员维护，不动",
          "regen": "更新（照空间快照和现在的规则重新生成）"}


def manifest_text(plan, acts):
    m = dict(plan.manifest)
    m["pending_new"] = sorted(rel for kind, rel, _ in acts if kind in ("new", "pending", "paired"))
    files = dict(m["files"])
    for kind, rel, current in acts:                  # a paired file keeps its old hash: once its partner is
        if kind == "paired" and current is not None:  # merged, the next init updates it in place
            files[rel] = setup_core.sha256(current)
    m["files"] = files
    return setup_core.dump_json(m)


def report(target, plan, acts, apply):
    print("目标目录：%s" % target)
    for n in plan.notes:
        print(n)
    counts = {}
    for kind, rel, current in acts:
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("same", "kept"):
            continue
        print("  [%s] %s" % (LABELS[kind], rel))
        if kind in ("new", "update", "paired", "regen"):
            diff = difflib.unified_diff((current or "").splitlines(True), plan.files[rel][0].splitlines(True),
                                        rel, rel + ".new", n=1)
            sys.stdout.writelines("      " + line for line in list(diff)[:60])
    print("合计：" + "，".join("%s %d" % (LABELS[k], v) for k, v in sorted(counts.items())))
    paired = {rel for kind, rel, _ in acts if kind == "paired"}
    if paired.intersection(setup_core.HOOK_CODE):
        print("提醒：.claude/hooks/ 下的 hook 脚本共用 pool.py，要换就一起换：本地改过的那个合并好，其余几个 .new 一起换上，再重跑 init；"
              "只换一部分，新旧脚本混着跑，收件箱的 hook 可能每一轮都出错。")
    on_disk = setup_core.disk_policy_version(target)
    if paired.intersection(setup_core.GUARD_PAIR) and on_disk is not None and on_disk != setup_core.POLICY_VERSION:
        print("提醒：guard.py 和 policy.json 要成对更新。两个 .new 一起合并以后再重跑 init；只合并 guard.py.new，hook 会全部拒绝；"
              "只合并 policy.json.new，旧 guard 会按新规则跑，部分硬规则悄悄变样。")
    problem = setup_core.pair_problem(target)
    if problem:
        print("提醒：" + problem)
    if not apply:
        print("这是预览，没有写任何文件。加 --apply 才会写。")


def apply_plan(target, plan, acts):
    written = 0
    for kind, rel, _ in acts:
        path = os.path.join(target, rel)
        if kind in ("create", "new", "update", "paired", "regen"):
            content, executable, _ = plan.files[rel]
            dest = path + ".new" if kind in ("new", "paired") else path
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            if executable:
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
        elif kind == "chmod":
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written += 1
    mpath = os.path.join(target, ".claude", "solo-lab", "manifest.json")
    text = manifest_text(plan, acts)
    if not (os.path.isfile(mpath) and setup_core.read_text(mpath) == text):
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(text)
        written += 1
    print("已写入 %d 处。" % written)
    return written


# --------------------------------------------------------------- login
LOGIN_WAIT = 300
MEEGLE_HOSTS = {"feishu": "project.feishu.cn", "lark": "project.larksuite.com"}


def needs_login():
    """(tools whose login is gone, the meegle host this tenant uses)."""
    out = []
    code, text, _ = setup_core.run(["lark-cli", "auth", "status"])
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {}
    if not setup_core.lark_user_ready(doc)[0]:
        out.append("lark-cli")
    code, _, _ = setup_core.run(["meegle", "auth", "status"])
    if code != 0:
        out.append("meegle")
    return out, MEEGLE_HOSTS.get(doc.get("brand"), MEEGLE_HOSTS["feishu"])


def login_state():
    """(a directory of this user's own under /tmp, the file of pending logins). lark-cli auth
    qrcode only writes under the current directory, /tmp or ~/files. Device codes stay in that
    file, readable by the user only, and go when the login ends."""
    d = "/tmp/solo-lab-login-%d" % os.getuid()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise setup_core.SetupError("%s 不是只有本人能读写的目录，登录用的临时文件不往里放：删掉它再重试" % d)
    return d, os.path.join(d, "pending.json")


def start_login(cli, host):
    """Start a device-code login: {url, expires_at, the codes the poll needs}. What the tool
    prints goes nowhere else."""
    if cli == "lark-cli":
        cmd, keys, links = ["lark-cli", "auth", "login", "--no-wait", "--json"], ("device_code",), ("verification_url",)
    else:
        cmd = ["meegle", "auth", "login", "--device-code", "--phase", "init", "--host", host, "--format", "json"]
        keys, links = ("device_code", "client_id"), ("verification_uri_complete", "verification_uri")
    code, out, _ = setup_core.run(cmd, 60)
    try:
        doc = json.loads(out) if code == 0 else {}
    except ValueError:
        doc = {}
    url = next((doc[k] for k in links if doc.get(k)), None) if isinstance(doc, dict) else None
    if not url or not all(doc.get(k) for k in keys):
        raise setup_core.SetupError("%s 的设备码登录没发起成（退出码 %s）：研究员在终端里运行它的 auth login 看原因" % (cli, code))
    until = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=int(doc.get("expires_in") or 600))
    return dict({k: doc[k] for k in keys}, url=url, expires_at=until.isoformat(timespec="seconds"))


def poll_login(cli, info, seconds):
    """Wait for the researcher to authorise; the answer is read back from auth status, not from here."""
    if cli == "lark-cli":
        setup_core.run(["lark-cli", "auth", "login", "--device-code", info["device_code"]], seconds)
    else:
        setup_core.run(["meegle", "auth", "login", "--device-code", "--phase", "poll", "--device-code-value", info["device_code"],
             "--client-id", info["client_id"], "--expires-in", str(max(int(seconds), 5)), "--interval", "5", "--format", "json"],
            seconds + 10)


def save_pending(path, pending):
    if not pending:
        if os.path.lexists(path):
            os.remove(path)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(pending, f)


def login(wait=False, timeout=LOGIN_WAIT):
    """Log meegle and lark-cli in by device code: this starts the login and prints a QR code
    and a link for the researcher; `--wait` polls until he has authorised. The session never
    sees a password, a verification code or a token, and no device code is printed."""
    d, path = login_state()
    pending = setup_core.load_json_file(path) or {}
    left = lambda info: (datetime.datetime.fromisoformat(info["expires_at"]) - datetime.datetime.now().astimezone()).total_seconds()
    if not wait:
        need, host = needs_login()
        if not need:
            save_pending(path, {})
            print("lark-cli 和 meegle 都已登录，不用再登录。")
            return 0
        for cli in need:
            info = start_login(cli, host)
            png = os.path.join(d, "%s.png" % cli)
            code, _, _ = setup_core.run(["lark-cli", "auth", "qrcode", info["url"], "-o", png])
            info["qrcode"] = png if code == 0 and os.path.isfile(png) else None
            pending[cli] = info
            save_pending(path, pending)
            print("%s：请研究员扫码或打开链接授权，%d 分钟内有效。" % (cli, (max(left(info), 0) + 59) // 60))
            print("  二维码图片：%s" % (info["qrcode"] or "没生成出来，用链接"))
            print("  链接：%s" % info["url"])
        print("把二维码图片和链接发给研究员。他授权以后运行 setup.py login --wait，读回登录状态。")
        return 0
    if not pending:
        print("没有在等的登录：先运行 setup.py login。")
        return 2
    deadline = time.monotonic() + timeout
    for cli, info in pending.items():
        budget = min(deadline - time.monotonic(), left(info))
        if budget > 0:
            poll_login(cli, info, budget)
    still, _ = needs_login()
    code = 0
    for cli in list(pending):
        info = pending[cli]
        if cli not in still:
            print("%s：已登录。" % cli)
        elif left(info) <= 0:
            print("%s：授权超时，这次的二维码作废；重新运行 setup.py login。" % cli)
            code = 1
        else:
            print("%s：还没授权，二维码还有 %d 分钟有效；研究员授权以后再运行 setup.py login --wait。" % (cli, (left(info) + 59) // 60))
            code = 1
            continue
        if info.get("qrcode") and os.path.isfile(info["qrcode"]):
            os.remove(info["qrcode"])
        del pending[cli]
    save_pending(path, pending)
    return code


# ----------------------------------------------------------------- env
ENV_HEAD = "# solo-lab：由 feishu/ids.json 生成"
ENV_OURS = re.compile(r"^\s*(?:export\s+)?(?:FEISHU_PROJECT_URL|FEISHU_WIKI_URL)\s*=")


def env_text(current, values):
    """A .env with our lines set. Every other line stays as it was and where it was."""
    kept = [line for line in (current or "").split("\n") if line.strip() != ENV_HEAD and not ENV_OURS.match(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept + ([""] if kept else []) + [ENV_HEAD] + ["%s=%s" % kv for kv in values.items()]) + "\n"


def env_cmd(target, apply):
    """Write the two links into the project's .env. Only these lines are shown or changed."""
    values, missing = setup_core.env_values(target)
    if missing:
        print("出错：%s。" % "；".join(missing))
        return 1
    current = setup_core.read_env(target)
    new = env_text(current, values)
    state = "新建" if current is None else ("相同" if new == current else "更新")
    print("[%s] .env" % state)
    for kv in values.items():
        print("  %s=%s" % kv)
    if not apply:
        print("这是预览。加 --apply 写入；.env 里的其他行不动，也不显示。")
        return 0
    if state == "新建":
        fd = os.open(os.path.join(target, ".env"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
    elif state == "更新":
        with open(os.path.join(target, ".env"), "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(new)
    print("已写入 .env。" if state != "相同" else ".env 已经是这样，没有改。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="初始化智能体组织的项目目录")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("announce")
    p.add_argument("--target", required=True)
    p.add_argument("--name", help="项目名，也是两个空间和会话标题前缀的默认名")
    p.add_argument("--task", help="任务文件：项目名、空间名和会话数从它读")
    p.add_argument("--sequences")
    p = sub.add_parser("init")
    p.add_argument("--target", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--sequences")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("login")
    p.add_argument("--wait", action="store_true", help="研究员授权以后：等到登录完成，读回登录状态")
    p.add_argument("--timeout", type=int, default=LOGIN_WAIT, help="--wait 最多等多少秒")
    p = sub.add_parser("env")
    p.add_argument("--target", required=True)
    p.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    try:
        setup_core.need_python()
        if args.cmd == "announce":
            return announce(project_dir(args.target), args.name, args.task, args.sequences)
        if args.cmd == "init":
            target = project_dir(args.target)
            plan = setup_core.build_plan(target, args.task, args.sequences)
            acts = setup_core.actions_for(target, plan)
            report(target, plan, acts, args.apply)
            if args.apply:
                apply_plan(target, plan, acts)
                for line in setup_core.trust_lines(target):    # hooks are in the directory now: before a session is
                    print(line)                                # opened in it, the researcher trusts it
            return 0
        if args.cmd == "login":
            return login(args.wait, args.timeout)
        if args.cmd == "env":
            return env_cmd(os.path.abspath(args.target), args.apply)
    except (setup_core.SetupError, ValueError, OSError) as e:
        print("出错：%s" % e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
