#!/usr/bin/env python3
"""Time an initialisation by stage: python3 -I onboarding.py begin | start <阶段> | end <阶段> | finish | status

The clock belongs to the project, not to a session: the initialising session and the scribe
both write it. What a stage counts as is fixed here, so no session decides it:

    feishu   the agent's own Feishu operations; their sum is the figure held against 300 seconds
    claude   opening sessions, checking claims, moving them, prompts of the client
    local    checks and files on this machine
    human    waiting for the researcher (scanning a login code, saying the word between two rounds of the pool)

300 seconds is a figure to report, not a gate: `finish` says whether it was met and names the
slowest stages, and nothing else depends on it. A first run and every later run that fills in
what was missing are kept apart. Python 3.9 standard library only.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project_paths  # noqa: E402

BUDGET_SECONDS = 300
CATEGORIES = (("feishu", "飞书操作"), ("claude", "Claude 平台"), ("local", "本机"), ("human", "等人"))
STAGES = {
    "project_space": ("feishu", "项目空间查找与创建"), "wiki_space": ("feishu", "知识空间查找与新建"),
    "binding": ("feishu", "结构绑定"), "nodes": ("feishu", "知识库节点"),
    "first_task": ("feishu", "首条研究任务登记"), "readback": ("feishu", "最终飞书回读"),
    "sessions": ("claude", "开会话"), "claims": ("claude", "认领核对"), "detach": ("claude", "移到顶层"),
    "section": ("claude", "归入分区"), "prompts": ("claude", "目录信任和客户端确认"),
    "precheck": ("local", "前置检查"), "init": ("local", "生成项目文件"), "env": ("local", "写 .env"),
    "login": ("human", "等人扫码登录"), "resume": ("human", "等研究员回话接着开池"),
}


class ClockError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def state_path(target):
    """.claude/state/onboarding.json; a Codex project keeps its own whole-run clock under that name."""
    runtime = project_paths.runtime_dir(target)
    if os.path.basename(runtime) == ".agent-org":
        return os.path.join(runtime, "state", "onboarding-stages.json")
    return os.path.join(os.path.dirname(runtime), "state", "onboarding.json")


@contextlib.contextmanager
def record(target):
    """The clock file under a lock: yields the record, saves it afterwards."""
    path = state_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                rec = {}
            if not isinstance(rec, dict) or not isinstance(rec.get("runs"), list):
                rec = {"budget_seconds": BUDGET_SECONDS, "runs": []}
            yield rec
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def seconds(start, end):
    return round((datetime.datetime.fromisoformat(end) - datetime.datetime.fromisoformat(start)).total_seconds(), 2)


def open_run(rec):
    run = rec["runs"][-1] if rec["runs"] else None
    if not run or run.get("finished_at"):
        raise ClockError("没有在计时的初始化：先运行 onboarding.py begin；配置结束以后补不了耗时")
    return run


def summary(run, at=None):
    """Totals of a run: seconds and count per category, the Feishu figure, the slowest stages."""
    at = at or run.get("finished_at") or now().isoformat()
    stages = {}
    for s in run["stages"]:
        cur = stages.setdefault(s["stage"], {"seconds": 0.0, "count": 0, "open": False})
        cur["count"] += 1
        cur["seconds"] = round(cur["seconds"] + seconds(s["start"], s.get("end") or at), 2)
        cur["open"] = cur["open"] or not s.get("end")
    by_cat = {c: {"seconds": 0.0, "count": 0} for c, _ in CATEGORIES}
    for name, cur in stages.items():
        cat = by_cat[STAGES[name][0]]
        cat["seconds"] = round(cat["seconds"] + cur["seconds"], 2)
        cat["count"] += cur["count"]
    feishu = by_cat["feishu"]["seconds"]
    return {"kind": run["kind"], "started_at": run["started_at"], "finished_at": run.get("finished_at"),
            "elapsed_seconds": seconds(run["started_at"], at), "feishu_seconds": feishu,
            "budget_seconds": BUDGET_SECONDS, "timing_pass": feishu <= BUDGET_SECONDS,
            "categories": by_cat, "stages": stages,
            "slowest": sorted(stages, key=lambda n: -stages[n]["seconds"])[:3]}


def clock(target, action, stage=None, at=None):
    """Apply one action and return the summary of the run it touched."""
    at = (at or now()).isoformat()
    if stage is not None and stage not in STAGES:
        raise ClockError("没有阶段 %s；可选：%s" % (stage, "、".join(STAGES)))
    with record(target) as rec:
        if action == "begin":
            if not rec["runs"] or rec["runs"][-1].get("finished_at"):       # an open run goes on: begin is idempotent
                rec["runs"].append({"kind": "repair" if rec["runs"] else "first", "started_at": at, "stages": []})
            return summary(rec["runs"][-1], at)
        if action == "status" and rec["runs"] and rec["runs"][-1].get("finished_at"):
            return summary(rec["runs"][-1])
        run = open_run(rec)
        running = [s for s in run["stages"] if not s.get("end")]
        if action == "start":
            if any(s["stage"] == stage for s in running):
                raise ClockError("阶段 %s 已经在计时，先 end" % stage)
            run["stages"].append({"stage": stage, "start": at})
        elif action == "end":
            hit = next((s for s in running if s["stage"] == stage), None)
            if not hit:
                raise ClockError("阶段 %s 没有在计时：先 start" % stage)
            hit["end"] = at
        elif action == "finish":
            if running:
                raise ClockError("还有没结束的阶段：%s；先 end" % "、".join(s["stage"] for s in running))
            run["finished_at"] = at
        return summary(run, at)


def text(result):
    kind = "首次" if result["kind"] == "first" else "补齐"
    done = "，已结束" if result.get("finished_at") else "，还在计时"
    lines = ["初始化计时（%s%s）：全程 %s 秒。" % (kind, done, result["elapsed_seconds"])]
    for cat, cn in CATEGORIES:
        names = [n for n in STAGES if STAGES[n][0] == cat and n in result["stages"]]
        parts = "、".join("%s %s%s" % (STAGES[n][1], result["stages"][n]["seconds"],
                                     "（%d 次）" % result["stages"][n]["count"] if result["stages"][n]["count"] > 1 else "")
                         for n in names) or "没有记录"
        head = "%s %s 秒" % (cn, result["categories"][cat]["seconds"])
        if cat == "feishu":
            head += "（指标 %d 秒以内：%s）" % (BUDGET_SECONDS, "达到" if result["timing_pass"] else "超过")
        lines.append("%s：%s" % (head, parts))
    if not result["timing_pass"]:
        lines.append("最慢的 3 段：%s" % "、".join("%s %s 秒" % (STAGES[n][1], result["stages"][n]["seconds"]) for n in result["slowest"]))
    lines.append("飞书操作这一项只是指标，不影响 setup.py check 的结论；Claude 平台、本机和等人的时间不计入。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="按阶段给一次初始化计时")
    ap.add_argument("action", choices=("begin", "start", "end", "finish", "status"))
    ap.add_argument("stage", nargs="?", help="start、end 要给阶段：%s" % "、".join(STAGES))
    ap.add_argument("--target", help="项目目录；不写就用当前项目")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if (args.action in ("start", "end")) != bool(args.stage):
        print("出错：start 和 end 要给阶段，其余不给。阶段：%s" % "、".join("%s（%s）" % (n, STAGES[n][1]) for n in STAGES))
        return 2
    try:
        result = clock(os.path.abspath(args.target) if args.target else project_paths.project_root(), args.action, args.stage)
    except (ClockError, OSError, ValueError) as e:
        print("出错：%s" % e)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
