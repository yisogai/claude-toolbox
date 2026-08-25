#!/usr/bin/env python3
"""codex_pool.py の app-server 多重化を、実機 Codex なしで検証する。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
POOL = SCRIPTS / "codex_pool.py"
MOCK = REPO / "tests" / "mock_app_server.py"
sys.path.insert(0, str(SCRIPTS))
from codex_run import try_acquire_slot  # noqa: E402


class PoolBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-pool-test-"))
        self.root = self.tmp / "root"
        (self.root / "var").mkdir(parents=True)
        shutil.copytree(REPO / "config", self.root / "config")
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, config=None, **extra):
        value = dict(os.environ)
        value["CODEX_BRIDGE_ROOT"] = str(self.root)
        value["MOCK_APP_SERVER_CONFIG"] = json.dumps(config or {}, ensure_ascii=False)
        value.update({k: str(v) for k, v in extra.items()})
        return value

    def jobs_file(self, jobs):
        path = self.tmp / "jobs.json"
        path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        return path

    def jobs(self, *ids):
        return [{"id": ident, "prompt": ident, "cwd": str(self.cwd)} for ident in ids]

    def run_pool(self, jobs, config=None, extra=(), timeout=30, env=None):
        output = self.tmp / "pool"
        argv = [sys.executable, str(POOL), "run", "--jobs-file", str(self.jobs_file(jobs)),
                "--pool-dir", str(output), "--mock-server", str(MOCK)] + list(extra)
        proc = subprocess.run(argv, capture_output=True, text=True, env=env or self.env(config), timeout=timeout)
        return proc, output

    def job(self, output, ident):
        return json.loads((output / "jobs" / ident / "job.json").read_text(encoding="utf-8"))


class TestPool(PoolBase):
    def test_interleaved_notifications_are_routed_per_job(self):
        proc, output = self.run_pool(self.jobs("one", "two", "three"), {
            "jobs": {"one": {"delay": 0.12, "message": "one の結果"},
                     "two": {"delay": 0.08, "message": "two の結果"},
                     "three": {"delay": 0.04, "message": "three の結果"}},
        }, extra=("--max-parallel", "3"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for ident in ("one", "two", "three"):
            payload = self.job(output, ident)
            self.assertEqual(payload["status"], "completed")
            self.assertIn(f"{ident} の結果", (output / "jobs" / ident / "last.md").read_text(encoding="utf-8"))
            events = (output / "jobs" / ident / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn(payload["thread_id"], events)
            self.assertNotIn("thread-999", events)
        self.assertEqual(json.loads((output / "pool.json").read_text())["status"], "completed")

    def test_one_failed_and_two_completed_returns_two(self):
        proc, output = self.run_pool(self.jobs("good-a", "bad", "good-b"), {
            "jobs": {"bad": {"fail": True, "message": "意図した失敗"}},
        }, extra=("--max-parallel", "3"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.job(output, "bad")["status"], "failed")
        self.assertEqual(self.job(output, "good-a")["status"], "completed")
        self.assertEqual(self.job(output, "good-b")["status"], "completed")

    def test_server_death_finalises_every_running_job(self):
        proc, output = self.run_pool(self.jobs("a", "b", "c"), {"die_after_turns": 2}, extra=("--max-parallel", "3"))
        self.assertNotEqual(proc.returncode, 0)
        for ident in ("a", "b", "c"):
            self.assertNotEqual(self.job(output, ident)["status"], "running")
            self.assertEqual(self.job(output, ident)["status"], "failed")

    def test_max_parallel_starts_third_after_one_of_first_two_completes(self):
        received = self.tmp / "received.jsonl"
        env = self.env({"jobs": {"a": {"delay": 0.20}, "b": {"delay": 0.20}, "c": {"delay": 0.01}}},
                       MOCK_APP_SERVER_RECEIVE_LOG=received)
        proc, output = self.run_pool(self.jobs("a", "b", "c"), extra=("--max-parallel", "2"), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        starts = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines()
                  if json.loads(line)["method"] == "turn/start"]
        prompts = [row["params"]["input"][0]["text"] for row in starts]
        self.assertEqual(prompts, ["a", "b", "c"])
        # c の turn/start は a/b が出した turn/completed 後に記録される。
        self.assertGreater(starts[2]["at"] - starts[1]["at"], 0.10)
        self.assertEqual(self.job(output, "c")["status"], "completed")

    def test_jobs_file_validation(self):
        cases = [
            [{"id": "same", "prompt": "a", "cwd": str(self.cwd)}, {"id": "same", "prompt": "b", "cwd": str(self.cwd)}],
            [{"id": "missing-cwd", "prompt": "a"}],
            self.jobs(*[f"job-{n}" for n in range(9)]),
        ]
        for jobs in cases:
            with self.subTest(jobs=jobs):
                proc, _ = self.run_pool(jobs)
                self.assertEqual(proc.returncode, 1)

    def test_pool_holds_shared_slot_for_its_lifetime(self):
        output = self.tmp / "pool"
        argv = [sys.executable, str(POOL), "run", "--jobs-file", str(self.jobs_file(self.jobs("held"))),
                "--pool-dir", str(output), "--mock-server", str(MOCK), "--job-timeout-sec", "5"]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.env({"jobs": {"held": {"delay": 0.5}}}))
        old_root = os.environ.get("CODEX_BRIDGE_ROOT")
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                slot = try_acquire_slot(1)
                if slot is None:
                    break
                slot.release()
                time.sleep(0.03)
            self.assertIsNone(slot, "pool が slot-0 を保持していない")
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            if old_root is None:
                os.environ.pop("CODEX_BRIDGE_ROOT", None)
            else:
                os.environ["CODEX_BRIDGE_ROOT"] = old_root
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_job_timeout_is_timeout_and_not_running(self):
        proc, output = self.run_pool(self.jobs("slow"), {"jobs": {"slow": {"hang": True}}},
                                     extra=("--job-timeout-sec", "0.2", "--idle-timeout-sec", "5"))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.job(output, "slow")["status"], "timeout")

    def test_output_schema_is_sent_and_last_message_is_parsed(self):
        schema = self.tmp / "schema.json"
        schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
        jobs = [{"id": "structured", "prompt": "structured", "cwd": str(self.cwd),
                 "output_schema_file": str(schema), "write": True, "model": "per-job-model", "effort": "xhigh"}]
        received = self.tmp / "received.jsonl"
        message = json.dumps({"ok": True}, ensure_ascii=False)
        proc, output = self.run_pool(jobs, {"jobs": {"structured": {"message": message}}},
                                     env=self.env({"jobs": {"structured": {"message": message}}},
                                                  MOCK_APP_SERVER_RECEIVE_LOG=received))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.job(output, "structured")
        self.assertEqual(job["structured_output"], {"ok": True})
        self.assertTrue(job["write"])
        self.assertEqual((job["model"], job["effort"]), ("per-job-model", "xhigh"))
        starts = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines()
                  if json.loads(line)["method"] == "turn/start"]
        self.assertEqual(starts[0]["params"]["outputSchema"], {"type": "object"})

    def test_sigterm_writes_every_job_before_stopping_server_group(self):
        output = self.tmp / "pool"
        argv = [sys.executable, str(POOL), "run", "--jobs-file", str(self.jobs_file(self.jobs("a", "b"))),
                "--pool-dir", str(output), "--mock-server", str(MOCK), "--max-parallel", "2"]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.env({"jobs": {"a": {"hang": True}, "b": {"hang": True}}}))
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all((output / "jobs" / ident / "events.jsonl").exists() for ident in ("a", "b")):
                    break
                time.sleep(0.03)
            process.send_signal(signal.SIGTERM)
            self.assertNotEqual(process.wait(timeout=10), 0)
            for ident in ("a", "b"):
                self.assertEqual(self.job(output, ident)["status"], "killed")
            self.assertTrue((output / "pool.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()


class AuthErrorDetection(unittest.TestCase):
    """contains_auth_error の回帰。実プロトコル形（v2 スキーマ実確認済み）で固定する。

    - 誤爆側: コンテンツ系（userMessage echo・agentMessage・commandExecution 出力）は
      「401」「codex login」を含んでも検出しない（2026-08-25 の二度の実誤爆の再発防止）。
    - 検出側: ErrorNotification は params.error（TurnError{message}）、turn/completed は
      params.turn.error に認証死コードが現れる。refresh_token_reused / expired /
      invalidated（サーバのローテーション検出コード）も AUTH_PATTERN で拾う。
    """

    def setUp(self):
        import codex_pool
        self.f = codex_pool.contains_auth_error

    def test_prompt_echo_with_401_is_not_auth_error(self):
        ev = {"method": "item/started", "params": {"item": {
            "type": "userMessage",
            "text": "レビュー本文: HTTP 401 Unauthorized の扱いと codex login の案内文を確認して"}}}
        self.assertFalse(self.f(ev))

    def test_command_output_with_auth_words_is_not_auth_error(self):
        # 二度目の実誤爆: 対象コードの grep 結果に 401 / unauthorized が普通に含まれる
        ev = {"method": "item/completed", "params": {"item": {
            "type": "commandExecution",
            "command": "grep -rn 'unauthorized' src/",
            "aggregatedOutput": "src/auth.py:12: raise Unauthorized('401 token_invalidated')"}}}
        self.assertFalse(self.f(ev))

    def test_error_notification_token_invalidated_detected(self):
        ev = {"method": "error", "params": {"threadId": "t", "turnId": "u", "willRetry": False,
                                            "error": {"message": "stream error: 401 token_invalidated"}}}
        self.assertTrue(self.f(ev))

    def test_turn_error_refresh_token_variants_detected(self):
        for code in ("refresh_token_reused", "refresh_token_expired", "refresh_token_invalidated"):
            ev = {"method": "turn/completed", "params": {"threadId": "t", "turn": {
                "id": "u", "items": [], "status": "failed",
                "error": {"message": f"auth failed: {code}"}}}}
            self.assertTrue(self.f(ev), code)

    def test_error_item_detected(self):
        ev = {"method": "item/completed", "params": {"item": {
            "type": "error", "message": "please run `codex login`"}}}
        self.assertTrue(self.f(ev))

    def test_plain_numbers_and_paths_not_detected(self):
        for s in ("処理に 4010ms かかった", "/tmp/log-401/x.txt", "logout しました"):
            ev = {"method": "error", "params": {"error": {"message": s}}}
            self.assertFalse(self.f(ev), s)
