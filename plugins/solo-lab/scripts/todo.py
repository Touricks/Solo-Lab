#!/usr/bin/env python3
"""The researcher's to-do list: something the researcher is to do by hand and that takes no decision.

    todo.py add --text T [--for ID] [--done-when T] [--terminate | --delete]
                                              a line in pool.json and the entry's inbox, listed after the decisions;
                                              --terminate / --delete: the work item --for is to go, on the web page
    todo.py done <n> [--note T]               the to-do is done: taken off the list

Standard library only.
"""
import argparse
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orgspec  # noqa: E402
import ledger_core  # noqa: E402
import pool  # noqa: E402

def todo_cmd(spec, root, action, n=None, text=None, item=None, done_when=None, note=None, kind=None):
    """研究员任务清单: what a session needs the researcher to do by hand where nothing is to be decided (fetch a
    key, connect a VPN, run a command only they may run). Until 1.0.9 it took a decision package, six to
    eight turns each. It is a line in pool.json that the entry lists after the decisions; no work item, no
    document. What tells it from a decision is for the session that writes it to judge: nothing here checks.

    A work item to be terminated or deleted on the web page (kind) is such a line too: one that was made by
    mistake or withdrawn before its work began cannot be archived, and no script terminates one. The entry
    tells the researcher of all of them as one thing (pool_cmd). Nobody looks whether it was done; when the
    researcher says so, `pool.py show --prune --apply` reads the ledger once and strikes those that are."""
    policy = pool.need_pool(root)
    if not policy:
        return 0
    by = pool.sender_label(*pool.caller(root, policy))
    if action == "add":
        if not (text or "").strip():
            print("出错：--text 写要研究员做什么；请他终止或删除工作项的，写为什么不要这条工作项了")
            return 1
        item = str(item).lstrip("#") if item else None
        if kind and not item:
            print("出错：--terminate、--delete 要和 --for <工作项 ID> 一起用")
            return 1
        title = link = None
        if kind:                                     # the item's name and link, read once, for the researcher to click
            try:
                lg = ledger_core.Ledger(root, "preview")
                it = lg.item(item)
                if it:
                    title, link = it.get("name"), lg.item_url(it.get("type"), item) if it.get("type") else None
            except Exception:                        # the name and the link are a convenience: the line is written without
                pass
        with pool.locked(root) as pl:
            same = next((t for t in pool.open_todos(pl) if kind and t.get("kind") and t.get("item") == item), None)
            if not same:
                number = max([t["n"] for t in pool.open_todos(pl)] + [int(pl.get("todo_seq") or 0)]) + 1
                pl["todo_seq"] = number
                pl["todos"].append({"n": number, "text": text.strip(), "item": item, "kind": kind, "title": title, "link": link,
                                    "done_when": (done_when or "").strip() or None, "by": by, "at": pool.stamp()})
        if same:
            print("#%s 已经在研究员任务清单里了（第 %d 条，%s 提），没有再记。" % (item, same["n"], same.get("by")))
            return 0
        pool.log_event(root, {"event": "todo", "n": number, "by": by, "item": item, "kind": kind, "line": text.strip()[:200]})
        entry = policy["agents"][policy["entry_role"]].get("fixed_session") or "PMO-p2-01"
        if kind:
            said = "研究员任务清单 %d：飞书项目里的 #%s 等研究员%s（%s）。这类工作项不管有几条，合成一件事跟研究员说，不算决策；他说处理完了，运行 pool.py show --prune --apply 核对。" % (
                number, item, pool.ITEM_KINDS[kind], text.strip())
        else:
            said = "研究员任务清单 %d：%s%s。研究员来谈时跟在决策清单后面列出，不算决策；他做完了，运行 todo.py done %d。" % (
                number, text.strip(), "（为 #%s）" % item if item else "", number)
        pool.append_inbox(root, entry, {"id": uuid.uuid4().hex[:12], "ts": pool.stamp(), "from": by, "read": False, "text": said})
        if kind:
            print("已记进研究员任务清单（第 %d 条）：#%s 等研究员在网页上%s。入口把清单上这类工作项合成一件事跟研究员说；"
                  "他处理完告诉入口，入口再核对台账划掉。不用为它写决策包，也不要去推它的节点。" % (number, item, pool.ITEM_KINDS[kind]))
        else:
            print("已记进研究员任务清单（第 %d 条）：%s。入口向研究员列出时会带上它。研究员做完了，或者你自己核对到了结果，"
                  "运行 todo.py done %d --note '<研究员的话或核对到的结果>'。" % (number, text.strip(), number))
        return 0
    with pool.locked(root) as pl:
        hit = next((t for t in pool.open_todos(pl) if t["n"] == n), None)
        if hit:
            pl["todos"].remove(hit)
    if not hit:
        print("研究员任务清单里没有第 %s 条；pool.py show 列着现在还开着的。" % n)
        return 1
    pool.log_event(root, {"event": "todo_done", "n": n, "by": by, "item": hit.get("item"), "line": (note or "").strip()[:200]})
    print("研究员任务清单第 %d 条已划掉：%s。%s" % (n, hit["text"], "" if hit.get("kind") or not hit.get("item") else
                                    "请书记员在 #%s 下加一条评论，写研究员做了什么%s。" % (
                                        hit["item"], "（%s）" % note.strip() if (note or "").strip() else "")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="研究员任务清单：要研究员亲手做、不用拍板的事", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add")
    p.add_argument("--text", help="要研究员做什么；带 --terminate、--delete 时写为什么不要这条工作项")
    p.add_argument("--for", dest="item", metavar="ID", help="为哪条工作项")
    p.add_argument("--terminate", dest="kind", action="store_const", const="terminate", help="请研究员在网页上终止 --for 的工作项")
    p.add_argument("--delete", dest="kind", action="store_const", const="delete", help="请研究员在网页上删除 --for 的工作项")
    p.add_argument("--done-when", help="做完怎么算数")
    p = sub.add_parser("done")
    p.add_argument("n", nargs="?", type=int, help="研究员任务清单里要划掉的那一条的编号")
    p.add_argument("--note", help="研究员的话，或者核对到的结果")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    spec = orgspec.OrgSpec()
    root = ledger_core.project_root()
    if os.path.isfile(os.path.join(root, ".agent-org", "manifest.json")):
        print("Codex 的会话操作使用 codex_runtime.py；本入口只复用 classify 的业务路由")
        return 2
    if args.cmd == "done" and args.n is None:
        print("出错：todo done 后面写编号，pool.py show 列着")
        return 1
    add = args.cmd == "add"
    return todo_cmd(spec, root, args.cmd, None if add else args.n, args.text if add else None, args.item if add else None,
                    args.done_when if add else None, None if add else args.note, args.kind if add else None)


if __name__ == "__main__":
    sys.exit(main())
