#!/usr/bin/env python3
"""Setup timing, bounded metadata reads, and restart-safe knowledge node creation."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import onboarding
import space
import ledger
import docs
import ledger_core


class InitializationTest(unittest.TestCase):
    def test_metadata_reads_overlap_and_keep_type_identity(self):
        barrier = threading.Barrier(4)
        def read(key, t):
            barrier.wait(timeout=2)                                  # all four types are being read at once
            return t["type_key"], {"name": t["name"], "fields": [{"field_key": t["type_key"] + "-field"}]}
        types = {"list": [{"type_key": str(n), "name": str(n), "api_name": "t%d" % n, "is_disable": 2} for n in range(4)]}
        with mock.patch.object(space, "call", return_value=types), mock.patch.object(space, "read_type", side_effect=read):
            result = space.snapshot({"project_key": "space"}, workers=4)["types"]
        self.assertEqual(list(result), ["0", "1", "2", "3"])
        for key, item in result.items():
            self.assertEqual(item["fields"][0]["field_key"], key + "-field")

    def test_metadata_failures_do_not_become_partial_success(self):
        types = {"list": [{"type_key": "a", "name": "a", "is_disable": 2}]}
        with mock.patch.object(space, "call", return_value=types), \
                mock.patch.object(space, "read_type", side_effect=space.SpaceError("read failure")):
            with self.assertRaisesRegex(space.SpaceError, "read failure"):
                space.snapshot({"project_key": "space"})

    def test_node_setup_lists_each_parent_once_and_rerun_reuses_ids(self):
        with tempfile.TemporaryDirectory(prefix="init[scope]-") as root:
            path = Path(root) / "feishu/ids.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"wiki": {"space_id": "wiki"}}))
            lg = ledger_core.Ledger(root, "apply")
            calls, created = [], []
            def lark(*args, **kw):
                calls.append(args)
                if args[1] == "+node-list": return {"data": {"nodes": []}}
                self.assertEqual(args[1], "+node-create")
                created.append(args)
                return {"data": {"node_token": "node-" + str(len(created)), "obj_token": "doc-" + str(len(created))}}
            with mock.patch.object(lg, "lark", side_effect=lark), contextlib.redirect_stdout(io.StringIO()):
                docs.ensure_all_nodes(lg)
                docs.ensure_all_nodes(lg)
            self.assertEqual(len(created), 21)                         # 7 top-level nodes, 7 sequences under two of them
            self.assertEqual(sum(c[1] == "+node-list" for c in calls), 3)
            self.assertEqual(len(json.loads(path.read_text())["wiki"]["nodes"]), 21)
            self.assertFalse(hasattr(lg, "_node_listing_cache"))


class StageClockTest(unittest.TestCase):
    """onboarding.py: stages with fixed categories, and the Feishu figure held against 300 seconds."""

    T0 = dt.datetime(2026, 11, 12, 10, 0, 0, tzinfo=dt.timezone.utc)

    def at(self, seconds):
        return self.T0 + dt.timedelta(seconds=seconds)

    def run_stages(self, root, stages, begin=0):
        onboarding.clock(root, "begin", at=self.at(begin))
        for name, start, end in stages:
            onboarding.clock(root, "start", name, self.at(start))
            onboarding.clock(root, "end", name, self.at(end))

    def test_only_feishu_stages_count_towards_the_figure(self):
        with tempfile.TemporaryDirectory(prefix="clock[scope]-") as root:
            self.run_stages(root, (("precheck", 0, 10), ("login", 10, 200), ("project_space", 200, 260), ("binding", 260, 290),
                                   ("sessions", 290, 700), ("nodes", 300, 400), ("first_task", 400, 440), ("readback", 700, 720)))
            result = onboarding.clock(root, "finish", at=self.at(725))
            self.assertEqual((result["kind"], result["elapsed_seconds"], result["feishu_seconds"]), ("first", 725.0, 250.0))
            self.assertTrue(result["timing_pass"])                    # waiting for a scan and opening sessions are not Feishu time
            self.assertEqual(result["categories"]["human"], {"seconds": 190.0, "count": 1})
            self.assertEqual(onboarding.STAGES["resume"][0], "human")       # waiting for the word between two rounds of the pool
            self.assertEqual(result["categories"]["claude"]["seconds"], 410.0)
            text = onboarding.text(result)
            self.assertIn("飞书操作 250.0 秒（指标 300 秒以内：达到）", text)
            self.assertNotIn("最慢的 3 段", text)
            self.assertTrue(Path(root, ".claude/state/onboarding.json").is_file())

    def test_over_the_figure_names_the_slowest_stages_and_fails_nothing(self):
        with tempfile.TemporaryDirectory(prefix="clock-") as root:
            self.run_stages(root, (("project_space", 0, 200), ("nodes", 200, 320), ("first_task", 320, 350)))
            result = onboarding.clock(root, "finish", at=self.at(360))
            self.assertFalse(result["timing_pass"])
            self.assertIn("最慢的 3 段：项目空间查找与创建 200.0 秒、知识库节点 120.0 秒、首条研究任务登记 30.0 秒", onboarding.text(result))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(onboarding.main(["status", "--target", root]), 0)

    def test_a_later_run_is_kept_apart_from_the_first(self):
        with tempfile.TemporaryDirectory(prefix="clock-") as root:
            self.run_stages(root, (("nodes", 0, 50),))
            onboarding.clock(root, "begin", at=self.at(20))            # begin again while it runs: the same run
            onboarding.clock(root, "finish", at=self.at(60))
            self.run_stages(root, (("readback", 1000, 1010),), begin=1000)
            second = onboarding.clock(root, "finish", at=self.at(1020))
            self.assertEqual((second["kind"], second["feishu_seconds"]), ("repair", 10.0))
            runs = json.loads(Path(root, ".claude/state/onboarding.json").read_text(encoding="utf-8"))["runs"]
            self.assertEqual([(r["kind"], len(r["stages"])) for r in runs], [("first", 1), ("repair", 1)])

    def test_stages_are_started_and_ended_in_order(self):
        with tempfile.TemporaryDirectory(prefix="clock-") as root:
            with self.assertRaises(onboarding.ClockError):
                onboarding.clock(root, "start", "nodes")                # nothing has begun
            onboarding.clock(root, "begin", at=self.at(0))
            for action, stage in (("start", "nope"), ("end", "nodes")):
                with self.assertRaises(onboarding.ClockError):
                    onboarding.clock(root, action, stage)
            onboarding.clock(root, "start", "nodes", self.at(1))
            for action in ("start", "finish"):
                with self.assertRaises(onboarding.ClockError):
                    onboarding.clock(root, action, "nodes" if action == "start" else None, self.at(2))
            onboarding.clock(root, "start", "prompts", self.at(3))      # stages of two sessions overlap
            onboarding.clock(root, "end", "prompts", self.at(5))
            onboarding.clock(root, "start", "prompts", self.at(8))
            onboarding.clock(root, "end", "prompts", self.at(9))
            onboarding.clock(root, "end", "nodes", self.at(10))
            result = onboarding.clock(root, "finish", at=self.at(11))
            self.assertEqual(result["stages"]["prompts"], {"seconds": 3.0, "count": 2, "open": False})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(onboarding.main(["start", "--target", root]), 2)
            self.assertIn("start 和 end 要给阶段", out.getvalue())


if __name__ == "__main__":
    unittest.main()
