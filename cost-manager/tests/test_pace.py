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
        # Codex 公式 usage のサンプリングは既定で無効にする（テストからは絶対に
        # ネットワークへ出ない）。必要なテストだけ `enable_official()` で有効化し、
        # 応答は `FCM_CODEX_OFFICIAL_FIXTURE` で注入する。
        # CODEX_HOME も存在しないディレクトリへ向けて二重に隔離する。
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"].setdefault("codex_official", {})["enabled"] = False
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.env["CODEX_HOME"] = str(self.tmp / "codexhome")
        self.env.pop("FCM_CODEX_OFFICIAL_FIXTURE", None)

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


# ---------------------------------------------------------------------------
# Codex 公式 used_percent の自動サンプリング（codex-official）
# ---------------------------------------------------------------------------
# ネットワークは一切使わない。HTTP 応答は fetcher 注入（in-process）または
# `FCM_CODEX_OFFICIAL_FIXTURE`（サブプロセス）で与える。

OFFICIAL_TOKEN = "sk-test-ACCESSTOKEN-DO-NOT-LEAK"
OFFICIAL_ACCOUNT = "acct-XYZ789-DO-NOT-LEAK"
OFFICIAL_EMAIL = "someone@example.com"
OFFICIAL_USER = "user-abc123-DO-NOT-LEAK"
OFFICIAL_REFRESH = "rt-REFRESHTOKEN-DO-NOT-LEAK"
OFFICIAL_SECRETS = (
    OFFICIAL_TOKEN, OFFICIAL_ACCOUNT, OFFICIAL_EMAIL, OFFICIAL_USER, OFFICIAL_REFRESH,
)


def official_mod():
    """codex_official をテスト実行時に遅延 import する（未実装時は他テストを巻き込まない）。"""
    import codex_official
    return codex_official


class CodexOfficialMixin:
    """公式 usage の合成 auth / 合成レスポンスを用意するヘルパ。"""

    def codex_home(self, auth="ok"):
        home = self.tmp / "codexhome"
        home.mkdir(exist_ok=True)
        self.env["CODEX_HOME"] = str(home)
        p = home / "auth.json"
        if auth == "missing":
            if p.exists():
                p.unlink()
        elif auth == "broken":
            p.write_text('{"tokens": {壊れている', encoding="utf-8")
        elif auth == "no_tokens":
            p.write_text(json.dumps({"OPENAI_API_KEY": None}), encoding="utf-8")
        elif auth == "empty_tokens":
            p.write_text(json.dumps({"tokens": {"id_token": "eyJ-dummy"}}), encoding="utf-8")
        else:
            p.write_text(json.dumps({
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "eyJ-dummy",
                    "access_token": OFFICIAL_TOKEN,
                    "refresh_token": OFFICIAL_REFRESH,
                    "account_id": OFFICIAL_ACCOUNT,
                },
                "last_refresh": "2026-08-24T00:00:00.000Z",
            }), encoding="utf-8")
        return p

    def official_body(self, used=34, reset_at=None, window=WEEK, secondary=None):
        """2026-08-24 実測の 200 レスポンス（識別子を含む形のまま）。"""
        return {
            "plan_type": "pro",
            "email": OFFICIAL_EMAIL,
            "user_id": OFFICIAL_USER,
            "account_id": OFFICIAL_ACCOUNT,
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": window,
                    "reset_after_seconds": 597445,
                    "reset_at": reset_at,
                },
                "secondary_window": secondary,
            },
            "additional_rate_limits": [
                {"limit_name": "GPT-5.3-Codex-Spark",
                 "rate_limit": {"primary_window": {"used_percent": 90, "reset_at": reset_at}}}
            ],
            "credits": {"has_credits": False, "balance": "0", "account_id": OFFICIAL_ACCOUNT},
        }

    def set_fixture(self, status=200, body=None, error=None):
        """サブプロセス実行時の HTTP 応答を注入する（ネットワークへは出ない）。"""
        f = self.tmp / "official_fixture.json"
        d = {"error": error} if error else {
            "status": status,
            "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False),
        }
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        self.env["FCM_CODEX_OFFICIAL_FIXTURE"] = str(f)

    def enable_official(self, **over):
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        o = cfg["budget"]["pace"].setdefault("codex_official", {})
        o.update({"enabled": True, "min_interval_sec": 0, "timeout_sec": 5, "max_age_sec": 21600})
        o.update(over)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def official_samples_path(self):
        return self.pace_dir / "codex_official_samples.jsonl"

    def write_official_sample(self, ts, used=34, reset_at=None, window=WEEK, plan="pro"):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": ts, "plan_type": plan,
            "primary": {"used_percent": used, "limit_window_seconds": window,
                        "reset_at": reset_at},
            "secondary": None,
        })
        with open(self.official_samples_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def assert_no_secrets(self, *extra_texts):
        """隔離ルート配下の全ファイルと与えられた出力に識別子・トークンが無いこと。"""
        blobs = [(str(p), p.read_bytes()) for p in self.root.rglob("*") if p.is_file()]
        for i, t in enumerate(extra_texts):
            blobs.append((f"<出力{i}>", (t or "").encode("utf-8")))
        for name, data in blobs:
            for s in OFFICIAL_SECRETS:
                self.assertNotIn(s.encode("utf-8"), data,
                                 f"{name} に識別子・トークンが漏れている: {s}")


def fake_fetcher(status=200, body=None, exc=None, log=None):
    def _f(url, headers, timeout):
        if log is not None:
            log.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if exc is not None:
            raise exc
        return status, (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    return _f


class TestCodexOfficialFetch(CodexOfficialMixin, PaceTestBase):
    """fetch_official / sample_official の単体（fetcher 注入・ネットワーク不使用）。"""

    def setUp(self):
        super().setUp()
        self.m = official_mod()
        self.auth = self.codex_home("ok")
        self.now = 1_755_000_000
        self.reset_at = self.now + WEEK // 2

    def test_fetch_returns_numbers_only(self):
        body = self.official_body(used=34, reset_at=self.reset_at)
        got = self.m.fetch_official(auth_path=self.auth, fetcher=fake_fetcher(body=body))
        self.assertEqual(set(got), {"plan_type", "primary", "secondary"})
        self.assertEqual(got["plan_type"], "pro")
        self.assertEqual(set(got["primary"]), {"used_percent", "limit_window_seconds", "reset_at"})
        self.assertEqual(got["primary"]["used_percent"], 34)
        self.assertEqual(got["primary"]["limit_window_seconds"], WEEK)
        self.assertEqual(got["primary"]["reset_at"], self.reset_at)
        self.assertIsNone(got["secondary"])
        blob = json.dumps(got, ensure_ascii=False)
        for s in OFFICIAL_SECRETS:
            self.assertNotIn(s, blob)
        self.assertNotIn("additional_rate_limits", blob)
        self.assertNotIn("credits", blob)

    def test_fetch_keeps_secondary_window_numbers(self):
        sec = {"used_percent": 12.5, "limit_window_seconds": 18000,
               "reset_at": self.now + 3600, "reset_after_seconds": 10}
        body = self.official_body(used=34, reset_at=self.reset_at, secondary=sec)
        got = self.m.fetch_official(auth_path=self.auth, fetcher=fake_fetcher(body=body))
        self.assertEqual(got["secondary"],
                         {"used_percent": 12.5, "limit_window_seconds": 18000,
                          "reset_at": self.now + 3600})

    def test_fetch_targets_chatgpt_only_with_auth_headers(self):
        log = []
        self.m.fetch_official(auth_path=self.auth, timeout_sec=7,
                              fetcher=fake_fetcher(body=self.official_body(reset_at=self.reset_at),
                                                   log=log))
        self.assertEqual(len(log), 1)
        self.assertTrue(log[0]["url"].startswith("https://chatgpt.com/"), log[0]["url"])
        h = log[0]["headers"]
        self.assertEqual(h["Authorization"], f"Bearer {OFFICIAL_TOKEN}")
        self.assertEqual(h["chatgpt-account-id"], OFFICIAL_ACCOUNT)
        self.assertEqual(log[0]["timeout"], 7)

    def _assert_error(self, fetcher=None, auth="ok", contains=None):
        auth_path = self.codex_home(auth)
        with self.assertRaises(self.m.OfficialError) as cm:
            self.m.fetch_official(auth_path=auth_path,
                                  fetcher=fetcher or fake_fetcher(body=self.official_body()))
        msg = str(cm.exception)
        for s in OFFICIAL_SECRETS:
            self.assertNotIn(s, msg, f"例外メッセージにトークン・識別子が漏れている: {msg}")
        if contains:
            self.assertIn(contains, msg)
        return msg

    def test_fetch_401_message_has_no_token(self):
        body = json.dumps({"detail": "invalid token", "access_token": OFFICIAL_TOKEN,
                           "email": OFFICIAL_EMAIL})
        msg = self._assert_error(fetcher=fake_fetcher(status=401, body=body), contains="401")
        self.assertIn("codex", msg)

    def test_fetch_timeout(self):
        self._assert_error(fetcher=fake_fetcher(exc=TimeoutError("timed out")),
                           contains="タイムアウト")

    def test_fetch_unexpected_exception_is_wrapped(self):
        boom = RuntimeError(f"leak {OFFICIAL_TOKEN} {OFFICIAL_EMAIL}")
        self._assert_error(fetcher=fake_fetcher(exc=boom))

    def test_fetch_bad_json(self):
        self._assert_error(fetcher=fake_fetcher(body="これは JSON ではない"), contains="JSON")

    def test_fetch_missing_primary_window(self):
        body = {"plan_type": "pro", "rate_limit": {"secondary_window": None}}
        self._assert_error(fetcher=fake_fetcher(body=body), contains="primary_window")

    def test_fetch_non_numeric_used_percent(self):
        body = self.official_body(used="たくさん", reset_at=self.reset_at)
        self._assert_error(fetcher=fake_fetcher(body=body), contains="primary_window")

    def test_auth_missing(self):
        self._assert_error(auth="missing", contains="auth.json")

    def test_auth_broken(self):
        self._assert_error(auth="broken", contains="auth.json")

    def test_auth_without_tokens_key(self):
        self._assert_error(auth="no_tokens", contains="tokens")

    def test_auth_without_access_token(self):
        self._assert_error(auth="empty_tokens", contains="access_token")

    def test_plan_type_is_sanitized(self):
        body = self.official_body(reset_at=self.reset_at)
        body["plan_type"] = "pro " + OFFICIAL_EMAIL
        got = self.m.fetch_official(auth_path=self.auth, fetcher=fake_fetcher(body=body))
        self.assertIsNone(got["plan_type"])

    # --- sample_official -----------------------------------------------------
    def test_sample_writes_numbers_only(self):
        path = self.official_samples_path()
        got = self.m.sample_official(
            auth_path=self.auth, samples_path=path, now=self.now,
            cfg={"min_interval_sec": 900},
            fetcher=fake_fetcher(body=self.official_body(used=34, reset_at=self.reset_at)))
        self.assertIsNotNone(got)
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(set(rec), {"ts", "plan_type", "primary", "secondary"})
        self.assertEqual(rec["ts"], self.now)
        self.assert_no_secrets(path.read_text(encoding="utf-8"))

    def test_sample_throttle_and_force(self):
        path = self.official_samples_path()
        f = fake_fetcher(body=self.official_body(used=34, reset_at=self.reset_at))
        cfg = {"min_interval_sec": 900}
        self.m.sample_official(auth_path=self.auth, samples_path=path, now=self.now,
                               cfg=cfg, fetcher=f)
        # 直後の 2 回目はスロットルされる
        self.assertIsNone(self.m.sample_official(auth_path=self.auth, samples_path=path,
                                                 now=self.now + 10, cfg=cfg, fetcher=f))
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().split("\n")), 1)
        # --force 相当は無視して書く
        self.assertIsNotNone(self.m.sample_official(auth_path=self.auth, samples_path=path,
                                                    now=self.now + 20, cfg=cfg, force=True,
                                                    fetcher=f))
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().split("\n")), 2)
        # 間隔を超えれば通常経路でも書く
        self.assertIsNotNone(self.m.sample_official(auth_path=self.auth, samples_path=path,
                                                    now=self.now + 2000, cfg=cfg, fetcher=f))
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().split("\n")), 3)

    def test_sample_ignores_broken_last_line(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("壊れた行\n", encoding="utf-8")
        got = self.m.sample_official(
            auth_path=self.auth, samples_path=path, now=self.now, cfg={"min_interval_sec": 900},
            fetcher=fake_fetcher(body=self.official_body(reset_at=self.reset_at)))
        self.assertIsNotNone(got)

    # --- 想定外入力（読取 / デコード / パースの 3 層） -----------------------
    def test_fetch_survives_hostile_bodies(self):
        """空・巨大整数リテラル・深いネスト・巨大本文・非 JSON でも例外型は OfficialError。"""
        bodies = [
            "",
            "null",
            "[]",
            json.dumps({"rate_limit": {"primary_window": {"used_percent": 1}}}),  # 数値欠落
            '{"plan_type": ' + "1" * 5000 + "}",           # 巨大整数リテラル -> ValueError
            "[" * 2000 + "]" * 2000,                        # 深いネスト -> RecursionError
            "あ" * 200_000,                                  # 巨大な非 JSON
            '{"rate_limit": {"primary_window": null}}',
        ]
        for b in bodies:
            with self.assertRaises(self.m.OfficialError, msg=f"落ちなかった本文: {b[:40]}"):
                self.m.fetch_official(auth_path=self.auth, fetcher=fake_fetcher(body=b))

    def test_auth_with_hostile_content(self):
        home = self.tmp / "codexhome"
        home.mkdir(exist_ok=True)
        self.env["CODEX_HOME"] = str(home)
        p = home / "auth.json"
        for content in (b"", b"\xff\xfe\x00\x01binary", ("1" * 5000).encode(),
                        b"[" * 2000 + b"]" * 2000, '{"tokens": "文字列"}'.encode("utf-8"),
                        b'{"tokens": {"access_token": 12345, "account_id": null}}'):
            p.write_bytes(content)
            with self.assertRaises(self.m.OfficialError, msg=f"落ちなかった: {content[:20]!r}"):
                self.m.fetch_official(auth_path=p,
                                      fetcher=fake_fetcher(body=self.official_body()))

    def test_samples_file_with_hostile_lines(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join([
                "",
                "壊れた行",
                "1" * 5000,                                    # 巨大整数リテラル
                "[" * 2000 + "]" * 2000,                       # 深いネスト
                json.dumps([1, 2, 3]),                         # dict でない
                json.dumps({"ts": "きのう", "primary": {}}),     # ts が数値でない
                json.dumps({"ts": 1, "primary": "文字列"}),      # primary が dict でない
                json.dumps({"ts": 2, "primary": {"used_percent": None}}),
                json.dumps({"ts": 3, "plan_type": "pro",
                            "primary": {"used_percent": 5, "limit_window_seconds": WEEK,
                                        "reset_at": 9}, "secondary": None}),
            ]) + "\n", encoding="utf-8")
        got = self.m.read_official_samples(path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["ts"], 3)

    def test_cli_json_output_has_no_secrets(self):
        self.enable_official()  # CLI も config の enabled に従う（無効なら取得しない）
        self.set_fixture(body=self.official_body(used=34, reset_at=self.reset_at))
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_official.py"), "--json", "--force"],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["primary"]["used_percent"], 34)
        self.assert_no_secrets(p.stdout, p.stderr)

    def test_cli_failure_exits_1_without_secrets(self):
        self.enable_official()
        self.codex_home("missing")
        self.set_fixture(body=self.official_body(reset_at=self.reset_at))
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_official.py"), "--force"],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("auth.json", p.stderr)
        self.assert_no_secrets(p.stdout, p.stderr)


class TestCodexOfficialRefresh(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """pace_refresh.py への組み込み（サブプロセス実行・応答はフィクスチャ注入）。"""

    def setUp(self):
        super().setUp()
        self.now = 1_755_000_000
        self.reset_at = self.now + WEEK // 2  # 公式窓の 50% が経過した状態
        self.codex_home("ok")
        self.enable_official()

    def prepare(self, used=34, ledger=True, samples=True):
        self.standard_fixture(self.now)
        if samples:
            self.write_samples([self.sample(self.now - 30, 41.2, self.now + WEEK // 2)])
        if ledger:
            self.standard_ledger(self.now)
        self.set_fixture(body=self.official_body(used=used, reset_at=self.reset_at))

    def test_official_in_cache_with_calibration(self):
        self.prepare(used=34)
        p = self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        o = cx["official"]
        self.assertEqual(set(o), {"used_pct", "window_start", "reset_at", "elapsed_ratio",
                                  "pace", "projected_end_pct", "plan_type", "sampled_at",
                                  "stale", "secondary"})
        self.assertAlmostEqual(o["used_pct"], 34.0)
        self.assertEqual(o["window_start"], self.reset_at - WEEK)
        self.assertEqual(o["reset_at"], self.reset_at)
        self.assertAlmostEqual(o["elapsed_ratio"], 0.5, places=6)
        self.assertAlmostEqual(o["pace"], 0.68, places=6)
        self.assertAlmostEqual(o["projected_end_pct"], 68.0, places=6)
        self.assertEqual(o["plan_type"], "pro")
        self.assertFalse(o["stale"])
        # 台帳 100.55cr / 0.34 = 295.735…
        self.assertAlmostEqual(cx["weekly_cap_est"], 100.55 / 0.34, places=3)
        self.assertEqual(cx["cap_source"], "estimated")
        self.assert_no_secrets(p.stdout, p.stderr)

    def test_manual_cap_takes_precedence(self):
        self.prepare(used=34)
        cfg_path = self.root / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["budget"]["pace"]["codex_weekly_credits"] = 300
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        self.assertEqual(cx["cap_source"], "manual")
        self.assertEqual(cx["weekly_cap"], 300)
        self.assertIsNotNone(cx["weekly_cap_est"])

    def test_used_percent_zero_gives_no_calibration(self):
        self.prepare(used=0)
        self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        self.assertEqual(cx["official"]["used_pct"], 0.0)
        self.assertIsNone(cx["weekly_cap_est"])
        self.assertTrue(any("較正" in n for n in cx["notes"]), cx["notes"])

    def test_official_window_replaces_approximation(self):
        """公式窓があるときは Claude の seven_day 窓ではなく公式窓で台帳を集計する。"""
        self.standard_fixture(self.now)
        self.write_samples([self.sample(self.now - 30, 41.2, self.now + WEEK // 2)])
        # 公式窓は now-1日 開始（reset_at = now + 6日 / 窓 7日）。
        reset_at = self.now + 6 * 24 * 3600
        self.set_fixture(body=self.official_body(used=34, reset_at=reset_at))
        self.write_codex_ledger([
            codex_row(iso(self.now - 3 * 24 * 3600), model="gpt-5.6-sol",
                      inp=10, out=10, credits_est=50.0),   # 公式窓の外（Claude 窓の中）
            codex_row(iso(self.now - 3600), model="gpt-5.6-sol",
                      inp=10, out=10, credits_est=7.0),    # 公式窓の中
        ])
        self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        self.assertAlmostEqual(cx["window_credits"], 7.0, places=6)
        self.assertEqual(cx["window_start"], reset_at - WEEK)
        self.assertFalse(any("近似" in n for n in cx["notes"]), cx["notes"])
        self.assertTrue(any("公式窓" in n for n in cx["notes"]), cx["notes"])

    def test_stale_sample_is_flagged_and_not_refetched(self):
        self.standard_fixture(self.now)
        self.write_samples([self.sample(self.now - 30, 41.2, self.now + WEEK // 2)])
        self.standard_ledger(self.now)
        self.write_official_sample(self.now - 40000, used=34, reset_at=self.reset_at)
        # スロットルで再取得しない（フィクスチャも auth も与えない＝取得すれば失敗する）
        self.enable_official(min_interval_sec=999999)
        self.codex_home("missing")
        self.run_refresh("--now", str(self.now))
        o = self.read_cache()["codex"]["official"]
        self.assertTrue(o["stale"])
        self.assertEqual(o["sampled_at"], self.now - 40000)
        self.assertEqual(len(self.official_samples_path().read_text().strip().split("\n")), 1)

    def test_official_without_claude_samples(self):
        """Claude 側のサンプルが無くても公式窓だけで Codex 節を出す。"""
        self.prepare(samples=False)
        self.run_refresh("--now", str(self.now))
        c = self.read_cache()
        self.assertIsNone(c["seven_day"])
        self.assertIsNotNone(c["codex"])
        self.assertAlmostEqual(c["codex"]["official"]["used_pct"], 34.0)

    def test_official_without_ledger(self):
        self.prepare(ledger=False)
        self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        self.assertEqual(cx["window_credits"], 0.0)
        self.assertIsNotNone(cx["official"])
        self.assertIsNone(cx["weekly_cap_est"])
        self.assertTrue(any("台帳" in n for n in cx["notes"]), cx["notes"])

    # --- 失敗系: refresh は落ちない -----------------------------------------
    def _assert_survives(self, note_word="公式"):
        p = self.run_refresh("--now", str(self.now))
        c = self.read_cache()
        self.assertIsNone(c.get("error"))
        cx = c.get("codex")
        if cx:
            self.assertIsNone(cx.get("official"))
        notes = list(c.get("notes") or []) + list((cx or {}).get("notes") or [])
        self.assertTrue(any(note_word in n for n in notes), notes)
        self.assert_no_secrets(p.stdout, p.stderr)
        return notes

    def test_auth_missing_survives(self):
        self.prepare()
        self.codex_home("missing")
        notes = self._assert_survives()
        self.assertTrue(any("auth.json" in n for n in notes), notes)

    def test_auth_broken_survives(self):
        self.prepare()
        self.codex_home("broken")
        self._assert_survives()

    def test_auth_without_tokens_survives(self):
        self.prepare()
        self.codex_home("no_tokens")
        self._assert_survives()

    def test_http_401_survives(self):
        self.prepare()
        self.set_fixture(status=401, body=json.dumps({"detail": "unauthorized",
                                                      "email": OFFICIAL_EMAIL}))
        notes = self._assert_survives()
        self.assertTrue(any("401" in n for n in notes), notes)

    def test_timeout_survives(self):
        self.prepare()
        self.set_fixture(error="timeout")
        self._assert_survives()

    def test_bad_json_survives(self):
        self.prepare()
        self.set_fixture(body="<html>maintenance</html>")
        self._assert_survives()

    def test_disabled_does_not_sample(self):
        self.prepare()
        self.enable_official(enabled=False)
        self.run_refresh("--now", str(self.now))
        cx = self.read_cache()["codex"]
        self.assertIsNone(cx["official"])
        self.assertFalse(self.official_samples_path().exists())
        self.assertTrue(any("近似" in n for n in cx["notes"]), cx["notes"])


class TestCodexOfficialStatusline(CodexOfficialMixin, CodexLedgerMixin,
                                  StatuslineMixin, PaceTestBase):
    def codex_value(self, official=None, **over):
        v = {"window_credits": 100.55, "window_jobs": 3, "five_hour_credits": 12.5,
             "five_hour_jobs": 1, "by_model": {}, "weekly_cap": None, "used_pct": None,
             "pace": None, "projected_end_pct": None, "weekly_cap_est": None,
             "cap_source": None, "official": official,
             "ledger_path": str(self.codex_ledger), "ignored_rows": 4, "notes": []}
        v.update(over)
        return v

    def official_value(self, used=12.0, elapsed=0.34, pace=0.35, stale=False):
        return {"used_pct": used, "window_start": self.NOW - 100, "reset_at": self.NOW + 100,
                "elapsed_ratio": elapsed, "pace": pace,
                "projected_end_pct": used / elapsed if elapsed else None,
                "plan_type": "pro", "sampled_at": self.NOW - 60, "stale": stale,
                "secondary": None}

    def test_official_segment(self):
        self.write_cache(codex=self.codex_value(official=self.official_value()))
        out = self.run_sl(self.payload())
        self.assertIn("🅒 12%/34% ·0.35", out)
        self.assertNotIn("cr", out)
        self.assertIn("📅W 41%/50%", out)  # 既存セグメントは不変

    def test_official_segment_color_follows_band(self):
        # pace 0.35 は band 下限未満 -> 薄色
        self.write_cache(codex=self.codex_value(official=self.official_value()))
        self.assertIn("\033[2m🅒 12%/34%", self.run_sl(self.payload()))
        # pace 0.90 は band 内 -> 緑
        self.write_cache(codex=self.codex_value(
            official=self.official_value(used=45.0, elapsed=0.5, pace=0.9)))
        self.assertIn("\033[32m🅒 45%/50% ·0.90", self.run_sl(self.payload()))
        # pace 1.2 は band 超過だが枯渇時点は窓の 83% 経過（margin 80 より後）-> 黄
        self.write_cache(codex=self.codex_value(
            official=self.official_value(used=60.0, elapsed=0.5, pace=1.2)))
        self.assertIn("\033[33m🅒 60%/50% ·1.20", self.run_sl(self.payload()))
        # pace 1.5・枯渇時点が窓の 67% 経過（margin 80 より前）-> 赤
        self.write_cache(codex=self.codex_value(
            official=self.official_value(used=30.0, elapsed=0.2, pace=1.5)))
        self.assertIn("\033[31m🅒 30%/20% ·1.50", self.run_sl(self.payload()))

    def test_official_stale_is_dimmed_with_suffix(self):
        self.write_cache(codex=self.codex_value(official=self.official_value(stale=True)))
        out = self.run_sl(self.payload())
        self.assertIn("\033[2m🅒 12%/34% ·0.35?", out)

    def test_without_official_is_byte_identical(self):
        """official が無いときの出力は現行（クレジット表示）と 1 バイトも変わらない。"""
        v = self.codex_value()
        del v["official"]
        del v["weekly_cap_est"]
        del v["cap_source"]
        self.write_cache(codex=v)
        legacy = self.run_sl(self.payload())
        self.write_cache(codex=self.codex_value(official=None))
        with_null = self.run_sl(self.payload())
        self.assertEqual(legacy.encode(), with_null.encode())
        self.assertIn("🅒 101cr", legacy)

    def test_statusline_makes_no_network_call(self):
        """statusline 自体はサンプリングしない（公式サンプルファイルを作らない）。"""
        self.write_cache(codex=self.codex_value(official=self.official_value()))
        self.run_sl(self.payload())
        self.assertFalse(self.official_samples_path().exists())


class TestCodexOfficialInPaceReport(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    def test_report_shows_official_line(self):
        now = 1_755_000_000
        reset_at = now + WEEK // 2
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.standard_ledger(now)
        self.codex_home("ok")
        self.enable_official()
        self.set_fixture(body=self.official_body(used=34, reset_at=reset_at))
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--refresh", "--now", str(now)],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("公式", p.stdout)
        self.assertIn("used 34%", p.stdout)
        self.assertIn("0.68", p.stdout)
        self.assertIn("pro", p.stdout)
        self.assertIn("weekly_cap_est", p.stdout)
        self.assert_no_secrets(p.stdout, p.stderr)


# ---------------------------------------------------------------------------
# codex-official の反証レビュー指摘（H1 / M2〜M5 / L6〜L11）の再現テスト
# ---------------------------------------------------------------------------


class TestCodexOfficialHostileWindow(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """H1: 窓の極端値で refresh 全体を落とさない（Claude 側集計を巻き添えにしない）。"""

    def setUp(self):
        super().setUp()
        self.now = 1_755_000_000
        self.reset_at = self.now + WEEK // 2
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(self.now)
        self.write_samples([self.sample(self.now - 30, 41.2, self.now + WEEK // 2)])
        self.standard_ledger(self.now)

    def _assert_refresh_survives(self):
        p = self.run_refresh("--now", str(self.now))
        c = self.read_cache()
        self.assertIsNone(c.get("error"))
        # Claude 側の集計が残っていること（Codex の不正窓に巻き込まれない）
        self.assertIsNotNone(c.get("seven_day"))
        self.assertAlmostEqual(c["seven_day"]["used"], 41.2)
        cx = c.get("codex")
        self.assertIsNotNone(cx)
        self.assertIsNone(cx.get("official"))
        notes = list(c.get("notes") or []) + list(cx.get("notes") or [])
        self.assertTrue(any("窓が不正" in n for n in notes), notes)
        self.assert_no_secrets(p.stdout, p.stderr)

    # --- 取得経路（フィクスチャ応答） ---------------------------------------
    def test_window_seconds_1e12_from_response(self):
        self.set_fixture(body=self.official_body(used=34, reset_at=self.reset_at, window=1e12))
        self._assert_refresh_survives()

    def test_window_seconds_1e300_from_response(self):
        self.set_fixture(body=self.official_body(used=34, reset_at=self.reset_at, window=1e300))
        self._assert_refresh_survives()

    def test_window_seconds_microseconds_from_response(self):
        self.set_fixture(body=self.official_body(used=34, reset_at=self.reset_at,
                                                 window=WEEK * 1_000_000))
        self._assert_refresh_survives()

    # --- サンプルファイル経路（既に書かれた不正行） -------------------------
    def _write_raw_sample(self, ts, used, window, reset_at):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": ts, "plan_type": "pro",
                           "primary": {"used_percent": used, "limit_window_seconds": window,
                                       "reset_at": reset_at},
                           "secondary": None})
        with open(self.official_samples_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _no_refetch(self):
        """再取得させない（スロットル + auth 無し）。既存の悪いサンプルだけを読ませる。"""
        self.enable_official(min_interval_sec=999999)
        self.codex_home("missing")

    def test_sample_window_nan(self):
        self._write_raw_sample(self.now - 60, 34, float("nan"), self.reset_at)
        self._no_refetch()
        self._assert_refresh_survives()

    def test_sample_window_infinity(self):
        self._write_raw_sample(self.now - 60, 34, float("inf"), self.reset_at)
        self._no_refetch()
        self._assert_refresh_survives()

    def test_sample_nanosecond_units(self):
        self._write_raw_sample(self.now - 60, 34, WEEK * 1_000_000_000,
                               self.reset_at * 1_000_000_000)
        self._no_refetch()
        self._assert_refresh_survives()

    def test_bad_sample_does_not_linger_as_last_line(self):
        """不正行は読み側でスキップされ、手前の有効サンプルが使われる。"""
        self.write_official_sample(self.now - 120, used=34, reset_at=self.reset_at)
        self._write_raw_sample(self.now - 60, 34, float("nan"), self.reset_at)
        self._no_refetch()
        self.run_refresh("--now", str(self.now))
        o = self.read_cache()["codex"]["official"]
        self.assertIsNotNone(o)
        self.assertEqual(o["sampled_at"], self.now - 120)


class TestCodexOfficialWallClock(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """M2: timeout_sec は 1 操作あたり。全体は `timeout_sec * 3` の壁時計で打ち切る。"""

    def setUp(self):
        super().setUp()
        self.m = official_mod()
        self.now = 1_755_000_000
        self.auth = self.codex_home("ok")

    def test_fetch_gives_up_at_overall_deadline(self):
        def slow(url, headers, timeout):
            time.sleep(10)
            return 200, "{}"

        t0 = time.monotonic()
        with self.assertRaises(self.m.OfficialError) as cm:
            self.m.fetch_official(auth_path=self.auth, timeout_sec=0.2, fetcher=slow)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"壁時計の見張りが効いていない（{elapsed:.1f} 秒）")
        self.assertIn("期限", str(cm.exception))
        for s in OFFICIAL_SECRETS:
            self.assertNotIn(s, str(cm.exception))

    def test_sample_official_gives_up_at_overall_deadline(self):
        def slow(url, headers, timeout):
            time.sleep(10)
            return 200, "{}"

        t0 = time.monotonic()
        with self.assertRaises(self.m.OfficialError):
            self.m.sample_official(auth_path=self.auth, samples_path=self.official_samples_path(),
                                   now=self.now, cfg={"min_interval_sec": 0, "timeout_sec": 0.2},
                                   fetcher=slow)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(self.official_samples_path().exists())

    def test_refresh_completes_with_slow_fetcher(self):
        """遅い応答でも refresh は期限内に完走し、注記 1 行で続行する。"""
        self.enable_official(timeout_sec=0.3)
        self.standard_fixture(self.now)
        self.write_samples([self.sample(self.now - 30, 41.2, self.now + WEEK // 2)])
        f = self.tmp / "official_fixture.json"
        f.write_text(json.dumps({"sleep": 10, "status": 200, "body": "{}"}), encoding="utf-8")
        self.env["FCM_CODEX_OFFICIAL_FIXTURE"] = str(f)
        t0 = time.monotonic()
        p = self.run_refresh("--now", str(self.now))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 9.0, f"refresh が遅い応答で待たされている（{elapsed:.1f} 秒）")
        c = self.read_cache()
        self.assertIsNone(c.get("error"))
        self.assertIsNotNone(c.get("seven_day"))
        notes = list(c.get("notes") or [])
        self.assertTrue(any("公式" in n for n in notes), notes)
        self.assert_no_secrets(p.stdout, p.stderr)


class TestCodexOfficialFixtureIsAnnounced(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """M3: フィクスチャ応答は無警告に効かせない。"""

    def test_fixture_marks_sample_and_notes(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        self.run_refresh("--now", str(now))
        rec = json.loads(self.official_samples_path().read_text(
            encoding="utf-8").strip().split("\n")[-1])
        self.assertTrue(rec.get("fixture"))
        c = self.read_cache()
        notes = list(c.get("notes") or [])
        self.assertTrue(any("フィクスチャ" in n for n in notes), notes)
        # フィクスチャ行でも official 表示自体は出る
        self.assertIsNotNone(c["codex"]["official"])


class TestCodexOfficialReportWithoutClaudeSamples(CodexOfficialMixin, CodexLedgerMixin,
                                                  PaceTestBase):
    """M4: Claude サンプルが無くても Codex 節（公式行）を出して exit 0。"""

    def test_report_renders_codex_section(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(now)
        self.standard_ledger(now)
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        self.run_refresh("--now", str(now))
        self.assertIsNone(self.read_cache()["seven_day"])
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("公式", p.stdout)
        self.assertIn("used 34%", p.stdout)
        self.assertIn("Claude 側", p.stdout)
        self.assert_no_secrets(p.stdout, p.stderr)

    def test_report_without_any_data_still_exits_3(self):
        now = 1_755_000_000
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)


class TestCodexOfficialNonWeekWindow(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """M5: 公式窓が 7 日以外でも台帳側 pace の分母は公式窓幅。"""

    def test_three_day_window_paces_match(self):
        now = 1_755_000_000
        span = 3 * 24 * 3600
        reset_at = now + span // 2  # 3 日窓の 50% 経過
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.write_codex_ledger([
            codex_row(iso(now - 3600), model="gpt-5.6-sol", inp=10, out=10, credits_est=20.0),
        ])
        self.set_fixture(body=self.official_body(used=34, reset_at=reset_at, window=span))
        self.run_refresh("--now", str(now))
        cx = self.read_cache()["codex"]
        o = cx["official"]
        self.assertAlmostEqual(o["elapsed_ratio"], 0.5, places=6)
        self.assertEqual(cx["cap_source"], "estimated")
        self.assertAlmostEqual(cx["pace"], o["pace"], places=6)
        self.assertAlmostEqual(cx["projected_end_pct"], o["projected_end_pct"], places=6)


class TestCodexOfficialSampleFileHygiene(CodexOfficialMixin, PaceTestBase):
    """L7 / L8: 後方読み・ローテーション・ts の範囲検証。"""

    def setUp(self):
        super().setUp()
        self.m = official_mod()
        self.now = 1_755_000_000
        self.auth = self.codex_home("ok")

    def test_read_returns_only_last_valid_record(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for ts in (self.now - 300, self.now - 200):
                f.write(json.dumps({"ts": ts, "plan_type": "pro",
                                    "primary": {"used_percent": 1, "limit_window_seconds": WEEK,
                                                "reset_at": self.now + WEEK},
                                    "secondary": None}) + "\n")
            f.write("壊れた行\n")
        got = self.m.read_official_samples(path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["ts"], self.now - 200)

    def test_invalid_ts_rows_are_skipped_and_not_throttled(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for ts in (0, -1, 2 ** 31, self.now * 1000, float("nan")):
                f.write(json.dumps({"ts": ts, "plan_type": "pro",
                                    "primary": {"used_percent": 1, "limit_window_seconds": WEEK,
                                                "reset_at": self.now + WEEK},
                                    "secondary": None}) + "\n")
        self.assertEqual(self.m.read_official_samples(path), [])
        # 不正 ts をスロットル判定に使わない（= 取得が走る）
        got = self.m.sample_official(
            auth_path=self.auth, samples_path=path, now=self.now,
            cfg={"min_interval_sec": 999999},
            fetcher=fake_fetcher(body=self.official_body(used=34, reset_at=self.now + WEEK)))
        self.assertIsNotNone(got)
        self.assertEqual(len(self.m.read_official_samples(path)), 1)

    def test_invalid_window_rows_are_skipped(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        bad = [
            {"limit_window_seconds": 0, "reset_at": self.now + WEEK},
            {"limit_window_seconds": -WEEK, "reset_at": self.now + WEEK},
            {"limit_window_seconds": float("nan"), "reset_at": self.now + WEEK},
            {"limit_window_seconds": float("inf"), "reset_at": self.now + WEEK},
            {"limit_window_seconds": WEEK * 1_000_000, "reset_at": self.now + WEEK},
            {"limit_window_seconds": WEEK, "reset_at": self.now * 1_000_000},
            {"limit_window_seconds": WEEK, "reset_at": 0},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for b in bad:
                f.write(json.dumps({"ts": self.now - 10, "plan_type": "pro",
                                    "primary": {"used_percent": 1, **b},
                                    "secondary": None}) + "\n")
        self.assertEqual(self.m.read_official_samples(path), [])

    def test_rotation_halves_oversized_file(self):
        path = self.official_samples_path()
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        n = 5100
        with open(path, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"ts": self.now - n + i, "plan_type": "pro",
                                    "primary": {"used_percent": 1, "limit_window_seconds": WEEK,
                                                "reset_at": self.now + WEEK},
                                    "secondary": None}) + "\n")
        got = self.m.sample_official(
            auth_path=self.auth, samples_path=path, now=self.now, force=True,
            fetcher=fake_fetcher(body=self.official_body(used=34, reset_at=self.now + WEEK)))
        self.assertIsNotNone(got)
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertLess(len(lines), n)
        self.assertGreater(len(lines), 100)
        # 最新サンプルは残る
        self.assertEqual(json.loads(lines[-1])["ts"], self.now)
        self.assertEqual(self.m.read_official_samples(path)[0]["ts"], self.now)


class TestCodexOfficialDisabled(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """L9 / L10: 無効化の扱い（CLI も従う・真偽値のみ真）。"""

    def test_cli_refuses_when_disabled(self):
        self.codex_home("ok")
        self.set_fixture(body=self.official_body(used=34, reset_at=1_755_000_000 + WEEK))
        for extra in ([], ["--force"]):
            p = subprocess.run(
                [sys.executable, str(SCRIPTS / "codex_official.py"), *extra],
                env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("無効化", p.stderr)
            self.assertFalse(self.official_samples_path().exists())

    def test_string_enabled_is_false_with_warning(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official(enabled="false")
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.standard_ledger(now)
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        self.run_refresh("--now", str(now))
        c = self.read_cache()
        self.assertIsNone(c["codex"]["official"])
        self.assertFalse(self.official_samples_path().exists())
        notes = list(c.get("notes") or []) + list(c["codex"].get("notes") or [])
        self.assertTrue(any("enabled" in n for n in notes), notes)

    def test_truthy_string_enabled_is_also_false(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official(enabled="true")
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        self.run_refresh("--now", str(now))
        self.assertFalse(self.official_samples_path().exists())


class TestPaceRefreshArgValidationOrder(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """L11: 引数検証は official_lane（ネットワーク）より前。"""

    def test_bad_resets_at_fails_before_sampling(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(now)
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        p = self.run_refresh("--resets-at", "0", "--now", str(now), check=False)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(self.official_samples_path().exists())


class TestPaceReportHostileCache(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """H1 の同類: 壊れた cache.json の official でも表示側が落ちない。"""

    def _write_cache(self, official):
        self.pace_dir.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_json(self.pace_dir / "cache.json", {
            "computed_at": 1_755_000_000 - 10, "duration_sec": 1.0,
            "window": None, "seven_day": None, "fable": None, "models": {},
            "total_usd": 0.0, "unknown_models": [], "notes": [],
            "codex": {"window_credits": 1.0, "window_jobs": 1, "five_hour_credits": 0.0,
                      "five_hour_jobs": 0, "by_model": {}, "weekly_cap": None,
                      "weekly_cap_est": None, "cap_source": None, "used_pct": None,
                      "pace": None, "projected_end_pct": None, "official": official,
                      "ledger_path": str(self.codex_ledger), "ignored_rows": 0, "notes": []},
        })

    def test_report_survives_out_of_range_reset_at(self):
        now = 1_755_000_000
        for reset_at in (now * 1_000_000_000, -1, 0, 1e300):
            self._write_cache({
                "used_pct": 12.0, "window_start": now - 100, "reset_at": reset_at,
                "elapsed_ratio": 0.5, "pace": 0.24, "projected_end_pct": 24.0,
                "plan_type": "pro", "sampled_at": now - 60, "stale": False, "secondary": None,
            })
            p = subprocess.run(
                [sys.executable, str(SCRIPTS / "pace_report.py"), "--now", str(now)],
                env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, f"reset_at={reset_at}: {p.stderr}")
            self.assertIn("公式", p.stdout)


class TestCodexOfficialReportCalibrationNote(CodexOfficialMixin, CodexLedgerMixin, PaceTestBase):
    """L6: cap_source が estimated のときの但し書き。"""

    def test_estimated_cap_note(self):
        now = 1_755_000_000
        self.codex_home("ok")
        self.enable_official()
        self.standard_fixture(now)
        self.write_samples([self.sample(now - 30, 41.2, now + WEEK // 2)])
        self.standard_ledger(now)
        self.set_fixture(body=self.official_body(used=34, reset_at=now + WEEK // 2))
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "pace_report.py"), "--refresh", "--now", str(now)],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("較正の定義上", p.stdout)


if __name__ == "__main__":
    unittest.main()
