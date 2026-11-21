#!/usr/bin/env python3
"""The sessions an initialisation opens. Run as pool.py open, where the plugin is installed: the list comes
from what setup.py knows about the project.

    python3.12 -I scripts/pool.py open --target DIR [--manual]
    python3.12 -I scripts/pool.py open --target DIR --wait SECONDS [--batch N]

`pool.py open` prints, for the session that initialises, the +2 sessions it opens
with their titles, models and opening lines, the tree below each, and the claims so far
(read only). The desktop app runs at most five sessions at once that sessions started on
their own, and starts at most ten of them after each message from the user. So the +2s are
opened in batches, and the batches in rounds, one round per message of the researcher:
--wait SECONDS --batch N waits until batch N has claimed and its first turns are over, and
says whether the next batch may follow or the round ends there; --wait SECONDS alone waits
until every session has claimed and the scribe has done its first writes. --manual prints how
the researcher opens the top sessions by hand when that session cannot open any.
Python 3.12 standard library only.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import setup  # noqa: E402

TURN_RUNNING = ("busy", "unknown", "uncertain")       # a session whose turn is not over, as far as the hooks know


def own_mode(target):
    """The permission mode the hooks recorded for the session that runs this command; None when unknown."""
    import pool
    sid = os.environ.get(pool.SESSION_ENV) or ""
    return pool.load_session(target, sid).get("permission_mode") if sid else None


def pool_checklist(target, manual=False):
    """`pool.py open`: how to open the pool, and the claims so far. Reads only."""
    import pool
    plan = setup.load_json_file(os.path.join(target, ".claude", "solo-lab", "pool-plan.json"))
    policy = setup.load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) or {}
    if not policy.get("agents") or not plan:
        print("这个项目还没有开池清单：先跑 init --apply。")
        return 2
    if plan.get("version") != setup.PLAN_VERSION:
        print("开池清单是旧版插件生成的：重跑 init --apply。")
        return 2
    pl, ok = pool.load(target)
    claimed = set(pl["claims"]) if ok else set()
    label, tops = plan["label"], plan["top"]

    def tree(entry, depth=1):
        lines = []
        for child in entry["children"]:
            lines.append("   %s└ %s（%s，%s）" % ("  " * (depth - 1), child["name"], child["model"],
                                              "已认领" if child["name"] in claimed else "未认领"))
            lines += tree(child, depth + 1)
        return lines
    print("开池清单：%s（共 %d 个会话，总上限 %d）。会话标题的前缀和侧栏分区的名字：%s" % (
        plan["project"], plan["total"], plan["max_sessions"], label))
    print("会话按委托的方向一层开一层：+2 开本序列的 +1，+1 开 +0。侧栏里谁开的就嵌在谁下面。")
    for above in setup.plan_entries(plan):
        for child in above["children"]:
            if setup.model_tier(child["model"]) > setup.model_tier(above["model"]) >= 0:
                print("注意：%s 的模型 %s 高于开它的 %s（%s）。会话的模型不能高于开它的会话，高出的不生效，它会沿用 %s 的模型；"
                      "要它用 %s，把任务文件 models 里 %s 的模型也调高。" % (
                          child["name"], child["model"], above["name"], above["model"], above["name"], child["model"], above["role"]))
    print("")
    if manual:
        print("手工开池（研究员的会话开不了会话时用）：")
        print("研究员在桌面应用里新建 %d 个会话（New session，目录选本项目，不开 worktree），模型照括号里的选，每个粘贴一句：" % len(tops))
        for i, top in enumerate(tops, 1):
            print("%d. %s（%s，%s）：%s" % (i, top["name"], top["model"], "已认领" if top["name"] in claimed else "未认领", top["opening"]))
            for line in tree(top):
                print(line)
            if ok and top["name"] not in claimed and any(e["name"] in claimed for e in setup.plan_entries({"top": top["children"]})):
                print("   它下面的会话还在，换会话时粘贴重建句：%s" % top["rebuild"])
        print("每个 +2 认领以后，照认领输出把自己的标题改成“[%s] <会话名>”，归入侧栏分区“%s”；它下面的会话跟着它走。" % (label, label))
        if bool(setup.feishu_gaps(target, setup.enabled_sequences(target))) or not setup.registered_tasks(target):
            print("书记员由路由开出，开局句里带着初始化的两件写入。它没做的话，把这段话发给它：%s" % plan["scribe_first"].lstrip("。"))
    else:
        batches, rounds = plan["batches"], plan["rounds"]
        left = [e["name"] for e in setup.plan_entries(plan) if e["name"] not in claimed]
        if pool.mode_class(own_mode(target)) == "bypass" and left:
            # measured 2026-11-13: in bypass mode the app asks the user before every start_session, in whichever session calls it
            print("注意：本会话是 bypass 模式。这个模式下，应用对每一次开会话都要研究员点一次确认，卡片弹在发起的那个会话里：本会话开 +2、"
                  "+2 开 +1、+1 开 +0 都一样。还没开的 %d 个会话要点 %d 次；没点之前那个会话停着，--wait 会等到超时。" % (len(left), len(left)))
            if claimed:
                print("已经开出的会话也是 bypass 模式，换模式会让新旧会话之间的消息被扣下，所以接着在本会话开。开之前告诉研究员：每开一个会话，"
                      "到弹出卡片的那个会话里点一次 Allow once。")
            else:
                print("开池之前先停下，把下面两种做法原样告诉研究员，等他选：")
                print("1. 换 auto 模式：研究员在项目目录新开一个 auto 模式的会话，说“用 solo-lab 初始化这个目录”，它从开池接着做，不用点。本会话到此为止。")
                print("2. 留在本会话：接着开，每开一个会话，研究员到弹出卡片的那个会话里点一次 Allow once。")
            print("")
        print("研究员的会话（做初始化的这个会话）只开下面这 %d 个 +2，分 %d 批、%d 轮开：" % (len(tops), len(batches), len(rounds)))
        print("- 为什么分批、分轮：会话自己开出的会话（+2 开的 +1、+1 开的 +0），应用有两条上限，所有序列合在一起算：同时最多 %d 个在跑；"
              "研究员每发一条消息以后最多开出 %d 个。所以一批只开一两个序列，等这一批的会话都认领、首轮都结束再开下一批；"
              "一轮开到 %d 个上下就停下，请研究员回一句话，再开下一轮。" % (setup.AUTO_RUNNING, setup.MESSAGE_STARTS, setup.ROUND_BUDGET))
        print("- 每一批做三件事，顺序不能换：1. 用会话管理工具 start_session 把这一批的 +2 都开出来；2. 马上对它们各调一次 detach_session，"
              "把它们移到顶层，和本会话同级；3. 用返回的桌面会话 ID 逐个运行它们的 expect。+2 要等预登记才能认领，认领以后才往下开，"
              "所以先移到顶层、后预登记。")
        print("- start_session 的参数：initiation 写 user_asked，context 写 fresh，use_worktree 写 false，权限模式不填（继承本会话）；"
              "name、title、model、prompt、background 照下表。它还没加载时，先用 ToolSearch 查 start_session 加载进来。")
        print("- 这一批的 expect 跑完，运行 pool.py open --target <项目目录> --wait 180 --batch <第几批>：它等到这一批的会话都认领、首轮都结束才返回，"
              "并写明下一步。超时的时候照它说的办：有首轮还没结束的，重跑一次再等；只是没认领的，接着往下做，最后一并处理。")
        if len(rounds) > 1:
            print("- 一轮的最后一批等完、后面还有下一轮：不要接着开。结束本轮，请研究员回一句“%s”；他回话以后再开下一轮。"
                  "研究员每发一条消息，应用才重新给 %d 个名额，下一轮的 +2 要等他回话以后才开。" % (setup.RESUME_WORDS, setup.MESSAGE_STARTS))
        print("- 最后一批等完：侧栏里没有叫“%s”的分区就用 create_section 建一个；用 move_sessions 把这 %d 个 +2 连同本会话归进去，它们下面的会话跟着走。"
              "再运行 pool.py open --target <项目目录> --wait 240：它等到 %d 个会话都认领、书记员的首轮结束才返回，没等到就列出还差谁。"
              "本会话的标题是应用自动起的，就用 set_session_title 改成“[%s] Initialization”：它是做初始化的会话，不是入口。" % (
                  label, len(tops), plan["total"], label))
        print("- 某个会话下面还有没认领的：它首轮没把下一级开出来。给它发消息没有用，被消息叫起来的那一轮开不了会话；"
              "请研究员点开那个会话，说一句“照认领输出开出下一级”。")
        print("- model 填不进去就不填，开好以后再改；模型高于本会话的不生效（会话的模型不能高于开它的会话），记进交接。")
        for top in tops:
            below = sum(1 for _ in setup.plan_entries({"top": top["children"]}))
            if below > setup.OPEN_BUDGET:
                print("注意：%s 下面要开 %d 个会话，一批最多容得下 %d 个：多出来的会被应用拒绝，pool --wait 会列出来，"
                      "到时请研究员在上一级的会话里说一句“照认领输出开出下一级”。" % (top["name"], below, setup.OPEN_BUDGET))
        number = 0
        for r, numbers in enumerate(rounds, 1):
            print("")
            print("第 %d 轮（%s）" % (r, "研究员说完初始化那句话以后" if r == 1 else "研究员回一句“%s”以后" % setup.RESUME_WORDS))
            for b in numbers:
                print("第 %d 批" % b)
                for top in (t for t in tops if t["name"] in batches[b - 1]):
                    number += 1
                    print("%d. %s（%s）" % (number, top["name"], "已认领，不用再开" if top["name"] in claimed else "未认领"))
                    print("   name：%s　title：%s　model：%s" % (top["short"], top["title"], top["model"]))
                    print("   prompt：%s" % top["brief"])
                    print("   background：%s" % top["background"])
                    print("   expect：%s" % top["expect"])
                    for line in tree(top):
                        print(line)
        print("")
        print("这个会话开不了会话时（比如这一轮不是研究员亲口开始的，是被别的会话的消息叫起来的）：pool.py open --target <项目目录> --manual 打印手工开池的做法。")
    print("")
    if ok:
        rows = pool.session_view(target, policy, pl)
        entry = len(pool.claims_of(pl, policy["entry_role"]))
        scribe = len(pool.claims_of(pl, policy["scribe_role"]))
        print("当前：%d/%d 条认领；入口 %d 条，书记员 %d 条；待转正的许可 %d 条。" % (
            len(pl["claims"]), plan["total"], entry, scribe, len(pl["permits"])))
        no_desk = [r["name"] for r in rows if not r["desk"]]
        if no_desk:
            print("没有记下桌面会话 ID 的：%s（终端里开的会话没有，桌面会话应该有）" % "、".join(no_desk))
        mixed = pool.mixed_modes(rows)
        if mixed:
            print("权限模式和多数会话不同类的：%s" % "、".join(mixed))
        if len(pl["claims"]) >= plan["total"] and entry == 1 and scribe == 1 and not pl["permits"]:
            print("会话池已开齐。接着运行 check.py check --target <项目目录> --live。")
    else:
        print("读不出 .claude/state/pool.json。")
    return 0


def pool_wait(target, seconds, batch=None, every=5):
    """Wait for what the researcher's session does not do itself: the levels below the +2s open
    one another, and the scribe, two levels down, builds the nodes and registers the first
    research task. With a batch: until every session of that batch has claimed and its first
    turn is over (the scribe may still be writing), so that the next batch finds room. Without:
    until every planned session has claimed and the scribe's first turn is over with the nodes
    in place. 0 when that is so; 1 with what is still missing."""
    import pool
    policy = setup.load_json_file(os.path.join(target, ".claude", "hooks", "policy.json")) or {}
    plan = setup.load_json_file(os.path.join(target, ".claude", "solo-lab", "pool-plan.json")) or {}
    if not policy or plan.get("version") != setup.PLAN_VERSION:
        print("这个项目没有开池清单，或者清单是旧版插件生成的：重跑 init --apply。")
        return 2
    batches = plan["batches"]
    if batch is not None and not 1 <= batch <= len(batches):
        print("没有第 %d 批：开池清单一共 %d 批。" % (batch, len(batches)))
        return 2
    opened_by = {c["name"]: e["name"] for e in setup.plan_entries(plan) for c in e["children"]}
    tops = plan["top"] if batch is None else [t for t in plan["top"] if t["name"] in batches[batch - 1]]
    entries = list(setup.plan_entries({"top": tops}))
    scribe_role = policy.get("scribe_role")
    deadline = time.time() + max(0, seconds)
    while True:
        pl, ok = pool.load(target)
        claims = pl["claims"] if ok else {}
        missing = [e["name"] for e in entries if e["name"] not in claims]
        rows = {r["name"]: r for r in (pool.session_view(target, policy, pl) if ok else [])}
        scribe = next((r for r in rows.values() if r["role"] == scribe_role), None)
        if batch is None:
            running = [] if scribe and scribe["status"] not in TURN_RUNNING else [(scribe or {}).get("name") or "书记员"]
        else:
            running = [e["name"] for e in entries if e["name"] in rows and e["role"] != scribe_role
                       and rows[e["name"]]["status"] in TURN_RUNNING + ("waiting_researcher",)]
        if (ok and not missing and not running) or time.time() >= deadline:
            break
        time.sleep(every)
    if not ok:
        print("读不出 .claude/state/pool.json。")
        return 1
    if batch is not None:
        mine = next(numbers for numbers in plan["rounds"] if batch in numbers)
        if batch == len(batches):
            then = "这是最后一批。接着把 +2 和本会话归入分区，再运行 pool.py open --target <项目目录> --wait 240。"
        elif batch == mine[-1]:                            # the round is over: the next one needs a message from the researcher
            used = sum(1 for b in mine for t in plan["top"] if t["name"] in batches[b - 1]
                       for _ in setup.plan_entries({"top": t["children"]}))
            then = ("这一轮到此为止：研究员每发一条消息，应用最多让会话自己开出 %d 个会话，这一轮用了 %d 个。现在结束本轮，"
                    "请研究员回一句“%s”；他回话以后再开第 %d 批：%s。" % (
                        setup.MESSAGE_STARTS, used, setup.RESUME_WORDS, batch + 1, "、".join(batches[batch])))
        else:
            then = "接着开第 %d 批：%s。" % (batch + 1, "、".join(batches[batch]))
        if not missing and not running:
            print("第 %d 批开齐：%d 个会话都已认领，首轮都结束了。%s" % (batch, len(entries), then))
            return 0
        print("等了 %d 秒，第 %d 批的 %d 个会话还没开齐。" % (seconds, batch, len(entries)))
    elif missing:
        print("等了 %d 秒，%d 个会话里还有 %d 个没认领：" % (seconds, len(entries), len(missing)))
    for name in missing:
        above = opened_by.get(name)
        print("- %s：%s" % (name, "+2，由做初始化的会话开出" if not above else
                             "由 %s 开出，%s %s" % (above, above, "已认领" if above in claims else "还没认领")))
    if any(opened_by.get(n) in claims for n in missing):
        print("上一级已认领、下一级没出来的：它首轮没把下一级开出来。请研究员点开上一级的会话，说一句“照认领输出开出下一级”；"
              "给它发消息没有用，被消息叫起来的那一轮开不了会话。")
    if batch is not None:
        asking = [n for n in running if rows[n]["status"] == "waiting_researcher"]
        if asking:
            print("停在确认框上、等研究员批准的：%s。请研究员到侧栏点开处理。" % "、".join(asking))
        if running:
            print("首轮还没结束的：%s。它们在跑的时候占着应用同时运行的名额：重跑这条命令再等一次；还是这样，就往下做。%s" % (
                "、".join(running), then))
        else:
            print("不用再等这一批。%s" % then)
        return 1
    if missing:
        return 1
    line = "会话池已开齐：%d/%d 条认领。" % (len(entries), len(entries))
    read = "用会话管理工具 list_events 读书记员（桌面会话 %s）的最后几条" % ((scribe or {}).get("desk") or "ID 没记下")
    if running:
        print(line + "书记员的首轮还没结束（状态：%s），两件初始化写入还在做：重跑这条命令接着等。" % (scribe or {}).get("status", "unknown"))
        return 1
    if scribe["status"] == "waiting_researcher":
        print(line + "书记员停在一个确认框上，等研究员批准：请研究员到侧栏点开它处理，再重跑这条命令。")
        return 1
    gaps = setup.feishu_gaps(target, setup.enabled_sequences(target))
    if gaps:
        print(line + "书记员的首轮已结束，但是%s。" % gaps[0])
        print(read + "，照技能里“书记员回话里的情况”处理。")
        return 1
    tasks = setup.registered_tasks(target)
    if tasks:
        print(line + "书记员的首轮已结束：知识库节点都在，首条研究任务已登记（#%s）。" % tasks[0][0].get("work_item_id", "?"))
    else:
        print(line + "书记员的首轮已结束：知识库节点都在；这个目录里没有首条研究任务的登记回执。空间里原来就有研究任务时书记员会跳过登记，"
              "check --live 会核对；不是这种情况，%s。" % read)
    return 0


def main(args):
    """`pool.py open`, with the arguments pool_cmd.py parsed."""
    try:
        setup.need_python()
        if args.batch is not None and args.wait is None:
            raise setup.SetupError("--batch 要和 --wait 一起用：pool.py open --target <项目目录> --wait 180 --batch <第几批>")
        if args.wait is not None:
            return pool_wait(os.path.abspath(args.target), args.wait, args.batch)
        return pool_checklist(os.path.abspath(args.target), args.manual)
    except (setup.SetupError, ValueError, OSError) as e:
        print("出错：%s" % e)
        return 2
