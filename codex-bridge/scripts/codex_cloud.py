#!/usr/bin/env python3
"""Codex Cloud（`codex cloud`、EXPERIMENTAL）の薄いラッパー。

best-of-N（`--attempts N`）の実装委譲をクラウド側に投げ、生成された変更をローカルへ
取り込むまでの配管。`codex_run.py` と違い **JSONL の解釈も job.json の生成もしない**。
子プロセスの stdout / stderr はそのまま流し、この層の責務は次の 3 点だけに絞る:

- 引数の検証（`--prompt` / `--prompt-file` の排他、必須項目）
- codex 実バイナリの解決（`codex_lib.resolve_codex_bin()` を再利用。cmux シムを除外）
- タイムアウト付き実行と、終了コードの正規化

`apply` はローカルの作業ツリーを書き換えるため、既定では **実行しない**。
`--yes` が無い場合は実行するはずだったコマンドを表示して exit 0 で終わる（安全側）。

クラウド環境が未設定・GitHub 未連携だと codex 側がエラーで落ちる。判別は困難なため、
子が非ゼロ終了かつ stderr がある場合は常にセットアップ手順の注記を stderr に添える。

実行例:
  # best-of-3 で投げる（ENV_ID は `codex cloud` の TUI で確認する）
  # ENV_ID は Codex Web の環境設定 URL 末尾の 32 桁 hex（例: 6a8c4ba225d08191a0b1f440d06bcbdc）。
  # --env 省略時は $CODEX_BRIDGE_CLOUD_ENV → cloud.json の environments[GitHub slug] →
  # env_id の順で既定値を使う。slug は --cd（既定: カレント）の origin remote から得る。
  python3 codex_cloud.py submit --attempts 3 --prompt-file prompt.md
  python3 codex_cloud.py list --json
  python3 codex_cloud.py status task_xyz
  python3 codex_cloud.py diff task_xyz --attempt 2
  # 既定はドライラン（コマンド表示のみ）。実際に当てるときだけ --yes を付ける
  python3 codex_cloud.py apply task_xyz --attempt 2 --yes

終了コード: 0=正常（`apply` のドライランを含む） / 1=引数・入出力エラー /
2=codex が非ゼロ終了 / 3=タイムアウト / 4=codex 実バイナリが見つからない。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_lib as lib  # noqa: E402

#: 既定タイムアウト（秒）。submit はクラウド側の起動待ちがあるため長め。
DEFAULT_TIMEOUT_SUBMIT = 600.0
DEFAULT_TIMEOUT_OTHER = 120.0

GRACE_SEC = 5.0          # SIGTERM から SIGKILL までの猶予（codex_run.py と同じ扱い）
STDERR_TAIL_LINES = 20   # 非ゼロ終了時に報告へ添える stderr 末尾の行数
JOIN_SEC = 3.0           # stderr ポンプスレッドの join 予算

SETUP_HINT = (
    "ヒント: Codex Cloud を使うには ChatGPT 側で GitHub 連携と"
    "クラウド環境（ENV_ID）の作成が必要です（`codex cloud` の TUI で環境一覧を確認できます）。"
)


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------

def add_common(p: argparse.ArgumentParser, default_timeout: float) -> None:
    """全サブコマンド共通の引数。既定タイムアウトだけサブコマンドごとに変える。"""
    p.add_argument("--codex-bin", default=None, help="codex 実バイナリのパス（既定: CODEX_BIN / PATH）")
    p.add_argument("--timeout-sec", type=float, default=default_timeout,
                   help=f"壁時計タイムアウト秒（既定 {default_timeout:g}）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex_cloud.py",
        description="codex cloud（EXPERIMENTAL）の薄いラッパー。best-of-N の実装委譲に使う",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # -- submit ----------------------------------------------------------
    s = sub.add_parser("submit", help="クラウドにタスクを投げる（codex cloud exec）")
    s.add_argument("--env", default=None,
               help="対象の環境 ID（省略時: $CODEX_BRIDGE_CLOUD_ENV → cloud.json のリポジトリ別設定 → env_id）")
    s.add_argument("--cd", default=None,
                   help="実行ディレクトリ（既定: カレント。ブランチ・リポジトリ別環境の解決にも使用）")
    s.add_argument("--attempts", type=int, default=1, help="best-of-N の N（既定 1）")
    s.add_argument("--branch", default=None, help="実行対象の git ブランチ（既定: 現在のブランチ）")
    s.add_argument("--prompt", default=None, help="プロンプト文字列")
    s.add_argument("--prompt-file", default=None, help="プロンプトのファイル")
    add_common(s, DEFAULT_TIMEOUT_SUBMIT)

    # -- list ------------------------------------------------------------
    ls = sub.add_parser("list", help="クラウドのタスク一覧（codex cloud list）")
    ls.add_argument("--env", default=None, help="環境 ID で絞り込む")
    ls.add_argument("--limit", type=int, default=None, help="最大件数（1-20。既定は codex 側の 20）")
    ls.add_argument("--cursor", default=None, help="前回応答のページングカーソル")
    ls.add_argument("--json", action="store_true", help="JSON で出力する")
    add_common(ls, DEFAULT_TIMEOUT_OTHER)

    # -- status ----------------------------------------------------------
    st = sub.add_parser("status", help="タスクの状態（codex cloud status）")
    st.add_argument("task_id", help="タスク ID")
    add_common(st, DEFAULT_TIMEOUT_OTHER)

    # -- diff ------------------------------------------------------------
    d = sub.add_parser("diff", help="タスクの unified diff（codex cloud diff）")
    d.add_argument("task_id", help="タスク ID")
    d.add_argument("--attempt", type=int, default=None, help="表示する attempt 番号（1 始まり）")
    add_common(d, DEFAULT_TIMEOUT_OTHER)

    # -- apply -----------------------------------------------------------
    a = sub.add_parser("apply", help="タスクの diff をローカルに適用（codex cloud apply）")
    a.add_argument("task_id", help="タスク ID")
    a.add_argument("--attempt", type=int, default=None, help="適用する attempt 番号（1 始まり）")
    a.add_argument("--yes", action="store_true",
                   help="実際に適用する（未指定ならコマンドを表示するだけで何もしない）")
    add_common(a, DEFAULT_TIMEOUT_OTHER)

    return p


# ---------------------------------------------------------------------------
# プロンプト
# ---------------------------------------------------------------------------

def decode_prompt(raw: bytes, source: str) -> str:
    """UTF-8 として読む。不正バイトは置換し、置換したことを stderr に残す。"""
    text = raw.decode("utf-8", errors="replace")
    if "�" in text:
        lib.eprint(f"警告: プロンプト（{source}）に不正な UTF-8 があったため置換して読み込んだ")
    return text


def extract_github_repo_slug(remote_url: str) -> str | None:
    """GitHub origin URL から比較用の ``owner/repo`` を取り出す。"""
    prefixes = ("git@github.com:", "https://github.com/", "ssh://git@github.com/")
    prefix = next((item for item in prefixes if remote_url.startswith(item)), None)
    if prefix is None:
        return None
    parts = remote_url[len(prefix):].rstrip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        return None
    return f"{owner}/{repo}".casefold()


def repository_slug(directory: Path, git_runner=subprocess.run) -> str | None:
    """directory の origin remote から GitHub リポジトリ slug を得る。失敗は未設定扱い。"""
    try:
        result = git_runner(
            ["git", "-C", str(directory), "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return extract_github_repo_slug(result.stdout.strip())


def _configured_env_id(value) -> str | None:
    """environments の手書き形式（文字列 / {"env_id": 文字列}）を吸収する。"""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        value = value.get("env_id")
        if isinstance(value, str) and value:
            return value
    return None


def resolve_env_id(args, *, config_path: Path | None = None, environ=None,
                   git_runner=subprocess.run) -> str | None:
    """submit の --env 省略時の既定値を解決する。

    優先順: 明示の --env → 環境変数 CODEX_BRIDGE_CLOUD_ENV → cloud.json の
    environments[実行ディレクトリの GitHub slug] → cloud.json の "env_id"。
    var/cloud.json は gitignore 済みのマシンローカル設定（アカウント紐付きの ID を
    リポジトリに残さないため）。config_path / environ / git_runner はテスト用の注入点。
    """
    if args.env:
        return args.env
    if environ is None:
        environ = os.environ
    from_env = environ.get("CODEX_BRIDGE_CLOUD_ENV")
    if from_env:
        return from_env
    cfg = config_path if config_path is not None else lib.var_dir() / "cloud.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    directory = Path(getattr(args, "cd", None) or Path.cwd())
    slug = repository_slug(directory, git_runner)
    environments = data.get("environments")
    if slug and isinstance(environments, dict):
        for configured_slug, value in environments.items():
            if isinstance(configured_slug, str) and configured_slug.casefold() == slug:
                resolved = _configured_env_id(value)
                if resolved:
                    return resolved
                break
    return _configured_env_id(data.get("env_id"))


def validate_submit_directory(args) -> bool:
    """submit の実行ディレクトリを検証し、子プロセスに渡せる形に正規化する。"""
    directory = Path(args.cd).expanduser() if args.cd else Path.cwd()
    if not directory.is_dir():
        lib.eprint(f"エラー: --cd は存在するディレクトリを指定してください: {directory}")
        return False
    args.cd = str(directory)
    return True


def load_prompt(args) -> tuple[str | None, int]:
    """submit のプロンプトを取り出す。戻り値は (prompt|None, exit_code)。

    `--prompt` と `--prompt-file` は排他。argparse の相互排他グループではなく自前で
    判定するのは、「両方」と「どちらも無し」を別々の日本語メッセージで返すため。
    """
    if args.prompt is not None and args.prompt_file is not None:
        lib.eprint("エラー: --prompt と --prompt-file は同時に指定できません")
        return None, 1
    if args.prompt is not None:
        return args.prompt, 0
    if args.prompt_file is not None:
        try:
            raw = Path(args.prompt_file).expanduser().read_bytes()
        except (OSError, ValueError) as e:
            lib.eprint(f"エラー: --prompt-file を読めません: {e}")
            return None, 1
        return decode_prompt(raw, f"--prompt-file（{args.prompt_file}）"), 0
    lib.eprint("エラー: --prompt / --prompt-file のいずれかでプロンプトを渡してください")
    return None, 1


# ---------------------------------------------------------------------------
# codex バイナリ解決
# ---------------------------------------------------------------------------

def resolve_bin(args) -> str | None:
    """codex 実バイナリを解決する。除外した cmux シムは警告として残す。"""
    resolved, skipped = lib.resolve_codex_bin(args.codex_bin)
    for s in skipped:
        lib.eprint(f"警告: cmux シムを除外した: {s}")
    return resolved


def require_bin(args) -> tuple[str | None, int]:
    """実行に使うバイナリ。見つからなければ exit 4 を返す。"""
    resolved = resolve_bin(args)
    if not resolved:
        lib.eprint("エラー: codex 実バイナリが見つかりません（--codex-bin / CODEX_BIN / PATH を確認）")
        return None, 4
    return resolved, 0


# ---------------------------------------------------------------------------
# コマンド組み立て
# ---------------------------------------------------------------------------

def build_argv(binary: str, args, prompt: str | None = None) -> list[str]:
    """実 CLI（`codex cloud …`）のフラグをそのままミラーした argv を作る。

    フラグ名・位置引数は `codex cloud <sub> --help` の出力に合わせてある。値が None の
    任意フラグは付けない（codex 側の既定に委ねる）。
    """
    argv = [binary, "cloud", args.cmd]
    if args.cmd == "submit":
        # 実 CLI 上のサブコマンド名は `exec`。ラッパー側だけ submit と呼んでいる。
        argv = [binary, "cloud", "exec", "--env", args.env, "--attempts", str(args.attempts)]
        if args.branch:
            argv += ["--branch", args.branch]
        if prompt is not None:
            argv.append(prompt)          # QUERY は位置引数
    elif args.cmd == "list":
        if args.env:
            argv += ["--env", args.env]
        if args.limit is not None:
            argv += ["--limit", str(args.limit)]
        if args.cursor:
            argv += ["--cursor", args.cursor]
        if args.json:
            argv.append("--json")
    elif args.cmd == "status":
        argv.append(args.task_id)
    elif args.cmd in ("diff", "apply"):
        argv.append(args.task_id)
        if args.attempt is not None:
            argv += ["--attempt", str(args.attempt)]
    return argv


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def _terminate(proc: subprocess.Popen) -> None:
    """子プロセスグループごと止める（codex は子孫を生やすため killpg で落とす）。"""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    deadline = time.monotonic() + GRACE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_child(argv: list[str], timeout: float, cwd: str | None = None) -> tuple[int | None, list[str]]:
    """子を起動し、stdout はそのまま継承、stderr は素通しさせつつ末尾だけ控える。

    戻り値は (returncode|None, stderr 末尾行)。returncode が None ならタイムアウト。
    stderr を PIPE にするのは末尾を報告に添えるためだけで、読んだ内容は加工せず
    そのまま `sys.stderr` に書き戻す（行の欠落・順序変更をしない）。
    """
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=None,                 # 親の stdout をそのまま継承する
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,      # killpg で子孫ごと止められるようにする
        )
    except (OSError, ValueError) as e:
        lib.eprint(f"エラー: codex を起動できません: {e}")
        return 127, []

    def pump() -> None:
        # sys.stderr が差し替えられている（テストの StringIO 等）と buffer を持たないため、
        # 無ければテキストとして書き戻す。いずれの経路でも内容は加工しない。
        raw_out = getattr(sys.stderr, "buffer", None)
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", errors="replace")
                if raw_out is not None:
                    raw_out.write(line)
                    raw_out.flush()
                else:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                tail.append(text.rstrip("\n"))
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        t.join(JOIN_SEC)
        return None, list(tail)
    t.join(JOIN_SEC)
    return rc, list(tail)


def execute(args, argv: list[str]) -> int:
    """子を実行し、終了コードを本スクリプトの規約へ正規化する。"""
    cwd = args.cd if args.cmd == "submit" else None
    rc, tail = run_child(argv, args.timeout_sec, cwd=cwd)
    if rc is None:
        lib.eprint(f"エラー: タイムアウト（{args.timeout_sec:g}s）: {shlex.join(argv)}")
        return 3
    if rc != 0:
        lib.eprint(f"エラー: codex が非ゼロ終了しました（exit {rc}）: {shlex.join(argv)}")
        if tail:
            lib.eprint("--- stderr 末尾 ---")
            for line in tail:
                lib.eprint(line)
            # 環境未設定・GitHub 未連携が最頻の失敗要因。判別は雑でよいので常に添える。
            lib.eprint(SETUP_HINT)
        return 2
    return 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main(argv_in=None) -> int:
    args = build_parser().parse_args(argv_in)

    # apply のドライランはローカルを一切触らないため、バイナリ解決より先に返す
    # （偽の --codex-bin を渡されても「何も起きない」ことを保証する）。
    if args.cmd == "apply" and not args.yes:
        display_bin = resolve_bin(args) or args.codex_bin or "codex"
        print("ドライラン: --yes が無いため実行しません。実行するはずだったコマンド:")
        print(f"  {shlex.join(build_argv(display_bin, args))}")
        print("実際に適用するには同じコマンドに --yes を付けて再実行してください。")
        return 0

    prompt = None
    if args.cmd == "submit":
        if not validate_submit_directory(args):
            return 1
        args.env = resolve_env_id(args)
        if not args.env:
            lib.eprint(
                "エラー: 環境 ID がありません（--env / $CODEX_BRIDGE_CLOUD_ENV / "
                "cloud.json の environments / env_id）"
            )
            return 1
        prompt, code = load_prompt(args)
        if code:
            return code

    binary, code = require_bin(args)
    if code:
        return code

    return execute(args, build_argv(binary, args, prompt))


if __name__ == "__main__":
    sys.exit(main())
