#!/usr/bin/env python3
"""週次枠ペーシング（pace）のテスト。

合成 transcript（fable / opus 混在・subagent 含む・requestId 重複行あり）と
`FABLE_COST_MANAGER_ROOT` / `FCM_PROJECTS_DIR` によるスクラッチ隔離で、
pace_refresh.py / pace_statusline.sh / pace_report.py の挙動を検証する。
実データ（~/.claude/projects）と実 var/ には一切触れない。

実行:
    python3 -m unittest discover -s cost-manager/tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CONFIG_SRC = Path(__file__).resolve().parent.parent / "config"
sys.path.insert(0, str(SCRIPTS))
import cost_lib as lib  # noqa: E402

WEEK = 7 * 24 * 3600


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def assistant_line(model, ts, usage, request_id, uuid, cwd):
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "uuid": uuid,
            "timestamp": iso(ts),
            "cwd": cwd,
            "message": {"model": model, "usage": usage},
        },
        ensure_ascii=False,
    )


def usage(inp=0, out=0):
    return {"input_tokens": inp, "output_tokens": out}


class PaceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fcm-pace-test-"))
        self.root = self.tmp / "root"
        (self.root / "config").mkdir(parents=True)
        for name in ("config.json", "pricing.json"):
            shutil.copy(CONFIG_SRC / name, self.root / "config" / name)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.env = dict(os.environ)
        self.env["FABLE_COST_MANAGER_ROOT"] = str(self.root)
        self.env["FCM_PROJECTS_DIR"] = str(self.projects)
        # Codex 使用量台帳もスクラッチへ隔離する（既定は実 codex-bridge/var/ を指すため、
        # 実台帳が存在する環境でも既存テストの期待値が動かないようにする）。
        self.codex_ledger = self.tmp / "codex_usage.jsonl"
        self.env["FCM_CODEX_LEDGER"] = str(self.codex_ledger)
        self.pace_dir = self.root / "var" / "pace"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 合成データ ---------------------------------------------------------
    def write_session(self, project_name, session_id, cwd, main_lines, sub_lines=None):
        pdir = self.projects / project_name
        pdir.mkdir(parents=True, exist_ok=True)
        main = pdir / f"{session_id}.jsonl"
        main.write_text("\n".join(main_lines) + "\n", encoding="utf-8")
        if sub_lines:
            sub = pdir / session_id / "subagents"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "agent-1.jsonl").write_text("\n".join(sub_lines) + "\n", encoding="utf-8")

    def standard_fixture(self, now):
        """fable $5 / opus $15（うち subagent $5）= 合計 $20、share 0.25 になる合成データ。"""
        cwd = str(self.work)
        t = now - 3600
        main = [
            # fable: input 500,000 tok × $10/MTok = $5.00
            assistant_line("claude-fable-5", t, usage(inp=500_000), "req-f1", "u-f1", cwd),
            # 同一 requestId の重複行（content block 分割）: dedup で1回だけ計上されるべき
            assistant_line("claude-fable-5", t, usage(inp=500_000), "req-f1", "u-f1b", cwd),
            # opus: input 2,000,000 tok × $5/MTok = $10.00
            assistant_line("claude-opus-5", t + 10, usage(inp=2_000_000), "req-o1", "u-o1", cwd),
            # 窓の外（8日前）: 除外されるべき
            assistant_line("claude-fable-5", now - 8 * 24 * 3600, usage(inp=9_000_000), "req-old", "u-old", cwd),
            # <synthetic> は集計対象外
            assistant_line("<synthetic>", t + 20, usage(inp=9_000_000), "req-syn", "u-syn", cwd),
        ]
        sub = [
            # subagent の opus: input 1,000,000 tok × $5/MTok = $5.00
            assistant_line("claude-opus-5", t + 30, usage(inp=1_000_000), "req-o2", "u-o2", cwd),
        ]
        self.write_session("-tmp-work", "sess-main", cwd, main, sub)

    def run_refresh(self, *extra, check=True):
        cmd = [sys.executable, str(SCRIPTS / "pace_refresh.py"), *extra]
        p = subprocess.run(cmd, env=self.env, capture_output=True, text=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def read_cache(self):
        with open(self.pace_dir / "cache.json", encoding="utf-8") as f:
            return json.load(f)

    def write_samples(self, entries):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        with open(self.pace_dir / "samples.jsonl", "w", encoding="utf-8") as f:
            for e in entries:
                f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")

    def sample(self, ts, used, resets_at, five=None):
        return {
            "ts": ts,
            "seven_day": {"used": used, "resets_at": resets_at},
            "five_hour": five,
            "session_id": "s",
            "model": "claude-fable-5",
        }


class TestRefreshAggregation(PaceTestBase):
    def test_share_est_pace_projected(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2  # 窓の 50% が経過した状態
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])

        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()

        self.assertAlmostEqual(c["total_usd"], 20.0, places=6)
        self.assertAlmostEqual(c["fable"]["usd"], 5.0, places=6)
        self.assertAlmostEqual(c["fable"]["share"], 0.25, places=6)
        self.assertAlmostEqual(c["seven_day"]["elapsed_ratio"], 0.5, places=6)
        # used 40% / 経過 50% -> pace 0.8, 週末到達見込み 80%
        self.assertAlmostEqual(c["seven_day"]["pace"], 0.8, places=6)
        self.assertAlmostEqual(c["seven_day"]["projected_end_pct"], 80.0, places=6)
        # Fable 推定 = 40% × 0.25 = 10%、上限 50% に対し pace 0.4、到達見込み 20%
        self.assertAlmostEqual(c["fable"]["est_pct"], 10.0, places=6)
        self.assertAlmostEqual(c["fable"]["pace"], 0.4, places=6)
        self.assertAlmostEqual(c["fable"]["projected_end_pct"], 20.0, places=6)
        # モデル別（subagent 分が opus に合算されていること）
        self.assertAlmostEqual(c["models"]["claude-opus-5"]["usd"], 15.0, places=6)
        self.assertEqual(c["models"]["claude-opus-5"]["tokens"], 3_000_000)
        self.assertEqual(c["models"]["claude-fable-5"]["tokens"], 500_000)
        self.assertEqual(c["window"]["resets_at"], resets_at)
        self.assertFalse(c["window"]["closed"])

    def test_no_samples(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        p = self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertEqual(c["notes"], ["no samples"])
        self.assertIsNone(c["window"])
        self.assertEqual(p.returncode, 0)

    def test_window_closed_when_resets_at_is_past(self):
        now = 1_755_000_000
        resets_at = now - 3600  # 既にリセット済み（窓が閉じている）
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 7200, 95.0, resets_at)])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertTrue(c["window"]["closed"])
        self.assertAlmostEqual(c["seven_day"]["elapsed_ratio"], 1.0, places=6)
        self.assertAlmostEqual(c["seven_day"]["projected_end_pct"], 95.0, places=6)
        self.assertTrue(any("窓は既に閉じています" in n for n in c["notes"]))

    def test_manual_window_override(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.run_refresh("--now", str(now), "--resets-at", str(resets_at), "--used", "40", "--quiet")
        c = self.read_cache()
        self.assertAlmostEqual(c["seven_day"]["used"], 40.0)
        self.assertTrue(any("手動指定" in n for n in c["notes"]))

    def test_unknown_model_tokens_only(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        cwd = str(self.work)
        self.write_session(
            "-tmp-work", "sess-main", cwd,
            [assistant_line("some-future-model-9", now - 60, usage(inp=1_000), "r1", "u1", cwd)],
        )
        self.write_samples([self.sample(now - 30, 10.0, resets_at)])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertEqual(c["models"]["some-future-model-9"]["tokens"], 1_000)
        self.assertAlmostEqual(c["models"]["some-future-model-9"]["usd"], 0.0)
        self.assertTrue(any("未収載" in n for n in c["notes"]))


class TestLicenseExclusion(PaceTestBase):
    def _licensed_fixture(self, now):
        licensed = self.tmp / "licensed"
        licensed.mkdir()
        (licensed / ".envrc").write_text(
            "# generated-by: claude-toolbox/license-switch\nexport CLAUDE_CODE_OAUTH_TOKEN=x\n",
            encoding="utf-8",
        )
        # 別ライセンス側で fable $100 相当（10,000,000 tok × $10/MTok）を消費
        self.write_session(
            "-tmp-licensed", "sess-lic", str(licensed),
            [assistant_line("claude-fable-5", now - 60, usage(inp=10_000_000), "req-l1", "u-l1", str(licensed))],
        )
        return licensed

    def test_license_switched_session_is_excluded(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self._licensed_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])

        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertAlmostEqual(c["total_usd"], 20.0, places=6)
        self.assertTrue(any("除外" in n for n in c["notes"]))

    def test_no_exclude_license_flag_includes_it(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self._licensed_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])

        self.run_refresh("--now", str(now), "--no-exclude-license", "--quiet")
        c = self.read_cache()
        self.assertAlmostEqual(c["total_usd"], 120.0, places=6)

    def test_exclude_cwd_prefixes_from_config(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["exclude_cwd_prefixes"] = [str(self.work)]
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])

        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertAlmostEqual(c["total_usd"], 0.0, places=6)
        self.assertTrue(any("除外" in n for n in c["notes"]))


class TestCalibration(PaceTestBase):
    def test_calibration_needs_three_pairs(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([
            self.sample(now - 7200, 10.0, resets_at),
            self.sample(now - 30, 40.0, resets_at),
        ])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertIsNone(c["calibration"]["usd_per_pct"])
        self.assertEqual(c["calibration"]["n_pairs"], 1)

    def test_calibration_median(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        cwd = str(self.work)
        # 各区間で opus $5.00 ずつ消費する4本のリクエスト
        lines = [
            assistant_line("claude-opus-5", now - 400 + i * 100, usage(inp=1_000_000), f"r{i}", f"u{i}", cwd)
            for i in range(4)
        ]
        self.write_session("-tmp-work", "sess-main", cwd, lines)
        # サンプルは各リクエストの直後に置き、Δused を 5% ずつにする -> $5/5% = $1.0/%
        self.write_samples([
            self.sample(now - 450, 10.0, resets_at),
            self.sample(now - 350, 15.0, resets_at),
            self.sample(now - 250, 20.0, resets_at),
            self.sample(now - 150, 25.0, resets_at),
            self.sample(now - 50, 30.0, resets_at),
        ])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertEqual(c["calibration"]["n_pairs"], 4)
        self.assertAlmostEqual(c["calibration"]["usd_per_pct"], 1.0, places=6)

    def test_row_cost_usd_sums_to_aggregate(self):
        """row_cost_usd() の合計が aggregate() の total と一致する（単価式の複製ずれ検出）。"""
        pricing = json.loads((CONFIG_SRC / "pricing.json").read_text(encoding="utf-8"))
        at = datetime(2026, 8, 22).date()
        rows = [
            {"model": "claude-opus-5", "usage": {"input_tokens": 12345, "output_tokens": 678,
                                                 "cache_read_input_tokens": 9999,
                                                 "cache_creation": {"ephemeral_5m_input_tokens": 4321,
                                                                    "ephemeral_1h_input_tokens": 123}}},
            {"model": "claude-fable-5", "usage": {"input_tokens": 1, "output_tokens": 2,
                                                 "cache_creation_input_tokens": 7}},
            {"model": "claude-sonnet-5", "usage": {"input_tokens": 500, "output_tokens": 9}},
            {"model": "unknown-model-x", "usage": {"input_tokens": 500, "output_tokens": 9}},
        ]
        total = sum(lib.row_cost_usd(r, pricing, at) for r in rows)
        rep = lib.aggregate(rows, pricing, at=at, usd_jpy=160)
        self.assertAlmostEqual(total, rep.total_usd, places=12)


class StatuslineMixin:
    """statusline を隔離環境で叩くための共通ヘルパ（TestCase ではない）。"""

    NOW = 1_755_000_000

    def sl_env(self, **over):
        env = dict(self.env)
        env["FCM_PACE_BASE_STATUSLINE"] = "/nonexistent-base-statusline"
        env["FCM_PACE_REFRESH_CMD"] = "true"
        env["FCM_PACE_NOW"] = str(self.NOW)
        env.update(over)
        return env

    def run_sl(self, payload, env=None):
        p = subprocess.run(
            ["bash", str(SCRIPTS / "pace_statusline.sh")],
            input=json.dumps(payload) if isinstance(payload, (dict, list)) else payload,
            env=env or self.sl_env(), capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def payload(self, seven=True, five=True):
        d = {"session_id": "abc123", "model": {"id": "claude-opus-5"},
             "workspace": {"current_dir": "/w"}, "rate_limits": {}}
        if five:
            d["rate_limits"]["five_hour"] = {"used_percentage": 23.5, "resets_at": self.NOW + 3600}
        if seven:
            d["rate_limits"]["seven_day"] = {"used_percentage": 41.2, "resets_at": self.NOW + WEEK // 2}
        if not d["rate_limits"]:
            del d["rate_limits"]
        return d

    def write_cache(self, **over):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        c = {"computed_at": self.NOW - 10, "duration_sec": 1.0,
             "fable": {"est_pct": 12.3, "cap_pct": 50, "pace": 0.63, "share": 0.3, "usd": 1.0, "tokens": 1},
             "models": {}, "total_usd": 3.0, "seven_day": {"used": 41.2}, "notes": []}
        c.update(over)
        lib.atomic_write_json(self.pace_dir / "cache.json", c)


class TestStatusline(StatuslineMixin, PaceTestBase):
    def test_full_rate_limits(self):
        self.write_cache()
        out = self.run_sl(self.payload())
        self.assertIn("📅W 41%/50%", out)
        self.assertIn("F≈12%/50% ·0.63", out)
        self.assertIn("⏱5h 24%/80%", out)
        self.assertIn("\033[", out)  # 色付き

    def test_no_rate_limits(self):
        out = self.run_sl({"session_id": "abc", "model": {"id": "claude-opus-5"}})
        self.assertIn("📅W ?", out)
        self.assertNotIn("F≈", out)
        # 記録も refresh もしない
        self.assertFalse((self.pace_dir / "samples.jsonl").exists())

    def test_five_hour_missing(self):
        self.write_cache()
        out = self.run_sl(self.payload(five=False))
        self.assertIn("📅W 41%/50%", out)
        self.assertNotIn("⏱5h", out)

    def test_seven_day_missing_only_five_hour(self):
        out = self.run_sl(self.payload(seven=False))
        self.assertIn("⏱5h", out)
        self.assertNotIn("📅W 4", out)

    def test_cache_missing_shows_question_mark(self):
        out = self.run_sl(self.payload())
        self.assertIn("F?", out)

    def test_stale_cache_is_dimmed_with_suffix(self):
        self.write_cache(computed_at=self.NOW - 5000)  # ttl(300)×3 より古い
        out = self.run_sl(self.payload())
        self.assertIn("·0.63?", out)

    def test_base_statusline_is_prepended(self):
        base = self.tmp / "base.sh"
        base.write_text("#!/usr/bin/env bash\ncat >/dev/null\nprintf 'BASE-OUT'\n", encoding="utf-8")
        out = self.run_sl(self.payload(), env=self.sl_env(FCM_PACE_BASE_STATUSLINE=str(base)))
        self.assertTrue(out.startswith("BASE-OUT"))
        self.assertIn("📅W", out)

    def test_broken_json_stdin_does_not_crash(self):
        out = self.run_sl("{not json at all")
        self.assertIn("📅W ?", out)

    # --- samples.jsonl -----------------------------------------------------
    def test_sample_throttle(self):
        self.run_sl(self.payload())
        self.run_sl(self.payload())  # 同一時刻 -> スロットル
        lines = (self.pace_dir / "samples.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["seven_day"]["used"], 41.2)
        self.assertEqual(rec["five_hour"]["resets_at"], self.NOW + 3600)
        self.assertEqual(rec["session_id"], "abc123")
        self.assertEqual(rec["model"], "claude-opus-5")

    def test_sample_recorded_after_interval(self):
        self.run_sl(self.payload())
        self.run_sl(self.payload(), env=self.sl_env(FCM_PACE_NOW=str(self.NOW + 61)))
        lines = (self.pace_dir / "samples.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_broken_last_line_and_empty_file(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        (self.pace_dir / "samples.jsonl").write_text("", encoding="utf-8")
        self.run_sl(self.payload())
        self.assertEqual(len((self.pace_dir / "samples.jsonl").read_text().strip().splitlines()), 1)

        (self.pace_dir / "samples.jsonl").write_text("これは JSON ではない\n", encoding="utf-8")
        self.run_sl(self.payload())
        lines = (self.pace_dir / "samples.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)  # 壊れた行は無視して追記される
        json.loads(lines[1])

    # --- single-flight -----------------------------------------------------
    def test_refresh_single_flight(self):
        counter = self.tmp / "refresh-count"
        cmd = f"bash -c 'echo x >> {counter}; sleep 1'"
        procs = [
            subprocess.Popen(
                ["bash", str(SCRIPTS / "pace_statusline.sh")],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self.sl_env(FCM_PACE_REFRESH_CMD=cmd), text=True,
            )
            for _ in range(5)
        ]
        for p in procs:
            p.communicate(json.dumps(self.payload()))
        time.sleep(2.0)
        n = len(counter.read_text().strip().splitlines()) if counter.exists() else 0
        self.assertEqual(n, 1, f"refresh が {n} 回起動した（single-flight 違反）")
        self.assertFalse((self.pace_dir / "refresh.lock").exists(), "ロックが解放されていない")

    def test_stale_lock_is_taken_over(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lock = self.pace_dir / "refresh.lock"
        lock.mkdir()
        old = self.NOW - 3600
        os.utime(lock, (old, old))
        counter = self.tmp / "refresh-count"
        self.run_sl(self.payload(), env=self.sl_env(FCM_PACE_REFRESH_CMD=f"bash -c 'echo x >> {counter}'"))
        time.sleep(1.0)
        self.assertTrue(counter.exists(), "10分超の stale ロックを奪えていない")

    # --- 想定外入力 ---------------------------------------------------------
    def test_huge_samples_file_is_fast(self):
        """10万行の samples.jsonl でも tail -1 経路が遅くならない。"""
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        path = self.pace_dir / "samples.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for i in range(100_000):
                f.write(json.dumps(self.sample(self.NOW - 100_000 + i, i / 1000.0,
                                               self.NOW + WEEK // 2)) + "\n")
        t0 = time.monotonic()
        out = self.run_sl(self.payload())
        elapsed = time.monotonic() - t0
        self.assertIn("📅W", out)
        self.assertLess(elapsed, 5.0, f"statusline が遅い: {elapsed:.2f}s")
        # 最終行が正しく読めること（read_last_jsonl_line の末尾読み）
        last = lib.read_last_jsonl_line(path)
        self.assertAlmostEqual(last["seven_day"]["used"], 99.999, places=6)


class TestPaceReport(PaceTestBase):
    def run_report(self, *extra):
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), *extra],
            env=self.env, capture_output=True, text=True,
        )
        return p

    def test_text_and_json(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at,
                                        five={"used": 20.0, "resets_at": now + 1800})])
        self.run_refresh("--now", str(now), "--quiet")

        p = self.run_report("--now", str(now))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("週次枠 : used 40.0%", p.stdout)
        self.assertIn("週末到達見込み 80%", p.stdout)
        self.assertIn("[未検証] A1", p.stdout)
        self.assertIn("[未検証] A2", p.stdout)
        self.assertIn("[未検証] A3", p.stdout)
        self.assertIn("license-switch", p.stdout)
        self.assertIn("5時間枠", p.stdout)

        p = self.run_report("--now", str(now), "--json")
        d = json.loads(p.stdout)
        self.assertAlmostEqual(d["seven_day"]["pace"], 0.8, places=6)
        self.assertAlmostEqual(d["fable"]["est_pct"], 10.0, places=6)
        self.assertEqual(d["used_source"], "samples.jsonl")

    def test_no_samples_exits_3(self):
        p = self.run_report()
        self.assertEqual(p.returncode, 3)
        self.assertIn("サンプルがまだありません", p.stdout)

    def test_refresh_flag(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        p = self.run_report("--now", str(now), "--refresh", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertAlmostEqual(d["total_usd"], 20.0, places=6)
        self.assertTrue((self.pace_dir / "cache.json").exists())


class TestLibHelpers(PaceTestBase):
    def test_is_license_switched_dir_walks_ancestors(self):
        parent = self.tmp / "p"
        child = parent / "a" / "b"
        child.mkdir(parents=True)
        self.assertFalse(lib.is_license_switched_dir(str(child)))
        (parent / ".envrc").write_text("# generated-by: claude-toolbox/license-switch\n", encoding="utf-8")
        self.assertTrue(lib.is_license_switched_dir(str(child)))

    def test_is_license_switched_dir_nearest_envrc_wins(self):
        parent = self.tmp / "q"
        child = parent / "a"
        child.mkdir(parents=True)
        (parent / ".envrc").write_text("# generated-by: claude-toolbox/license-switch\n", encoding="utf-8")
        (child / ".envrc").write_text("export FOO=1\n", encoding="utf-8")
        self.assertFalse(lib.is_license_switched_dir(str(child)))

    def test_is_license_switched_dir_bad_input(self):
        self.assertFalse(lib.is_license_switched_dir(None))
        self.assertFalse(lib.is_license_switched_dir(""))
        self.assertFalse(lib.is_license_switched_dir("/no/such/dir/anywhere/xyz"))

    def test_read_last_jsonl_line_edge_cases(self):
        p = self.tmp / "x.jsonl"
        p.write_text("", encoding="utf-8")
        self.assertIsNone(lib.read_last_jsonl_line(p))
        p.write_text("broken\n{\"a\":1}\nbroken2\n", encoding="utf-8")
        self.assertEqual(lib.read_last_jsonl_line(p), {"a": 1})
        self.assertIsNone(lib.read_last_jsonl_line(self.tmp / "missing.jsonl"))

    def test_first_cwd_of(self):
        p = self.tmp / "t.jsonl"
        p.write_text('{"type":"user"}\n{"cwd":"/Users/x/y z","type":"user"}\n', encoding="utf-8")
        self.assertEqual(lib.first_cwd_of(p), "/Users/x/y z")
        self.assertIsNone(lib.first_cwd_of(self.tmp / "nope.jsonl"))

    def test_pace_config_defaults(self):
        self.assertEqual(lib.pace_config({})["refresh_ttl_sec"], 300)
        self.assertEqual(lib.pace_config({"budget": {"pace": {"refresh_ttl_sec": 7}}})["refresh_ttl_sec"], 7)
        self.assertEqual(lib.pace_config({"budget": {"pace": {"refresh_ttl_sec": 7}}})["fable_cap_pct"], 50)


# ---------------------------------------------------------------------------
# 反証レビュー（verifier）指摘の再現テスト
# ---------------------------------------------------------------------------

class TestNullCache(PaceTestBase):
    """high-1: cache.json のキーが null でも pace_report がクラッシュしない。"""

    def test_report_survives_null_valued_cache(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        # pace_refresh がサンプル無しで書く「fable などが null」のキャッシュ
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_json(self.pace_dir / "cache.json", {
            "computed_at": now - 10, "duration_sec": 0.1, "window": None,
            "seven_day": None, "fable": None, "models": None, "total_usd": 0.0,
            "calibration": None, "samples_n": 0, "notes": None,
        })
        # その後で有効サンプルが 1 行入る
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])

        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("週次枠 : used 40.0%", p.stdout)

        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now), "--json"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        json.loads(p.stdout)


class TestUnknownModelsUndecidable(StatuslineMixin, PaceTestBase):
    """high-2 / medium-6: pricing.json 未収載モデルがあるとき Fable 推定を不能扱いにする。"""

    def _fixture(self, now):
        cwd = str(self.work)
        self.write_session("-tmp-work", "sess-main", cwd, [
            # 未収載の Fable（USD 換算できない）
            assistant_line("claude-fable-6", now - 60, usage(inp=1_000_000), "r1", "u1", cwd),
            # 収載済みの opus: 1,000,000 tok × $5/MTok = $5.00
            assistant_line("claude-opus-5", now - 50, usage(inp=1_000_000), "r2", "u2", cwd),
        ])

    def test_refresh_marks_fable_undecidable(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self._fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertEqual(c["unknown_models"], ["claude-fable-6"])
        self.assertEqual(c["unknown_tokens"], 1_000_000)
        self.assertIsNone(c["fable"]["est_pct"])
        self.assertIsNone(c["fable"]["pace"])
        self.assertIsNone(c["fable"]["share"])

    def test_statusline_shows_warning_marker(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_json(self.pace_dir / "cache.json", {
            "computed_at": self.NOW - 10, "duration_sec": 1.0,
            "fable": {"est_pct": None, "cap_pct": 50, "pace": None, "share": None,
                      "usd": 0.0, "tokens": 0},
            "unknown_models": ["claude-fable-6"], "unknown_tokens": 1_000_000,
            "models": {}, "total_usd": 5.0, "seven_day": {"used": 41.2}, "notes": [],
        })
        out = self.run_sl(self.payload())
        self.assertIn("F?!", out)
        self.assertIn("\033[33m", out)  # 警告色（YEL）

    def test_report_refuses_to_recommend(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self._fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        self.run_refresh("--now", str(now), "--quiet")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("未収載モデルがあるため Fable 推定不能", p.stdout)
        self.assertIn("claude-fable-6", p.stdout)
        self.assertNotIn("fable のまま使う余地", p.stdout)


class TestRefreshFailureIsCached(StatuslineMixin, PaceTestBase):
    """high-3(a): refresh が想定外の例外で落ちてもネガティブキャッシュを残す。"""

    def _break_config(self):
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["fable_cap_pct"] = "五十"  # float() で ValueError
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_error_cache_is_written_and_exit_1(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        self._break_config()
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertTrue((self.pace_dir / "cache.json").exists(), "エラー時にキャッシュが書かれていない")
        c = self.read_cache()
        self.assertIn("ValueError", c["error"])
        self.assertAlmostEqual(c["computed_at"], now, delta=60)

    def test_statusline_shows_error_marker(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_json(self.pace_dir / "cache.json", {
            "computed_at": self.NOW - 10, "error": "ValueError: boom", "notes": ["集計に失敗しました"],
        })
        out = self.run_sl(self.payload())
        self.assertIn("F!", out)
        self.assertNotIn("F?!", out)
        self.assertIn("\033[33m", out)

    def test_ttl_backoff_after_failure(self):
        """エラーキャッシュの mtime で TTL バックオフが効き、再起動ループにならない。"""
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        self._break_config()
        counter = self.tmp / "refresh-count"
        cmd = (f"bash -c '{sys.executable} {SCRIPTS}/pace_refresh.py --now {now} --quiet; "
               f"echo x >> {counter}'")
        for _ in range(5):
            self.run_sl(self.payload(), env=self.sl_env(FCM_PACE_REFRESH_CMD=cmd,
                                                        FCM_PACE_NOW=str(self.NOW)))
            time.sleep(0.6)
        n = len(counter.read_text().strip().splitlines()) if counter.exists() else 0
        self.assertEqual(n, 1, f"refresh が {n} 回起動した（失敗時のバックオフが効いていない）")


class TestUnreadableTranscript(PaceTestBase):
    """high-3(b): 読めない transcript は 1 件 skip するだけで例外にしない。"""

    def _make_unreadable(self):
        p = self.projects / "-tmp-bad" / "sess-bad.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type":"user","cwd":"/tmp/bad"}\n', encoding="utf-8")
        os.chmod(p, 0o000)
        return p

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root では chmod 000 が効かない")
    def test_iter_usage_skips_unreadable_file(self):
        p = self._make_unreadable()
        rows, offset = lib.iter_usage(p)
        self.assertEqual(rows, [])
        self.assertEqual(offset, 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root では chmod 000 が効かない")
    def test_refresh_survives_unreadable_file(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self._make_unreadable()
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        c = self.read_cache()
        self.assertNotIn("error", c)
        self.assertAlmostEqual(c["total_usd"], 20.0, places=6)


class TestSampleValidation(PaceTestBase):
    """high-3(c) / low-12 / medium-4: 不正な resets_at を持つサンプルは無視する。"""

    def test_millisecond_resets_at_is_ignored(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, (now + WEEK // 2) * 1000)])
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        c = self.read_cache()
        self.assertEqual(c["notes"], ["no samples"])

    def test_zero_and_negative_resets_at_are_ignored(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([
            self.sample(now - 40, 30.0, 0),
            self.sample(now - 30, 40.0, -5),
        ])
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        c = self.read_cache()
        self.assertEqual(c["notes"], ["no samples"])

    def test_out_of_range_ts_is_ignored(self):
        """ts がミリ秒値でも日別スナップショット（fromtimestamp）が落ちない。"""
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([
            self.sample(now - 300, 40.0, resets_at),
            self.sample(now * 1000, 41.0, resets_at),
        ])
        self.run_refresh("--now", str(now), "--quiet")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now), "--json"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(len(d["daily"]), 1)
        self.assertAlmostEqual(d["seven_day"]["used"], 40.0, places=6)

    def test_invalid_last_sample_falls_back_to_earlier_valid_one(self):
        """medium-4: 最終行が無効でも手前の有効サンプルで窓を決める。"""
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([
            self.sample(now - 300, 40.0, resets_at),
            {"ts": now - 30, "seven_day": {"used": 41.0, "resets_at": None},
             "five_hour": None, "session_id": "s", "model": "claude-fable-5"},
        ])
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertIsNotNone(c["window"])
        self.assertEqual(c["window"]["resets_at"], resets_at)
        self.assertAlmostEqual(c["seven_day"]["used"], 40.0, places=6)

    def test_report_falls_back_to_earlier_valid_sample(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([
            self.sample(now - 300, 40.0, resets_at),
            {"ts": now - 30, "seven_day": {"used": 41.0, "resets_at": None},
             "five_hour": None, "session_id": "s", "model": "claude-fable-5"},
        ])
        self.run_refresh("--now", str(now), "--quiet")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now), "--json"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["used_source"], "samples.jsonl")
        self.assertAlmostEqual(d["seven_day"]["used"], 40.0, places=6)


class TestStatuslineWindowValidity(StatuslineMixin, PaceTestBase):
    """medium-5: used と resets_at が揃った窓だけを有効とする。"""

    def half_payload(self):
        return {"session_id": "abc123", "model": {"id": "claude-opus-5"},
                "rate_limits": {"seven_day": {"used_percentage": 41.2}}}

    def test_partial_seven_day_shows_question_mark(self):
        out = self.run_sl(self.half_payload())
        self.assertNotEqual(out.strip(), "", "出力が空になっている")
        self.assertIn("📅W ?", out)
        self.assertNotIn("📅W 41", out)

    def test_partial_window_is_recorded_as_null(self):
        self.run_sl(self.half_payload())
        path = self.pace_dir / "samples.jsonl"
        self.assertTrue(path.exists())
        rec = json.loads(path.read_text().strip().splitlines()[-1])
        self.assertIsNone(rec["seven_day"], "resets_at 欠落の窓が null で記録されていない")

    def test_partial_five_hour_is_recorded_as_null(self):
        d = self.payload()
        del d["rate_limits"]["five_hour"]["resets_at"]
        out = self.run_sl(d)
        self.assertNotIn("⏱5h", out)
        rec = json.loads((self.pace_dir / "samples.jsonl").read_text().strip().splitlines()[-1])
        self.assertIsNone(rec["five_hour"])
        self.assertIsNotNone(rec["seven_day"])


class TestExcludePrefixNormalization(PaceTestBase):
    """medium-7: exclude_cwd_prefixes の ~ 展開と末尾スラッシュ。"""

    def _set_prefix(self, value):
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["exclude_cwd_prefixes"] = [value]
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_trailing_slash_prefix_matches(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self._set_prefix(str(self.work) + "/")
        self.run_refresh("--now", str(now), "--quiet")
        self.assertAlmostEqual(self.read_cache()["total_usd"], 0.0, places=6)

    def test_tilde_prefix_is_expanded(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self._set_prefix("~/work")
        env = dict(self.env)
        env["HOME"] = str(self.tmp)
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_refresh.py"), "--now", str(now), "--quiet"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertAlmostEqual(self.read_cache()["total_usd"], 0.0, places=6)


class TestSampleLock(StatuslineMixin, PaceTestBase):
    """medium-8: サンプル記録の check-then-act を mkdir ロックで直列化する。"""

    def test_concurrent_statuslines_record_once(self):
        payload_file = self.tmp / "payload.json"
        payload_file.write_text(json.dumps(self.payload()), encoding="utf-8")
        handles = [open(payload_file, "rb") for _ in range(10)]
        procs = [
            subprocess.Popen(
                ["bash", str(SCRIPTS / "pace_statusline.sh")],
                stdin=h, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self.sl_env(),
            )
            for h in handles
        ]
        for p in procs:
            p.wait()
        for h in handles:
            h.close()
        lines = (self.pace_dir / "samples.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1, f"同時実行で {len(lines)} 行記録された（スロットル破れ）")
        self.assertFalse((self.pace_dir / "sample.lock").exists(), "サンプルロックが解放されていない")

    def test_stale_sample_lock_is_taken_over(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lock = self.pace_dir / "sample.lock"
        lock.mkdir()
        old = self.NOW - 3600
        os.utime(lock, (old, old))
        self.run_sl(self.payload())
        self.assertTrue((self.pace_dir / "samples.jsonl").exists(), "60秒超の stale ロックを奪えていない")


class TestSymlinkedStatusline(StatuslineMixin, PaceTestBase):
    """medium-9: symlink 経由で置いても SCRIPT_DIR が実体を指す。"""

    def test_script_dir_resolves_symlink(self):
        link = self.tmp / "pace_statusline_link.sh"
        link.symlink_to(SCRIPTS / "pace_statusline.sh")
        env = self.sl_env()
        env.pop("FCM_PACE_REFRESH_CMD")  # 既定コマンド（$SCRIPT_DIR/pace_refresh.py）を使う
        p = subprocess.run(["bash", str(link)], input=json.dumps(self.payload()),
                           env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        cache = self.pace_dir / "cache.json"
        for _ in range(40):
            if cache.exists():
                break
            time.sleep(0.25)
        self.assertTrue(cache.exists(), "symlink 経由だと pace_refresh.py を起動できていない")


class TestStatuslineConfigRobustness(StatuslineMixin, PaceTestBase):
    """low-13: 非数値の refresh_ttl_sec で stderr にエラーを漏らさない。"""

    def test_non_numeric_ttl(self):
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["refresh_ttl_sec"] = "とても長い"
        cfg["budget"]["pace"]["sample_min_interval_sec"] = "ときどき"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(
            ["bash", str(SCRIPTS / "pace_statusline.sh")],
            input=json.dumps(self.payload()), env=self.sl_env(), capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "", f"stderr に漏れがある: {p.stderr!r}")
        self.assertIn("📅W 41%/50%", p.stdout)


# ---------------------------------------------------------------------------
# Codex 使用量レーン（codex-bridge の台帳を読む）
# ---------------------------------------------------------------------------

CODEX_PRICING = Path(__file__).resolve().parent.parent.parent / "codex-bridge" / "config" / "codex_pricing.json"


def codex_row(ts, model="gpt-5.6-terra", inp=0, cached=0, cwrite=0, out=0, reasoning=0,
              credits_est=None, session="sess-main", mock=None, status="completed"):
    """台帳 1 行（codex-bridge/scripts/codex_run.py の append_ledger と同じ形）。"""
    row = {
        "ts": ts,
        "job_dir": "/tmp/job",
        "mode": "task",
        "model": model,
        "effort": "medium",
        "write": True,
        "cwd": "/tmp/work",
        "claude_session_id": session,
        "thread_id": "th-1",
        "usage": {
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cwrite,
            "output_tokens": out,
            "reasoning_output_tokens": reasoning,
        },
        "credits_est": credits_est,
        "status": status,
    }
    if mock is not None:
        row["mock"] = mock
    return row


class CodexLedgerMixin:
    """合成台帳を書くヘルパ。"""

    def write_codex_ledger(self, entries):
        with open(self.codex_ledger, "w", encoding="utf-8") as f:
            for e in entries:
                f.write((e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n")

    def standard_ledger(self, now):
        """窓内 3 件（ISO ts / epoch ts 混在）+ 無視行 4 件 + 窓外 1 件。

        期待クレジット:
          terra: 非キャッシュ入力 1,000,000 tok × 50cr/MTok + 出力 100,000 × 300 = 80.0
          luna : 非キャッシュ 1,500,000 × 5 + キャッシュ 500,000 × 0.5 + 出力 10,000 × 30 = 8.05
          sol  : 行の credits_est（12.5）を優先
          合計 100.55（うち直近 5 時間は sol の 12.5 のみ）
        """
        self.write_codex_ledger([
            # ISO8601 文字列の ts（codex-bridge の現行実装が書く形）
            codex_row(iso(now - 3 * 24 * 3600), model="gpt-5.6-terra", inp=1_000_000, out=100_000),
            # epoch 数値の ts（仕様上どちらも受ける）
            codex_row(now - 2 * 24 * 3600, model="gpt-5.6-luna",
                      inp=2_000_000, cached=500_000, out=10_000),
            # credits_est がある行はそれを優先（直近 5 時間窓にも入る）
            codex_row(now - 600, model="gpt-5.6-sol", inp=10, out=10, credits_est=12.5),
            # --- 以下は無視される行 ---
            codex_row(now - 3600, model="gpt-5.6-sol", inp=9_000_000, out=9_000_000, mock="ok"),
            "これは JSON ではない",
            json.dumps({"ts": iso(now - 3600), "model": "gpt-5.6-sol"}),  # usage 無し
            codex_row(None, model="gpt-5.6-sol", inp=1_000_000, out=1_000_000),  # ts 欠落
            # --- 窓外（8 日前）---
            codex_row(iso(now - 8 * 24 * 3600), model="gpt-5.6-terra", inp=9_000_000, out=9_000_000),
        ])


class TestCodexLib(CodexLedgerMixin, PaceTestBase):
    def test_parse_codex_ts_both_forms(self):
        # codex-bridge の現行実装が書く形（ISO8601・秒精度・Z サフィックス）
        self.assertEqual(lib.parse_codex_ts("2026-08-22T09:15:03Z"),
                         datetime(2026, 8, 22, 9, 15, 3, tzinfo=timezone.utc))
        self.assertEqual(lib.parse_codex_ts(1_755_000_000),
                         datetime.fromtimestamp(1_755_000_000, tz=timezone.utc))
        self.assertEqual(lib.parse_codex_ts("1755000000"),
                         datetime.fromtimestamp(1_755_000_000, tz=timezone.utc))
        for bad in (None, "", "きのう", 0, -1, 1_755_000_000_000, True, {}):
            self.assertIsNone(lib.parse_codex_ts(bad), f"不正な ts を通した: {bad!r}")

    def test_credits_for_matches_codex_bridge_formula(self):
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        usage = {"input_tokens": 2_000_000, "cached_input_tokens": 500_000,
                 "cache_write_input_tokens": 100_000, "output_tokens": 10_000,
                 "reasoning_output_tokens": 8_000}
        # 非キャッシュ 1.5M×5 + 書込 0.1M×5 + キャッシュ 0.5M×0.5 + 出力 0.01M×30
        self.assertAlmostEqual(
            lib.codex_credits_for("gpt-5.6-luna", usage, pricing), 8.55, places=4)
        self.assertIsNone(lib.codex_credits_for("gpt-9-unknown", usage, pricing))
        self.assertIsNone(lib.codex_credits_for("gpt-5.6-luna", "usage ではない", pricing))

    def test_iter_ledger_counts_ignored_rows(self):
        now = 1_755_000_000
        self.standard_ledger(now)
        since = datetime.fromtimestamp(now - WEEK, tz=timezone.utc)
        until = datetime.fromtimestamp(now, tz=timezone.utc)
        stats = {}
        rows = list(lib.iter_codex_ledger(self.codex_ledger, since=since, until=until, stats=stats))
        self.assertEqual(len(rows), 3)
        self.assertEqual(stats["ignored"], 4)
        self.assertEqual(stats["mock"], 1)
        self.assertEqual(stats["broken"], 1)
        self.assertEqual(stats["no_usage"], 1)
        self.assertEqual(stats["bad_ts"], 1)
        self.assertEqual(stats["out_of_window"], 1)

    def test_iter_ledger_missing_file(self):
        stats = {}
        rows = list(lib.iter_codex_ledger(self.tmp / "nope.jsonl", stats=stats))
        self.assertEqual(rows, [])
        self.assertEqual(stats["ignored"], 0)

    def test_aggregate_codex(self):
        now = 1_755_000_000
        self.standard_ledger(now)
        since = datetime.fromtimestamp(now - WEEK, tz=timezone.utc)
        until = datetime.fromtimestamp(now, tz=timezone.utc)
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        agg = lib.aggregate_codex(
            lib.iter_codex_ledger(self.codex_ledger, since=since, until=until), pricing)
        self.assertAlmostEqual(agg["credits"], 100.55, places=4)
        self.assertEqual(agg["jobs"], 3)
        self.assertAlmostEqual(agg["by_model"]["gpt-5.6-terra"]["credits"], 80.0, places=4)
        self.assertAlmostEqual(agg["by_model"]["gpt-5.6-luna"]["credits"], 8.05, places=4)
        self.assertAlmostEqual(agg["by_model"]["gpt-5.6-sol"]["credits"], 12.5, places=4)
        self.assertEqual(agg["by_model"]["gpt-5.6-terra"]["output_tokens"], 100_000)

    def test_unknown_model_without_credits_est(self):
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        rows = [{"model": "gpt-9-future", "usage": {"input_tokens": 1_000, "output_tokens": 10},
                 "credits_est": None}]
        agg = lib.aggregate_codex(rows, pricing)
        self.assertEqual(agg["unknown_models"], ["gpt-9-future"])
        self.assertAlmostEqual(agg["credits"], 0.0)
        self.assertFalse(agg["by_model"]["gpt-9-future"]["known"])

    def test_huge_ledger_is_fast(self):
        """10 万行の台帳でも走査＋集計が 1 秒以内（refresh の追加所要の上限）。"""
        now = 1_755_000_000
        with open(self.codex_ledger, "w", encoding="utf-8") as f:
            for i in range(100_000):
                f.write(json.dumps(codex_row(now - 100_000 + i, inp=1_000, out=100)) + "\n")
        since = datetime.fromtimestamp(now - WEEK, tz=timezone.utc)
        until = datetime.fromtimestamp(now, tz=timezone.utc)
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        t0 = time.monotonic()
        agg = lib.aggregate_codex(
            lib.iter_codex_ledger(self.codex_ledger, since=since, until=until), pricing)
        elapsed = time.monotonic() - t0
        self.assertEqual(agg["jobs"], 100_000)
        self.assertLess(elapsed, 1.0, f"台帳の集計が遅い: {elapsed:.2f}s")


class TestCodexLedgerRobustness(CodexLedgerMixin, PaceTestBase):
    """想定外入力（読取・デコード・パースの3層）で落ちないこと。"""

    def test_pathological_lines(self):
        with open(self.codex_ledger, "wb") as f:
            f.write(b"\xff\xfe\x00 not utf8 json\n")                       # 不正エンコーディング
            f.write(json.dumps(codex_row("2026-08-22T00:00:00Z", model="gpt-5.6-luna",
                                         inp=1_000, out=10)).encode() + b"\n")
            # 4,300 桁超の整数リテラル -> json が JSONDecodeError ではなく ValueError を投げる
            f.write(b'{"ts":"2026-08-22T00:00:00Z","usage":{"input_tokens":' + b"9" * 1_000_000 + b"}}\n")
            f.write(b'{"ts":"2026-08-22T00:00:00Z","usage":[]}\n')          # usage が配列
            f.write(b"[]\n")                                                # dict ではない
            # 型不正の値（int にできない）は 0 として計上する
            f.write(b'{"ts":"2026-08-22T00:00:00Z","usage":{"input_tokens":"abc"},"model":"gpt-5.6-luna"}\n')
            # 改行で終わっていない最終行
            f.write(b'{"ts":"2026-08-22T00:00:00Z","usage":{"input_tokens":1},"model":"gpt-5.6-luna"}')
        stats = {}
        rows = list(lib.iter_codex_ledger(self.codex_ledger, stats=stats))
        self.assertEqual(len(rows), 3)
        self.assertEqual(stats["broken"], 3)
        self.assertEqual(stats["no_usage"], 1)
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        self.assertAlmostEqual(lib.aggregate_codex(rows, pricing)["credits"], 0.0053, places=6)

    def test_directory_as_ledger_path(self):
        stats = {}
        self.assertEqual(list(lib.iter_codex_ledger(self.tmp, stats=stats)), [])
        self.assertEqual(stats["ignored"], 0)

    def test_refresh_survives_pathological_ledger(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        with open(self.codex_ledger, "wb") as f:
            f.write(b'{"ts":"x","usage":{"input_tokens":' + b"9" * 1_000_000 + b"}}\n")
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        c = self.read_cache()
        self.assertNotIn("error", c)
        self.assertEqual(c["codex"]["window_jobs"], 0)
        self.assertEqual(c["codex"]["ignored_rows"], 1)


class TestCodexRefresh(CodexLedgerMixin, PaceTestBase):
    def _setup(self, now, cap=None):
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.standard_ledger(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        if cap is not None:
            cfg_path = self.root / "config" / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["budget"]["pace"]["codex_weekly_credits"] = cap
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return resets_at

    def test_codex_in_cache(self):
        now = 1_755_000_000
        self._setup(now)
        self.run_refresh("--now", str(now), "--quiet")
        cx = self.read_cache()["codex"]
        self.assertAlmostEqual(cx["window_credits"], 100.55, places=4)
        self.assertEqual(cx["window_jobs"], 3)
        self.assertAlmostEqual(cx["five_hour_credits"], 12.5, places=4)
        self.assertEqual(cx["five_hour_jobs"], 1)
        self.assertEqual(cx["ignored_rows"], 4)
        self.assertIsNone(cx["weekly_cap"])
        self.assertIsNone(cx["pace"])
        self.assertEqual(cx["ledger_path"], str(self.codex_ledger))
        self.assertAlmostEqual(cx["by_model"]["gpt-5.6-terra"]["credits"], 80.0, places=4)
        self.assertTrue(any("上限は未設定" in n for n in cx["notes"]))
        self.assertTrue(any("近似" in n for n in cx["notes"]))
        # Claude 側の集計には一切影響しない
        self.assertAlmostEqual(self.read_cache()["total_usd"], 20.0, places=6)

    def test_no_ledger_gives_null(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self.run_refresh("--now", str(now), "--quiet")
        self.assertIsNone(self.read_cache()["codex"])

    def test_no_samples_gives_null_codex(self):
        now = 1_755_000_000
        self.standard_ledger(now)
        self.run_refresh("--now", str(now), "--quiet")
        c = self.read_cache()
        self.assertEqual(c["notes"], ["no samples"])
        self.assertIsNone(c["codex"])

    def test_weekly_cap_gives_pct_and_pace(self):
        now = 1_755_000_000
        self._setup(now, cap=402.2)  # 100.55 / 402.2 = 25% ちょうど
        self.run_refresh("--now", str(now), "--quiet")
        cx = self.read_cache()["codex"]
        self.assertAlmostEqual(cx["weekly_cap"], 402.2, places=4)
        self.assertAlmostEqual(cx["used_pct"], 25.0, places=4)
        # 窓の 50% 経過で 25% 消費 -> pace 0.5、週末到達見込み 50%
        self.assertAlmostEqual(cx["pace"], 0.5, places=6)
        self.assertAlmostEqual(cx["projected_end_pct"], 50.0, places=6)

    def test_non_numeric_cap_is_treated_as_unset(self):
        now = 1_755_000_000
        self._setup(now, cap="たくさん")
        self.run_refresh("--now", str(now), "--quiet")
        cx = self.read_cache()["codex"]
        self.assertIsNone(cx["weekly_cap"])
        self.assertTrue(any("数値ではない" in n for n in cx["notes"]))


class TestCodexStatusline(CodexLedgerMixin, StatuslineMixin, PaceTestBase):
    def codex_cache_value(self, **over):
        v = {"window_credits": 100.55, "window_jobs": 3, "five_hour_credits": 12.5,
             "five_hour_jobs": 1, "by_model": {}, "weekly_cap": None, "used_pct": None,
             "pace": None, "projected_end_pct": None,
             "ledger_path": str(self.codex_ledger), "ignored_rows": 4, "notes": []}
        v.update(over)
        return v

    def test_no_codex_output_is_byte_identical(self):
        """台帳なし（codex キー無し / null）のとき既存表示と 1 バイトも変わらない。"""
        self.write_cache()
        without_key = self.run_sl(self.payload())
        self.write_cache(codex=None)
        with_null = self.run_sl(self.payload())
        self.assertEqual(without_key.encode(), with_null.encode())
        self.assertNotIn("🅒", without_key)

    def test_credits_segment_when_no_cap(self):
        self.write_cache(codex=self.codex_cache_value())
        out = self.run_sl(self.payload())
        self.assertIn("🅒 101cr", out)
        self.assertIn("\033[2m🅒", out)  # 薄色
        self.assertIn("📅W 41%/50%", out)  # 既存セグメントは不変

    def test_small_credits_use_one_decimal(self):
        self.write_cache(codex=self.codex_cache_value(window_credits=2.5))
        self.assertIn("🅒 2.5cr", self.run_sl(self.payload()))

    def test_pct_segment_when_cap_set(self):
        self.write_cache(codex=self.codex_cache_value(weekly_cap=300, used_pct=34.0, pace=0.6))
        out = self.run_sl(self.payload())
        self.assertIn("🅒 34%/50% ·0.60", out)
        self.assertIn("\033[2m🅒", out)  # pace 0.60 < band 下限 -> 薄色（余らせ気味）

    def test_pct_segment_on_pace_is_green(self):
        self.write_cache(codex=self.codex_cache_value(weekly_cap=300, used_pct=45.0, pace=0.9))
        out = self.run_sl(self.payload())
        self.assertIn("\033[32m🅒 45%/50% ·0.90", out)


class TestCodexInCostReport(CodexLedgerMixin, PaceTestBase):
    def run_cost_report(self, *extra, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", *extra],
            env=env or self.env, capture_output=True, text=True,
        )

    def _fixture(self, now, ledger_session="sess-main"):
        # subprocess の os.getcwd() は symlink 解決後のパスを返す（macOS の /var -> /private/var）。
        # encode_cwd の一致を取るため realpath を使う。
        cwd = os.path.realpath(str(self.work))
        # cost_report の scope=session は projects/<encode_cwd(cwd)>/<session>.jsonl を引く
        self.write_session(lib.encode_cwd(cwd), "sess-main", cwd, [
            assistant_line("claude-opus-5", now - 3600, usage(inp=1_000_000), "r1", "u1", cwd),
            assistant_line("claude-opus-5", now - 60, usage(inp=1_000_000), "r2", "u2", cwd),
        ])
        self.write_codex_ledger([
            codex_row(iso(now - 1800), model="gpt-5.6-terra", inp=1_000_000, out=100_000,
                      session=ledger_session),
            # 範囲外（1 日前）
            codex_row(iso(now - 86400), model="gpt-5.6-terra", inp=9_000_000, out=9_000_000,
                      session=ledger_session),
        ])

    def _env(self, cwd):
        env = dict(self.env)
        env["CLAUDE_CODE_SESSION_ID"] = "sess-main"
        env["PWD"] = cwd
        return env

    def _read_md(self):
        mds = sorted((self.root / "reports").glob("**/*.md"))
        self.assertTrue(mds, "レポート Markdown が生成されていない")
        return mds[-1].read_text(encoding="utf-8")

    def _read_log(self):
        line = (self.root / "var" / "log" / "reports.jsonl").read_text(
            encoding="utf-8").strip().splitlines()[-1]
        return json.loads(line)

    def test_matching_session_rows_are_listed(self):
        now = int(time.time())
        self._fixture(now)
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--desc", "テスト",
             "--since", iso(now - 7200)],
            env=self._env(os.path.realpath(str(self.work))), cwd=os.path.realpath(str(self.work)), capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        md = self._read_md()
        self.assertIn("## Codex（参考）", md)
        self.assertIn("gpt-5.6-terra", md)
        self.assertIn("| **合計** | 1 | **80.0** |", md)
        self.assertIn("Codex（参考）: 80.0cr / 1 件", p.stdout)
        log = self._read_log()
        self.assertEqual(log["codex"]["jobs"], 1)
        self.assertAlmostEqual(log["codex"]["credits"], 80.0, places=4)

    def test_other_session_rows_are_excluded(self):
        now = int(time.time())
        self._fixture(now, ledger_session="sess-other")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--desc", "テスト",
             "--since", iso(now - 7200)],
            env=self._env(os.path.realpath(str(self.work))), cwd=os.path.realpath(str(self.work)), capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        md = self._read_md()
        self.assertNotIn("## Codex（参考）", md)
        self.assertEqual(self._read_log()["codex"]["jobs"], 0)

    def test_global_scope_uses_time_window_only(self):
        now = int(time.time())
        self._fixture(now, ledger_session="sess-other")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--desc", "テスト",
             "--scope", "global", "--since", iso(now - 7200)],
            env=self._env(os.path.realpath(str(self.work))), cwd=os.path.realpath(str(self.work)), capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("## Codex（参考）", self._read_md())
        self.assertEqual(self._read_log()["codex"]["jobs"], 1)

    def test_no_ledger_keeps_report_unchanged(self):
        now = int(time.time())
        cwd = os.path.realpath(str(self.work))
        self.write_session(lib.encode_cwd(cwd), "sess-main", cwd, [
            assistant_line("claude-opus-5", now - 60, usage(inp=1_000_000), "r1", "u1", cwd),
        ])
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--desc", "テスト",
             "--since", iso(now - 7200)],
            env=self._env(cwd), cwd=cwd, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        md = self._read_md()
        self.assertNotIn("Codex", md)
        self.assertIsNone(self._read_log()["codex"])


class TestCodexInPaceReport(CodexLedgerMixin, PaceTestBase):
    def test_text_and_json_have_codex(self):
        now = 1_755_000_000
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.standard_ledger(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        self.run_refresh("--now", str(now), "--quiet")

        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Codex（codex-bridge の使用量台帳より・参考）", p.stdout)
        self.assertIn("101cr / 3 件", p.stdout)
        self.assertIn("上限未設定のため", p.stdout)
        self.assertIn("gpt-5.6-terra", p.stdout)
        self.assertIn("無視した行: 4 件", p.stdout)

        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now), "--json"],
            env=self.env, capture_output=True, text=True,
        )
        d = json.loads(p.stdout)
        self.assertAlmostEqual(d["codex"]["window_credits"], 100.55, places=4)
        self.assertEqual(d["codex"]["window_jobs"], 3)

    def test_no_ledger_has_no_codex_section(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self.run_refresh("--now", str(now), "--quiet")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Codex", p.stdout)


# ---------------------------------------------------------------------------
# 2 周目 verifier 指摘（pace-fixes-r2）の回帰テスト
# ---------------------------------------------------------------------------

def huge_int_line(ts: str) -> str:
    """4,300 桁超の整数リテラルを含む JSONL 行（json.loads が ValueError を投げる）。

    Python 3.11+ の `int_max_str_digits`（既定 4300）により、構文としては正しい JSON でも
    `json.JSONDecodeError` ではなく素の `ValueError` になる。type=="user" にしてあるので
    iter_usage / _scan_file_events / find_first_user_text の 3 経路すべてを通る。
    """
    return (
        '{"type":"user","timestamp":"' + ts + '",'
        '"message":{"role":"user","content":"ダミー"},'
        '"n":' + "9" * 5000 + "}"
    )


class TestHugeIntegerLiteralInTranscript(PaceTestBase):
    """medium-1: 4,300 桁超の整数リテラル行 1 本で集計全体を落とさない（当該行だけ skip）。"""

    def _fixture(self, now):
        cwd = str(self.work)
        t = now - 3600
        self.write_session("-tmp-work", "sess-main", cwd, [
            huge_int_line(iso(t - 5)),
            assistant_line("claude-fable-5", t, usage(inp=500_000), "req-f1", "u-f1", cwd),
            assistant_line("claude-opus-5", t + 10, usage(inp=2_000_000), "req-o1", "u-o1", cwd),
        ], [
            assistant_line("claude-opus-5", t + 30, usage(inp=1_000_000), "req-o2", "u-o2", cwd),
        ])

    def test_refresh_survives_huge_integer_line(self):
        now = 1_755_000_000
        self._fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        c = self.read_cache()
        self.assertNotIn("error", c)
        # 壊れた 1 行だけが skip され、他の行は従来どおり計上される
        self.assertAlmostEqual(c["total_usd"], 20.0, places=6)

    def test_cost_report_global_survives_huge_integer_line(self):
        now = int(time.time())
        self._fixture(now)
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--scope", "global",
             "--since", iso(now - 7200), "--until", iso(now + 60)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("合計: $20.00", p.stdout)


class TestCodexWindowEndInCostReport(CodexLedgerMixin, PaceTestBase):
    """medium-2: Codex 窓の終端は Claude 行と同じ until。窓外行は注記に件数を出す。"""

    def _fixture(self, now, extra_rows=()):
        cwd = os.path.realpath(str(self.work))
        # Claude 側の最終アクティビティは now-3600（end_display はここへ補正される）
        self.write_session(lib.encode_cwd(cwd), "sess-main", cwd, [
            assistant_line("claude-opus-5", now - 3600, usage(inp=1_000_000), "r1", "u1", cwd),
        ])
        self.write_codex_ledger([
            # Claude の最終アクティビティより**後**に完了した Codex ジョブ
            codex_row(iso(now - 600), model="gpt-5.6-terra", inp=1_000_000, out=100_000,
                      session="sess-main"),
            *extra_rows,
        ])
        return cwd

    def _run(self, cwd, now):
        env = dict(self.env)
        env["CLAUDE_CODE_SESSION_ID"] = "sess-main"
        env["PWD"] = cwd
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "cost_report.py"), "--no-image", "--desc", "テスト",
             "--since", iso(now - 7200)],
            env=env, cwd=cwd, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def _read_md(self):
        mds = sorted((self.root / "reports").glob("**/*.md"))
        self.assertTrue(mds, "レポート Markdown が生成されていない")
        return mds[-1].read_text(encoding="utf-8")

    def test_codex_job_after_last_claude_activity_is_listed(self):
        now = int(time.time())
        cwd = self._fixture(now)
        p = self._run(cwd, now)
        self.assertIn("Codex（参考）: 80.0cr / 1 件", p.stdout)
        self.assertIn("## Codex（参考）", self._read_md())

    def test_out_of_window_rows_are_noted(self):
        now = int(time.time())
        cwd = self._fixture(now, extra_rows=[
            # since より前（範囲外）
            codex_row(iso(now - 86400), model="gpt-5.6-terra", inp=9_000_000, out=9_000_000,
                      session="sess-main"),
        ])
        self._run(cwd, now)
        self.assertIn("範囲外 1 件", self._read_md())


class TestCodexNegativeCap(CodexLedgerMixin, PaceTestBase):
    """medium-3: codex_weekly_credits が負値でも pace_refresh（非 quiet）/ pace_report が落ちない。"""

    def _setup(self, now, cap):
        resets_at = now + WEEK // 2
        self.standard_fixture(now)
        self.standard_ledger(now)
        self.write_samples([self.sample(now - 30, 40.0, resets_at)])
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["codex_weekly_credits"] = cap
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_refresh_verbose_survives_negative_cap(self):
        now = 1_755_000_000
        self._setup(now, -5)
        p = self.run_refresh("--now", str(now), check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        cx = self.read_cache()["codex"]
        self.assertIsNone(cx["weekly_cap"])
        self.assertIsNone(cx["used_pct"])
        self.assertTrue(any("上限は未設定" in n for n in cx["notes"]), cx["notes"])
        self.assertIn("上限未設定", p.stdout)

    def test_pace_report_survives_negative_cap(self):
        now = 1_755_000_000
        self._setup(now, -5)
        self.run_refresh("--now", str(now), "--quiet")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("上限未設定のため", p.stdout)


class TestCodexUnreadableLedger(CodexLedgerMixin, PaceTestBase):
    """low-4: 台帳が存在するのに開けないとき、黙って 0 件にせず注記を出す。"""

    def _make_unreadable(self):
        self.write_codex_ledger([codex_row(iso(1_755_000_000 - 600), inp=1_000, out=10)])
        os.chmod(self.codex_ledger, 0o000)
        if os.access(self.codex_ledger, os.R_OK):  # root 実行などで効かない場合
            self.skipTest("chmod による読取禁止が効かない環境")

    def tearDown(self):
        if self.codex_ledger.exists():
            os.chmod(self.codex_ledger, 0o600)
        super().tearDown()

    def test_stats_marks_unreadable(self):
        self._make_unreadable()
        stats = {}
        rows = list(lib.iter_codex_ledger(self.codex_ledger, stats=stats))
        self.assertEqual(rows, [])
        self.assertEqual(stats["unreadable"], 1)

    def test_refresh_notes_unreadable_ledger(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self._make_unreadable()
        p = self.run_refresh("--now", str(now), "--quiet", check=False)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        cx = self.read_cache()["codex"]
        self.assertTrue(any("台帳を読めませんでした" in n for n in cx["notes"]), cx["notes"])


class TestStatuslineNumericRobustness(StatuslineMixin, PaceTestBase):
    """low-5 / low-6: 非数値・空文字の config 値で表示がずれない・stderr を汚さない。"""

    def _set_pace_config(self, **over):
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"].update(over)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _run_raw(self):
        return subprocess.run(
            ["bash", str(SCRIPTS / "pace_statusline.sh")],
            input=json.dumps(self.payload()), env=self.sl_env(), capture_output=True, text=True,
        )

    def test_leading_dot_ttl_does_not_leak_stderr(self):
        # low-5: ".5" は num_or を素通りし、${TTL%.*} が空文字になって [ -gt "" ] が壊れる
        self.write_cache()
        self._set_pace_config(refresh_ttl_sec=".5")
        p = self._run_raw()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "", f"stderr に漏れがある: {p.stderr!r}")
        self.assertIn("📅W 41%/50%", p.stdout)

    def test_empty_config_value_does_not_shift_fields(self):
        # low-6: @tsv の空フィールドは IFS=TAB の read で詰められ、CAP 等が 1 つずれる
        self.write_cache()
        self._set_pace_config(refresh_ttl_sec="")
        p = self._run_raw()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "", f"stderr に漏れがある: {p.stderr!r}")
        self.assertIn("F≈12%/50% ·0.63", p.stdout)
        # band が 1 つずれると pace 0.82 が「余らせ気味（薄色）」に化ける
        self.assertIn("\033[32m📅W 41%/50%", p.stdout)

    def test_empty_cache_value_does_not_shift_fields(self):
        # cache.json 側も同様（est_pct が空文字でも cap/pace がずれない）
        self.write_cache(codex={"window_credits": 100.55, "window_jobs": 3,
                                "five_hour_credits": 12.5, "five_hour_jobs": 1, "by_model": {},
                                "weekly_cap": None, "used_pct": None, "pace": None,
                                "projected_end_pct": None, "ledger_path": "x",
                                "ignored_rows": 0, "notes": []},
                         fable={"est_pct": "", "cap_pct": 50, "pace": 0.63, "share": 0.3,
                                "usd": 1.0, "tokens": 1})
        p = self._run_raw()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "", f"stderr に漏れがある: {p.stderr!r}")
        self.assertIn("🅒 101cr", p.stdout)


class TestCodexNonFiniteCredits(CodexLedgerMixin, StatuslineMixin, PaceTestBase):
    """low-6: NaN / inf の credits_est をそのまま流すと cache.json が jq で読めなくなる。"""

    def test_aggregate_codex_ignores_non_finite(self):
        pricing = json.loads(CODEX_PRICING.read_text(encoding="utf-8"))
        rows = [
            {"model": "gpt-5.6-sol", "usage": {"input_tokens": 10, "output_tokens": 10},
             "credits_est": float("nan")},
            {"model": "gpt-5.6-sol", "usage": {"input_tokens": 10, "output_tokens": 10},
             "credits_est": float("inf")},
            {"model": "gpt-5.6-sol", "usage": {"input_tokens": 10, "output_tokens": 10},
             "credits_est": 3.5},
        ]
        agg = lib.aggregate_codex(rows, pricing)
        self.assertAlmostEqual(agg["credits"], 3.5, places=6)
        self.assertEqual(agg["ignored"], 2)
        self.assertAlmostEqual(agg["by_model"]["gpt-5.6-sol"]["credits"], 3.5, places=6)

    def test_statusline_keeps_codex_segment_with_nan_row(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        self.write_codex_ledger([
            codex_row(iso(now - 600), model="gpt-5.6-sol", inp=10, out=10,
                      credits_est=float("nan")),
            codex_row(iso(now - 500), model="gpt-5.6-sol", inp=10, out=10, credits_est=12.5),
        ])
        self.run_refresh("--now", str(now), "--quiet")
        raw = (self.pace_dir / "cache.json").read_text(encoding="utf-8")

        def reject(name):  # NaN / Infinity は JSON として不正（jq が読めない）
            raise AssertionError(f"cache.json に {name} リテラルが混入している")

        json.loads(raw, parse_constant=reject)
        out = self.run_sl(self.payload())
        self.assertIn("🅒", out)
        self.assertIn("12.5cr", out)


class TestStaleRegularFileLock(StatuslineMixin, PaceTestBase):
    """low-7: refresh.lock が（ディレクトリでなく）通常ファイルとして残っても回収する。"""

    def test_stale_regular_file_lock_is_taken_over(self):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lock = self.pace_dir / "refresh.lock"
        lock.write_text("", encoding="utf-8")
        old = self.NOW - 3600
        os.utime(lock, (old, old))
        counter = self.tmp / "refresh-count"
        self.run_sl(self.payload(),
                    env=self.sl_env(FCM_PACE_REFRESH_CMD=f"bash -c 'echo x >> {counter}'"))
        time.sleep(1.0)
        self.assertTrue(counter.exists(), "通常ファイルの stale ロックを回収できていない")


class TestPaceReportRefreshErrorCache(PaceTestBase):
    """low-10: `pace_report.py --refresh` も想定外例外でネガティブキャッシュを書く。"""

    def test_unexpected_exception_writes_error_cache(self):
        now = 1_755_000_000
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 40.0, now + WEEK // 2)])
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["fable_cap_pct"] = "はんぶん"  # float() で ValueError
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--refresh", "--now", str(now)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(p.returncode, 1, p.stdout)
        c = self.read_cache()
        self.assertIn("ValueError", c["error"])


if __name__ == "__main__":
    unittest.main()
