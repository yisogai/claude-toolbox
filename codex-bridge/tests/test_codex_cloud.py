#!/usr/bin/env python3
"""codex_cloud.py（`codex cloud` ラッパー）の引数検証テスト。

**実機の codex は一切呼ばない**。クラウド環境が未設定なうえ、呼べば課金・実タスク投入に
なるため、ここでは「子プロセスを起動する前に落ちる／そもそも起動しない」経路だけを見る。
子を起動しないことは、存在しないパスを `--codex-bin` に渡しても正常終了することで示す。

実行:
  python3 -m unittest discover -s codex-bridge/tests -v
  python3 -m pytest codex-bridge/tests/test_codex_cloud.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
CLOUD = SCRIPTS / "codex_cloud.py"

# 既存テストと同じく、スクリプト群（codex_cloud / codex_lib）を import 可能にする
sys.path.insert(0, str(SCRIPTS))


class CloudBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-cloud-test-"))
        #: 実在しない codex パス。これを渡しても実行に至らないことを各テストで確認する
        self.fake_bin = self.tmp / "no-such-codex"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        e = dict(os.environ)
        # PATH 上の本物の codex を拾って実機を叩かないよう、解決経路を潰しておく
        e.pop("CODEX_BIN", None)
        e["PATH"] = str(self.tmp / "empty-bin")
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def cloud(self, argv, timeout=60):
        return subprocess.run([sys.executable, str(CLOUD)] + argv,
                              capture_output=True, text=True, env=self.env(), timeout=timeout)


class TestCloudArgValidation(CloudBase):
    def test_help_ok(self):
        for argv in (["--help"], ["submit", "--help"], ["list", "--help"],
                     ["status", "--help"], ["diff", "--help"], ["apply", "--help"]):
            proc = self.cloud(argv)
            self.assertEqual(proc.returncode, 0, f"{argv}: {proc.stderr}")
            self.assertTrue(proc.stdout.strip(), f"{argv}: ヘルプが空")

    def test_submit_prompt_and_prompt_file_conflict_exit1(self):
        pf = self.tmp / "p.md"
        pf.write_text("本文\n", encoding="utf-8")
        proc = self.cloud(["submit", "--env", "env_1", "--prompt", "x",
                           "--prompt-file", str(pf), "--codex-bin", str(self.fake_bin)])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("同時に指定できません", proc.stderr)

    def test_submit_without_prompt_exit1(self):
        proc = self.cloud(["submit", "--env", "env_1", "--codex-bin", str(self.fake_bin)])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--prompt", proc.stderr)

    def test_apply_without_yes_is_dry_run(self):
        """--yes 無しは子プロセスを起動しない（偽 codex-bin でも exit 0 になることで示す）。"""
        proc = self.cloud(["apply", "TASK123", "--codex-bin", str(self.fake_bin)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ドライラン", proc.stdout)
        self.assertIn("TASK123", proc.stdout)
        self.assertIn("--yes", proc.stdout)

    def test_submit_missing_codex_bin_exit4(self):
        proc = self.cloud(["submit", "--env", "env_1", "--prompt", "x",
                           "--codex-bin", str(self.fake_bin)])
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("見つかりません", proc.stderr)

    def test_submit_cd_must_be_an_existing_directory(self):
        proc = self.cloud(["submit", "--env", "env_1", "--prompt", "x",
                           "--cd", str(self.tmp / "no-such-dir"),
                           "--codex-bin", str(self.fake_bin)])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--cd は存在するディレクトリ", proc.stderr)

    def test_submit_cd_is_used_as_child_cwd(self):
        fake_codex = self.tmp / "fake-codex"
        fake_codex.write_text("#!/bin/sh\npwd\n", encoding="utf-8")
        fake_codex.chmod(0o755)
        workdir = self.tmp / "workdir"
        workdir.mkdir()
        proc = self.cloud(["submit", "--env", "env_1", "--prompt", "x",
                           "--cd", str(workdir), "--codex-bin", str(fake_codex)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(Path(proc.stdout.strip()).samefile(workdir))


class TestCloudArgvMirrors(CloudBase):
    """組み立てた argv が実 CLI（`codex cloud <sub> --help`）のフラグと一致すること。"""

    def build(self, argv):
        import codex_cloud
        args = codex_cloud.build_parser().parse_args(argv)
        return args, codex_cloud

    def test_submit_argv(self):
        args, mod = self.build(["submit", "--env", "env_1", "--attempts", "3",
                                "--branch", "feat/x", "--prompt", "やること"])
        got = mod.build_argv("/bin/codex", args, "やること")
        self.assertEqual(got, ["/bin/codex", "cloud", "exec", "--env", "env_1",
                               "--attempts", "3", "--branch", "feat/x", "やること"])

    def test_diff_and_apply_argv(self):
        args, mod = self.build(["diff", "T1", "--attempt", "2"])
        self.assertEqual(mod.build_argv("/bin/codex", args),
                         ["/bin/codex", "cloud", "diff", "T1", "--attempt", "2"])
        args, mod = self.build(["apply", "T1", "--yes"])
        self.assertEqual(mod.build_argv("/bin/codex", args),
                         ["/bin/codex", "cloud", "apply", "T1"])

    def test_list_argv(self):
        args, mod = self.build(["list", "--env", "env_1", "--limit", "5", "--json"])
        self.assertEqual(mod.build_argv("/bin/codex", args),
                         ["/bin/codex", "cloud", "list", "--env", "env_1", "--limit", "5", "--json"])


if __name__ == "__main__":
    unittest.main()


class CloudEnvResolution(CloudBase):
    """submit の --env 省略時の既定値解決（--env → $CODEX_BRIDGE_CLOUD_ENV → var/cloud.json）。

    実機 codex は呼ばない。環境変数で解決が成功したことは「env 検証を通過して
    バイナリ解決（exit 4）まで進む」ことで示す。
    """

    def run_cloud(self, argv, env):
        return subprocess.run([sys.executable, str(CLOUD)] + argv,
                              capture_output=True, text=True, env=env, timeout=60)

    def test_submit_without_any_env_source(self):
        env = self.env()
        env.pop("CODEX_BRIDGE_CLOUD_ENV", None)
        r = self.run_cloud(["submit", "--prompt", "x", "--codex-bin", str(self.fake_bin)], env)
        # 開発機に var/cloud.json があれば解決に成功して exit 4（バイナリ解決）まで進み、
        # 無ければ解決失敗の exit 1。どちらでも「submit が実行に至らない」ことは変わらない。
        self.assertIn(r.returncode, (1, 4), r.stderr)
        if r.returncode == 1:
            self.assertIn("環境 ID がありません", r.stderr)

    def test_submit_env_from_environment_variable_reaches_bin_resolution(self):
        env = self.env()
        env["CODEX_BRIDGE_CLOUD_ENV"] = "6a8c4ba225d08191deadbeef00000000"
        r = self.run_cloud(["submit", "--prompt", "x", "--codex-bin", str(self.fake_bin)], env)
        # env 解決を通過し、次段のバイナリ解決で exit 4 になる（exit 1 なら解決失敗）
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertNotIn("環境 ID がありません", r.stderr)


class TestRepositoryEnvironmentResolution(CloudBase):
    def resolve(self, config, cd=None, git_runner=None, environ=None):
        import codex_cloud
        config_path = self.tmp / "cloud.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        args = codex_cloud.build_parser().parse_args(["submit", "--prompt", "x"])
        args.cd = str(cd) if cd is not None else None
        kwargs = {"config_path": config_path, "environ": environ if environ is not None else {}}
        if git_runner is not None:
            kwargs["git_runner"] = git_runner
        return codex_cloud.resolve_env_id(args, **kwargs)

    def test_extract_github_repo_slug(self):
        import codex_cloud
        for url in (
            "git@github.com:ReRaKuIMS/Lav-Rails.git",
            "https://github.com/ReRaKuIMS/Lav-Rails",
            "ssh://git@github.com/ReRaKuIMS/Lav-Rails.git",
        ):
            self.assertEqual(codex_cloud.extract_github_repo_slug(url), "rerakuims/lav-rails")
        for url in ("", "git@gitlab.com:owner/repo.git", "https://github.com/owner", "owner/repo"):
            self.assertIsNone(codex_cloud.extract_github_repo_slug(url))

    def test_repository_mapping_accepts_string_and_matches_case_insensitively(self):
        repo = self.tmp / "repo"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "git@github.com:ReRaKuIMS/Lav-Rails.git"],
                       check=True, capture_output=True, text=True)
        self.assertEqual(
            self.resolve(
                {"env_id": "global", "environments": {"RERAKUIMS/LAV-RAILS": "repo-env"}},
                cd=repo,
            ),
            "repo-env",
        )

    def test_repository_mapping_accepts_env_id_object(self):
        class Completed:
            returncode = 0
            stdout = "https://github.com/owner/repo.git\n"

        self.assertEqual(
            self.resolve({"environments": {"owner/repo": {"env_id": "repo-env"}}},
                         git_runner=lambda *args, **kwargs: Completed()),
            "repo-env",
        )

    def test_repository_mapping_reaches_bin_resolution(self):
        import codex_cloud
        repo = self.tmp / "repo-for-main"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "git@github.com:ReRaKuIMS/Lav-Rails.git"],
                       check=True, capture_output=True, text=True)
        (self.tmp / "cloud.json").write_text(
            json.dumps({"environments": {"rerakuims/lav-rails": "repo-env"}}),
            encoding="utf-8",
        )
        with mock.patch.object(codex_cloud.lib, "var_dir", return_value=self.tmp), \
             mock.patch.dict(os.environ, {"CODEX_BRIDGE_CLOUD_ENV": ""}):
            code = codex_cloud.main([
                "submit", "--cd", str(repo), "--prompt", "x", "--codex-bin", str(self.fake_bin),
            ])
        self.assertEqual(code, 4)

    def test_git_unavailable_falls_back_to_global_default(self):
        def missing_git(*args, **kwargs):
            raise FileNotFoundError("git")

        self.assertEqual(
            self.resolve({"env_id": "global", "environments": {"owner/repo": "repo-env"}},
                         git_runner=missing_git),
            "global",
        )
