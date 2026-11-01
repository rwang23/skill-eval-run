"""Behavior tests for skill-eval-run."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load("eval_run")
results = _load("eval_results")

SKILL = """---
name: sample-skill
description: Sample Skill for tests. Use when testing.
---

1. Do the thing.
"""


def make_skill(root: Path) -> Path:
    skill = root / "sample-skill"
    (skill / "evals" / "fixtures" / "notes").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (skill / "scripts" / "tool.py").write_bytes(b"print(1)\n")
    (skill / "evals" / "fixtures" / "notes" / "input.md").write_text("hello", encoding="utf-8")
    cases = [
        {"id": "positive-a", "prompt": "Use the sample skill on the notes.", "fixture": "notes",
         "expected_output": "Uses the skill.", "expectations": ["Reads the notes and uses the sample skill."],
         "routing": {"case_type": "positive", "should_trigger": ["sample-skill"], "should_not_trigger": []}},
        {"id": "held-out-b", "prompt": "A held-out request.", "context": "Look in {workspace} first.",
         "expected_output": "Answers.", "expectations": ["Answers the held-out request."], "pressure": "deadline",
         "routing": {"case_type": "held-out", "should_trigger": ["sample-skill"], "should_not_trigger": []}},
    ]
    (skill / "evals" / "evals.json").write_text(json.dumps({"evals": cases}), encoding="utf-8")
    return skill


class EvalRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.skill = make_skill(self.root)
        self.run_dir = self.root / "run"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def plan(self, *executors: str) -> dict:
        argv = ["plan", str(self.skill), "--out", str(self.run_dir)]
        for e in executors:
            argv += ["--executor", e]
        self.assertEqual(run.main(argv), 0)
        return json.loads((self.run_dir / "run.json").read_text(encoding="utf-8"))

    def test_plan_counts_runs_and_rejects_bad_input(self) -> None:
        plan = self.plan("host:model-a", "codex:model-b")
        self.assertEqual(plan["runs"], 4)
        self.assertEqual(plan["package_revision"], results.package_revision(self.skill))
        self.assertEqual(run.main(["plan", str(self.skill), "--out", str(self.run_dir), "--executor", "host:x"]), 3, "non-empty run dir")
        self.assertEqual(run.main(["plan", str(self.skill), "--out", str(self.root / "r2"), "--executor", "nope:x"]), 3)
        self.assertEqual(run.main(["plan", str(self.skill), "--out", str(self.root / "r3"), "--executor", "host:x", "--cases", "ghost"]), 3)

    def test_prepare_isolates_workspaces_and_writes_blind_prompts(self) -> None:
        self.plan("host:model-a", "host:model-b")
        self.assertEqual(run.main(["prepare", str(self.run_dir)]), 0)
        for name in ("host-model-a", "host-model-b"):
            workspace = self.run_dir / "workspaces" / name / "positive-a"
            self.assertEqual((workspace / "input.md").read_text(encoding="utf-8"), "hello")
            prompt = (self.run_dir / "prompts" / name / "positive-a.txt").read_text(encoding="utf-8")
            self.assertIn(str(workspace), prompt)
            self.assertNotIn("eval", prompt.lower(), "the executor is not told it is being evaluated")
        held = (self.run_dir / "prompts" / "host-model-a" / "held-out-b.txt").read_text(encoding="utf-8")
        self.assertTrue(held.endswith(f"Look in {self.run_dir / 'workspaces' / 'host-model-a' / 'held-out-b'} first."))

    def test_execute_needs_confirmation_and_runs_a_cli_adapter(self) -> None:
        script = ("import sys,json,pathlib; p=sys.stdin.read(); pathlib.Path('out.txt').write_text('x');"
                  "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'python scripts/tool.py && cat C:/s/sample-skill/SKILL.md'}}));"
                  "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done: '+str(len(p))}}))")
        run.ADAPTERS["fake"] = {"argv": [sys.executable, "-c", script], "prompt": "stdin"}
        try:
            self.plan("fake:model-x")
            run.main(["prepare", str(self.run_dir)])
            self.assertEqual(run.main(["execute", str(self.run_dir)]), 3, "refuses without --yes")
            self.assertEqual(run.main(["execute", str(self.run_dir), "--yes", "--max-parallel", "2"]), 0)
            self.assertEqual(run.main(["facts", str(self.run_dir)]), 0)
        finally:
            del run.ADAPTERS["fake"]
        facts = {f["run_id"]: f for f in json.loads((self.run_dir / "facts.json").read_text(encoding="utf-8"))}
        fact = facts["fake-model-x/positive-a"]
        self.assertEqual(fact["skills_read_or_invoked"], ["sample-skill"])
        self.assertEqual(fact["scripts_run"], ["tool.py"])
        self.assertEqual(fact["workspace_created"], ["out.txt"])
        self.assertTrue(fact["answer"].startswith("done: "))

    def test_timeout_kills_the_whole_process_tree(self) -> None:
        marker = self.root / "grandchild.pid"
        child = ("import subprocess,sys,time,pathlib;"
                 f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);"
                 f"pathlib.Path(r'{marker}').write_text(str(p.pid)); time.sleep(120)")
        run.ADAPTERS["sleepy"] = {"argv": [sys.executable, "-c", child], "prompt": "argument"}
        try:
            self.plan("sleepy:model-z")
            run.main(["prepare", str(self.run_dir)])
            self.assertEqual(run.main(["execute", str(self.run_dir), "--yes", "--timeout", "5", "--executor", "sleepy-model-z"]), 0)
        finally:
            del run.ADAPTERS["sleepy"]
        log = [json.loads(line) for line in (self.run_dir / "execution.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(entry["exit"] == "timeout" for entry in log))
        pid = int(marker.read_text())
        import subprocess as sp
        if sys.platform == "win32":
            alive = str(pid) in sp.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        else:
            import os
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        self.assertFalse(alive, "the grandchild must not survive a timeout")

    def test_claude_transcript_attach_facts_packet_and_collect(self) -> None:
        self.plan("host:claude-sonnet", "host:gpt-terra")
        run.main(["prepare", str(self.run_dir)])
        transcript = self.root / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(x) for x in [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "sample-skill"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Final answer."}]}},
        ]), encoding="utf-8")
        for executor in ("host-claude-sonnet", "host-gpt-terra"):
            for case in ("positive-a", "held-out-b"):
                self.assertEqual(run.main(["attach", str(self.run_dir), "--executor", executor, "--case", case, "--transcript", str(transcript)]), 0)
        (self.run_dir / "workspaces" / "host-gpt-terra" / "positive-a" / "input.md").write_text("changed", encoding="utf-8")
        self.assertEqual(run.main(["facts", str(self.run_dir)]), 0)
        facts = {f["run_id"]: f for f in json.loads((self.run_dir / "facts.json").read_text(encoding="utf-8"))}
        self.assertEqual(facts["host-claude-sonnet/positive-a"]["skills_read_or_invoked"], ["sample-skill"])
        self.assertEqual(facts["host-gpt-terra/positive-a"]["workspace_modified"], ["input.md"])
        self.assertEqual(run.main(["packet", str(self.run_dir)]), 0)
        packet = json.loads((self.run_dir / "judge-packet.json").read_text(encoding="utf-8"))
        self.assertEqual(len(packet), 4)
        self.assertEqual(packet[0]["answer"], "Final answer.")

        judgement = self.root / "j.json"
        entries = [{"run_id": p["run_id"], "routing": "pass", "compliance": "pass", "expectations": "pass",
                    "passed": p["run_id"] != "host-gpt-terra/positive-a", "reason": "r"} for p in packet]
        judgement.write_text(json.dumps(entries), encoding="utf-8")
        self.assertEqual(run.main(["collect", str(self.run_dir), "--judgement", str(judgement), "--judge", "judge-x", "--judge-model", "claude-sonnet"]), 3,
                         "a judge model that executed cases is not independent")
        self.assertEqual(run.main(["collect", str(self.run_dir), "--judgement", str(judgement), "--judge", "self"]), 3)
        self.assertEqual(run.main(["collect", str(self.run_dir), "--judgement", str(judgement), "--judge", "judge-x", "--judge-model", "opus"]), 0)
        recorded = json.loads((self.skill / "evals" / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["schema_version"], 2)
        self.assertEqual(recorded["package_revision"], results.package_revision(self.skill))
        self.assertEqual({r["model"] for r in recorded["runs"]}, {"claude-sonnet", "gpt-terra"})
        self.assertTrue(all(r["kind"] == "client" and r["judge"] == "judge-x" and r["blind"] and r["isolated"] for r in recorded["runs"]))
        report = (self.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Differs between executors: positive-a", report)

    def test_collect_refuses_when_package_changed_after_plan_or_runs_missing(self) -> None:
        self.plan("host:m1")
        run.main(["prepare", str(self.run_dir)])
        transcript = self.root / "t.txt"
        transcript.write_text("plain text answer", encoding="utf-8")
        for case in ("positive-a", "held-out-b"):
            run.main(["attach", str(self.run_dir), "--executor", "host-m1", "--case", case, "--transcript", str(transcript)])
        run.main(["facts", str(self.run_dir)])
        partial = self.root / "j.json"
        partial.write_text(json.dumps([{"run_id": "host-m1/positive-a", "passed": True}]), encoding="utf-8")
        self.assertEqual(run.main(["collect", str(self.run_dir), "--judgement", str(partial), "--judge", "j", "--judge-model", "m9"]), 3)
        full = self.root / "k.json"
        full.write_text(json.dumps([{"run_id": f"host-m1/{c}", "passed": True} for c in ("positive-a", "held-out-b")]), encoding="utf-8")
        (self.skill / "scripts" / "tool.py").write_bytes(b"print(2)\n")
        self.assertEqual(run.main(["collect", str(self.run_dir), "--judgement", str(full), "--judge", "j", "--judge-model", "m9"]), 3)
        self.assertFalse((self.skill / "evals" / "results.json").exists())


class ResultsWriterTests(unittest.TestCase):
    def test_new_package_archives_old_runs_and_self_judging_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skill = make_skill(Path(temp))
            results.record_run(skill, {"positive-a": True}, runner="r", model="m1", judge="j", observed_at="2026-11-01T00:00:00Z")
            results.record_run(skill, {"positive-a": False}, runner="r", model="m1", judge="j", observed_at="2026-11-01T01:00:00Z")
            data = json.loads((skill / "evals" / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["runs"]), 1, "same kind, runner, and model replaces its run")
            (skill / "scripts" / "tool.py").write_bytes(b"print(3)\n")
            results.record_run(skill, {"positive-a": True}, runner="r", model="m1", judge="j", observed_at="2026-11-01T02:00:00Z")
            history = (skill / "evals" / "history.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(history), 1)
            self.assertEqual(json.loads(history[0])["passing_by_run"], {"client|m1": []})
            with self.assertRaises(ValueError):
                results.record_run(skill, {"positive-a": True}, runner="r", model="m1", judge="self", observed_at="x")
            with self.assertRaises(ValueError):
                results.record_run(skill, {"ghost": True}, runner="r", model="m1", judge="j", observed_at="x")


class PackageContractTests(unittest.TestCase):
    def test_every_eval_case_is_specified_and_bounded(self) -> None:
        data = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["evals"]), 10)
        for case in data["evals"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["expected_output"].strip())
                self.assertTrue(case["expectations"])
                self.assertIn(case["routing"]["case_type"], {"positive", "negative", "near-neighbor", "held-out"})

    def test_skill_md_stays_standalone(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: skill-eval-run", text)
        self.assertLess(len(text.splitlines()), 200)


if __name__ == "__main__":
    unittest.main()
