#!/usr/bin/env python3
"""codex-bridge の配管テスト（python3 unittest / 標準ライブラリのみ）。

Codex CLI 未インストール環境でも配管全体（ストリーミング・タイムアウト・kill・job.json
生成・台帳追記）を検証できるよう、`tests/mock_codex.py` をバイナリの代わりに起動する。

実行:
  python3 -m unittest discover -s codex-bridge/tests -v
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
RUN = SCRIPTS / "codex_run.py"
JOB = SCRIPTS / "codex_job.py"
RENDER = SCRIPTS / "render_prompt.py"
UI_SCREENSHOT = SCRIPTS / "ui_screenshot.py"

sys.path.insert(0, str(SCRIPTS))
import codex_lib as lib  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-bridge-test-"))
        self.root = self.tmp / "root"
        (self.root / "var").mkdir(parents=True)
        shutil.copytree(REPO / "config", self.root / "config")
        self.cd = self.tmp / "work"
        self.cd.mkdir()
        self.codex_home = self.tmp / "codex_home"
        self.codex_home.mkdir()
        #: M-3: 孫プロセス（mock の setsid 子）の PID。tearDown の冒頭で必ず kill する
        self.grandchild_pids: list[int] = []

    def tearDown(self):
        # M-3: addCleanup は tearDown の**後**に走るため、rmtree で pid ファイルを消してから
        # kill しようとして取り逃していた。冒頭で殺す。
        self.kill_grandchild()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- ヘルパ -----------------------------------------------------------
    def env(self, **extra):
        e = dict(os.environ)
        e.pop("OPENAI_API_KEY", None)
        e.pop("CODEX_API_KEY", None)
        e["CODEX_BRIDGE_ROOT"] = str(self.root)
        e["CODEX_HOME"] = str(self.codex_home)
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def job_argv(self, scenario, job_dir, extra_args=(), prompt="テストプロンプト", mode="task"):
        argv = [sys.executable, str(RUN), "--mode", mode, "--job-dir", str(job_dir),
                "--cd", str(self.cd), "--mock", scenario]
        if prompt is not None:            # None なら --prompt を付けない（prompt-file / stdin の検証用）
            argv += ["--prompt", prompt]
        return argv + list(extra_args)

    def run_job(self, scenario, name="job", extra_args=(), env=None, prompt="テストプロンプト",
                timeout=120, mode="task"):
        job_dir = self.tmp / name
        argv = self.job_argv(scenario, job_dir, extra_args, prompt, mode)
        proc = subprocess.run(argv, capture_output=True, text=True,
                              env=env or self.env(), timeout=timeout)
        return proc, job_dir

    def spawn_job(self, scenario, name="job", extra_args=(), prompt="テストプロンプト", mode="task"):
        job_dir = self.tmp / name
        proc = subprocess.Popen(self.job_argv(scenario, job_dir, extra_args, prompt, mode),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.addCleanup(self._reap, proc)
        return proc, job_dir

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass

    def remember_grandchild(self):
        """mock が残した孫 PID をテスト属性に控える（tearDown で kill するため）。"""
        p = self.cd / "MOCK_GRANDCHILD.pid"
        try:
            pid = int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if pid not in self.grandchild_pids:
            self.grandchild_pids.append(pid)
        return pid

    def kill_grandchild(self):
        self.remember_grandchild()
        for pid in list(getattr(self, "grandchild_pids", [])):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def leftover_processes(self, marker):
        """job-dir 名を目印に、mock プロセスが残っていないかを確認する。"""
        deadline = time.monotonic() + 5
        out = []
        while time.monotonic() < deadline:
            ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True)
            out = [line for line in ps.stdout.splitlines()
                   if marker in line and "mock_codex.py" in line and " ps " not in line]
            if not out:
                return []
            time.sleep(0.3)
        return out

    def load(self, job_dir):
        return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

    def ledger(self):
        p = self.root / "var" / "codex_usage.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestScenarios(Base):
    def test_ok(self):
        proc, job_dir = self.run_job("ok", extra_args=["--write", "--model", "gpt-5.6-terra",
                                                       "--effort", "high"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["thread_id"], "th_mock_0001")
        self.assertTrue(job["write"])
        self.assertEqual(job["mode"], "task")
        self.assertEqual(len(job["touched_files"]), 2)
        self.assertEqual({t["kind"] for t in job["touched_files"]}, {"add", "update"})
        self.assertEqual(len(job["commands"]), 2)
        # usage 5 フィールドが揃う
        self.assertEqual(set(job["usage"]), set(lib.USAGE_FIELDS))
        # terra: (12000-4000)*50 + 1000*50 + 4000*5 + 3000*300 = 1,370,000 credits/Mtok
        self.assertAlmostEqual(job["credits_est"], 1.37, places=4)
        self.assertGreater(job["duration_sec"], 0)
        # モックが実際にファイルを書いている
        self.assertTrue((self.cd / "MOCK_TOUCHED.txt").exists())
        # last.md
        self.assertTrue(Path(job["last_message_path"]).exists())
        # M10: --mock 実行は本物の使用量台帳に架空 usage を書かない
        self.assertEqual(job["mock"], "ok")
        self.assertEqual(self.ledger(), [])
        # events.jsonl が逐次書かれている
        lines = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 7)
        # forced_login_method 未設定の警告
        self.assertTrue(any("forced_login_method" in w for w in job["warnings"]), job["warnings"])

    def test_failed(self):
        proc, job_dir = self.run_job("failed")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        self.assertTrue(any("sandbox denied" in e for e in job["errors"]))
        self.assertIsNone(job["usage"])
        self.assertEqual(self.ledger(), [])   # usage が無ければ台帳に載せない

    def test_t14_mock_failed_keeps_usage_none_and_ledger_empty(self):
        proc, job_dir = self.run_job("failed", name="t14-failed")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        job = self.load(job_dir)
        self.assertIsNone(job["usage"])
        self.assertIsNone(job["usage_source"])
        self.assertFalse(job["usage_partial"])
        self.assertIsNone(job["rate_limits"])
        self.assertEqual(self.ledger(), [])

    def test_idle_timeout_kills_process_group(self):
        t0 = time.monotonic()
        proc, job_dir = self.run_job(
            "hang", extra_args=["--idle-timeout-sec", "2", "--timeout-sec", "60"])
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 3, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "idle_timeout")
        self.assertLess(elapsed, 30)
        self.assertTrue(any("アイドルタイムアウト" in e for e in job["errors"]))
        # プロセスが残っていないこと（job-dir 名で一意に判定できる）
        leftovers = self.leftover_processes(str(job_dir))
        self.assertEqual(leftovers, [], f"プロセスが残存している: {leftovers}")

    def test_wall_timeout(self):
        proc, job_dir = self.run_job(
            "slow", extra_args=["--timeout-sec", "2", "--idle-timeout-sec", "60"])
        self.assertEqual(proc.returncode, 3, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "timeout")
        self.assertTrue(any("壁時計" in e for e in job["errors"]))
        # イベントは流れていた（アイドルではない）ことの確認
        self.assertGreaterEqual(
            len((job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.leftover_processes(str(job_dir)), [])

    def test_exit0_without_turn_is_error(self):
        proc, job_dir = self.run_job("exit0_no_turn")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["exit_code"], 0)   # codex の exit code だけで completed にしない

    def test_schema_structured_output(self):
        schema = REPO / "templates" / "prompts" / "review.schema.json"
        proc, job_dir = self.run_job("schema", extra_args=["--schema", str(schema)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertIsInstance(job["structured_output"], dict)
        self.assertEqual(job["structured_output"]["verdict"], "needs-attention")
        self.assertEqual(len(job["structured_output"]["findings"]), 1)

    def test_garbage_does_not_crash(self):
        proc, job_dir = self.run_job("garbage")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        warns = " / ".join(job["warnings"])
        self.assertIn("JSON として解釈できない", warns)
        self.assertIn("不正な UTF-8", warns)
        # 生ログは欠落なく残っている
        raw = (job_dir / "events.jsonl").read_bytes()
        self.assertIn(b"\xff\xfe", raw)


class TestEnvHygiene(Base):
    def _dump(self, job_dir):
        return json.loads((self.cd / "MOCK_ENV.json").read_text(encoding="utf-8"))

    def test_api_key_removed_by_default(self):
        env = self.env(OPENAI_API_KEY="sk-test-xxx", CODEX_API_KEY="cx-test-yyy")
        proc, job_dir = self.run_job("envdump", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dump = self._dump(job_dir)
        self.assertIsNone(dump["OPENAI_API_KEY"])
        self.assertIsNone(dump["CODEX_API_KEY"])
        self.assertEqual(dump["keys"], [])
        job = self.load(job_dir)
        self.assertTrue(any("OPENAI_API_KEY を削除" in w for w in job["warnings"]))

    def test_allow_api_key_keeps_it(self):
        env = self.env(OPENAI_API_KEY="sk-test-xxx")
        proc, job_dir = self.run_job("envdump", extra_args=["--allow-api-key"], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dump = self._dump(job_dir)
        self.assertEqual(dump["OPENAI_API_KEY"], "sk-test-xxx")
        job = self.load(job_dir)
        self.assertTrue(any("従量課金" in w for w in job["warnings"]))


class TestBinaryResolution(Base):
    def make_bin(self, d: Path) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        p = d / "codex"
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
        return p

    def test_shim_excluded_and_real_binary_found(self):
        shim = self.make_bin(self.tmp / "cmux-cli-shims" / "abcd-uuid")
        real = self.make_bin(self.tmp / "usr" / "bin")
        path_env = os.pathsep.join([str(shim.parent), str(real.parent)])
        resolved, skipped = lib.resolve_codex_bin(None, path_env=path_env)
        self.assertEqual(resolved, str(real))
        self.assertEqual(skipped, [str(shim)])

    def test_only_shim_means_not_found(self):
        shim = self.make_bin(self.tmp / "cmux-cli-shims" / "abcd-uuid")
        resolved, skipped = lib.resolve_codex_bin(None, path_env=str(shim.parent))
        self.assertIsNone(resolved)
        self.assertEqual(skipped, [str(shim)])

    def test_codex_bin_env_wins(self):
        real = self.make_bin(self.tmp / "elsewhere")
        other = self.make_bin(self.tmp / "path" / "bin")
        os.environ["CODEX_BIN"] = str(real)
        try:
            resolved, _ = lib.resolve_codex_bin(None, path_env=str(other.parent))
            self.assertEqual(resolved, str(real.resolve()))
            # --codex-bin は CODEX_BIN より優先
            resolved2, _ = lib.resolve_codex_bin(str(other), path_env="")
            self.assertEqual(resolved2, str(other.resolve()))
        finally:
            del os.environ["CODEX_BIN"]

    def test_not_found_exit4(self):
        job_dir = self.tmp / "nf"
        env = self.env()
        env["PATH"] = str(self.tmp / "empty")
        env.pop("CODEX_BIN", None)
        proc = subprocess.run(
            [sys.executable, str(RUN), "--mode", "task", "--job-dir", str(job_dir),
             "--cd", str(self.cd), "--prompt", "x"],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "not_found")
        self.assertTrue(job["errors"])


class TestParallelSlots(Base):
    """M5 / L18: スロットは flock ベース（PID 読取→unlink→再作成の TOCTOU を持たない）。"""

    def slot_path(self, i=0) -> Path:
        locks = self.root / "var" / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        return locks / f"slot-{i}.lock"

    def hold_lock(self, i=0):
        """テストプロセス側で slot-i を flock して保持する（fd を返す）。"""
        fd = os.open(str(self.slot_path(i)), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lambda: os.close(fd))
        return fd

    def test_second_job_waits_for_slot(self):
        self.hold_lock(0)
        t0 = time.monotonic()
        proc, job_dir = self.run_job("ok", extra_args=["--max-parallel", "1", "--timeout-sec", "2"])
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 3, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "timeout")
        self.assertTrue(any("並列スロット" in e for e in job["errors"]))
        self.assertGreaterEqual(elapsed, 2.0)   # 待ってから諦めている

    def test_lock_file_with_reused_pid_does_not_block(self):
        """L18: ロックファイルに生きている無関係な PID があっても待たされない。

        旧実装は「ファイルが在る & PID が生きている」で占有中と判断したため、PID 再利用
        （前回保持者の PID が別プロセスに割り当てられた場合）で永久に取得できなかった。
        """
        self.slot_path(0).write_text(f"{os.getpid()}\n", encoding="utf-8")  # 生きているが無関係
        t0 = time.monotonic()
        proc, job_dir = self.run_job("ok", extra_args=["--max-parallel", "1", "--timeout-sec", "20"])
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 10, "スロット取得で待たされている")

    def test_stale_lock_is_reclaimed(self):
        self.slot_path(0).write_text("999999999\n", encoding="utf-8")  # 存在しない PID
        proc, job_dir = self.run_job("ok", extra_args=["--max-parallel", "1", "--timeout-sec", "30"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_only_one_of_two_acquisitions_wins(self):
        import codex_run
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            a = codex_run.try_acquire_slot(1)
            self.assertIsNotNone(a)
            b = codex_run.try_acquire_slot(1)
            self.assertIsNone(b, "同じスロットを 2 本が同時に取得した")
            a.release()
            c = codex_run.try_acquire_slot(1)
            self.assertIsNotNone(c, "解放後に取得できない")
            c.release()
        finally:
            os.environ.pop("CODEX_BRIDGE_ROOT", None)

    def test_slot_released_after_run(self):
        proc, job_dir = self.run_job("ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # ロックファイル自体は残す（削除しない）が、ロックは解放されている
        fd = os.open(str(self.slot_path(0)), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 例外なら解放されていない
        finally:
            os.close(fd)


class TestRenderPrompt(Base):
    def render(self, argv):
        return subprocess.run([sys.executable, str(RENDER)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=60)

    def test_missing_placeholder_exit1(self):
        proc = self.render(["implement", "--set", "OBJECTIVE=X"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("未充足", proc.stderr)
        self.assertIn("SCOPE", proc.stderr)

    def test_full_render(self):
        ctx = self.tmp / "ctx.md"
        ctx.write_text("周辺情報の本文\n", encoding="utf-8")
        proc = self.render([
            "implement",
            "--set", "OBJECTIVE=目的です", "--set", "SCOPE=範囲です",
            "--set", "NON_GOALS=非目標です", "--set", "ACCEPTANCE=受入です",
            "--set", "FORBIDDEN=禁止です", "--set-file", f"CONTEXT={ctx}",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("目的です", proc.stdout)
        self.assertIn("周辺情報の本文", proc.stdout)
        self.assertNotIn("{{", proc.stdout)

    def test_review_template_and_out(self):
        diff = self.tmp / "d.patch"
        diff.write_text("--- a\n+++ b\n+1 行追加\n", encoding="utf-8")
        out = self.tmp / "prompt.md"
        proc = self.render(["review", "--set-file", f"DIFF={diff}",
                            "--set", "FOCUS=正しさ", "--set", "CONTEXT=なし",
                            "--out", str(out)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertIn("1 行追加", text)
        self.assertNotIn("{{", text)

    def test_research_explore_and_verify_templates_resolve_and_require_values(self):
        cases = (
            ("research", ("QUESTION=問い", "FOCUS=重点"), "QUESTION"),
            ("explore", ("TARGET=対象", "QUESTIONS=確認事項"), "TARGET"),
            ("verify", ("CLAIM=主張", "CONTEXT=背景"), "CLAIM"),
        )
        for template, values, missing in cases:
            with self.subTest(template=template):
                full = self.render([template] + [item for value in values for item in ("--set", value)])
                self.assertEqual(full.returncode, 0, full.stderr)
                self.assertNotIn("{{", full.stdout)
                incomplete = self.render([template, "--set", values[1]])
                self.assertEqual(incomplete.returncode, 1)
                self.assertIn(missing, incomplete.stderr)

    def test_new_prompt_schemas_are_strict_json(self):
        for name in ("research", "explore", "verify"):
            with self.subTest(name=name):
                schema = json.loads((REPO / "templates" / "prompts" / f"{name}.schema.json").read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])


class TestJobCli(Base):
    def job_cli(self, argv):
        return subprocess.run([sys.executable, str(JOB)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=120)

    def test_result_summary_and_json(self):
        proc, job_dir = self.run_job("ok", extra_args=["--write"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = self.job_cli(["result", str(job_dir)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("completed", r.stdout)
        self.assertIn("MOCK_TOUCHED.txt", r.stdout)
        self.assertIn("npm run lint", r.stdout)          # 失敗コマンドだけ出る
        self.assertNotIn("python3 -m pytest", r.stdout)  # 成功コマンドは出さない
        self.assertIn("## 結果: 完了", r.stdout)          # last.md の中身
        j = self.job_cli(["result", str(job_dir), "--json"])
        self.assertEqual(j.returncode, 0, j.stderr)
        self.assertEqual(json.loads(j.stdout)["status"], "completed")

    def test_result_truncates_long_message(self):
        proc, job_dir = self.run_job("ok")
        job = self.load(job_dir)
        Path(job["last_message_path"]).write_text("あ" * 9000, encoding="utf-8")
        r = self.job_cli(["result", str(job_dir)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("字を省略", r.stdout)
        self.assertLess(r.stdout.count("あ"), 4100)

    def test_result_handles_invalid_utf8_last_message(self):
        proc, job_dir = self.run_job("ok")
        job = self.load(job_dir)
        Path(job["last_message_path"]).write_bytes(b"\xff\xfe broken \x80 bytes\n")
        r = self.job_cli(["result", str(job_dir)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("broken", r.stdout)

    def test_status_missing_job_dir(self):
        r = self.job_cli(["status", str(self.tmp / "no-such-dir")])
        self.assertEqual(r.returncode, 2)
        r2 = self.job_cli(["result", str(self.tmp / "no-such-dir")])
        self.assertEqual(r2.returncode, 2)

    def test_corrupt_job_json_is_distinguished_from_missing(self):
        """L-2: 「無い（実行中）」と「壊れている」を区別する。"""
        job_dir = self.tmp / "corrupt"
        job_dir.mkdir()
        (job_dir / "job.json").write_text('{"status": "comp', encoding="utf-8")
        s = self.job_cli(["status", str(job_dir)])
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("corrupt", s.stdout)
        self.assertNotIn("running", s.stdout)
        r = self.job_cli(["result", str(job_dir)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("壊れ", r.stderr)

    def test_status_running_job(self):
        job_dir = self.tmp / "running"
        job_dir.mkdir()
        (job_dir / "events.jsonl").write_text(
            json.dumps({"type": "thread.started", "thread_id": "th_x"}) + "\n"
            + json.dumps({"type": "turn.started"}) + "\n", encoding="utf-8")
        r = self.job_cli(["status", str(job_dir)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("running", r.stdout)
        self.assertIn("turn.started", r.stdout)

    def test_status_on_huge_events_is_fast(self):
        job_dir = self.tmp / "huge"
        job_dir.mkdir()
        line = json.dumps({"type": "item.updated", "item": {"type": "reasoning", "text": "x" * 50}})
        with open(job_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for _ in range(100_000):
                f.write(line + "\n")
        t0 = time.monotonic()
        r = self.job_cli(["status", str(job_dir)])
        elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("100000", r.stdout.replace(",", ""))
        self.assertLess(elapsed, 1.0, f"status に {elapsed:.2f}s かかった")

    def write_ledger(self, rows):
        p = self.root / "var" / "codex_usage.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def ledger_row(self, model, credits, mock=None, ts="2026-08-22T01:00:00Z"):
        return {"ts": ts, "job_dir": str(self.tmp / "x"), "mode": "task", "model": model,
                "effort": "high", "write": True, "cwd": str(self.cd),
                "claude_session_id": None, "thread_id": "th_x", "mock": mock,
                "usage": {"input_tokens": 12000, "cached_input_tokens": 4000,
                          "cache_write_input_tokens": 1000, "output_tokens": 3000,
                          "reasoning_output_tokens": 1200},
                "credits_est": credits, "status": "completed"}

    def test_usage_aggregation(self):
        self.write_ledger([self.ledger_row("gpt-5.6-terra", 1.37),
                           self.ledger_row("gpt-5.6-luna", 0.137)])
        r = self.job_cli(["usage", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["jobs"], 2)
        self.assertIn("gpt-5.6-terra", data["by_model"])
        self.assertIn("gpt-5.6-luna", data["by_model"])
        # luna: (8000+1000)*5 + 4000*0.5 + 3000*30 = 137,000 → 0.137
        self.assertAlmostEqual(data["by_model"]["gpt-5.6-luna"]["credits"], 0.137, places=4)
        t = self.job_cli(["usage"])
        self.assertEqual(t.returncode, 0, t.stderr)
        self.assertIn("モデル別", t.stdout)
        # --since で絞れる
        future = self.job_cli(["usage", "--json", "--since", "2099-01-01"])
        self.assertEqual(json.loads(future.stdout)["jobs"], 0)
        bad = self.job_cli(["usage", "--since", "not-a-date"])
        self.assertEqual(bad.returncode, 1)

    def test_usage_excludes_mock_rows(self):
        """M10: 台帳に混入した mock 行は集計から除外する。"""
        self.write_ledger([self.ledger_row("gpt-5.6-terra", 1.37),
                           self.ledger_row("gpt-5.6-terra", 99.0, mock="ok")])
        r = self.job_cli(["usage", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["jobs"], 1)
        self.assertAlmostEqual(data["credits_total"], 1.37, places=4)


class TestArgvAndLib(Base):
    def test_t01_normalize_usage_accepts_camel_case(self):
        usage = lib.normalize_usage({"inputTokens": 10, "outputTokens": 5})
        self.assertEqual(set(usage), set(lib.USAGE_FIELDS))
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)

    def test_t02_normalize_usage_prefers_snake_case(self):
        usage = lib.normalize_usage({"inputTokens": 10, "input_tokens": 20})
        self.assertEqual(usage["input_tokens"], 20)

    def test_t03_normalize_rate_limits_accepts_real_camel_and_snake_shapes(self):
        camel = {
            "limitId": "codex", "limitName": None,
            "primary": {"usedPercent": 0, "windowDurationMins": 10080,
                        "resetsAt": 1788835715},
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "individualLimit": None, "spendControlReached": None, "planType": "pro",
            "rateLimitReachedType": None,
        }
        snake = {
            "limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": 0.0, "window_minutes": 10080,
                        "resets_at": 1788835715},
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "individual_limit": None, "spend_control_reached": None, "plan_type": "pro",
            "rate_limit_reached_type": None,
        }
        one = lib.normalize_rate_limits(camel, "fixture")
        two = lib.normalize_rate_limits(snake, "fixture")
        one.pop("observed_at")
        two.pop("observed_at")
        self.assertEqual(one, two)
        self.assertEqual(one["primary"]["window_minutes"], 10080)
        self.assertIsNone(one["secondary"])

    def test_t04_merge_rate_limits_does_not_clear_sparse_fields(self):
        previous = {"primary": {"used_percent": 1}, "secondary": {"used_percent": 2}}
        merged = lib.merge_rate_limits(previous, {
            "primary": {"used_percent": 3}, "secondary": None})
        self.assertEqual(merged["primary"]["used_percent"], 3)
        self.assertEqual(merged["secondary"], {"used_percent": 2})

    def test_build_argv_order(self):
        import codex_run
        args = codex_run.build_parser().parse_args(
            ["--mode", "review", "--job-dir", str(self.tmp / "a"), "--review-scope", "base:main",
             "--prompt", "x", "--model", "gpt-5.6-sol", "--effort", "xhigh"])
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd), self.tmp / "last.md")
        self.assertEqual(argv[:3], ["/bin/codex", "exec", "--json"])
        # C1: review サブコマンドに PROMPT（-）は渡さない
        # L-4: ref は `--base=<ref>` の = 形（`-` で始まる ref でも clap が値として受ける）
        self.assertEqual(argv[-1], "--base=main")
        self.assertNotIn("--full-auto", argv)   # 0.149.0 で削除済み。渡すと即エラーになる
        # -s / -C はサブコマンド(review)より前
        self.assertLess(argv.index("-s"), argv.index("review"))
        self.assertLess(argv.index("-C"), argv.index("review"))
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("model_reasoning_effort=xhigh", argv)
        self.assertIn("--base=main", argv)

    def test_build_argv_write_and_schema(self):
        import codex_run
        schema = REPO / "templates" / "prompts" / "review.schema.json"
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--write",
             "--schema", str(schema), "--prompt", "x", "--resume-last"])
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd), self.tmp / "last.md")
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertIn("--output-schema", argv)
        self.assertLess(argv.index("--output-schema"), argv.index("resume"))
        self.assertEqual(argv[argv.index("resume") + 1], "--last")

    def test_conflicting_options_rejected(self):
        import codex_run
        rc = codex_run.main(["--mode", "task", "--job-dir", str(self.tmp / "a"),
                             "--prompt", "x", "--resume-last", "--resume", "th_1"])
        self.assertEqual(rc, 1)
        rc2 = codex_run.main(["--mode", "review", "--job-dir", str(self.tmp / "a"),
                              "--prompt", "x", "--review-scope", "bogus"])
        self.assertEqual(rc2, 1)

    def test_credits_est_unknown_model_is_none(self):
        self.assertIsNone(lib.credits_est({"input_tokens": 10}, "gpt-unknown"))
        self.assertIsNone(lib.credits_est(None, "gpt-5.6-sol"))

    def test_normalize_usage_fills_missing_fields(self):
        u = lib.normalize_usage({"input_tokens": "5", "output_tokens": None, "bogus": 1})
        self.assertEqual(set(u), set(lib.USAGE_FIELDS))
        self.assertEqual(u["input_tokens"], 5)
        self.assertEqual(u["output_tokens"], 0)

    def _rollout_path(self, thread_id):
        path = self.codex_home / "sessions/2026/09/01" / f"rollout-2026-09-01T11-57-03-{thread_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_t11_scan_rollout_returns_usage_and_rate_limits(self):
        thread_id = "01a05ae2-7e9b-0000-0000-000000000001"
        row = {"timestamp": "2026-09-01T02:56:47.688Z", "ordinal": 12,
               "type": "event_msg", "payload": {"type": "token_count", "info": {
                   "total_token_usage": {"input_tokens": 22176, "cached_input_tokens": 6912,
                       "cache_write_input_tokens": 0, "output_tokens": 5,
                       "reasoning_output_tokens": 0, "total_tokens": 22181},
                   "last_token_usage": {"input_tokens": 22176, "cached_input_tokens": 6912,
                       "cache_write_input_tokens": 0, "output_tokens": 5,
                       "reasoning_output_tokens": 0, "total_tokens": 22181},
                   "model_context_window": 258400}, "rate_limits": {
                       "limit_id": "codex", "limit_name": None,
                       "primary": {"used_percent": 0.0, "window_minutes": 10080,
                                   "resets_at": 1788835715}, "secondary": None,
                       "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                       "individual_limit": None, "spend_control_reached": None,
                       "plan_type": "pro", "rate_limit_reached_type": None}}}
        self._rollout_path(thread_id).write_text(json.dumps(row) + "\n", encoding="utf-8")
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        try:
            result = lib.scan_rollout(thread_id)
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old
        self.assertEqual(set(result["usage"]), set(lib.USAGE_FIELDS))
        self.assertEqual(result["usage"]["input_tokens"], 22176)
        self.assertEqual(result["rate_limits"]["primary"]["window_minutes"], 10080)

    def test_t12_scan_rollout_keeps_rate_limits_when_info_is_null(self):
        thread_id = "01a05ae2-7e9b-0000-0000-000000000002"
        row = {"timestamp": "2026-09-01T02:57:27.727Z", "ordinal": 12,
               "type": "event_msg", "payload": {"type": "token_count", "info": None,
                   "rate_limits": {"limit_id": "codex", "limit_name": None,
                       "primary": {"used_percent": 0.0, "window_minutes": 10080,
                                   "resets_at": 1788835715}, "secondary": None,
                       "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                       "individual_limit": None, "spend_control_reached": None,
                       "plan_type": "pro", "rate_limit_reached_type": None}}}
        self._rollout_path(thread_id).write_text(json.dumps(row) + "\n", encoding="utf-8")
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        try:
            result = lib.scan_rollout(thread_id)
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old
        self.assertIsNone(result["usage"])
        self.assertEqual(result["rate_limits"]["primary"]["window_minutes"], 10080)

    def test_t13_scan_rollout_handles_missing_empty_and_broken_files(self):
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        try:
            missing = lib.scan_rollout("missing")
            self.assertEqual(missing, {"usage": None, "rate_limits": None, "path": None})
            for suffix, content in (("empty", ""), ("broken", '{"type":"token_count"')):
                path = self._rollout_path(suffix)
                path.write_text(content, encoding="utf-8")
                result = lib.scan_rollout(suffix)
                self.assertIsNone(result["usage"])
                self.assertIsNone(result["rate_limits"])
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old

    def test_atomic_write_leaves_no_tmp(self):
        target = self.tmp / "out" / "x.json"
        lib.atomic_write_json(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["a"], 1)
        self.assertEqual([p for p in target.parent.iterdir() if p.name.startswith(".tmp-")], [])

    def test_bad_cd_rejected(self):
        import codex_run
        rc = codex_run.main(["--mode", "task", "--job-dir", str(self.tmp / "a"),
                             "--cd", str(self.tmp / "nope"), "--prompt", "x"])
        self.assertEqual(rc, 1)


class TestReviewScopeArgv(Base):
    """C1 / L14: review サブコマンドは PROMPT と conflicts_with_all の関係にある。"""

    def parse(self, argv_extra):
        import codex_run
        return codex_run.build_parser().parse_args(
            ["--mode", "review", "--job-dir", str(self.tmp / "a"), "--prompt", "x"] + argv_extra)

    def argv_for(self, argv_extra, warnings=None):
        import codex_run
        args = self.parse(argv_extra)
        return codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd),
                                          self.tmp / "last.md", warnings if warnings is not None else [])

    def test_ref_uses_equals_form(self):
        """L-4: `--base <ref>` だと `-` 始まりの ref を clap がフラグと誤認する。"""
        warns = []
        argv = self.argv_for(["--review-scope", "base:-x-branch"], warns)
        self.assertIn("--base=-x-branch", argv)
        self.assertNotIn("--base", argv)          # 分離形は使わない
        argv2 = self.argv_for(["--review-scope", "commit:-abc123"], [])
        self.assertIn("--commit=-abc123", argv2)

    def test_review_scope_has_no_trailing_prompt_dash(self):
        # `review --uncommitted -` は clap の ArgumentConflict になる（一次情報で確認済み）
        for scope, flag in (("uncommitted", "--uncommitted"),
                            ("base:main", "--base=main"), ("commit:abc123", "--commit=abc123")):
            with self.subTest(scope=scope):
                warns = []
                argv = self.argv_for(["--review-scope", scope], warns)
                self.assertIn("review", argv)
                self.assertIn(flag, argv)
                self.assertNotIn("-", argv[argv.index("review"):],
                                 f"review-scope に PROMPT（-）を渡している: {argv}")
                self.assertTrue(any("プロンプト" in w for w in warns), warns)

    def test_normal_and_resume_keep_stdin_dash(self):
        import codex_run
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x"])
        self.assertEqual(codex_run.build_codex_argv(
            args, ["/bin/codex"], str(self.cd), self.tmp / "last.md", [])[-1], "-")
        args2 = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x", "--resume-last"])
        self.assertEqual(codex_run.build_codex_argv(
            args2, ["/bin/codex"], str(self.cd), self.tmp / "last.md", [])[-1], "-")

    def test_review_scope_run_succeeds(self):
        proc, job_dir = self.run_job("ok", extra_args=["--review-scope", "uncommitted"],
                                     mode="review")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(any("プロンプト" in w for w in job["warnings"]), job["warnings"])

    def test_review_scope_allows_missing_prompt(self):
        """L-3: review-scope はプロンプトを使わないので未指定でもエラーにしない。"""
        import codex_run

        class TtyStdin(io.StringIO):
            def isatty(self):
                return True

        old_stdin, old_root, old_home = sys.stdin, os.environ.get("CODEX_BRIDGE_ROOT"), \
            os.environ.get("CODEX_HOME")
        sys.stdin = TtyStdin("")
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        try:
            with contextlib.redirect_stdout(io.StringIO()):   # テスト出力を汚さない
                rc = codex_run.main(["--mode", "review", "--job-dir", str(self.tmp / "rs"),
                                     "--cd", str(self.cd), "--mock", "ok",
                                     "--review-scope", "uncommitted"])
                # プロンプト必須は通常モードでは維持される
                rc2 = codex_run.main(["--mode", "task", "--job-dir", str(self.tmp / "rs2"),
                                      "--cd", str(self.cd), "--mock", "ok"])
        finally:
            sys.stdin = old_stdin
            for k, v in (("CODEX_BRIDGE_ROOT", old_root), ("CODEX_HOME", old_home)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)
        self.assertEqual(rc2, 1)

    def test_review_scope_drops_schema(self):
        schema = REPO / "templates" / "prompts" / "review.schema.json"
        warns = []
        argv = self.argv_for(["--review-scope", "uncommitted", "--schema", str(schema)], warns)
        self.assertNotIn("--output-schema", argv)
        proc, job_dir = self.run_job(
            "ok", extra_args=["--review-scope", "uncommitted", "--schema", str(schema)],
            mode="review")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertIsNone(job["structured_output"])
        self.assertTrue(any("schema" in w for w in job["warnings"]), job["warnings"])
        # last.md は散文なので JSON パース失敗の警告を出してはいけない
        self.assertFalse(any("JSON として解釈できなかった" in w for w in job["warnings"]),
                         job["warnings"])


class TestErrorItemIsNonFatal(Base):
    """H2: item 種別 error は非致命（exec_events.rs: "non-fatal error surfaced as an item"）。"""

    def test_item_error_does_not_fail_the_job(self):
        proc, job_dir = self.run_job("ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(any("deprecated config key" in w for w in job["warnings"]), job["warnings"])
        self.assertEqual(job["errors"], [])

    def test_toplevel_error_without_turn_completed_fails(self):
        proc, job_dir = self.run_job("toplevel_error")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(any("fatal stream error" in e for e in job["errors"]))

    def test_toplevel_error_then_completed_is_completed(self):
        proc, job_dir = self.run_job("error_then_complete")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")


class TestEscapedGrandchild(Base):
    """H3: 子孫が setsid でグループを抜けても job.json を書いて終了する。"""

    def test_wall_timeout_with_escaped_grandchild(self):
        t0 = time.monotonic()
        proc, job_dir = self.run_job(
            "escape", extra_args=["--timeout-sec", "5", "--idle-timeout-sec", "60"], timeout=60)
        elapsed = time.monotonic() - t0
        # M-3: 孫 PID を控える（tearDown 冒頭で kill する。addCleanup では rmtree に間に合わない）
        self.assertIsNotNone(self.remember_grandchild(), "孫 PID を控えられていない")
        self.assertLess(elapsed, 12, "壁時計タイムアウト後に終了できていない")
        self.assertTrue((job_dir / "job.json").exists(), "job.json が書かれていない")
        job = self.load(job_dir)
        self.assertEqual(job["status"], "timeout")
        self.assertEqual(proc.returncode, 3, proc.stderr)


class TestSignalHandling(Base):
    """M8: codex_run 自身が SIGTERM を受けても job.json を残し、子を孤児にしない。"""

    def wait_for(self, pred, limit=15.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.1)
        return False

    def test_sigterm_writes_killed_job_json(self):
        proc, job_dir = self.spawn_job(
            "slow", extra_args=["--timeout-sec", "120", "--idle-timeout-sec", "120"])
        events = job_dir / "events.jsonl"
        self.assertTrue(self.wait_for(lambda: events.exists() and events.stat().st_size > 0),
                        "モックのイベントが出ていない")
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("SIGTERM 後に終了しなかった")
        self.assertTrue((job_dir / "job.json").exists(), "job.json が書かれていない")
        job = self.load(job_dir)
        self.assertEqual(job["status"], "killed")
        self.assertTrue(any("SIGTERM" in e or "シグナル" in e for e in job["errors"]), job["errors"])
        # 子（mock）が孤児として残っていない
        self.assertEqual(self.leftover_processes(str(job_dir)), [])
        # ロックも残置されていない
        lock = self.root / "var" / "locks" / "slot-0.lock"
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_t10_signal_handler_appends_available_usage(self):
        import codex_run
        from types import SimpleNamespace
        from unittest import mock

        job_dir = self.tmp / "signal-ledger"
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(job_dir), "--prompt", "x"])
        col = SimpleNamespace(
            thread_id="th_signal", usage={
                "input_tokens": 10, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 5,
                "reasoning_output_tokens": 0},
            touched=[], commands=[], errors=[], warnings=[])
        handlers = {}
        old_root = os.environ.get("CODEX_BRIDGE_ROOT")
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            with mock.patch.object(codex_run.signal, "signal",
                                   side_effect=lambda sig, fn: handlers.__setitem__(sig, fn)), \
                 mock.patch.object(codex_run.os, "_exit", side_effect=SystemExit):
                codex_run.install_signal_handlers({
                    "args": args, "cd": str(self.cd), "job_dir": job_dir,
                    "started": lib.now_utc(), "queued_sec": 0, "warnings": [],
                    "proc": None, "col": col, "slot": None})
                with self.assertRaises(SystemExit):
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
        finally:
            if old_root is None:
                os.environ.pop("CODEX_BRIDGE_ROOT", None)
            else:
                os.environ["CODEX_BRIDGE_ROOT"] = old_root
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "killed")
        self.assertEqual(rows[0]["usage_source"], "turn.completed")


class TestJobDirReuse(Base):
    """M6: 同じ job-dir を使い回したとき、前回の last.md / job.json を今回の結果にしない。"""

    def test_stale_last_md_is_not_reported(self):
        proc, job_dir = self.run_job("ok", name="reuse")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## 結果: 完了", (job_dir / "last.md").read_text(encoding="utf-8"))

        proc2, _ = self.run_job("exit0_no_turn", name="reuse")
        self.assertEqual(proc2.returncode, 1, proc2.stderr)
        r = subprocess.run([sys.executable, str(JOB), "result", str(job_dir)],
                           capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("## 結果: 完了", r.stdout, "前回の last.md が今回の結果として出ている")

    def test_stale_job_json_removed_at_start(self):
        job_dir = self.tmp / "reuse2"
        job_dir.mkdir()
        (job_dir / "job.json").write_text(
            json.dumps({"status": "completed", "stale": True}), encoding="utf-8")
        proc, _ = self.spawn_job("hang", name="reuse2",
                                 extra_args=["--idle-timeout-sec", "20", "--timeout-sec", "20"])
        deadline = time.monotonic() + 10
        gone = False
        while time.monotonic() < deadline:
            if not (job_dir / "job.json").exists():
                gone = True
                break
            time.sleep(0.1)
        # SIGTERM なら codex_run 側がプロセスグループごと後始末する（SIGKILL だと孫が残る）
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.assertTrue(gone, "実行中も前回の job.json が残っている（完了と誤読される）")
        self.assertEqual(self.leftover_processes(str(job_dir)), [])


class TestCommandCap(Base):
    """M7: 上限 50 を超えても失敗コマンドは落とさない。"""

    def test_failed_command_is_kept_over_cap(self):
        proc, job_dir = self.run_job("manycmds")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        cmds = job["commands"]
        failed = [c for c in cmds if c["status"] == "failed"]
        self.assertEqual(len(failed), 1, f"失敗コマンドが落ちている（{len(cmds)} 件記録）")
        self.assertIn("cmd-55", failed[0]["command"])
        self.assertTrue(any("切り捨て" in w or "省略" in w for w in job["warnings"]), job["warnings"])
        r = subprocess.run([sys.executable, str(JOB), "result", str(job_dir)],
                           capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertIn("cmd-55", r.stdout)


class TestFailedCommandCap(Base):
    """H-1: 失敗コマンドが無制限に記録されて job.json / result が肥大化しないこと。"""

    def run_manyfails(self):
        proc, job_dir = self.run_job("manyfails", name="mf", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return job_dir

    def test_failed_commands_are_capped_and_job_json_stays_small(self):
        job_dir = self.run_manyfails()
        job = self.load(job_dir)
        failed = [c for c in job["commands"] if c.get("status") == "failed"]
        self.assertGreaterEqual(len(failed), 1)
        self.assertLessEqual(len(failed), 50, f"失敗コマンドが {len(failed)} 件も記録されている")
        size = (job_dir / "job.json").stat().st_size
        self.assertLess(size, 100_000, f"job.json が {size} バイトに肥大化している")
        self.assertTrue(any("失敗コマンド" in w and "切り捨て" in w for w in job["warnings"]),
                        job["warnings"])

    def result_of(self, job_dir):
        r = subprocess.run([sys.executable, str(JOB), "result", str(job_dir)],
                           capture_output=True, text=True, env=self.env(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_failed_command_list_is_rounded_to_20(self):
        out = self.result_of(self.run_manyfails())
        listed = [ln for ln in out.splitlines() if ln.startswith("- exit=")]
        self.assertEqual(len(listed), 20, f"失敗コマンドを {len(listed)} 件並べている")
        self.assertIn("他 30 件", out)

    def test_result_output_is_capped(self):
        job_dir = self.run_manyfails()
        # 1 件あたりのコマンドが長い場合でも result 全体を打ち切る
        job = self.load(job_dir)
        for c in job["commands"]:
            c["command"] = "pytest " + "y" * 900
        (job_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        body = self.result_of(job_dir).rstrip("\n")
        self.assertLessEqual(len(body), 12_000, f"result が {len(body)} 字ある")
        self.assertIn("省略", body)


class TestPromptDecoding(Base):
    """M-4: 不正な UTF-8 を含むプロンプトで traceback にならない。"""

    def test_invalid_utf8_prompt_file_is_replaced(self):
        p = self.tmp / "prompt.md"
        p.write_bytes(b"\xff\xfe \xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e \x80 prompt\n")
        proc, job_dir = self.run_job("ok", extra_args=["--prompt-file", str(p)], prompt=None)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(any("UTF-8" in w and "プロンプト" in w for w in job["warnings"]),
                        job["warnings"])

    def test_invalid_utf8_stdin_is_replaced(self):
        job_dir = self.tmp / "stdinjob"
        argv = self.job_argv("ok", job_dir, prompt=None)
        proc = subprocess.run(argv, input=b"\xff\xfe stdin \x80 prompt\n",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env(), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        job = self.load(job_dir)
        self.assertTrue(any("UTF-8" in w and "プロンプト" in w for w in job["warnings"]),
                        job["warnings"])

    def test_unreadable_prompt_file_exits_1_with_japanese_message(self):
        proc, job_dir = self.run_job(
            "ok", extra_args=["--prompt-file", str(self.tmp / "no-such-file.md")], prompt=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("読めません", proc.stderr)


class TestGrandchildCleanup(Base):
    """M-3: テスト終了後に mock の孫プロセス（time.sleep(30)）が残らない。"""

    def sleepers(self):
        ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True)
        return [ln for ln in ps.stdout.splitlines()
                if "time.sleep(30)" in ln and "codex-bridge-test-" in ln and " ps " not in ln]

    def test_escaped_grandchild_is_reaped_by_teardown(self):
        before = set(self.sleepers())
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_codex_bridge.TestEscapedGrandchild"],
            cwd=str(REPO), capture_output=True, text=True, env=self.env(), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        deadline = time.monotonic() + 3
        left = [ln for ln in self.sleepers() if ln not in before]
        while left and time.monotonic() < deadline:
            time.sleep(0.3)
            left = [ln for ln in self.sleepers() if ln not in before]
        for ln in left:      # 自分が起こした残骸は片付けてから落とす
            try:
                os.kill(int(ln.split()[0]), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.assertEqual(left, [], f"孫プロセスが残っている: {left}")


class TestUsageResumeAccounting(Base):
    """M-2: --resume の usage がスレッド累計でも台帳集計が二重計上しない。"""

    def job_cli(self, argv):
        return subprocess.run([sys.executable, str(JOB)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=120)

    def row(self, *, ts, thread_id, resumed, resume_of, inp, out, credits):
        return {"ts": ts, "job_dir": str(self.tmp / "x"), "mode": "task",
                "model": "gpt-5.6-terra", "effort": "high", "write": True, "cwd": str(self.cd),
                "claude_session_id": None, "thread_id": thread_id, "mock": None,
                "resumed": resumed, "resume_of": resume_of,
                "usage": {"input_tokens": inp, "cached_input_tokens": 0,
                          "cache_write_input_tokens": 0, "output_tokens": out,
                          "reasoning_output_tokens": 0},
                "credits_est": credits, "status": "completed"}

    def write_rows(self, rows):
        p = self.root / "var" / "codex_usage.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def cumulative_rows(self):
        return [
            self.row(ts="2026-08-22T01:00:00Z", thread_id="th_a", resumed=False,
                     resume_of=None, inp=10000, out=2000, credits=0.5),
            # resume 時の usage はスレッド累計（1 ターン目を含む）で返ってくる想定
            self.row(ts="2026-08-22T02:00:00Z", thread_id="th_a", resumed=True,
                     resume_of="th_a", inp=25000, out=5000, credits=1.25),
        ]

    def test_resumed_row_counted_as_delta(self):
        # 差分計上ロジックの検証なのでモードを明示する（同梱 config の既定は実機検証の結果で
        # per_turn に切り替わっており、未固定だと config の値に依存して揺れる）
        self.write_rows(self.cumulative_rows())
        r = self.job_cli(["usage", "--json", "--usage-mode", "cumulative"])
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)["by_model"]["gpt-5.6-terra"]
        self.assertEqual(m["input"], 25000, "resume 行が二重計上されている")
        self.assertEqual(m["output"], 5000, "resume 行が二重計上されている")

    def test_per_turn_mode_sums_raw_values(self):
        self.write_rows(self.cumulative_rows())
        r = self.job_cli(["usage", "--json", "--usage-mode", "per_turn"])
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)["by_model"]["gpt-5.6-terra"]
        self.assertEqual(m["input"], 35000)
        self.assertEqual(m["output"], 7000)

    def test_t15_old_and_new_ledger_rows_can_be_mixed(self):
        old_row, new_row = self.cumulative_rows()
        new_row["usage_source"] = "rollout.token_count"
        new_row["usage_partial"] = True
        self.write_rows([old_row, new_row])
        result = self.job_cli(["usage", "--json", "--usage-mode", "per_turn"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["jobs"], 2)

    def test_t16_partial_usage_is_not_resume_delta_baseline(self):
        partial, resumed = self.cumulative_rows()
        partial["usage_partial"] = True
        adjusted = __import__("codex_job").adjust_resumed_rows([partial, resumed])
        self.assertEqual(adjusted[1]["usage"]["input_tokens"], 25000)
        self.assertEqual(adjusted[1]["usage"]["output_tokens"], 5000)

    def test_run_records_resume_fields_in_ledger(self):
        import codex_run
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            args = codex_run.build_parser().parse_args(
                ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x",
                 "--resume", "th_prev"])
            payload = {"ended_at": "2026-08-22T00:00:00Z", "thread_id": "th_prev",
                       "status": "completed", "mock": None, "credits_est": 0.001,
                       "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 5,
                                 "reasoning_output_tokens": 0}}
            codex_run.append_ledger(args, str(self.cd), payload)
        finally:
            os.environ.pop("CODEX_BRIDGE_ROOT", None)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["resumed"])
        self.assertEqual(rows[0]["resume_of"], "th_prev")

    def test_fresh_run_marks_not_resumed(self):
        import codex_run
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            args = codex_run.build_parser().parse_args(
                ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x"])
            codex_run.append_ledger(args, str(self.cd), {
                "ended_at": "2026-08-22T00:00:00Z", "thread_id": "th_new", "status": "completed",
                "mock": None, "credits_est": 0.001,
                "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                          "cache_write_input_tokens": 0, "output_tokens": 5,
                          "reasoning_output_tokens": 0}})
        finally:
            os.environ.pop("CODEX_BRIDGE_ROOT", None)
        rows = self.ledger()
        self.assertFalse(rows[0]["resumed"])
        self.assertIsNone(rows[0]["resume_of"])


class TestSchemaValidation(Base):
    """L-5: --schema は実行前に存在・ファイル・JSON を確認する。"""

    def run_main(self, schema):
        import codex_run
        return codex_run.main(["--mode", "task", "--job-dir", str(self.tmp / "sv"),
                               "--cd", str(self.cd), "--prompt", "x", "--schema", str(schema)])

    def test_missing_schema_exits_1(self):
        self.assertEqual(self.run_main(self.tmp / "nope.json"), 1)

    def test_directory_schema_exits_1(self):
        d = self.tmp / "schemadir"
        d.mkdir()
        self.assertEqual(self.run_main(d), 1)

    def test_broken_json_schema_exits_1(self):
        p = self.tmp / "broken.json"
        p.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.run_main(p), 1)

    def test_valid_schema_passes_validation(self):
        proc, job_dir = self.run_job(
            "schema", extra_args=["--schema", str(REPO / "templates" / "prompts" / "review.schema.json")])
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestDocs(Base):
    """L-6: 停止方法（SIGINT ではなく SIGTERM）が README にあること。"""

    def test_readme_documents_sigterm_stop(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("SIGINT", text)
        self.assertIn("SIGTERM", text)


class TestForcedLoginMethod(Base):
    """M9: forced_login_method の判定は TOML として読む（部分一致で誤判定しない）。"""

    def warns_for(self, text):
        import codex_run
        home = self.tmp / f"ch{abs(hash(text)) % 100000}"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(text, encoding="utf-8")
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            return codex_run.config_warnings()
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old

    def test_absent(self):
        w = self.warns_for('model = "gpt-5.6-terra"\n')
        self.assertTrue(any("forced_login_method" in x for x in w), w)

    def test_chatgpt_is_ok(self):
        self.assertEqual(self.warns_for('forced_login_method = "chatgpt"\n'), [])

    def test_apikey_warns_about_metered_billing(self):
        w = self.warns_for('forced_login_method = "apikey"\n')
        self.assertTrue(any("従量課金" in x for x in w), w)

    def test_api_warns_about_metered_billing(self):
        """M-1: 公式の危険値は "api"（"apikey" は存在しない）。"""
        w = self.warns_for('forced_login_method = "api"\n')
        self.assertTrue(any("従量課金" in x for x in w), w)

    def test_commented_out_is_not_counted(self):
        w = self.warns_for('# forced_login_method = "chatgpt"\nmodel = "gpt-5.6-terra"\n')
        self.assertTrue(any("forced_login_method" in x for x in w),
                        f"コメント行を設定済みと誤判定した: {w}")

    def test_inside_string_is_not_counted(self):
        w = self.warns_for('notes = "forced_login_method = chatgpt にすること"\n')
        self.assertTrue(any("forced_login_method" in x for x in w),
                        f"文字列内の記述を設定済みと誤判定した: {w}")

    def test_broken_toml_warns(self):
        w = self.warns_for('forced_login_method = "chatgpt\n[[[\n')
        self.assertTrue(any("読めな" in x for x in w), w)


class TestStderrSurfacing(Base):
    """M11: 起動・引数エラーの原因（stderr）を result まで運ぶ。"""

    def test_startup_error_surfaces_stderr(self):
        proc, job_dir = self.run_job("startup_error")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "error")
        joined = " / ".join(job["errors"])
        self.assertIn("--full-auto", joined, f"stderr の原因が errors に無い: {job['errors']}")
        r = subprocess.run([sys.executable, str(JOB), "result", str(job_dir)],
                           capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--full-auto", r.stdout)


class TestLowSeverityFixes(Base):
    def test_file_change_counts_only_completed(self):
        """L12: in_progress / failed の file_change は touched_files に入れない。"""
        proc, job_dir = self.run_job("partial_change")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        paths = [Path(t["path"]).name for t in job["touched_files"]]
        self.assertEqual(paths, ["DONE.txt"], paths)

    def test_queued_sec_is_separate_from_duration(self):
        """L13: スロット待ち時間は started_at ではなく queued_sec に出す。"""
        lock = self.root / "var" / "locks"
        lock.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock / "slot-0.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        released = threading.Timer(2.0, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        released.start()
        self.addCleanup(released.cancel)
        proc, job_dir = self.run_job("ok", extra_args=["--timeout-sec", "40"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertIn("queued_sec", job)
        self.assertGreaterEqual(job["queued_sec"], 2.0, "スロット待ちが queued_sec に出ていない")
        self.assertLess(job["duration_sec"], job["queued_sec"],
                        "duration_sec にスロット待ちが混ざっている")

    def test_skip_git_repo_check_added_outside_git(self):
        """L15: git 管理外の --cd では --skip-git-repo-check を自動付与する。"""
        import codex_run
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x"])
        warns = []
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd),
                                          self.tmp / "last.md", warns)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertLess(argv.index("--skip-git-repo-check"), len(argv) - 1)
        self.assertTrue(any("git" in w for w in warns), warns)

        repo = self.tmp / "gitrepo"
        (repo / ".git").mkdir(parents=True)
        argv2 = codex_run.build_codex_argv(args, ["/bin/codex"], str(repo),
                                           self.tmp / "last.md", [])
        self.assertNotIn("--skip-git-repo-check", argv2)
        # サブディレクトリでも git 管理下と判定する
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        self.assertNotIn("--skip-git-repo-check",
                         codex_run.build_codex_argv(args, ["/bin/codex"], str(sub),
                                                    self.tmp / "last.md", []))

    def test_slot_poll_interval_is_short(self):
        """L-7: スロット待ちのポーリングは 0.5 秒間隔（deadline は従来どおり）。"""
        import codex_run
        self.assertLessEqual(codex_run.LOCK_POLL_SEC, 0.5)

    def test_detect_auth_error_patterns(self):
        """L16: 部分一致ではなく語境界で認証エラーを判定する。"""
        import codex_run
        cases = {
            "please run `codex login` to authenticate\n": True,
            "Error: unauthorized\n": True,
            "401 Unauthorized\n": True,
            "not logged in\n": True,
            "cloning 401 files into /tmp/login-cache\n": False,
            "logout completed successfully\n": False,
            "warning: relogin scheduled\n": False,
            "compiled 4010 modules\n": False,
        }
        for i, (text, expected) in enumerate(cases.items()):
            p = self.tmp / f"stderr{i}.log"
            p.write_text(text, encoding="utf-8")
            with self.subTest(text=text.strip()):
                self.assertEqual(codex_run.detect_auth_error(p), expected)


class TestWorkflowTemplate(Base):
    """H4 / L17: Workflow テンプレートの再開方法と process.cwd ガード。"""

    def setUp(self):
        super().setUp()
        self.js = (REPO / "workflows" / "implement-review-loop.js").read_text(encoding="utf-8")

    def test_fix_round_uses_resume_thread_id(self):
        # codex_run.py のコマンド組み立て部分に --resume-last が残っていないこと
        # （残る --resume-last は「使わない」と説明する散文のみ）
        cmd_lines = [line for line in self.js.splitlines()
                     if "runCodexCmd(" in line or "--prompt-file" in line or "--mode task" in line]
        self.assertTrue(cmd_lines)
        for line in cmd_lines:
            self.assertNotIn("--resume-last", line,
                             f"--resume-last はレビュー側スレッドを掴みうる: {line.strip()}")
        self.assertIn("--resume ${implThreadId}", self.js)
        self.assertIn("implementation.thread_id", self.js)

    def eval_build_cmd(self, round_, thread_id):
        """H-2: 文字列 grep ではなく、node で実際にコマンド組み立てを評価する。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        # `export const X = …` を globalThis への代入に変えて、Workflow 本体を関数として実行する
        src = self.js.replace("export const ", "globalThis.").replace(
            "const buildCodexCmd", "globalThis.buildCodexCmd"
        )
        arg = "null" if thread_id is None else json.dumps(thread_id)
        harness = self.tmp / "eval_build_cmd.cjs"
        harness.write_text(
            "const args = { task: 'ダミー仕様', worktree: '/tmp/wt' };\n"
            "const phase = () => {};\n"
            "const log = () => {};\n"
            "const agent = async () => ({});\n"
            "async function main() {\n" + src + "\n}\n"
            "main().then(() => {\n"
            f"  process.stdout.write(String(globalThis.buildCodexCmd({round_}, {arg})));\n"
            "}).catch((e) => { console.error(e); process.exit(1); });\n",
            encoding="utf-8")
        r = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_fix_round_command_resumes_impl_thread(self):
        cmd = self.eval_build_cmd(2, "TH")
        self.assertIn("--resume TH", cmd, cmd)
        self.assertNotIn("--resume-last", cmd, cmd)
        self.assertIn("codex_run.py", cmd)
        self.assertIn("--mode task", cmd)

    def test_first_round_command_has_no_resume(self):
        cmd = self.eval_build_cmd(1, None)
        self.assertNotIn("--resume", cmd, cmd)

    def test_fix_round_uses_fix_phase(self):
        """L-1: 修正ラウンドは phase / label も Fix にする。"""
        self.assertIn("'Fix'", self.js)
        self.assertIn("fix-r", self.js)

    def test_process_cwd_guard(self):
        self.assertNotIn("process.cwd?.()", self.js)
        self.assertIn("typeof process !== 'undefined'", self.js)

    def test_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        wrapped = self.tmp / "wrapped.mjs"
        wrapped.write_text(
            "const args = {}; const phase = () => {}; const log = () => {};\n"
            "const agent = async () => ({});\n"
            "export default async function main() {\n"
            + self.js.replace("export const ", "const ") + "\n}\n",
            encoding="utf-8")
        r = subprocess.run([node, "--check", str(wrapped)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestLedgerNonMock(Base):
    """M10: 実行が本物なら台帳に載る（mock 時のみ載せない）ことの確認。"""

    def test_append_ledger_records_real_run(self):
        import codex_run
        os.environ["CODEX_BRIDGE_ROOT"] = str(self.root)
        try:
            args = codex_run.build_parser().parse_args(
                ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x"])
            payload = {
                "ended_at": "2026-08-22T00:00:00Z", "thread_id": "th_1", "status": "completed",
                "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                          "cache_write_input_tokens": 0, "output_tokens": 5,
                          "reasoning_output_tokens": 0},
                "credits_est": 0.001, "mock": None,
            }
            codex_run.append_ledger(args, str(self.cd), payload)
            rows = self.ledger()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["mock"])
            # mock 実行はスキップされる
            payload["mock"] = "ok"
            codex_run.append_ledger(args, str(self.cd), payload)
            self.assertEqual(len(self.ledger()), 1)
        finally:
            os.environ.pop("CODEX_BRIDGE_ROOT", None)


class TestImageAndWebSearch(Base):
    """画像入力・imagegen・web search の Codex CLI 配線をモックで検証する。"""

    @staticmethod
    def png(width=1, height=1):
        # IHDR まであれば本実装のマジックバイト・寸法パースを検証できる。
        return (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big")
                + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")

    def input_image(self, name="input.png"):
        path = self.tmp / name
        path.write_bytes(self.png())
        return path

    def test_image_argv_uses_equals_form_for_multiple_and_resume(self):
        import codex_run
        one, two = self.input_image("one.png").resolve(), self.input_image("two.PNG").resolve()
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "job"), "--prompt", "x",
             "--resume", "th_1", "--image", str(one), "--image", str(two)])
        args.actual_images = [str(one), str(two)]
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd), self.tmp / "last.md", [])
        self.assertEqual(argv[argv.index("resume"):],
                         ["resume", "th_1", f"--image={one}", f"--image={two}", "-"])
        self.assertNotIn("--image", argv)

    def test_image_validation_rejects_invalid_inputs(self):
        missing = self.run_job("ok", extra_args=["--image", str(self.tmp / "missing.png")])
        self.assertEqual(missing[0].returncode, 1)
        invalid = self.tmp / "input.txt"
        invalid.write_text("not an image", encoding="utf-8")
        bad_extension = self.run_job("ok", extra_args=["--image", str(invalid)])
        self.assertEqual(bad_extension[0].returncode, 1)
        images = [self.input_image(f"many-{n}.png") for n in range(5)]
        too_many = self.run_job("ok", extra_args=sum((["--image", str(path)] for path in images), []))
        self.assertEqual(too_many[0].returncode, 1)
        review = self.run_job("ok", mode="review", extra_args=[
            "--review-scope", "uncommitted", "--image", str(images[0])])
        self.assertEqual(review[0].returncode, 1)

    def test_task_image_is_recorded_in_job_payload(self):
        image = self.input_image()
        proc, job_dir = self.run_job("ok", extra_args=["--image", str(image)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["images"], [str(image.resolve())])
        self.assertIsNone(job["image"])

    def test_imagegen_prompt_is_written_to_stdin_and_uses_workspace_write(self):
        import codex_run
        out = (self.cd / "generated.png").resolve()
        script = self.tmp / "capture_codex.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "cd = args[args.index('-C') + 1]\n"
            "pathlib.Path(cd, 'CAPTURED_ARGV.json').write_text(json.dumps(args), encoding='utf-8')\n"
            "pathlib.Path(cd, 'CAPTURED_STDIN.txt').write_text(sys.stdin.read(), encoding='utf-8')\n"
            "out = args[args.index('-o') + 1]\n"
            "pathlib.Path(out).write_text('mock last', encoding='utf-8')\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'th_capture'}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n",
            encoding="utf-8")
        script.chmod(0o755)
        proc = subprocess.run(
            [sys.executable, str(RUN), "--mode", "imagegen", "--job-dir", str(self.tmp / "imagegen"),
             "--out", str(out), "--prompt", "夕焼けの海", "--codex-bin", str(script)],
            capture_output=True, text=True, env=self.env(), timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stderr)  # 出力画像が無いので意図どおり failed に降格
        self.assertEqual((self.cd / "CAPTURED_STDIN.txt").read_text(encoding="utf-8"),
                         codex_run.imagegen_prompt("夕焼けの海", out))
        captured_argv = json.loads((self.cd / "CAPTURED_ARGV.json").read_text(encoding="utf-8"))
        self.assertEqual(captured_argv[captured_argv.index("-C") + 1], str(out.parent))
        args = codex_run.build_parser().parse_args(
            ["--mode", "imagegen", "--job-dir", str(self.tmp / "argjob"), "--out", str(out), "--prompt", "x"])
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd), self.tmp / "last.md", [])
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertEqual(args.timeout_sec, None)

    def test_imagegen_recovers_generated_image_and_records_metadata(self):
        out = self.cd / "recovered.png"
        source = self.codex_home / "generated_images" / "thread" / "x.png"
        source.parent.mkdir(parents=True)
        source.write_bytes(self.png(12, 34))
        # 開始時刻以降という回収条件に合わせ、モック起動時より後の mtime を与える。
        future = time.time() + 30
        os.utime(source, (future, future))
        proc, job_dir = self.run_job("ok", mode="imagegen", extra_args=["--out", str(out)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["image"], {"path": str(out.resolve()), "bytes": len(self.png(12, 34)),
                                         "width": 12, "height": 34})
        self.assertTrue(any("generated_images から回収" in w for w in job["warnings"]), job["warnings"])

    def test_imagegen_without_output_image_is_failed(self):
        out = self.cd / "missing-output.png"
        proc, job_dir = self.run_job("ok", mode="imagegen", extra_args=["--out", str(out)])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        job = self.load(job_dir)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(any("有効な PNG / JPEG" in e for e in job["errors"]), job["errors"])
        self.assertEqual(job["image"]["path"], str(out.resolve()))

    def test_imagegen_option_validation(self):
        import codex_run
        self.assertEqual(codex_run.main(["--mode", "imagegen", "--job-dir", str(self.tmp / "a"),
                                         "--prompt", "x"]), 1)
        self.assertEqual(codex_run.main(["--mode", "imagegen", "--job-dir", str(self.tmp / "b"),
                                         "--out", "relative.png", "--prompt", "x"]), 1)
        self.assertEqual(codex_run.main(["--mode", "task", "--job-dir", str(self.tmp / "c"),
                                         "--out", str(self.cd / "x.png"), "--prompt", "x"]), 1)
        self.assertEqual(codex_run.main(["--mode", "imagegen", "--job-dir", str(self.tmp / "d"),
                                         "--out", str(self.cd / "x.png"), "--resume-last", "--prompt", "x"]), 1)

    def test_web_search_argv_and_review_scope_warning(self):
        import codex_run
        args = codex_run.build_parser().parse_args(
            ["--mode", "task", "--job-dir", str(self.tmp / "a"), "--prompt", "x", "--web-search"])
        argv = codex_run.build_codex_argv(args, ["/bin/codex"], str(self.cd), self.tmp / "last.md", [])
        index = argv.index("web_search=\"live\"")
        self.assertEqual(argv[index - 1], "-c")
        review = codex_run.build_parser().parse_args(
            ["--mode", "review", "--job-dir", str(self.tmp / "b"), "--prompt", "x",
             "--review-scope", "uncommitted", "--web-search"])
        warnings = []
        review_argv = codex_run.build_codex_argv(review, ["/bin/codex"], str(self.cd),
                                                  self.tmp / "last.md", warnings)
        self.assertNotIn("web_search=\"live\"", review_argv)
        self.assertTrue(any("web-search" in warning for warning in warnings), warnings)


class TestUiScreenshot(Base):
    PNG = b"\x89PNG\r\n\x1a\n"

    def screenshot(self, argv):
        return subprocess.run([sys.executable, str(UI_SCREENSHOT)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=60)

    def fake_chrome(self, name: str, writes_png: bool) -> Path:
        script = self.tmp / name
        body = [
            "#!/usr/bin/env python3",
            "import pathlib, sys",
            "output = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--screenshot='))",
        ]
        if writes_png:
            body.append("pathlib.Path(output).write_bytes(b'\\x89PNG\\r\\n\\x1a\\n')")
        body.append("sys.exit(0)")
        script.write_text("\n".join(body) + "\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_url_html_are_exclusive_and_required(self):
        out = self.tmp / "shots"
        missing = self.screenshot(["--out-dir", str(out)])
        both = self.screenshot(["--url", "https://example.test", "--html", str(self.tmp / "x.html"),
                                "--out-dir", str(out)])
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertEqual(both.returncode, 1, both.stderr)

    def test_invalid_viewports_exit1(self):
        proc = self.screenshot(["--url", "https://example.test", "--out-dir", str(self.tmp / "shots"),
                                "--viewports", "1440-by-900"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ビューポート", proc.stderr)

    def test_nonexistent_chrome_exit4(self):
        proc = self.screenshot(["--url", "https://example.test", "--out-dir", str(self.tmp / "shots"),
                                "--chrome-bin", str(self.tmp / "missing-chrome")])
        self.assertEqual(proc.returncode, 4, proc.stderr)

    def test_fake_chrome_without_png_exits2(self):
        chrome = self.fake_chrome("chrome-no-png.py", writes_png=False)
        proc = self.screenshot(["--url", "https://example.test", "--out-dir", str(self.tmp / "shots"),
                                "--chrome-bin", str(chrome), "--viewports", "100x200"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fake_chrome_with_png_exits0_and_prints_path(self):
        chrome = self.fake_chrome("chrome-png.py", writes_png=True)
        out = self.tmp / "shots"
        proc = self.screenshot(["--url", "https://example.test", "--out-dir", str(out),
                                "--chrome-bin", str(chrome), "--viewports", "100x200"])
        expected = (out / "shot-100x200.png").resolve()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [str(expected)])
        self.assertEqual(expected.read_bytes(), self.PNG)

    def test_fake_chrome_partial_success_exits3(self):
        chrome = self.tmp / "chrome-partial.py"
        chrome.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "output = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--screenshot='))\n"
            "if output.endswith('100x200.png'):\n"
            "    pathlib.Path(output).write_bytes(b'\\x89PNG\\r\\n\\x1a\\n')\n",
            encoding="utf-8")
        chrome.chmod(0o755)
        proc = self.screenshot([
            "--url", "https://example.test", "--out-dir", str(self.tmp / "shots"),
            "--chrome-bin", str(chrome), "--viewports", "100x200,300x400",
        ])
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1)


class TestUiPromptTemplates(Base):
    def render(self, argv):
        return subprocess.run([sys.executable, str(RENDER)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=60)

    def test_ui_review_template_resolves_and_requires_placeholders(self):
        ok = self.render(["ui-review", "--set", "CONTEXT=x", "--set", "FOCUS=y"])
        missing = self.render(["ui-review", "--set", "CONTEXT=x"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn("{{", ok.stdout)
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("FOCUS", missing.stderr)

    def test_ui_compare_template_resolves_and_requires_placeholders(self):
        ok = self.render(["ui-compare", "--set", "CHANGES=x"])
        missing = self.render(["ui-compare"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn("{{", ok.stdout)
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("CHANGES", missing.stderr)


if __name__ == "__main__":
    unittest.main()
