#!/usr/bin/env python3
"""Tests for when the pool reaches the researcher (1.0.3): who started a turn, what the inbox
hook adds for it, and the guard's gate on the wrap-up tool and AskUserQuestion in a turn another
agent session started; and what the inbox hook says of the local decision state (1.0.4): the
frozen actions, the decisions past their expiry time. Standard library only, no Feishu access.

    python3 -I -B tests/test_delivery.py
"""
import ast
import contextlib
import datetime
import io
import json
import os
import re
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, TESTS)
import pool as pool_lib  # noqa: E402
from test_plugin import PoolProjectCase, example_blocks  # noqa: E402
import fake_space  # noqa: E402
import ledger  # noqa: E402
import setup  # noqa: E402

WRAPUP = "mcp__ccd_turn__wrap_up"
WOKEN = "本轮由其他 agent 会话叫醒"
QUEUED = "本轮登记了待定决策"
LIST_DUE = ("先回答研究员，再跑 python3 -I .claude/solo-lab/bin/ledger.py query queue，把待决定的决策和等研究员执行的决策附在末尾，"
            "每条一行：状态、事项、紧急程度、回复期限、执行方、要研究员亲手做的事、链接，改过版的在事项后面写版本号；"
            "route.py pool 里还没处理的补位请求一起列，补位请求不算决策。都没有就不附。")
QUEUED_TEXT = ("本轮登记了待定决策 #%s：在这一行状态里提一次，写事项、选项一句话、执行方、要研究员亲手做的事（包头写了的话）和工作项链接，"
               "不超过 3 行。")
DENIED = ("这一轮是其他 agent 会话叫醒的，研究员多半不在：不用 AskUserQuestion。要研究员拍板的事写决策包或发【需拍板】给入口；"
          "+1、+0 写进交给上一级的交付包。")

# How prompts start, as the hooks got them in the desktop app on 2026-11-15. Bodies are shortened
# and the session IDs are placeholders.
CROSS = ('<cross-session-message from="local_maint-entry" from-name="Maintenance-Entry">\n'
         '【通知 #1001】Maint-p2-01 → Test-p2-01：HOOKTEST 第二轮\n这一轮只看 hook。\n</cross-session-message>')
SIDE = ('<system-reminder>\nThe session "Alg-p1-01" you started (local_alg-p1-01) finished a turn.\n</system-reminder>\n'
        '<system-reminder>\nIts last message: 已交付。\n</system-reminder>\n\n'
        '<side-sessions-event>None of the sessions you started is working now; read what they wrote.</side-sessions-event>')
TYPED = ('<system-reminder>\nThe session "Alg-p0-01" you started (local_alg-p0-01) was stopped or archived by the user '
         'before it began.\n</system-reminder>\n\n\n这个会话出bug了，重试')
STARTED = ("Background (from the session that started this one):\n本目录是「示例项目」的智能体组织，按会话池运行。\n\n"
           "Task:\n认领 alg-p1（会话名 Alg-p1-01）。先运行 route.py claim alg-p1 --as Alg-p1-01")
NOTIFIED = "<task-notification>\n<task-id>bg-1</task-id>\n<status>completed</status>\n</task-notification>"
REMINDERS = SIDE.split("<side-sessions-event>")[0]       # two reminder blocks: six lines before what follows them
ONLY_REMINDERS = TYPED.split("这个会话出bug了")[0]          # the app's reminder and nothing after it
FIRST_TALK = "研究员来谈了。"
SIDE_REPORT = "回报里说它在等研究员、带着提问的，不转告研究员：被其他 agent 会话叫醒的轮次，它收尾里的提问已被 hook 去掉。"


def wrap(text, name="[示例] PMO-p0-01"):
    """A message another session sent, as the desktop app hands it to the hook."""
    return '<cross-session-message from="local_peer" from-name="%s">\n%s\n</cross-session-message>' % (name, text)


def constant(path, name):
    """The value of a module-level string constant of a hook script, read without running the script."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


def audit_lines(text, title):
    """The "- " lines under one heading of route.py audit's output."""
    out = []
    for line in text.split(title, 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        out.append(line)
    return out


def scribe_receipt(item, text, to="PMO-p2-01"):
    """A later 【登记回执】 the scribe sends for a work item it registered before: 决定 confirmed, archived ..."""
    return wrap("【登记回执 #%s】PMO-p0-01 → %s：%s" % (item, to, text))


# ------------------------------------------------------------ turn origin
class TurnOriginTest(PoolProjectCase):
    """pool.turn_origin, strip_reminders and unwrap on the prompts the desktop app sends."""

    def setUp(self):
        super().setUp()
        self.policy = json.loads(self.read(".claude/hooks/policy.json"))

    def origin(self, prompt):
        return pool_lib.turn_origin(self.policy, prompt)

    def test_the_five_starts_seen_in_the_app(self):
        self.assertEqual(self.origin(CROSS), ("agent", "Maintenance-Entry"))
        self.assertEqual(self.origin("Another Claude session sent a message:\n" + CROSS), ("agent", "Maintenance-Entry"))
        self.assertEqual(self.origin(SIDE), ("agent", "本会话开出的会话"))
        self.assertEqual(self.origin(TYPED), ("researcher", None))
        self.assertEqual(self.origin(STARTED), ("agent", "开出本会话的会话"))
        self.assertEqual(self.origin(NOTIFIED), ("system", None))

    def test_reminders_alone_start_no_turn_of_the_researcher(self):
        self.assertEqual(self.origin(ONLY_REMINDERS), ("system", None))
        self.assertEqual(self.origin(""), ("system", None))
        self.assertEqual(self.origin(" \n\n"), ("system", None))
        # what follows the reminders decides, however many lines they take
        self.assertEqual(self.origin(REMINDERS + "<cross-session-message>\n继续\n</cross-session-message>"), ("agent", None))
        self.assertEqual(self.origin(REMINDERS + "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md"), ("agent", "Alg-p2-01"))

    def test_reminders_and_the_wrapper_come_off(self):
        self.assertEqual(pool_lib.strip_reminders(TYPED), "这个会话出bug了，重试")
        self.assertTrue(pool_lib.strip_reminders(SIDE).startswith("<side-sessions-event>"))
        self.assertEqual(pool_lib.strip_reminders("  早"), "早")
        body, sender = pool_lib.unwrap(CROSS)
        self.assertTrue(body.startswith("【通知 #1001】Maint-p2-01 → Test-p2-01"), body)
        self.assertNotIn("cross-session-message", body)
        self.assertEqual(sender, {"from": "local_maint-entry", "from_name": "Maintenance-Entry"})
        self.assertEqual(pool_lib.unwrap("Another Claude session sent a message:\n" + CROSS), (body, sender))
        self.assertEqual(pool_lib.unwrap("早"), ("早", None))
        self.assertEqual(pool_lib.unwrap(TYPED), (TYPED, None))
        # a sender without a title is named by its session ID; one without either has no source
        self.assertEqual(self.origin('<cross-session-message from="local_peer">\n继续\n</cross-session-message>'),
                         ("agent", "local_peer"))
        self.assertEqual(self.origin("<cross-session-message>\n继续\n</cross-session-message>"), ("agent", None))

    def test_headers_and_the_researchers_words(self):
        self.assertEqual(self.origin("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md"), ("agent", "Alg-p2-01"))
        self.assertEqual(self.origin("早，今天先看评测"), ("researcher", None))
        self.assertEqual(self.origin("你上周收到的【委托 #2101】Alg-p2-01 → Alg-p1-01 那件事，结论再讲一遍。"),
                         ("researcher", None))


# ------------------------------------------------------------------ inbox
class DeliveryCase(PoolProjectCase):
    """A started pool: the entry, the scribe, the algorithm and quality sequences claimed."""

    ASK = {"ask": "先跑哪个？", "actions": [{"label": "先跑 A"}, {"label": "先跑 B"}], "blocked": True}

    def setUp(self):
        super().setUp()
        self.started_pool()

    def context(self, sid, text):
        _, out = self.prompt(sid, text)
        return out.get("additionalContext", "")

    def set_state(self, sid, **fields):
        path = os.path.join(self.tmp, ".claude", "state", "sessions", sid + ".json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st.update(fields)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)

    def ago(self, minutes):
        return pool_lib.stamp(pool_lib.now() - datetime.timedelta(minutes=minutes))

    def receipt(self, item, name=None, **fields):
        path = os.path.join(self.tmp, "packages", "sent", name or "%s.json" % item)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict({"work_item_id": item}, **fields), f, ensure_ascii=False)

    def registered(self, item, to="PMO-p2-01"):
        """The scribe's register receipt for work item item, as another session's message."""
        return wrap("【登记回执 #%s】PMO-p0-01 → %s：已登记成 #%s，主文档 https://example.feishu.cn/wiki/X%s" % (item, to, item, item))

    def wrap_up(self, sid, **fields):
        """The guard's answer to a wrap-up call; it must let the call through (exit 0), with or without a rewrite."""
        code, out = self.guard(sid, WRAPUP, dict({"recap": "做完了"}, **fields))
        self.assertEqual(code, 0, out)
        return out

    def ask(self, sid):
        return self.guard(sid, "AskUserQuestion", {"questions": []})

    def guard_log(self):
        return [json.loads(x) for x in self.read(".claude/logs/guard.jsonl").splitlines()]

    def announced(self):
        return [(r["id"], r["urgent"]) for r in pool_lib.read_events(self.tmp) if r.get("event") == "decision_announced"]

    def gates(self, sid):
        return [r.get("agent_turn_gate") for r in self.guard_log() if r.get("session_id") == sid and r.get("tool") == WRAPUP]


class InboxDeliveryTest(DeliveryCase):
    def test_the_wording_about_decisions(self):
        """2026-11-16: the list the researcher gets holds the decisions to be decided and those waiting for
        the researcher to carry them out; it and the mention at registration say who carries each out
        (执行方) and what the researcher must do by hand."""
        for path in (os.path.join(ROOT, "scaffold", "hooks", "inbox.py"), os.path.join(self.tmp, ".claude", "hooks", "inbox.py")):
            self.assertEqual(constant(path, "LIST_DUE"), LIST_DUE, path)
            self.assertEqual(constant(path, "QUEUED"), QUEUED_TEXT, path)

    def test_a_queued_decision_is_mentioned_in_the_turn_it_is_registered(self):
        self.receipt("2301", type="decision", queue=True, urgent=False)
        text = self.context("s-entry", self.registered("2301"))
        self.assertIn(WOKEN + "（来自 [示例] PMO-p0-01），研究员多半不在。面向研究员最多写一行状态：不提问，不标“等研究员回答”，"
                      "不复述已经登记过的决策；收尾里的提问会被 hook 去掉。" + QUEUED_TEXT % "2301", text)
        self.assertNotIn("它是插队的", text)
        st = self.session("s-entry")
        self.assertEqual(st["turn_origin"], "agent")
        self.assertNotIn("ask_ok", st)
        self.assertNotIn("researcher_at", st)

    def test_one_that_jumps_the_queue_may_be_asked_once(self):
        self.receipt("2302", type="decision", queue=True, urgent=True)
        text = self.context("s-entry", self.registered("2302"))
        self.assertIn("本轮登记了待定决策 #2302", text)
        self.assertIn("它是插队的：本轮可以在收尾里问研究员一次。", text)
        self.assertEqual(self.session("s-entry")["ask_ok"], "2302")
        self.context("s-entry", "好")
        self.assertNotIn("ask_ok", self.session("s-entry"))
        self.context("s-entry", wrap("【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md", "[示例] Alg-p2-01"))
        self.assertNotIn("ask_ok", self.session("s-entry"))
        self.assertEqual(self.announced(), [("2302", True)])

    def test_later_receipts_for_a_queued_decision_mention_nothing(self):
        """The scribe answers every request about a decision with the same 【登记回执 #ID】: only the first is its registration."""
        self.receipt("2302", type="decision", queue=True, urgent=True)
        self.context("s-entry", self.registered("2302"))
        self.context("s-entry", "选 B")                                                  # the researcher decides
        for later in (scribe_receipt("2302", "已确认“决定”，决定内容：选 B"), scribe_receipt("2302", "交接记录已追加：第 2 版"),
                      scribe_receipt("2302", "#2302 已归档"), wrap("【登记回执 #2302（重发，上一条作废）】PMO-p0-01 → PMO-p2-01：已登记成 #2302"),
                      REMINDERS + self.registered("2302")):
            text = self.context("s-entry", later)
            self.assertIn(WOKEN, text, later)
            self.assertNotIn(QUEUED, text, later)
            self.assertNotIn("它是插队的", text, later)
            self.assertNotIn("ask_ok", self.session("s-entry"), later)
            self.assertEqual(self.wrap_up("s-entry", **self.ASK).get("updatedInput"), {"recap": "做完了"}, later)
            self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "stripped")
        self.assertEqual(self.announced(), [("2302", True)])
        # a receipt the entry sees for another session is not a registration it mentions
        self.receipt("2303", type="decision", queue=True, urgent=True)
        self.assertNotIn(QUEUED, self.context("s-entry", self.registered("2303", to="Alg-p2-01")))
        self.assertNotIn("ask_ok", self.session("s-entry"))
        self.assertIn("本轮登记了待定决策 #2303", self.context("s-entry", self.registered("2303")))
        self.assertEqual(self.session("s-entry")["ask_ok"], "2303")

    def test_messages_behind_reminders_are_read(self):
        self.receipt("2302", type="decision", queue=True, urgent=True)
        self.assertIn("本轮登记了待定决策 #2302", self.context("s-entry", REMINDERS + self.registered("2302")))
        self.assertEqual(self.session("s-entry")["ask_ok"], "2302")
        self.prompt("s-alg1", REMINDERS + wrap("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md", "[示例] Alg-p2-01"))
        self.assertEqual(self.pool()["owners"]["2101"], "Alg-p1-01")
        self.assertEqual(self.session("s-alg1")["work_item"], "2101")

    def test_other_receipts_put_nothing_into_the_queue(self):
        self.receipt("2303", type="same_seq", queue=False, urgent=False)
        self.receipt("2304", status="registered", draft="packages/drafts/alg/x.md")       # written before 1.0.3
        self.receipt("2306", type="decision", queue=False, urgent=False)                    # 研究员主动发起
        for item in ("2303", "2304", "2306", "2399"):                                       # 2399 has no receipt
            text = self.context("s-entry", self.registered(item))
            self.assertIn(WOKEN, text, item)
            self.assertNotIn(QUEUED, text, item)
            self.assertNotIn("ask_ok", self.session("s-entry"), item)
        self.receipt("2305", name="2305-20261116-决策包-评测口径.md.json", type="decision", queue=True, urgent=False)
        self.assertIn("本轮登记了待定决策 #2305", self.context("s-entry", self.registered("2305")))

    def test_a_pool_session_woken_by_another_session_asks_nothing(self):
        self.receipt("2301", type="decision", queue=True, urgent=True)
        text = self.context("s-alg1", wrap("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 packages/drafts/alg/x.md", "[示例] Alg-p2-01"))
        self.assertIn(WOKEN + "（来自 [示例] Alg-p2-01）：不向研究员提问，要拍板的事照规则写进包里；收尾里的提问会被 hook 去掉。", text)
        self.assertNotIn(QUEUED, text)
        self.assertEqual(self.session("s-alg1")["turn_origin"], "agent")
        self.assertEqual(self.pool()["owners"]["2101"], "Alg-p1-01")          # the header inside the wrapper is read
        text = self.context("s-alg2", self.registered("2301", to="Alg-p2-01"))   # only the entry mentions decisions
        self.assertIn("不向研究员提问", text)
        self.assertNotIn(QUEUED, text)
        self.assertNotIn("ask_ok", self.session("s-alg2"))
        self.assertIn(WOKEN + "：不向研究员提问", self.context("s-q1", "<cross-session-message>\n继续\n</cross-session-message>"))
        self.assertIn(WOKEN + "，研究员多半不在。", self.context("s-entry", "<cross-session-message>\n继续\n</cross-session-message>"))
        # a bare header names its sender, and what the turn adds comes before the inbox
        self.route("notify", "Alg-p1-01", "--text", "规格 v3 已签字", sid="s-entry")
        text = self.context("s-alg1", "【委托 #2102】Alg-p2-01 → Alg-p1-01：需求包 x.md")
        self.assertIn(WOKEN + "（来自 Alg-p2-01）：", text)
        self.assertLess(text.index(WOKEN), text.index("规格 v3 已签字"))
        self.assertNotIn(SIDE_REPORT, text)
        # the app reports a session this one opened from the wrap-up as written, question and all
        text = self.context("s-alg2", SIDE)
        self.assertIn(WOKEN + "（来自 本会话开出的会话）：不向研究员提问，要拍板的事照规则写进包里；收尾里的提问会被 hook 去掉。" + SIDE_REPORT, text)
        self.assertIn(WOKEN + "（来自 本会话开出的会话），研究员多半不在。", self.context("s-entry", SIDE))
        self.assertIn(SIDE_REPORT, self.context("s-entry", SIDE))

    def test_the_researcher_coming_back_to_the_entry(self):
        text = self.context("s-entry", "早")
        self.assertIn(FIRST_TALK + LIST_DUE, text)
        for old in ("query blocked", "等研究员：", "阻塞", "待办"):      # what the researcher does by hand is a decision in the queue
            self.assertNotIn(old, text)
        self.assertNotIn(WOKEN, text)
        self.assertEqual(self.session("s-entry")["turn_origin"], "researcher")
        self.set_state("s-entry", researcher_at=self.ago(10))
        text = self.context("s-entry", "接着说")
        self.assertNotIn(LIST_DUE, text)
        self.assertNotIn("研究员隔了", text)
        self.set_state("s-entry", researcher_at=pool_lib.stamp(pool_lib.now() - pool_lib.FOCUS_IDLE + datetime.timedelta(minutes=1)))
        self.assertNotIn(LIST_DUE, self.context("s-entry", "还在"))                  # a minute short of pool.FOCUS_IDLE
        self.set_state("s-entry", researcher_at=self.ago(31))
        text = self.context("s-entry", "我回来了")
        m = re.search(r"研究员隔了 (\d+) 分钟回来。" + re.escape(LIST_DUE), text)
        self.assertTrue(m, text)
        self.assertLessEqual(abs(int(m.group(1)) - 31), 1)
        # a turn another session starts leaves researcher_at alone
        self.set_state("s-entry", researcher_at=self.ago(45))
        self.assertNotIn(LIST_DUE, self.context("s-entry", wrap("【通知】Alg-p2-01 → PMO-p2-01：消融 A 跑完了", "[示例] Alg-p2-01")))
        m = re.search(r"研究员隔了 (\d+) 分钟回来", self.context("s-entry", "结果呢"))
        self.assertTrue(m)
        self.assertLessEqual(abs(int(m.group(1)) - 45), 1)
        self.assertNotIn(LIST_DUE, self.context("s-alg2", "早"))              # only the entry lists them

    def test_a_session_from_before_1_0_3_is_not_told_it_is_the_first_message(self):
        self.context("s-entry", wrap("【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md", "[示例] Alg-p2-01"))
        self.set_state("s-entry", turn_origin="message", prompts=40, plugin_version="1.0.2")   # no researcher_at in 1.0.2
        text = self.context("s-entry", "我回来了，权限策略那件事怎么样了？")
        self.assertIn(FIRST_TALK + LIST_DUE, text)
        self.assertNotIn("第一条消息", text)

    def test_reminders_in_front_of_the_researchers_words(self):
        text = self.context("s-entry", TYPED)
        self.assertEqual(self.session("s-entry")["turn_origin"], "researcher")
        self.assertIn(FIRST_TALK + LIST_DUE, text)
        self.assertNotIn(WOKEN, text)
        self.set_state("s-entry", researcher_at=self.ago(40))
        self.assertIn(LIST_DUE, self.context("s-entry", TYPED))

    def test_a_background_task_report_adds_nothing(self):
        for prompt in (NOTIFIED, ONLY_REMINDERS):
            text = self.context("s-entry", prompt)
            st = self.session("s-entry")
            self.assertEqual(st["turn_origin"], "system")
            self.assertNotIn("researcher_at", st)
            self.assertNotIn(WOKEN, text)
            self.assertNotIn(LIST_DUE, text)
        text = self.context("s-alg1", NOTIFIED)
        self.assertEqual(self.session("s-alg1")["turn_origin"], "system")
        self.assertNotIn(WOKEN, text)

    def test_an_opening_line_behind_reminders_still_claims(self):
        line = "认领 infra-p2（会话名 Infra-p2-01），然后照认领输出开出本序列的 +1 和 +0。"
        _, out = self.prompt("s-infra2", TYPED.replace("这个会话出bug了，重试", line))
        self.assertIn("已按开局句认领", out.get("additionalContext", ""))
        self.assertEqual(self.pool()["claims"]["Infra-p2-01"]["session_id"], "s-infra2")
        self.assertNotIn("researcher_at", self.session("s-infra2"))       # the opening line is no talk yet
        # another session's message is not the researcher's opening line
        self.prompt("s-product2", wrap("认领 product-p2（会话名 Product-p2-01），然后照认领输出开出本序列的 +1 和 +0。"))
        self.assertNotIn("Product-p2-01", self.pool()["claims"])


# ------------------------------------------------------------------ guard
class GuardDeliveryTest(DeliveryCase):
    def test_a_woken_pool_session_loses_the_question(self):
        self.prompt("s-alg1", wrap("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md", "[示例] Alg-p2-01"))
        out = self.wrap_up("s-alg1", **self.ASK)
        self.assertEqual(out.get("permissionDecision"), "allow")
        self.assertEqual(out.get("updatedInput"), {"recap": "做完了"})
        self.assertIn("这一轮是其他 agent 会话叫醒的：收尾里的提问已去掉，只留回顾。", out.get("additionalContext", ""))
        last = self.guard_log()[-1]
        self.assertEqual((last["tool"], last["agent_turn_gate"]), (WRAPUP, "stripped"))
        self.assertEqual(last["soft_hits"], [])
        self.assertEqual((last["asked"], last["turn_origin"]), (True, "agent"))
        out = self.wrap_up("s-alg1", ask="先跑哪个？", note="其余的键原样留着")
        self.assertEqual(out.get("updatedInput"), {"recap": "做完了", "note": "其余的键原样留着"})
        for one in ({"blocked": True}, {"actions": [{"label": "先跑 A"}]}):         # any one of the three is a question
            self.assertEqual(self.wrap_up("s-alg1", **one).get("updatedInput"), {"recap": "做完了"}, one)
            self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "stripped")
        self.prompt("s-alg1", "先停一下")                                     # the researcher's turn
        self.assertEqual(self.wrap_up("s-alg1", **self.ASK), {})
        last = self.guard_log()[-1]
        self.assertNotIn("agent_turn_gate", last)
        self.assertEqual((last["asked"], last["turn_origin"]), (True, "researcher"))

    def test_the_entry_may_ask_once_about_a_decision_that_jumps_the_queue(self):
        self.receipt("2302", type="decision", queue=True, urgent=True)
        self.prompt("s-entry", self.registered("2302"))
        out = self.wrap_up("s-entry", **self.ASK)
        self.assertNotIn("updatedInput", out)
        self.assertNotIn("permissionDecision", out)
        self.assertIn("本轮登记了插队决策 #2302，可以问这一次", out.get("additionalContext", ""))
        self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "allowed_urgent")
        code, out = self.ask("s-entry")                                        # the one question goes in the wrap-up
        self.assertEqual((code, out.get("permissionDecision")), (0, "deny"))
        self.assertIn("本轮登记了插队决策 #2302：在收尾里问研究员这一次。", out.get("additionalContext", ""))
        self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "denied")
        self.prompt("s-entry", wrap("【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md", "[示例] Alg-p2-01"))
        self.assertEqual(self.wrap_up("s-entry", **self.ASK).get("updatedInput"), {"recap": "做完了"})
        code, out = self.ask("s-entry")
        self.assertEqual((code, out.get("permissionDecision")), (0, "deny"))
        self.assertIn("要研究员拍板的事写成决策包，请书记员登记进决策队列。", out.get("additionalContext", ""))
        # a session that is not the entry loses the question even with ask_ok in its state
        self.prompt("s-alg2", wrap("【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md", "[示例] Alg-p1-01"))
        self.set_state("s-alg2", ask_ok="2302")
        self.assertEqual(self.wrap_up("s-alg2", **self.ASK).get("updatedInput"), {"recap": "做完了"})
        self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "stripped")

    def test_the_researchers_own_session_and_a_wrap_up_without_a_question(self):
        self.prompt("s-mine", wrap("【通知】PMO-p2-01 → PMO-p2-01：看一下", "[示例] PMO-p2-01"))
        self.assertEqual(self.session("s-mine")["turn_origin"], "agent")
        self.assertEqual(self.wrap_up("s-mine", **self.ASK), {})
        self.prompt("s-alg1", wrap("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md", "[示例] Alg-p2-01"))
        self.assertEqual(self.wrap_up("s-alg1"), {})
        self.assertEqual(self.wrap_up("s-alg1", ask="", actions=[], blocked=False), {})
        self.assertNotIn("agent_turn_gate", self.guard_log()[-1])
        self.set_state("s-alg1", turn_origin="message")                       # written before 1.0.3: not agent
        self.assertEqual(self.wrap_up("s-alg1", **self.ASK), {})

    def test_ask_user_question_in_a_woken_turn_is_denied(self):
        self.prompt("s-alg2", wrap("【委托 #2105】PMO-p2-01 → Alg-p2-01：需求包 x.md", "[示例] PMO-p2-01"))
        _, out = self.guard("s-alg2", "AskUserQuestion", {"questions": []})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertEqual(out.get("permissionDecisionReason"), DENIED)
        self.assertIn("【需拍板】", out.get("additionalContext", ""))
        self.assertEqual(self.guard_log()[-1]["agent_turn_gate"], "denied")
        self.prompt("s-alg0", wrap("【委托 #2106】Alg-p1-01 → Alg-p0-01：需求包 x.md\n会话名：Alg-p0-2106", "[示例] Alg-p1-01"))
        _, out = self.guard("s-alg0", "AskUserQuestion", {"questions": []})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("交给上一级的交付包", out.get("additionalContext", ""))
        self.prompt("s-alg2", "研究员：先停一下")
        self.assertEqual(self.ask("s-alg2"), (0, {}))
        self.assertNotIn("agent_turn_gate", self.guard_log()[-1])
        self.prompt("s-mine", wrap("【通知】PMO-p2-01 → PMO-p2-01：看一下", "[示例] PMO-p2-01"))
        self.assertEqual(self.ask("s-mine"), (0, {}))

    def test_the_apps_own_turns_are_left_alone(self):
        for sid in ("s-alg1", "s-entry"):
            for prompt in (NOTIFIED, ONLY_REMINDERS):
                self.prompt(sid, prompt)
                self.assertEqual(self.wrap_up(sid, **self.ASK), {}, (sid, prompt))
                last = self.guard_log()[-1]
                self.assertNotIn("agent_turn_gate", last)
                self.assertEqual((last["asked"], last["turn_origin"]), (True, "system"))
                self.assertEqual(self.ask(sid), (0, {}))

    def test_audit_counts_the_questions_by_who_started_the_turn(self):
        self.receipt("2302", type="decision", queue=True, urgent=True)
        self.prompt("s-entry", self.registered("2302"))
        self.wrap_up("s-entry", **self.ASK)                                   # the one the entry may ask
        self.prompt("s-entry", wrap("【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md", "[示例] Alg-p2-01"))
        self.wrap_up("s-entry", **self.ASK)                                   # taken out
        self.wrap_up("s-entry")                                               # nothing asked
        self.prompt("s-entry", "早")
        self.wrap_up("s-entry", **self.ASK)                                   # the researcher is there
        self.prompt("s-alg1", "【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md")   # a header without the app's wrapper
        self.wrap_up("s-alg1", blocked=True)
        code, text = self.route("audit", sid="s-entry")
        self.assertEqual(code, 0, text)
        self.assertEqual(audit_lines(text, "池会话带提问的收尾，按这一轮是谁开的"), [
            "- Alg-p1-01：其他 agent 叫醒的轮次 1 次，提问留下 0 次（其中插队决策放行 0 次）；研究员开的轮次 0 次；应用的轮次 0 次",
            "- PMO-p2-01：其他 agent 叫醒的轮次 2 次，提问留下 1 次（其中插队决策放行 1 次）；研究员开的轮次 1 次；应用的轮次 0 次"])
        self.assertEqual(audit_lines(text, "有插件抬头、没有桌面应用外壳的消息"),
                         ["- Alg-p1-01：1 次；最近一次：【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md"])


# ----------------------------------------------------------------- replay
class ReplayTest(DeliveryCase):
    """The day from the issue: other sessions wake the entry 90 times; the researcher talks to it twice,
    each time 30 minutes or more after the last. Three of the wake-ups are the scribe's later receipts
    for decisions already in the queue (决定 confirmed, archived): they mention nothing."""

    def test_ninety_wake_ups_and_two_talks(self):
        for item, urgent in (("2401", False), ("2402", True), ("2403", False)):    # decisions in the queue
            self.receipt(item, type="decision", queue=True, urgent=urgent)
        self.receipt("2410", type="same_seq", queue=False, urgent=False)
        turns = []
        for i in range(90):
            if i in (10, 40, 70):
                turns.append(("receipt", self.registered({10: "2401", 40: "2402", 70: "2403"}[i])))
            elif i in (25, 55):                                                    # a decision revised: no new one
                item = "2401" if i == 25 else "2403"
                turns.append(("revised", wrap("【通知 #%s】Alg-p2-01 → PMO-p2-01：决策包 #%s 改版（v2），选项 B 的代价改了" % (
                    item, item), "[示例] Alg-p2-01")))
            elif i in (31, 82, 85):                                                # the same ID again: not a registration
                item, what = {31: ("2401", "已确认“决定”，决定内容：选 A"), 82: ("2402", "已确认“决定”，决定内容：选 B"),
                              85: ("2401", "#2401 已归档")}[i]
                turns.append(("later", scribe_receipt(item, what)))
            elif i % 3 == 0:
                turns.append(("other", wrap("【交付 #%d】Alg-p2-01 → PMO-p2-01：交付包 packages/drafts/alg/r%d.md" % (
                    2500 + i, i), "[示例] Alg-p2-01")))
            elif i % 3 == 1:
                turns.append(("other", self.registered("2410")))
            else:
                turns.append(("other", wrap("【通知】Quality-p2-01 → PMO-p2-01：评测集第 %d 轮跑完了" % i, "[示例] Quality-p2-01")))
        talks = {30: "评测口径那条决策我看了，选 A", 80: "今天先到这，明天看结果"}
        seen = {"receipt": [], "revised": [], "later": [], "other": [], "talk": []}
        for i, (kind, text) in enumerate(turns):
            seen[kind].append(self.context("s-entry", text))
            self.wrap_up("s-entry", ask="要不要现在定？")
            if i in talks:
                self.set_state("s-entry", researcher_at=self.ago(40 if i == 30 else 50))
                seen["talk"].append(self.context("s-entry", talks[i]))
                self.wrap_up("s-entry", ask="选 A 还是 B？")
        woken = seen["receipt"] + seen["revised"] + seen["later"] + seen["other"]
        self.assertEqual((len(woken), len(seen["later"])), (90, 3))
        self.assertTrue(all(WOKEN in c for c in woken))
        self.assertEqual(sum(c.count(QUEUED) for c in woken + seen["talk"]), 3)
        self.assertTrue(all(QUEUED in c for c in seen["receipt"]))
        self.assertFalse(any(QUEUED in c for c in seen["revised"] + seen["later"]))
        self.assertEqual(sum("它是插队的" in c for c in woken), 1)
        gates = self.gates("s-entry")
        self.assertEqual(len(gates), 92)
        self.assertEqual((gates.count("stripped"), gates.count("allowed_urgent"), gates.count("denied")), (89, 1, 0))
        self.assertEqual(len(seen["talk"]), 2)
        for c in seen["talk"]:
            self.assertEqual(c.count(LIST_DUE), 1)
            self.assertIn("研究员隔了", c)
            self.assertNotIn(WOKEN, c)
        self.assertNotIn("ask_ok", self.session("s-entry"))



# ----------------------------------------------------------------- 1.0.4
FROZEN_HEAD = "待定决策冻结的动作，决策定下以前谁都不做（hook 不拦，要自己守住），选项中立的准备照常做："
WAITS = "#%s 已到期，继续等，研究员来谈时排在最前；不推给研究员，不提醒截止。"
DELIVERED = wrap("【交付 #2101】Alg-p2-01 → PMO-p2-01：交付包 y.md", "[示例] Alg-p2-01")


def default_due(item, option, executor="RnD-p2-01"):
    return "#%s 已到期：请书记员按到期默认确认（ledger.py confirm %s --by 到期默认），书记员确认后通知执行方 %s 照到期默认动作办（%s）；已经请过的不再请。" % (
        item, item, executor, option)


class GatesInboxTest(DeliveryCase):
    """1.0.4: the inbox hook reads the local decision state (.claude/state/gates.json) and never writes it. Every
    pool session is told each turn what the decisions still waiting freeze. The entry is told each turn about
    those past their expiry time (B8): a reversible one is for the scribe to confirm by its default; any other
    keeps waiting, comes first when the researcher talks, and is not pushed to the researcher. A revision is
    never announced. 1.0.6: the expiry is the registration plus the time org/decisions.md states now, worked out
    as ledger.py works it out; the state keeps no expiry of its own."""

    def pending(self, item, hours, **fields):
        since = pool_lib.now() - datetime.timedelta(hours=hours)
        return dict({"id": item, "title": "事项 %s" % item, "registered_at": pool_lib.stamp(since), "frozen": []}, **fields)

    def gates(self, *entries, **more):
        with open(os.path.join(self.tmp, ".claude", "state", "gates.json"), "w", encoding="utf-8") as f:
            json.dump(dict({"version": 2, "pending": {e["id"]: e for e in entries}}, **more), f, ensure_ascii=False)

    def expiry(self, written):
        """org/decisions.md with the plugin's line about the expiry time saying this."""
        path = os.path.join(self.tmp, "org", "decisions.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(len(re.findall(r"可逆决策的到期时长：登记后 [\d.]+ (?:小时|天)", text)), 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(re.sub(r"可逆决策的到期时长：登记后 [\d.]+ (?:小时|天)", "可逆决策的到期时长：" + written, text))

    def test_what_has_expired_reaches_the_entry_only(self):
        reversible = self.pending("2701", 25, reversible=True, default="按轮次分目录", default_option="按轮次分目录", default_applies=True,
                                  executor="RnD-p2-01")
        self.gates(reversible,
                   self.pending("2702", 30, reversible=False, default="等研究员", default_option=None, default_applies=False),
                   self.pending("2703", 26, reversible=True, default="等研究员", default_option=None, default_applies=False,
                                executor="研究员", researcher_action="把旧记录拷到 NAS"),
                   self.pending("2704", 2, reversible=True, default="按变体分目录", default_option="按变体分目录", default_applies=True))
        woken = self.context("s-entry", DELIVERED)
        self.assertIn(WOKEN, woken)
        self.assertIn(default_due("2701", "按轮次分目录"), woken)
        for item in ("2702", "2703"):
            self.assertIn(WAITS % item, woken)
        self.assertNotIn("#2704", woken)                                          # registered 2 hours ago
        for sid, text in (("s-alg2", wrap("【交付 #2101】Alg-p1-01 → Alg-p2-01：交付包 y.md", "[示例] Alg-p1-01")), ("s-q1", "继续")):
            self.assertNotIn("已到期", self.context(sid, text), sid)              # the entry's alone
        talk = self.context("s-entry", "早")
        self.assertIn(FIRST_TALK + LIST_DUE + "已经到期、继续等研究员的决策（#2702、#2703）排在清单最前，不提截止。", talk)
        self.assertIn(default_due("2701", "按轮次分目录"), talk)                   # every turn, until the scribe confirms it
        self.assertNotIn(WOKEN, talk)
        self.assertIn(WAITS % "2702", self.context("s-entry", NOTIFIED))          # the app's turns too
        self.gates(self.pending("2702", 30, reversible=False, default="等研究员", default_applies=False))
        text = self.context("s-entry", DELIVERED)
        self.assertNotIn("#2701", text)                                           # confirmed: gone from the local state
        self.assertIn(WAITS % "2702", text)
        self.gates()
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))
        with open(os.path.join(self.tmp, ".claude", "state", "gates.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        code, out = self.prompt("s-entry", DELIVERED)                               # never blocks the prompt
        self.assertEqual(code, 0)
        self.assertIn(WOKEN, out.get("additionalContext", ""))

    def test_the_expiry_follows_decisions_md_at_once(self):
        """The entry is told a decision has expired by the expiry time org/decisions.md states now, the same time
        `confirm --by 到期默认` goes by: after the researcher changes the line the two still agree, with no write to
        the local state in between. What a state written before 1.0.6 kept as expires_at is not read."""
        late = self.pending("2731", 30, reversible=True, default="按轮次分目录", default_option="按轮次分目录", default_applies=True,
                            executor="RnD-p2-01", expires_at=pool_lib.stamp(pool_lib.now() - datetime.timedelta(hours=6)))
        waits = self.pending("2732", 30, reversible=False, default="等研究员", default_applies=False,
                             expires_at=pool_lib.stamp(pool_lib.now() + datetime.timedelta(days=30)))     # stale, the other way
        self.gates(late, waits, expiry_hours=24, expiry_from="默认", releases=[])
        woken = self.context("s-entry", DELIVERED)
        self.assertIn(default_due("2731", "按轮次分目录"), woken)
        self.assertIn(WAITS % "2732", woken)
        self.expiry("登记后 48 小时")                                               # 30 hours is not 48
        text = self.context("s-entry", DELIVERED)
        self.assertNotIn("已到期", text)
        self.assertNotIn("排在清单最前", self.context("s-entry", "早"))
        self.expiry("登记后 1 天")
        self.assertIn(default_due("2731", "按轮次分目录"), self.context("s-entry", DELIVERED))
        self.expiry("登记后 12 小时")
        self.gates(self.pending("2733", 13, reversible=True, default="A", default_option="A", default_applies=True, executor="Alg-p2-01"))
        self.assertIn(default_due("2733", "A", "Alg-p2-01"), self.context("s-entry", DELIVERED))
        self.expiry("登记后两天")                                                   # unreadable: 24 hours, and the prompt goes through
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))
        os.remove(os.path.join(self.tmp, "org", "decisions.md"))                    # no file: 24 hours
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))
        self.gates(self.pending("2734", 25, reversible=True, default="A", default_option="A", default_applies=True, executor="Alg-p2-01"),
                   self.pending("2735", 25, registered_at="不是时间", default_applies=True))        # no registration time: not expired
        text = self.context("s-entry", DELIVERED)
        self.assertIn(default_due("2734", "A", "Alg-p2-01"), text)
        self.assertNotIn("#2735", text)

    def test_a_decision_rolled_back_keeps_waiting(self):
        """1.0.6 (the researcher, 2026-11-16): a decision that was decided once and rolled back to 待决定 takes no
        default. Past its expiry the entry is told it keeps waiting and comes first when the researcher talks; the
        scribe is not asked to confirm it by its default. So is one a state from 1.0.4 or 1.0.5 marked reopened_at."""
        default = dict(reversible=True, default="按轮次分目录", default_option="按轮次分目录", executor="RnD-p2-01")
        back = pool_lib.stamp(pool_lib.now() - datetime.timedelta(hours=1))
        self.gates(self.pending("2741", 30, default_applies=False, rolled_back_at=back, **default),
                   self.pending("2742", 30, default_applies=True, reopened_at=back, **default),         # as 1.0.5 wrote it
                   self.pending("2743", 30, default_applies=True, **default),
                   self.pending("2744", 3, default_applies=False, rolled_back_at=back, **default))       # not past its expiry yet
        woken = self.context("s-entry", DELIVERED)
        for item in ("2741", "2742"):
            self.assertIn(WAITS % item, woken)
            self.assertNotIn("confirm %s --by 到期默认" % item, woken)
        self.assertIn(default_due("2743", "按轮次分目录"), woken)
        self.assertNotIn("#2744", woken)
        talk = self.context("s-entry", "早")
        self.assertIn(FIRST_TALK + LIST_DUE + "已经到期、继续等研究员的决策（#2741、#2742）排在清单最前，不提截止。", talk)

    def test_every_pool_session_sees_what_is_frozen(self):
        self.gates(self.pending("2711", 2, title="实验平台用什么隔离候选代码", frozen=[
            {"kind": "category", "category": "privileged_app", "name": "特权应用", "qualifier": "Docker", "text": "特权应用：Docker"},
            {"kind": "command", "prefix": "docker build", "words": ["docker", "build"], "text": "`docker build`"},
            {"kind": "text", "text": "照某个选项改实验平台的计量和内存闸"}]),
                   self.pending("2712", 1, title="运行记录怎么分目录", frozen=[{"kind": "text", "text": "把已有的运行记录挪到新的目录结构下"}]),
                   self.pending("2713", 1, title="不冻结什么"))
        line = (FROZEN_HEAD + "#2711（实验平台用什么隔离候选代码）：“特权应用：Docker”“`docker build`”“照某个选项改实验平台的计量和内存闸”；"
                "#2712（运行记录怎么分目录）：“把已有的运行记录挪到新的目录结构下”。")
        for sid, text in (("s-entry", "早"), ("s-alg1", wrap("【委托 #2101】Alg-p2-01 → Alg-p1-01：需求包 x.md", "[示例] Alg-p2-01")),
                          ("s-alg0", NOTIFIED), ("s-scribe", "继续"), ("s-q1", "继续")):
            self.assertEqual(self.context(sid, text).count(line), 1, sid)
        self.assertNotIn("#2713", self.context("s-alg2", "继续"))                 # nothing frozen, nothing said
        self.assertEqual(self.context("s-mine", "看看"), "")                       # the researcher's own session
        self.gates(self.pending("2714", 1, title="很长", frozen=[{"kind": "text", "text": "第 %d 件谁都不做的事，写得很长很长很长" % k}
                                                                  for k in range(40)]))
        text = [p for p in self.context("s-alg1", "继续").split("\n\n") if p.startswith(FROZEN_HEAD)][0]
        self.assertLessEqual(len(text), 700)
        self.assertTrue(text.endswith("……（全文看 python3 -I .claude/solo-lab/bin/ledger.py query queue 列的决策包）"), text[-80:])

    def test_a_revised_decision_is_not_announced_again(self):
        """The scribe's 【登记回执】 for a revision mentions nothing, even when the decision now jumps the queue and was
        registered before the entry kept track of what it mentioned."""
        self.receipt("2721", type="decision", queue=True, urgent=True, version=2,
                     revisions=[{"version": 2, "date": "2026-11-22", "note": "改成插队", "draft": "packages/drafts/alg/v2.md"}])
        text = self.context("s-entry", self.registered("2721"))
        self.assertIn(WOKEN, text)
        self.assertNotIn(QUEUED, text)
        self.assertNotIn("它是插队的", text)
        self.assertNotIn("ask_ok", self.session("s-entry"))
        self.assertEqual(self.announced(), [])
        self.assertEqual(self.wrap_up("s-entry", **self.ASK).get("updatedInput"), {"recap": "做完了"})


class ExpiryTest(DeliveryCase):
    """1.0.4, B8 with the ledger. A reversible decision registered more than 24 hours ago: the entry is told, the
    scribe's confirm --by 到期默认 writes a decision that starts with 到期默认： and is the option the package names,
    and the entry hears no more of it. An irreversible one past its expiry: confirm --by 到期默认 is refused, the pool
    sessions are still told what it freezes (the hook stops no command for it, 1.0.6), and the researcher gets no
    reminder; it comes first when they talk."""

    def setUp(self):
        super().setUp()
        self.fake = fake_space.FakeSpace()
        self.quiet(ledger.register, self.lg(), os.path.join(self.tmp, setup.FIRST_TASK))

    def lg(self):
        return fake_space.SimLedger(self.tmp, "apply", self.fake, ())

    def quiet(self, fn, *args, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kw)

    def register(self, block, name):
        text = re.sub(r"^- 关联工作项：#\d+[^；\n]*", "- 关联工作项：待建", example_blocks()[block], flags=re.M)
        path = os.path.join(self.tmp, "packages", "drafts", "pmo", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(re.sub(r"^- 所属研究任务：.*\n", "", text, flags=re.M))
        return self.quiet(ledger.register, self.lg(), path)

    def age(self, item, hours):
        """The decision as if it had been registered that many hours ago, in the local state and in its receipt."""
        since = (ledger.now_at() - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
        for path, entry in ((ledger.gates_path(self.tmp), lambda data: data["pending"][item]),
                            (os.path.join(self.tmp, "packages", "sent", "%s.json" % item), lambda data: data)):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            entry(data)["registered_at"] = since
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    def test_an_expired_decision_takes_its_default_only_when_reversible(self):
        rev, perm = self.register(11, "rev.md"), self.register(10, "perm.md")
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))
        self.age(rev, 25)
        self.age(perm, 100)
        woken = self.context("s-entry", DELIVERED)
        self.assertIn(default_due(rev, "按轮次分目录"), woken)
        self.assertIn(WAITS % perm, woken)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ledger.confirm(self.lg(), rev, ledger.BY_DEFAULT)
        self.assertIn("到期默认：照选项“按轮次分目录”办，执行完请书记员归档。", out.getvalue())
        content = self.fake.item(rev)["fields"][self.lg().field("decision", "决定内容")["field_key"]]
        self.assertEqual(content, "到期默认：按轮次分目录：runs/rnd/<轮次>/<变体>/（登记后 24 小时内研究员没有决定）")
        with self.assertRaises(ledger.LedgerError), contextlib.redirect_stdout(io.StringIO()):
            ledger.confirm(self.lg(), perm, ledger.BY_DEFAULT)
        woken = self.context("s-entry", DELIVERED)
        self.assertNotIn("#%s 已到期" % rev, woken)
        self.assertIn(WAITS % perm, woken)
        self.assertIn("#%s（实验平台用什么隔离候选代码）：“特权应用：Docker”" % perm, self.context("s-alg2", "继续"))    # still frozen, and told so
        self.assertEqual(self.bash("s-alg2", "open -a Docker"), (0, {}))               # the rules keep it, not the hook (1.0.6)
        talk = self.context("s-entry", "早")
        self.assertIn("已经到期、继续等研究员的决策（#%s）排在清单最前，不提截止。" % perm, talk)

    def test_the_hook_and_the_ledger_agree_after_the_time_is_changed(self):
        """The case that went wrong before 1.0.6: the researcher changes the expiry time in org/decisions.md, and
        until the next write to the local state the entry was told every turn to ask for 到期默认 while the scribe's
        command was refused (or, the time made shorter, the entry was told nothing). Both read the line now."""
        path = os.path.join(self.tmp, "org", "decisions.md")

        def expiry(written):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(re.sub(r"可逆决策的到期时长：登记后 [\d.]+ (?:小时|天)", "可逆决策的到期时长：" + written, text))

        def refused():
            try:
                self.quiet(ledger.confirm, self.lg(), rev, ledger.BY_DEFAULT)
            except ledger.LedgerError as e:
                return str(e)
            return None
        rev = self.register(11, "rev.md")
        self.age(rev, 30)
        expiry("登记后 48 小时")
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))               # the entry is not told ...
        self.assertIn("登记了 30 小时，还没满到期时长 48 小时（org/decisions.md）", refused())    # ... and the command would be refused
        expiry("登记后 24 小时")
        self.assertIn(default_due(rev, "按轮次分目录"), self.context("s-entry", DELIVERED))
        self.assertIsNone(refused())                                                # told, and taken
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))

    def test_a_decision_the_researcher_rolled_back_is_not_taken_by_its_default(self):
        """With the ledger: a reversible decision the researcher decided and then rolled back. However long it has
        waited, the entry is told it keeps waiting, the scribe's confirm --by 到期默认 is refused with the reason, and
        it comes first when the researcher talks."""
        rev = self.register(11, "rev.md")
        self.age(rev, 30)
        self.quiet(ledger.confirm, self.lg(), rev, "研究员", ["决定内容=按变体分目录", "决定人=研究员"], option="按变体分目录")
        self.assertNotIn("已到期", self.context("s-entry", DELIVERED))
        self.quiet(ledger.rollback, self.lg(), rev, "决定", "研究员想再看看", by="研究员")
        woken = self.context("s-entry", DELIVERED)
        self.assertIn(WAITS % rev, woken)
        self.assertNotIn("请书记员按到期默认确认", woken)
        with self.assertRaises(ledger.LedgerError) as e, contextlib.redirect_stdout(io.StringIO()):
            ledger.confirm(self.lg(), rev, ledger.BY_DEFAULT)
        self.assertIn("定过一次，又回滚到了待决定", str(e.exception))
        self.assertIn("已经到期、继续等研究员的决策（#%s）排在清单最前，不提截止。" % rev, self.context("s-entry", "早"))
        self.assertIn("#%s（运行记录按轮次还是按变体分目录）：“`python3 tools/relayout_runs.py`”" % rev, self.context("s-alg2", "继续"))   # frozen again

if __name__ == "__main__":
    unittest.main()
