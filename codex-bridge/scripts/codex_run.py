#!/usr/bin/env python3
"""Codex CLI（`codex exec`）を非対話・構造化・タイムアウト付きで呼ぶ実行ドライバ。

Claude Code から実装委譲 / レビューを Codex に投げるための配管。stdout の JSONL を
逐次 `events.jsonl` に落としながら監視し、壁時計・アイドルの両タイムアウトでプロセス
グループごと停止し、結果を `job.json`（アトミック書込）にまとめる。

成否は **終了コードではなく JSONL イベント**で判定する（Codex はテスト失敗でも turn が
正常完了すれば exit 0 を返すため）。`turn.completed` 到達を最優先し、非致命の `error` item
（exec_events.rs で "non-fatal" と定義）は `warnings` に回す。

上位から SIGTERM / SIGHUP を受けた場合も、子プロセスグループを止めて `job.json`
（`status=killed`）を書き、並列スロットを解放してから終了する。

実行例:
  python3 codex_run.py --mode task --job-dir /tmp/job1 --cd /path/to/repo \\
      --model gpt-5.6-terra --effort high --write --prompt-file prompt.md
  # 実機不要の配管確認（モック）
  python3 codex_run.py --mode task --job-dir /tmp/job1 --cd /tmp/work --mock ok --prompt "test"

終了コード: 0=completed / 2=failed / 3=timeout・idle_timeout / 4=codex 不在・認証エラー /
1=その他（error・killed・引数エラー）。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_lib as lib  # noqa: E402

MOCK_SCENARIOS = ("ok", "failed", "hang", "slow", "exit0_no_turn", "schema", "garbage", "envdump",
                  "escape", "manycmds", "manyfails", "startup_error", "partial_change",
                  "toplevel_error", "error_then_complete")
MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

MAX_COMMANDS = 50            # 成功コマンドの記録上限
MAX_FAILED_COMMANDS = 50     # 失敗コマンドの記録上限（H-1: 失敗側にも別枠の上限を置く）
MAX_WARNINGS = 50
GRACE_SEC = 5.0          # SIGTERM から SIGKILL までの猶予
JOIN_TOTAL_SEC = 3.0     # 読取スレッド join に使う**合計**予算（H3: 子孫が残ると EOF が来ない）
LOCK_POLL_SEC = 0.5      # 並列スロット待ちのポーリング間隔（L-7。deadline は --timeout-sec のまま）


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex_run.py",
        description="codex exec を非対話で実行し、job.json にまとめる",
    )
    p.add_argument("--mode", choices=("task", "review", "imagegen"), required=True, help="実行モード")
    p.add_argument("--job-dir", required=True, help="成果物（events.jsonl / last.md / job.json）の出力先")
    p.add_argument("--cd", default=None, help="Codex の作業ディレクトリ（既定: カレント）")
    p.add_argument("--model", default="gpt-5.6-terra", choices=MODELS, help="モデル（既定 gpt-5.6-terra）")
    p.add_argument("--effort", default="high", choices=EFFORTS, help="model_reasoning_effort（既定 high）")
    p.add_argument("--write", action="store_true", help="書込を許可（-s workspace-write。既定は read-only）")
    p.add_argument("--prompt-file", default=None, help="プロンプトのファイル")
    p.add_argument("--prompt", default=None, help="プロンプト文字列")
    p.add_argument("--schema", default=None, help="--output-schema に渡す JSON Schema のパス")
    p.add_argument("--image", action="append", default=[], help="入力画像（最大4枚）")
    p.add_argument("--out", default=None, help="imagegen の出力 PNG（絶対パス）")
    p.add_argument("--web-search", action="store_true", help="Web 検索を live モードで許可")
    p.add_argument("--timeout-sec", type=float, default=None,
                   help="壁時計タイムアウト秒（既定: imagegen 600、それ以外 3600）")
    p.add_argument("--idle-timeout-sec", type=float, default=600.0, help="無イベント許容秒（既定 600）")
    p.add_argument("--resume-last", action="store_true", help="直前スレッドを再開（exec resume --last）")
    p.add_argument("--resume", default=None, help="指定 thread_id を再開（exec resume <ID>）")
    p.add_argument("--review-scope", default=None,
                   help="Codex ネイティブレビュー: uncommitted | base:<ref> | commit:<sha>")
    p.add_argument("--max-parallel", type=int, default=1, help="同時実行スロット数（既定 1）")
    p.add_argument("--allow-api-key", action="store_true",
                   help="OPENAI_API_KEY / CODEX_API_KEY を子環境に残す（従量課金に切り替わる）")
    p.add_argument("--codex-bin", default=None, help="codex 実バイナリのパス")
    p.add_argument("--mock", default=None, choices=MOCK_SCENARIOS,
                   help="tests/mock_codex.py をバイナリの代わりに起動する（配管テスト用）")
    return p


# ---------------------------------------------------------------------------
# 並列スロット
# ---------------------------------------------------------------------------

class Slot:
    """`var/locks/slot-N.lock` を `flock(LOCK_EX|LOCK_NB)` で押さえる並列制御。

    ロックファイルは**削除しない**。「ファイルの存在＋中の PID が生きているか」で判定して
    いた旧方式は、(a) PID 読取→unlink→再作成の間に別プロセスが割り込む TOCTOU と、
    (b) PID 再利用で永久に取得できなくなる問題があった。flock はプロセス終了（kill / クラッシュ）
    でカーネルが自動解放するため、どちらも起こらない。
    """

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def try_acquire_slot(max_parallel: int):
    d = lib.locks_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(max(1, max_parallel)):
        p = d / f"slot-{i}.lock"
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                continue        # 他プロセスが保持中
            continue
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass                # 中身は診断用。書けなくてもロックは有効
        return Slot(p, fd)
    return None


def acquire_slot(max_parallel: int, deadline: float):
    while True:
        slot = try_acquire_slot(max_parallel)
        if slot is not None:
            return slot
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(LOCK_POLL_SEC, remaining))


# ---------------------------------------------------------------------------
# イベント収集
# ---------------------------------------------------------------------------

class Collector:
    """JSONL イベントから job.json に必要な情報を積み上げる。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread_id = None
        self.usage = None
        self.turn_completed = False
        self.turn_failed = False      # top-level turn.failed のみ
        self.top_errors = False       # top-level error イベント（致命の可能性がある）
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.touched: list[dict] = []
        self._touched_seen: set = set()
        self.commands: list[dict] = []
        self._commands_seen: set = set()
        self.commands_dropped = 0
        self.success_commands = 0
        self.failed_commands = 0
        self.failed_dropped = 0
        self._item_errors_seen: set = set()
        self.agent_messages: list[str] = []
        self.event_count = 0
        self.last_event_mono = time.monotonic()

    # -- 記録ヘルパ --------------------------------------------------------
    def warn(self, msg: str) -> None:
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(msg)
        elif len(self.warnings) == MAX_WARNINGS:
            self.warnings.append(f"警告が {MAX_WARNINGS} 件を超えたため以降を省略した")

    def touch_activity(self) -> None:
        self.last_event_mono = time.monotonic()

    # -- イベント処理 ------------------------------------------------------
    def handle(self, ev: dict) -> None:
        et = ev.get("type")
        if et == "thread.started":
            self.thread_id = ev.get("thread_id") or self.thread_id
        elif et == "turn.completed":
            self.turn_completed = True
            u = lib.normalize_usage(ev.get("usage"))
            if u is not None:
                self.usage = u
        elif et == "turn.failed":
            self.turn_failed = True
            msg = ((ev.get("error") or {}).get("message")
                   if isinstance(ev.get("error"), dict) else None)
            self.errors.append(msg or "turn.failed（詳細不明）")
        elif et == "error":
            # top-level error。turn.completed に到達すれば completed を優先する
            self.top_errors = True
            self.errors.append(ev.get("message") or "error イベント（詳細不明）")
        elif et in ("item.started", "item.updated", "item.completed"):
            self._handle_item(ev.get("item") or {}, et)

    def _handle_item(self, item: dict, et: str) -> None:
        it = item.get("type")
        if it == "file_change":
            # L12: 実際に適用されたのは status=completed のものだけ
            if item.get("status") != "completed":
                return
            for ch in item.get("changes") or []:
                if not isinstance(ch, dict):
                    continue
                key = (ch.get("path"), ch.get("kind"))
                if key in self._touched_seen:
                    continue
                self._touched_seen.add(key)
                self.touched.append({"path": ch.get("path"), "kind": ch.get("kind")})
        elif it == "command_execution":
            if et != "item.completed":
                return
            key = item.get("id") or (item.get("command"), item.get("exit_code"))
            if key in self._commands_seen:
                return
            self._commands_seen.add(key)
            rec = {
                "command": item.get("command"),
                "exit_code": item.get("exit_code"),
                "status": item.get("status"),
            }
            # M7: 失敗コマンドは診断価値が高いので成功側の上限では落とさない。
            # H-1: ただし失敗にも別枠の上限を置く（失敗が数千件でも job.json を肥大化させない）。
            is_failure = rec["status"] == "failed" or rec["exit_code"] not in (0, None)
            if is_failure:
                if self.failed_commands < MAX_FAILED_COMMANDS:
                    self.failed_commands += 1
                    self.commands.append(rec)
                else:
                    self.failed_dropped += 1
            elif self.success_commands < MAX_COMMANDS:
                self.success_commands += 1
                self.commands.append(rec)
            else:
                self.commands_dropped += 1
        elif it == "agent_message":
            if et == "item.completed" and item.get("text"):
                self.agent_messages.append(str(item.get("text")))
        elif it == "error":
            # H2: item 種別の error は非致命（exec_events.rs の Item::Error は
            # "non-fatal error surfaced as an item"）。失敗扱いにせず warnings へ。
            key = item.get("id") or item.get("message")
            if key not in self._item_errors_seen:
                self._item_errors_seen.add(key)
                self.warn(f"非致命の error item: {item.get('message') or '（詳細不明）'}")


# ---------------------------------------------------------------------------
# 子プロセスの起動・監視
# ---------------------------------------------------------------------------

def kill_group(proc: subprocess.Popen) -> None:
    """プロセスグループごと SIGTERM → 5 秒後 SIGKILL。"""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    deadline = time.monotonic() + GRACE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def close_pipes(proc: subprocess.Popen, threads_stuck: bool) -> None:
    """子プロセスのパイプを閉じる。

    読取スレッドが `read()` で止まったままだと `BufferedReader.close()` は内部ロック待ちで
    **永久にブロックする**。その場合は生の fd を閉じるだけにする。
    """
    for s in (proc.stdout, proc.stderr):
        if s is None:
            continue
        if threads_stuck:
            try:
                os.close(s.fileno())
            except (OSError, ValueError):
                pass
        else:
            try:
                s.close()
            except OSError:
                pass


def stream_stdout(proc: subprocess.Popen, events_path: Path, col: Collector) -> None:
    """stdout の JSONL を events.jsonl に逐次追記しつつ解析する。"""
    with open(events_path, "ab", buffering=0) as out:
        for raw in proc.stdout:
            out.write(raw if raw.endswith(b"\n") else raw + b"\n")
            with col.lock:
                col.touch_activity()
                col.event_count += 1
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if "�" in text:
                    col.warn("不正な UTF-8 を含むイベント行を置換して読み込んだ")
                try:
                    ev = json.loads(text)
                except json.JSONDecodeError:
                    col.warn(f"JSON として解釈できない行を無視した: {text[:120]}")
                    continue
                if not isinstance(ev, dict):
                    col.warn(f"dict でないイベント行を無視した: {text[:120]}")
                    continue
                try:
                    col.handle(ev)
                except Exception as e:  # 未知イベント形状で落ちない
                    col.warn(f"イベント解析に失敗した ({type(e).__name__}): {text[:120]}")


def stream_stderr(proc: subprocess.Popen, path: Path) -> None:
    with open(path, "ab", buffering=0) as out:
        for raw in proc.stderr:
            out.write(raw)


def feed_stdin(proc: subprocess.Popen, prompt: str) -> None:
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass


# ---------------------------------------------------------------------------
# argv 組み立て
# ---------------------------------------------------------------------------

def is_git_worktree(cd: str) -> bool:
    """`cd` が git 管理下か（`.git` を上へ辿るだけ。git バイナリは呼ばない）。"""
    try:
        p = Path(cd).resolve()
    except OSError:
        return False
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return True
    return False


def build_codex_argv(args, binary: list[str], cd: str, last_md: Path,
                     warnings: list | None = None) -> list[str]:
    """`codex exec` の引数列を作る。

    `-s` `-C` `-p` はサブコマンド（resume / review）より前に置く必要がある。
    `--full-auto` は 0.149.0 で削除済みのため渡さない。

    **`review` サブコマンドには PROMPT（`-`）を渡さない**: `ReviewArgs` の
    `--uncommitted` / `--base` / `--commit` は `conflicts_with_all = [..., "prompt"]` なので、
    `review --uncommitted -` は clap の ArgumentConflict で必ず失敗する（一次情報で確認済み）。
    """
    warns = warnings if warnings is not None else []
    argv = list(binary) + ["exec", "--json"]
    if not is_git_worktree(cd):
        # L15: git 管理外だと codex が起動時に弾くため明示的に外す
        argv += ["--skip-git-repo-check"]
        warns.append(f"--cd が git 管理外のため --skip-git-repo-check を付けた: {cd}")
    argv += ["-C", cd]
    argv += ["-m", args.model]
    argv += ["-c", f"model_reasoning_effort={args.effort}"]
    argv += ["-s", "workspace-write" if args.write or args.mode == "imagegen" else "read-only"]
    if args.web_search:
        if args.review_scope:
            warns.append("--review-scope 指定のため --web-search は付けなかった")
        else:
            # 値の引用符も引数に含め、TOML の文字列として解釈させる。
            argv += ["-c", 'web_search="live"']
    if args.schema:
        if args.review_scope:
            # L14: exec review は --output-schema を無視する。付けずに警告する
            warns.append("--review-scope 指定のため --schema（--output-schema）を外した"
                         "（exec review は output-schema を無視する）")
        else:
            argv += ["--output-schema", str(Path(args.schema).expanduser().resolve())]
    argv += ["-o", str(last_md)]

    if args.review_scope:
        argv += ["review"]
        scope = args.review_scope
        if scope == "uncommitted":
            argv += ["--uncommitted"]
        elif scope.startswith("base:"):
            # L-4: `--base <ref>` だと `-` 始まりの ref を clap がフラグと誤認する（= 形なら値として通る）
            argv += [f"--base={scope[len('base:'):]}"]
        elif scope.startswith("commit:"):
            argv += [f"--commit={scope[len('commit:'):]}"]
        warns.append("--review-scope 指定時はプロンプトを渡せない（clap の conflicts_with_all）ため無視した")
        return argv

    if args.resume_last:
        argv += ["resume", "--last"]
    elif args.resume:
        argv += ["resume", args.resume]
    # `--image <FILE>...` は可変長引数なので、次の `-`（stdin 指示）を画像として
    # 吸い込ませないよう、画像ごとに必ず `=` 結合形を用いる。
    for image in getattr(args, "actual_images", args.image):
        argv += [f"--image={image}"]
    argv += ["-"]  # プロンプトは stdin（非 TTY で空出力になる既知問題 #19945 の回避）
    return argv


def decode_prompt(raw: bytes, source: str, warnings: list[str]) -> str:
    """プロンプトを UTF-8 として読む。不正バイトは置換し、置換したことを警告に残す（M-4）。"""
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        warnings.append(f"プロンプト（{source}）に不正な UTF-8 があったため置換して読み込んだ")
    return text


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_magic(path: Path) -> str | None:
    """PNG / JPEG のマジックバイトを返す。出力画像の完了判定にも使う。"""
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if head.startswith(PNG_SIGNATURE):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """PNG IHDR / JPEG SOF0・SOF2 から幅高さを読む（外部依存なし）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data.startswith(PNG_SIGNATURE) and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            break
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2 or i + length > len(data):
            break
        if marker in (0xC0, 0xC2) and length >= 7:
            return (int.from_bytes(data[i + 5:i + 7], "big"),
                    int.from_bytes(data[i + 3:i + 5], "big"))
        i += length
    return None


def sips_dimensions(path: Path) -> tuple[int, int] | None:
    """sips が扱える画像の寸法を取得する。失敗時は None。"""
    try:
        result = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values = dict(re.findall(r"pixel(Width|Height):\s*(\d+)", result.stdout))
    try:
        return int(values["Width"]), int(values["Height"])
    except (KeyError, ValueError):
        return None


def prepare_images(args, job_dir: Path, warnings: list[str]) -> list[str] | None:
    """入力画像を検証し、darwin では必要なら job-dir に縮小コピーを作る。"""
    supplied = args.image
    if len(supplied) > 4:
        lib.eprint("エラー: --image は最大4枚まで指定できます")
        return None
    resolved: list[Path] = []
    for raw in supplied:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            lib.eprint(f"エラー: --image のファイルがありません: {path}")
            return None
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            lib.eprint(f"エラー: --image は png/jpg/jpeg/gif/webp のみ指定できます: {path}")
            return None
        resolved.append(path)

    if not resolved:
        return []
    can_sips = sys.platform == "darwin" and os.path.isfile("/usr/bin/sips") \
        and os.access("/usr/bin/sips", os.X_OK)
    actual: list[str] = []
    for index, path in enumerate(resolved, start=1):
        if not can_sips:
            reason = "darwin 以外" if sys.platform != "darwin" else "/usr/bin/sips が使えない"
            warnings.append(f"{reason} のため画像を原本のまま渡す: {path}")
            actual.append(str(path))
            continue
        dimensions = sips_dimensions(path)
        if dimensions is None:
            warnings.append(f"sips で画像サイズを取得できないため原本のまま渡す: {path}")
            actual.append(str(path))
            continue
        if max(dimensions) <= 2048:
            actual.append(str(path))
            continue
        destination = job_dir / "images" / f"{index}-{path.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["/usr/bin/sips", "--resampleHeightWidthMax", "2048", str(path), "--out", str(destination)],
                capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            warnings.append(f"sips で画像を縮小できないため原本のまま渡す: {path} ({e})")
            actual.append(str(path))
            continue
        if result.returncode != 0 or not destination.is_file():
            detail = result.stderr.strip() if result.returncode != 0 else "出力ファイルがない"
            warnings.append(f"sips で画像を縮小できないため原本のまま渡す: {path} ({detail})")
            actual.append(str(path))
            continue
        warnings.append(f"長辺が 2048px を超えるため画像を縮小して渡す: {path} → {destination}")
        actual.append(str(destination.resolve()))
    return actual


def imagegen_prompt(prompt: str, out: Path) -> str:
    return (f"$imagegen {prompt}。生成した画像を {out} に PNG で保存して。"
            "組み込みの image_gen ツールを使い、OPENAI_API_KEY を要する CLI フォールバックは使わないこと。")


def output_image_payload(out: Path, warnings: list[str]) -> dict:
    """imagegen 出力の job.json 用メタデータを作る。読取不能でも path は残す。"""
    payload = {"path": str(out), "bytes": None, "width": None, "height": None}
    try:
        payload["bytes"] = out.stat().st_size
    except OSError:
        return payload
    dimensions = image_dimensions(out)
    if dimensions is None:
        warnings.append(f"生成画像の幅・高さを PNG IHDR / JPEG SOF0・SOF2 から取得できなかった: {out}")
    else:
        payload["width"], payload["height"] = dimensions
    return payload


def recover_generated_image(out: Path, started_epoch: float, warnings: list[str]) -> bool:
    """組み込み image_gen の既定保存先から、今回以降の最新画像を回収する。"""
    root = lib.codex_home() / "generated_images"
    candidates: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"} \
                    and path.stat().st_mtime >= started_epoch and image_magic(path) is not None:
                candidates.append(path)
    except OSError as e:
        warnings.append(f"generated_images を走査できなかった: {e}")
        return False
    if not candidates:
        return False
    source = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out)
    except OSError as e:
        warnings.append(f"generated_images から画像を回収できなかった: {e}")
        return False
    warnings.append(f"--out に画像が無かったため generated_images から回収した: {source} → {out}")
    return True


def child_env(args, warnings: list[str]) -> dict:
    env = dict(os.environ)
    if args.allow_api_key:
        if env.get("OPENAI_API_KEY") or env.get("CODEX_API_KEY"):
            warnings.append("--allow-api-key 指定のため API キーを残した（ChatGPT プランではなく従量課金になりうる）")
    else:
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            if k in env:
                del env[k]
                warnings.append(f"子プロセス環境から {k} を削除した（無言の従量課金切替 #20099 の回避）")
    return env


def config_warnings() -> list[str]:
    """`~/.codex/config.toml` の `forced_login_method` を **TOML として**確認する。

    部分一致（`"forced_login_method" in text`）だと、コメント行・文字列内の言及・
    `"apikey"` 指定を「設定済み」と誤判定する。設定は変更しない（警告のみ）。
    """
    cfg = lib.codex_home() / "config.toml"
    try:
        raw = cfg.read_bytes()
    except OSError:
        return [f"{cfg} が読めなかった。forced_login_method = \"chatgpt\" の設定を推奨（変更はしていない）"]
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return [f"{cfg} を TOML として読めなかった（{e}）。"
                f"forced_login_method = \"chatgpt\" の確認を推奨（変更はしていない）"]
    value = data.get("forced_login_method")
    if value == "chatgpt":
        return []
    if value is None:
        return [f"{cfg} に forced_login_method が無い。\"chatgpt\" の明示を推奨（変更はしていない）"]
    if value in ("api", "apikey"):
        # M-1: 公式に定義されている危険値は "api"（"apikey" は存在しないが、誤記も同じ扱いにする）
        return [f"{cfg} の forced_login_method = {value!r} は API キーによる**従量課金**を強制する設定。"
                f"ChatGPT プランで使うなら \"chatgpt\" にする（変更はしていない）"]
    return [f"{cfg} の forced_login_method = {value!r} は想定外の値。"
            f"\"chatgpt\" を推奨（変更はしていない）"]


# ---------------------------------------------------------------------------
# 結果まとめ
# ---------------------------------------------------------------------------

EXIT_BY_STATUS = {
    "completed": 0,
    "failed": 2,
    "timeout": 3,
    "idle_timeout": 3,
    "not_found": 4,
    "auth_error": 4,
    "killed": 1,
    "error": 1,
}

#: 認証エラーの兆候。部分一致（"login" / "401"）だと `logout` / `4010` / パス名で誤爆するため、
#: 語境界つきのパターンに絞る。refresh_token 系はサーバが返す認証死のコード
#: （reused / expired / invalidated。ローテーション型 refresh token の再利用検出。
#: 2026-08-25 調査）で、`\btoken[_ ]invalidated\b` は `_` が語文字のため
#: `refresh_token_invalidated` にマッチしない。別 alternation として明示する。
AUTH_PATTERN = re.compile(
    r"\b(unauthorized|not logged in|401\s+unauthorized|invalid[_ -]api[_ -]key|token[_ ]invalidated)\b"
    r"|\brefresh[_ ]token[_ ](reused|expired|invalidated)\b"
    r"|please run .?codex login"
    r"|\bcodex login\b",
    re.IGNORECASE,
)

STDERR_TAIL_CHARS = 600


def detect_auth_error(stderr_path: Path) -> bool:
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return AUTH_PATTERN.search(text) is not None


def stderr_tail(stderr_path: Path, limit: int = STDERR_TAIL_CHARS) -> str:
    """起動・引数エラーの原因を result まで運ぶための stderr 末尾。"""
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > limit:
        text = "…" + text[-limit:]
    return text


def write_job(job_dir: Path, payload: dict) -> None:
    lib.atomic_write_json(job_dir / "job.json", payload)


def base_payload(args, cd: str, started, ended, status: str, queued_sec: float = 0.0) -> dict:
    return {
        "status": status,
        "exit_code": None,
        "thread_id": None,
        "model": args.model,
        "effort": args.effort,
        "mode": args.mode,
        "write": bool(args.write),
        "mock": args.mock,           # モック実行かどうか（台帳・集計から除外するため）
        "cwd": cd,
        "started_at": lib.iso(started),
        "ended_at": lib.iso(ended),
        "queued_sec": round(max(0.0, queued_sec), 3),   # スロット待ち（duration には含めない）
        "duration_sec": round((ended - started).total_seconds(), 3),
        "usage": None,
        "credits_est": None,
        "touched_files": [],
        "commands": [],
        "last_message_path": None,
        "structured_output": None,
        "images": list(getattr(args, "actual_images", [])),
        "image": None,
        "errors": [],
        "warnings": [],
    }


def append_ledger(args, cd: str, payload: dict) -> None:
    if not payload.get("usage"):
        return
    if payload.get("mock"):
        # M10: モックの架空 usage を本物の台帳に混ぜない
        return
    try:
        lib.append_jsonl(lib.usage_ledger_path(), {
            "ts": payload["ended_at"],
            "job_dir": payload.get("job_dir") or str(Path(args.job_dir).expanduser().resolve()),
            "mode": args.mode,
            "model": args.model,
            "effort": args.effort,
            "write": bool(args.write),
            "cwd": cd,
            "claude_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            "thread_id": payload.get("thread_id"),
            # M-2: resume 時の usage はスレッド累計の可能性がある。集計側が差分計上できるよう印を残す
            "resumed": bool(args.resume or args.resume_last),
            "resume_of": args.resume or (payload.get("thread_id") if args.resume_last else None),
            "mock": payload.get("mock"),
            "usage": payload["usage"],
            "credits_est": payload.get("credits_est"),
            "status": payload.get("status"),
        })
    except OSError as e:
        payload.setdefault("warnings", []).append(f"使用量台帳への追記に失敗した: {e}")


# ---------------------------------------------------------------------------
# シグナル処理（M8: codex_run 自身が殺されても job.json とロックを残さない）
# ---------------------------------------------------------------------------

def install_signal_handlers(ctx: dict) -> None:
    """SIGTERM / SIGHUP で「子を殺す → job.json(status=killed) → slot 解放 → exit」。

    これが無いと、上位（Claude Code の Bash / シェル）から止められたときに job.json が
    生成されず、ロックが残り、codex 本体が孤児として走り続ける。
    """
    def handler(signum, frame):   # noqa: ARG001
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        proc = ctx.get("proc")
        if proc is not None:
            kill_group(proc)
        col = ctx.get("col")
        args = ctx.get("args")
        job_dir = ctx.get("job_dir")
        if args is None or job_dir is None:
            os._exit(1)
        ended = lib.now_utc()
        p = base_payload(args, ctx.get("cd") or "", ctx.get("started") or ended, ended,
                         "killed", ctx.get("queued_sec") or 0.0)
        p["job_dir"] = str(job_dir)
        p["exit_code"] = proc.poll() if proc is not None else None
        if col is not None:
            p["thread_id"] = col.thread_id
            p["usage"] = col.usage
            p["credits_est"] = lib.credits_est(col.usage, args.model) if col.usage else None
            p["touched_files"] = col.touched
            p["commands"] = col.commands
            p["errors"] = list(col.errors)
            p["warnings"] = list(ctx.get("warnings") or []) + list(col.warnings)
        else:
            p["warnings"] = list(ctx.get("warnings") or [])
        p["errors"] = (p.get("errors") or []) + [
            f"{name} を受けたため停止した（子プロセスグループも終了させた）"]
        try:
            write_job(Path(job_dir), p)
        except OSError:
            pass
        slot = ctx.get("slot")
        if slot is not None:
            slot.release()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        os._exit(EXIT_BY_STATUS["killed"])

    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, handler)
        except (ValueError, OSError, AttributeError):
            pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # argparse の既定を None にし、既存モードの 3600 秒は維持しつつ imagegen だけ短くする。
    if args.timeout_sec is None:
        args.timeout_sec = 600.0 if args.mode == "imagegen" else 3600.0

    if args.resume_last and args.resume:
        lib.eprint("エラー: --resume-last と --resume は同時に指定できません")
        return 1
    if args.review_scope and (args.resume_last or args.resume):
        lib.eprint("エラー: --review-scope と --resume 系は同時に指定できません")
        return 1
    if args.review_scope and args.review_scope != "uncommitted" \
            and not (args.review_scope.startswith("base:") or args.review_scope.startswith("commit:")):
        lib.eprint("エラー: --review-scope は uncommitted | base:<ref> | commit:<sha> のいずれか")
        return 1
    if args.image and args.review_scope:
        lib.eprint("エラー: --image と --review-scope は同時に指定できません")
        return 1
    if args.mode == "imagegen":
        if not args.out:
            lib.eprint("エラー: --mode imagegen では --out が必須です")
            return 1
        supplied_out = Path(args.out)
        raw_out = supplied_out.expanduser()
        if not supplied_out.is_absolute() or raw_out.suffix != ".png":
            lib.eprint("エラー: imagegen の --out は絶対パスの .png ファイルで指定してください")
            return 1
        if args.resume_last or args.resume or args.review_scope or args.schema:
            lib.eprint("エラー: --mode imagegen は resume 系 / --review-scope / --schema と併用できません")
            return 1
        args.out_path = raw_out.resolve()
    elif args.out:
        lib.eprint("エラー: --out は --mode imagegen でのみ指定できます")
        return 1

    if args.schema:
        # L-5: 存在しない / ディレクトリ / JSON でない schema は実行前に弾く
        sp = Path(args.schema).expanduser()
        if not sp.exists():
            lib.eprint(f"エラー: --schema のファイルがありません: {sp}")
            return 1
        if not sp.is_file():
            lib.eprint(f"エラー: --schema がファイルではありません: {sp}")
            return 1
        try:
            json.loads(sp.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            lib.eprint(f"エラー: --schema を JSON として読めません（{sp}）: {e}")
            return 1

    job_dir = Path(args.job_dir).expanduser().resolve()
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        lib.eprint(f"エラー: job-dir を作成できません: {e}")
        return 1

    cd = str(Path(args.cd).expanduser().resolve()) if args.cd else \
        (str(args.out_path.parent) if args.mode == "imagegen" else os.getcwd())
    if not os.path.isdir(cd):
        lib.eprint(f"エラー: --cd が存在しません: {cd}")
        return 1
    if args.mode == "imagegen":
        try:
            args.out_path.relative_to(Path(cd).resolve())
        except ValueError:
            lib.eprint("エラー: imagegen の --out は --cd 配下に置く必要があります")
            return 1

    # プロンプト取得（引数 > ファイル > stdin）
    # M-4: 不正な UTF-8 でも traceback にせず、置換して読んだことを warnings に残す
    prompt_warnings: list[str] = []
    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file:
        try:
            raw = Path(args.prompt_file).expanduser().read_bytes()
        except (OSError, ValueError) as e:
            lib.eprint(f"エラー: --prompt-file を読めません: {e}")
            return 1
        prompt = decode_prompt(raw, f"--prompt-file（{args.prompt_file}）", prompt_warnings)
    elif args.review_scope:
        # L-3: review-scope はプロンプトを使わない（clap の conflicts_with_all）ので未指定を許す
        prompt = ""
    elif not sys.stdin.isatty():
        try:
            raw = sys.stdin.buffer.read()
        except (OSError, ValueError, AttributeError) as e:
            lib.eprint(f"エラー: 標準入力からプロンプトを読めません: {e}")
            return 1
        prompt = decode_prompt(raw, "標準入力", prompt_warnings)
    else:
        lib.eprint("エラー: --prompt / --prompt-file / stdin のいずれかでプロンプトを渡してください")
        return 1

    warnings = config_warnings() + prompt_warnings

    job_dir = Path(args.job_dir).expanduser().resolve()
    prepared_images = prepare_images(args, job_dir, warnings)
    if prepared_images is None:
        return 1
    args.actual_images = prepared_images
    if args.mode == "imagegen":
        prompt = imagegen_prompt(prompt, args.out_path)

    started = lib.now_utc()

    # M8: 上位から止められても job.json / ロック / 子プロセスの後始末をする
    ctx = {"args": args, "cd": cd, "job_dir": job_dir, "warnings": warnings,
           "started": started, "proc": None, "slot": None, "col": None, "queued_sec": 0.0}
    install_signal_handlers(ctx)

    # --- バイナリ解決（モック時はここだけが差し替わる） ---
    if args.mock:
        mock_path = lib.code_root() / "tests" / "mock_codex.py"
        if not mock_path.exists():
            ended = lib.now_utc()
            p = base_payload(args, cd, started, ended, "not_found")
            p["errors"] = [f"モックが見つかりません: {mock_path}"]
            p["warnings"] = warnings
            write_job(job_dir, p)
            lib.eprint(p["errors"][0])
            return 4
        binary = [sys.executable, str(mock_path), args.mock]
    else:
        resolved, skipped = lib.resolve_codex_bin(args.codex_bin)
        for s in skipped:
            warnings.append(f"cmux シムを除外した: {s}")
        if not resolved:
            ended = lib.now_utc()
            p = base_payload(args, cd, started, ended, "not_found")
            p["errors"] = ["codex 実バイナリが見つかりません（--codex-bin / CODEX_BIN / PATH を確認）"]
            p["warnings"] = warnings
            write_job(job_dir, p)
            lib.eprint(p["errors"][0])
            return 4
        binary = [resolved]

    last_md = job_dir / "last.md"
    events_path = job_dir / "events.jsonl"
    stderr_path = job_dir / "stderr.log"
    # M6: 同じ job-dir の再利用で前回の結果を今回のものとして報告しないよう、全部消してから始める
    for f in (events_path, stderr_path):
        f.write_bytes(b"")
    for f in (last_md, job_dir / "job.json"):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            warnings.append(f"前回の {f.name} を削除できなかった: {e}")

    cmd = build_codex_argv(args, binary, cd, last_md, warnings)
    env = child_env(args, warnings)
    schema_active = bool(args.schema) and not args.review_scope

    enqueued_mono = time.monotonic()
    deadline = enqueued_mono + max(0.0, args.timeout_sec)

    # --- 並列スロット ---
    slot = acquire_slot(args.max_parallel, deadline)
    queued_sec = time.monotonic() - enqueued_mono
    if slot is None:
        ended = lib.now_utc()
        p = base_payload(args, cd, started, ended, "timeout", queued_sec)
        p["errors"] = [f"並列スロット（max-parallel={args.max_parallel}）を取得できないまま timeout-sec に達した"]
        p["warnings"] = warnings
        write_job(job_dir, p)
        lib.eprint(p["errors"][0])
        return 3

    # L13: スロット待ちは実行時間に含めない（started_at は取得後）
    started = lib.now_utc()
    ctx.update({"slot": slot, "started": started, "queued_sec": queued_sec})

    col = Collector()
    status = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cd,
            env=env,
            start_new_session=True,   # プロセスグループごと止められるようにする
        )
    except OSError as e:
        slot.release()
        ended = lib.now_utc()
        p = base_payload(args, cd, started, ended, "not_found")
        p["errors"] = [f"codex の起動に失敗しました: {e}"]
        p["warnings"] = warnings
        write_job(job_dir, p)
        lib.eprint(p["errors"][0])
        return 4

    ctx.update({"proc": proc, "slot": slot, "col": col, "started": started,
                "queued_sec": queued_sec})

    threads = [
        threading.Thread(target=stream_stdout, args=(proc, events_path, col), daemon=True),
        threading.Thread(target=stream_stderr, args=(proc, stderr_path), daemon=True),
    ]
    if args.review_scope:
        # C1: review サブコマンドにはプロンプトを渡せないので stdin は即クローズする
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    else:
        threads.append(threading.Thread(target=feed_stdin, args=(proc, prompt), daemon=True))
    for t in threads:
        t.start()

    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                status = "timeout"
                col.errors.append(f"壁時計タイムアウト（{args.timeout_sec}s）で停止した")
                kill_group(proc)
                break
            with col.lock:
                idle = now - col.last_event_mono
            if args.idle_timeout_sec > 0 and idle >= args.idle_timeout_sec:
                status = "idle_timeout"
                col.errors.append(f"アイドルタイムアウト（{args.idle_timeout_sec}s 無イベント）で停止した")
                kill_group(proc)
                break
            time.sleep(0.05)
        exit_code = proc.wait()
    except KeyboardInterrupt:
        status = "killed"
        col.errors.append("KeyboardInterrupt により停止した")
        kill_group(proc)
        exit_code = proc.wait()
    except BaseException:
        slot.release()
        raise

    # H3: 子孫が setsid でプロセスグループを抜けていると読取スレッドは EOF を受け取れない。
    # join には**全体で** JOIN_TOTAL_SEC の予算しか与えず、残ったスレッドは daemon のまま放置し、
    # job.json の書込と slot 解放を先に済ませる（close() はバッファのロックで固まりうるため最後）。
    join_deadline = time.monotonic() + JOIN_TOTAL_SEC
    threads_stuck = False
    for t in threads:
        t.join(timeout=max(0.0, join_deadline - time.monotonic()))
        if t.is_alive():
            threads_stuck = True
    slot.release()
    if threads_stuck:
        col.warn("子孫プロセスが stdout/stderr を握ったままのため、読取スレッドを残して終了した"
                 "（codex 側が setsid した孫が生きている可能性がある）")

    ended = lib.now_utc()

    # --- status 判定（codex の exit code だけで completed にしない） ---
    if status is None:
        if col.turn_failed:
            status = "failed"           # top-level turn.failed
        elif col.turn_completed:
            status = "completed"        # turn.completed 到達を最優先（非致命の error があっても）
        elif col.top_errors:
            status = "failed"           # turn.completed 未到達 + top-level error
        elif exit_code is not None and exit_code < 0:
            status = "killed"
            col.errors.append(f"シグナル {-exit_code} で終了した")
        else:
            status = "error"
            col.errors.append(
                f"turn.completed / turn.failed のいずれにも到達せずに終了した（exit={exit_code}）")
        if status in ("failed", "error") and detect_auth_error(stderr_path):
            status = "auth_error"
            col.errors.append("stderr に認証エラーの兆候（unauthorized / not logged in / codex login）がある")

    # M11: 起動・引数・git チェック失敗はイベントが出ないので、原因（stderr）を errors に載せる
    if status in ("error", "auth_error", "failed", "not_found") and col.event_count <= 2:
        tail = stderr_tail(stderr_path)
        if tail:
            col.errors.append(f"stderr（末尾 {STDERR_TAIL_CHARS} 字まで）:\n{tail}")

    # imagegen は turn.completed だけでは成功にしない。Codex の組み込みツールが標準保存先に
    # 取り残した場合は、今回の開始時刻以降で最も新しい画像を --out に回収してから検証する。
    if args.mode == "imagegen" and status == "completed":
        if image_magic(args.out_path) is None:
            recover_generated_image(args.out_path, started.timestamp(), warnings)
        if image_magic(args.out_path) is None:
            status = "failed"
            col.errors.append(f"imagegen は turn.completed したが、有効な PNG / JPEG を --out に取得できなかった: {args.out_path}")

    if col.commands_dropped:
        col.warn(f"実行コマンドが多いため成功分 {col.commands_dropped} 件を記録から切り捨てた"
                 f"（上限 {MAX_COMMANDS} 件）")
    if col.failed_dropped:
        # H-1: 失敗が数千件でも job.json を肥大化させない（件数だけ残す）
        col.warn(f"失敗コマンド {col.failed_dropped} 件を記録から切り捨てた"
                 f"（上限 {MAX_FAILED_COMMANDS} 件。全件は events.jsonl にある）")

    payload = base_payload(args, cd, started, ended, status, queued_sec)
    payload["exit_code"] = exit_code
    payload["thread_id"] = col.thread_id
    payload["usage"] = col.usage
    payload["credits_est"] = lib.credits_est(col.usage, args.model) if col.usage else None
    payload["touched_files"] = col.touched
    payload["commands"] = col.commands
    payload["errors"] = col.errors
    payload["warnings"] = warnings + col.warnings
    payload["job_dir"] = str(job_dir)
    if args.mode == "imagegen":
        payload["image"] = output_image_payload(args.out_path, payload["warnings"])

    # --- last.md（空なら agent_message から復元。#19945 の空出力対策） ---
    try:
        size = last_md.stat().st_size
    except OSError:
        size = 0
    if size == 0 and col.agent_messages:
        lib.atomic_write_text(last_md, col.agent_messages[-1])
        payload["warnings"].append("codex が -o に何も書かなかったため、agent_message から last.md を復元した")
        size = last_md.stat().st_size
    payload["last_message_path"] = str(last_md) if size > 0 else None

    # --- structured_output（L14: review-scope 時は schema を渡していないのでパースしない） ---
    if schema_active:
        if payload["last_message_path"]:
            try:
                raw = last_md.read_text(encoding="utf-8", errors="replace")
                payload["structured_output"] = json.loads(raw)
            except json.JSONDecodeError as e:
                payload["warnings"].append(f"last.md を JSON として解釈できなかった: {e}")
            except OSError as e:
                payload["warnings"].append(f"last.md を読めなかった: {e}")
        else:
            payload["warnings"].append("--schema を指定したが last.md が空のため structured_output は null")

    append_ledger(args, cd, payload)
    write_job(job_dir, payload)

    print(f"status={status} exit_code={exit_code} events={col.event_count} "
          f"duration={lib.fmt_duration(payload['duration_sec'])} job={job_dir}")
    if col.errors:
        for e in col.errors[:5]:
            lib.eprint(f"error: {e}")

    rc = EXIT_BY_STATUS.get(status, 1)
    close_pipes(proc, threads_stuck)
    if threads_stuck:
        # 読取スレッドがパイプで固まったままなので、終了処理（バッファの flush/close）を
        # 通らずに落とす。job.json は既に書き終えている。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
