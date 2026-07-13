#!/usr/bin/env python3
"""fable-cost-manager 共有コアライブラリ。

Claude Code の transcript（~/.claude/projects 配下の JSONL）からトークン使用量を
抽出・requestId 単位で重複排除・料金換算するための共通処理をまとめる。
cost_start.py / cost_report.py / cost_status.py / render_md.py / render_image.py から
`sys.path.insert(0, <このファイルのディレクトリ>)` の上で `import cost_lib as lib` して使う。

このファイル単体では何も実行しない（ライブラリ専用）。動作確認は下記のように
Python の対話的呼び出しで行う。

実行例:
    python3 -c "
    import sys; sys.path.insert(0, 'scripts')
    import cost_lib as lib
    print(lib.encode_cwd('/Users/you/work/my_project.v2'))
    "

主要な公開 API:
    repo_root() / projects_dir()                    -- ルート解決（env オーバーライド対応）
    encode_cwd(path)                                 -- cwd -> projects ディレクトリ名エンコード
    current_session_id()                             -- env CLAUDE_CODE_SESSION_ID
    iter_transcripts(session_id, cwd, glob_all, since) -- 対象 JSONL パスの列挙
    iter_usage(path, start_offset=0)                 -- (rows, new_offset) を返す行パーサ
    Accumulator                                       -- requestId/uuid dedup
    collect_dedup_rows(paths, since, until)          -- iter_usage + Accumulator の合成ヘルパ
    scan_activity(tfiles, gap_max_sec)               -- 実処理時間（active time）用の活動走査
    active_seconds(intervals, clip_start, clip_end)  -- interval union のクリップ済み合計秒
    load_config() / load_pricing()
    resolve_model(model, pricing) / rate_for(resolved, pricing, at)
    billing_class(agg, pricing)                      -- "payg"(Fable) | "included"(Max20x込み) 判定
    aggregate(rows, pricing, at, usd_jpy) -> Report
    atomic_write_json / atomic_write_text
    fmt_tokens / fmt_usd / fmt_jpy / fmt_duration / to_jst / parse_iso
    make_slug / make_task_id / report_basename
    load_active_task / save_active_task / archive_task / append_report_log
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

# 表示用タイムゾーン: JST 固定。zoneinfo は使わない（環境の tzdata に依存させないため）。
JST = timezone(timedelta(hours=9))
UTC = timezone.utc

_DEFAULT_ROOT = str(Path(__file__).resolve().parent.parent)
_DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

SYNTHETIC_MODEL = "<synthetic>"


# ---------------------------------------------------------------------------
# ルート解決
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """データルート（config/ var/ reports/ の親）。env FABLE_COST_MANAGER_ROOT で上書き可。

    テスト時に config/ を差し替えたスクラッチルートを指すことを想定（var/ 汚染防止）。
    """
    return Path(os.environ.get("FABLE_COST_MANAGER_ROOT", _DEFAULT_ROOT)).expanduser()


def code_root() -> Path:
    """コードルート（templates/ scripts/ の親）。このファイルの実位置から常に固定的に決まる。

    templates/ はリポジトリのコード資産であり、FABLE_COST_MANAGER_ROOT のテスト用
    スクラッチルート切替の対象にはしない（テスト時に templates/ までコピーする必要がないように）。
    """
    return Path(__file__).resolve().parent.parent


def projects_dir() -> Path:
    """transcript 探索元。env FCM_PROJECTS_DIR で上書き可（テストは凍結コピーへ向ける）。"""
    return Path(os.environ.get("FCM_PROJECTS_DIR", _DEFAULT_PROJECTS_DIR)).expanduser()


def encode_cwd(path: str) -> str:
    """cwd を ~/.claude/projects 配下のディレクトリ名へエンコードする（非可逆）。

    実データで検証済みのルール: 英数字とハイフン以外の文字（'/' '_' '.' 空白 等）を
    すべて '-' に置換する。例:
      /Users/you/work/my_project.v2
        -> -Users-you-work-my-project-v2
      /Users/you/work/task_tracker
        -> -Users-you-work-task-tracker   (アンダースコアも '-' 化)
    逆算はできないため、cwd の実値は JSONL 行の "cwd" フィールドから読む。
    """
    return re.sub(r"[^a-zA-Z0-9-]", "-", str(path))


def current_session_id() -> Optional[str]:
    """現在の実行セッション ID（env CLAUDE_CODE_SESSION_ID）。無ければ None。"""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


# ---------------------------------------------------------------------------
# transcript 探索
# ---------------------------------------------------------------------------

@dataclass
class TFile:
    """走査対象の1 JSONL ファイル。"""
    path: Path
    session_id: str
    kind: str  # "main" | "subagent" | "workflow"


def _session_dir(session_id: str, cwd: str) -> Path:
    return projects_dir() / encode_cwd(cwd) / session_id


def iter_transcripts(
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    glob_all: bool = False,
    since: Optional[datetime] = None,
) -> Iterator[TFile]:
    """対象 JSONL ファイルを列挙する。

    - 通常時（glob_all=False）: session_id（省略時は現在のセッション）について
      メイン transcript + subagents/agent-*.jsonl + subagents/workflows/wf_*/agent-*.jsonl
      を列挙する。cwd 省略時は os.getcwd()。
    - glob_all=True（--scope global 用）: ~/.claude/projects 配下の全 *.jsonl を列挙。
      since が与えられれば mtime < since のファイルは走査前にスキップする（高速化）。
    """
    if glob_all:
        pdir = projects_dir()
        if not pdir.is_dir():
            return
        for p in sorted(pdir.glob("**/*.jsonl")):
            if since is not None:
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
                if mtime < since:
                    continue
            # session_id はファイル名 or 親ディレクトリ名から推定（ベストエフォート）
            sid = p.stem
            kind = "main"
            if "subagents" in p.parts:
                idx = p.parts.index("subagents")
                sid = p.parts[idx - 1] if idx > 0 else p.stem
                kind = "workflow" if "workflows" in p.parts else "subagent"
            yield TFile(path=p, session_id=sid, kind=kind)
        return

    if session_id is None:
        session_id = current_session_id()
    if not session_id:
        return
    if cwd is None:
        cwd = os.getcwd()

    proj_dir = projects_dir() / encode_cwd(cwd)
    main_file = proj_dir / f"{session_id}.jsonl"
    if main_file.exists():
        yield TFile(path=main_file, session_id=session_id, kind="main")

    sess_dir = _session_dir(session_id, cwd)
    sub_dir = sess_dir / "subagents"
    for p in sorted(sub_dir.glob("agent-*.jsonl")):
        yield TFile(path=p, session_id=session_id, kind="subagent")

    wf_dir = sub_dir / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("wf_*")):
            for p in sorted(wf.glob("agent-*.jsonl")):
                yield TFile(path=p, session_id=session_id, kind="workflow")


# ---------------------------------------------------------------------------
# 行パース
# ---------------------------------------------------------------------------

def iter_usage(path, start_offset: int = 0):
    """JSONL を1行ずつ読み、type=="assistant" かつ message.usage!=null の行を抽出する。

    戻り値: (rows, new_offset)
      rows: dict のリスト。各要素は
        {"requestId", "uuid", "model", "timestamp", "usage", "source_file"}
      new_offset: 次回呼び出し用のバイトオフセット（フェーズ2の増分パース用の席）。
        最終行が改行で終わっていない場合はその行の先頭を new_offset とし、次回に回す。
        壊れた行（JSON decode error）は skip する。
        message.model が欠落 or "<synthetic>" の行は集計対象外として skip する。
    """
    path = Path(path)
    rows = []
    try:
        size = path.stat().st_size
    except OSError:
        return rows, start_offset

    if start_offset < 0 or start_offset > size:
        start_offset = 0

    with open(path, "rb") as f:
        f.seek(start_offset)
        pos = start_offset
        for raw in f:
            if not raw.endswith(b"\n"):
                # 改行未達の最終行 -> 次回に回す（pos は進めない）
                break
            line_start = pos  # この行の先頭バイトオフセット（anon dedup キー用の一意位置）
            pos += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message") or {}
            usage = message.get("usage")
            if not usage:
                continue
            model = message.get("model")
            if not model or model == SYNTHETIC_MODEL:
                continue
            rows.append(
                {
                    "requestId": obj.get("requestId"),
                    "uuid": obj.get("uuid"),
                    "model": model,
                    "timestamp": obj.get("timestamp"),
                    "usage": usage,
                    "source_file": str(path),
                    "lineno": line_start,
                }
            )
        new_offset = pos

    return rows, new_offset


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

class Accumulator:
    """requestId（欠落時は uuid）単位で dedup するアキュムレータ。

    - requestId が同じ行が複数存在する場合（content block 分割で同一usageが複数行に
      再掲される）、output_tokens が最大の行を採用する。
    - requestId が欠落している行は uuid をキーとして同じ map に載せる。
    - requestId も uuid も両方欠落している行は、ファイルパス＋行位置で一意な
      ("anon", source_file, lineno) をキーにする（("uuid", None) に全て衝突して
      1行に潰れるのを防ぐ）。
    - グローバル uuid 集合を別途保持し、同じ uuid の行が別ファイル（resume による
      再シリアライズ等）で再度現れた場合は無条件で skip する（requestId が振り直され
      ていても二重計上を防ぐ）。
    """

    def __init__(self) -> None:
        self._by_key: dict = {}
        self._seen_uuids: set = set()

    def add(self, row: dict) -> bool:
        """row を取り込む。取り込んだら True、resume 重複として skip したら False。"""
        uuid = row.get("uuid")
        if uuid and uuid in self._seen_uuids:
            return False

        rid = row.get("requestId")
        if rid:
            key = ("rid", rid)
        elif uuid:
            key = ("uuid", uuid)
        else:
            # requestId も uuid も無い行は行位置で一意化（衝突による潰れ防止）
            key = ("anon", row.get("source_file"), row.get("lineno"))

        existing = self._by_key.get(key)
        out_tok = _int_or_zero((row.get("usage") or {}).get("output_tokens"))
        if existing is None:
            self._by_key[key] = row
        else:
            existing_out = _int_or_zero((existing.get("usage") or {}).get("output_tokens"))
            if out_tok >= existing_out:
                self._by_key[key] = row

        if uuid:
            self._seen_uuids.add(uuid)
        return True

    def rows(self) -> list:
        return list(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)


def _int_or_zero(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_iso(ts) -> datetime:
    """ISO8601 文字列（'Z' サフィックス対応）を aware datetime(UTC) に変換する。"""
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def collect_dedup_rows(
    tfiles: Iterable,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    stats: Optional[dict] = None,
) -> list:
    """複数 transcript を走査し、時間窓でフィルタしつつ dedup 済み行リストを返す。

    tfiles は iter_transcripts() が返す TFile、または Path/str の混在でも良い。

    時間窓（since / until のいずれか）が明示されている場合、timestamp を持たない
    課金行は窓に収まるか判定できないため除外する（窓を素通りして常時計上されるのを
    防ぐ）。窓指定が無いときは従来どおり全て計上する。

    stats（dict を渡すと）に集計メタを書き込む:
      - "dropped_no_timestamp": 窓指定時に timestamp 欠落で除外した行数
      - "earliest_ts": 窓内に採用した生データ行の最早 timestamp（datetime, UTC）。
                       表示用の開始時刻に使う（dedup 採用行の timestamp とのズレ回避）。
    """
    acc = Accumulator()
    has_window = since is not None or until is not None
    dropped_no_ts = 0
    earliest_ts: Optional[datetime] = None
    for tf in tfiles:
        path = tf.path if isinstance(tf, TFile) else tf
        rows, _ = iter_usage(path, 0)
        for row in rows:
            ts = row.get("timestamp")
            if ts:
                try:
                    dt = parse_iso(ts)
                except (ValueError, TypeError):
                    dt = None
            else:
                dt = None
            if dt is None:
                if has_window:
                    # 窓が指定されているのに時刻不明 -> 窓判定できないため除外
                    dropped_no_ts += 1
                    continue
                # 窓指定なし: 従来どおり計上
                acc.add(row)
                continue
            if since is not None and dt < since:
                continue
            if until is not None and dt > until:
                continue
            if earliest_ts is None or dt < earliest_ts:
                earliest_ts = dt
            acc.add(row)
    if stats is not None:
        stats["dropped_no_timestamp"] = dropped_no_ts
        stats["earliest_ts"] = earliest_ts
    return acc.rows()


def find_first_user_text(tfiles: Iterable, since=None, until=None, limit: int = 80) -> Optional[str]:
    """スコープ内で最初に見つかったユーザーメッセージのテキストを抜粋する（--desc 省略時のフォールバック用）。"""
    candidates = []
    for tf in tfiles:
        path = tf.path if isinstance(tf, TFile) else Path(tf)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    ts = obj.get("timestamp")
                    dt = None
                    if ts:
                        try:
                            dt = parse_iso(ts)
                        except (ValueError, TypeError):
                            dt = None
                    if since is not None and dt is not None and dt < since:
                        continue
                    if until is not None and dt is not None and dt > until:
                        continue
                    text = _extract_text(obj.get("message", {}).get("content"))
                    if text:
                        candidates.append((dt or datetime.min.replace(tzinfo=UTC), text))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    text = candidates[0][1].strip().replace("\n", " ")
    return text[:limit]


def _extract_text(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return block["text"]
    return None


# ---------------------------------------------------------------------------
# 実処理時間（active time）
# ---------------------------------------------------------------------------
# コスト集計（iter_usage / Accumulator / collect_dedup_rows / aggregate）とは完全に
# 独立した transcript 走査。dedup ロジックには一切触れない。

@dataclass
class ActivityScan:
    """scan_activity() の戻り値。"""
    intervals: list       # list[tuple[datetime, datetime]] ターン/ギャップ分割済み・未マージ・未クリップ
    event_times: list     # sorted list[datetime] 全イベント時刻（レポートターン除外後）
    report_cutoff: object  # datetime | None（検出したレポートターンのターン開始 ts の最小値。連続ターン境界列は先頭まで遡り済み）


_COMMAND_TAG_PREFIXES = ("<command-name>", "<local-command")


def _starts_with_command_tag(text: str) -> bool:
    return (text or "").lstrip().startswith(_COMMAND_TAG_PREFIXES)


def _is_human_prompt(rec: dict) -> bool:
    """メインファイルの1レコードが「人間の入力プロンプト」かどうかを判定する（実データ検証済みの判定式）。

    スラッシュコマンド展開（<command-name>/<local-command...> で始まるテキスト）・
    tool_result・サイドチェーン・isMeta・toolUseResult 付き行は人間プロンプトとして扱わない。

    注: 「ターン開始」イベントはこの人間プロンプト判定だけでは不十分。スラッシュコマンド起動時に
    連続して現れる <local-command-caveat> / <command-name> / <local-command-stdout> の3レコードは
    ここでは人間プロンプト扱いされない（isMeta=True または <command-name>/<local-command プレフィックス
    のため）が、それらもユーザー操作によるターン開始である。ターン境界の総合判定は
    「人間プロンプト または スラッシュコマンドレコード」= `_is_turn_start()` を使うこと
    （`_is_command_record()` がスラッシュコマンドレコード側を判定する）。
    """
    if rec.get("type") != "user":
        return False
    message = rec.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if rec.get("isSidechain") is True:
        return False
    if rec.get("isMeta") is True:
        return False
    if "toolUseResult" in rec:
        return False

    content = message.get("content")
    if isinstance(content, str):
        return not _starts_with_command_tag(content)
    if isinstance(content, list):
        first_text = None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return False
            if first_text is None and block.get("type") == "text":
                first_text = block.get("text") or ""
        if first_text is None:
            # text ブロックが無い（画像のみ等）場合、tool_result も無ければ人間扱いにする。
            return True
        return not _starts_with_command_tag(first_text)
    return False


def _is_command_record(rec: dict) -> bool:
    """メインファイルの1レコードが「スラッシュコマンド起動レコード」かどうかを判定する。

    `/cost-manager` 等のスラッシュコマンド起動時に type=="user"（message.role=="user"）で
    連続出現する3種のレコードを指す:
      1. isMeta=True, content が "<local-command-caveat>" で始まる
      2. isMeta 無し, content が "<command-name>" で始まる
      3. isMeta 無し, content が "<local-command-stdout>" で始まる
    isMeta の値は問わない（1. は isMeta=True だが "<local-command-caveat>" は
    "<local-command" プレフィックスに一致するため拾える）。tool_result（toolUseResult キーあり）・
    サイドチェーン行は対象外。
    """
    if rec.get("type") != "user":
        return False
    message = rec.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if rec.get("isSidechain") is True:
        return False
    if "toolUseResult" in rec:
        return False

    content = message.get("content")
    if isinstance(content, str):
        return _starts_with_command_tag(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return _starts_with_command_tag(block.get("text") or "")
        return False
    return False


def _is_turn_start(rec: dict) -> bool:
    """レコードが「ターン開始」イベントかどうか（人間プロンプト or スラッシュコマンドレコード）。

    scan_activity() のセグメント分割・レポートターン除外の基準点はいずれもこの判定を使う
    （`_is_human_prompt` 単独では、スラッシュコマンド起動の3連レコードがターン開始として
    認識されず、コマンド起動でレポートを生成した際に直前の実作業ごと過剰除外される
    バグがあったため）。
    """
    return _is_human_prompt(rec) or _is_command_record(rec)


def _is_report_marker(rec: dict) -> bool:
    """assistant レコードが「cost_report.py を python で実行する Bash tool_use」を含むかどうかを判定する。

    判定条件は command 文字列が "cost_report.py" と "python" の両方を含むこと。スキル経由の起動は
    常に `python3 <abs-path>/cost_report.py ...` 形式のため検出できる。閲覧系コマンド
    （`cat scripts/cost_report.py` や `git log cost_report.py` 等）は "python" を含まないため
    誤検出しない。一方でシェバン直接実行（`./scripts/cost_report.py ...`）は "python" を含まない
    ため検出対象外（既知の制約）。
    """
    if rec.get("type") != "assistant":
        return False
    message = rec.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Bash":
            command = str((block.get("input") or {}).get("command", ""))
            if "cost_report.py" in command and "python" in command:
                return True
    return False


def _scan_file_events(path) -> list:
    """1 transcript ファイルを走査し、timestamp を持つレコードを (ts, is_turn_start, is_marker) のリストで返す。

    is_turn_start は「ターン開始」イベント（人間プロンプト or スラッシュコマンドレコード。
    `_is_turn_start()` 参照）を表す。iter_usage() と同様にファイル全体を1行ずつ JSON パースする
    （増分オフセットは扱わない）。壊れた行（JSON decode error）・timestamp 欠落/パース不能行は skip する。
    """
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_raw = rec.get("timestamp")
                if not ts_raw:
                    continue
                try:
                    ts = parse_iso(ts_raw)
                except (ValueError, TypeError):
                    continue
                events.append((ts, _is_turn_start(rec), _is_report_marker(rec)))
    except OSError:
        pass
    return events


def scan_activity(
    tfiles: Iterable,
    gap_max_sec: float = 900.0,
    invoking_session_id: Optional[str] = None,
) -> ActivityScan:
    """複数 transcript を走査し、活動 interval と全イベント時刻を抽出する。

    tfiles は collect_dedup_rows() と同じ TFile/Path/str の混在リストを受け取る
    （iter_transcripts() が返すものをそのまま渡せる）。コスト集計ロジック
    （iter_usage/Accumulator/collect_dedup_rows/aggregate）には一切手を触れない。

    - サブエージェントファイル（パスに "subagents" を含む）: ターン開始判定・
      レポートマーカー判定を行わず、全レコードを活動イベント扱いにする。セグメント
      分割はギャップ（> gap_max_sec）のみで行う。
    - メインファイル: ターン開始イベント（人間プロンプト or スラッシュコマンドレコード。
      `_is_turn_start()` 参照）で必ずセグメントを分割し（ギャップの大小に関係なく、
      人間の入力待ちを実処理時間から除くため）、さらにギャップが gap_max_sec を超えても
      分割する。また、そのファイルの最後のターン開始 ts（L）以降に cost_report.py を
      python で実行する Bash tool_use（レポートターンマーカー）が見つかった場合、その
      ファイルの ts >= L のイベントを全て除外する（現在進行中のレポート生成ターン自体を
      実処理時間・最終アクティビティ判定から除くため）。スラッシュコマンド起動レコードも
      ターン開始に含めることで、コマンド起動でレポートを生成した場合に L がコマンド
      起動時刻まで正しく前進し、直前の実作業ターンが過剰除外されないようにしている。
      L の直前のイベントもターン開始である間は L をそこまで遡らせる（連続するターン境界列
      = コマンド展開の3連続レコード等は先頭まで遡ってまとめて除外する）。
      除外が起きた L のうち最小値を report_cutoff として返す。

    invoking_session_id（呼び出し元セッション ID。env CLAUDE_CODE_SESSION_ID 由来を想定）が
    与えられた場合、そのセッションのメインファイル（TFile なら session_id、生 Path なら
    ファイル名 stem との一致で判定）は**マーカーの有無に関係なく**最終ターン除外を無条件で
    適用する。理由: レポートスクリプトが計測対象セッションの内側から実行されている場合、
    そのメインファイルの開いている最終ターンは構造上必ずレポート実行ターン自身である（いま
    自分がそのターンの tool 呼び出しとして走っているのだから）。一方、実行中の Bash tool_use
    レコード（マーカー）は flush 競合により transcript にまだ書かれていないことがあり、
    マーカー検出だけに頼ると除外に失敗する。マーカー検出は別セッションのファイル・
    invoking_session_id=None（手動実行等）の場合のフォールバックとしてそのまま残る。
    """
    all_intervals: list = []
    all_event_times: list = []
    report_cutoffs: list = []

    for tf in tfiles:
        if isinstance(tf, TFile):
            path = tf.path
            file_session_id = tf.session_id
        else:
            path = Path(tf)
            file_session_id = path.stem
        is_subagent = "subagents" in Path(path).parts

        events = _scan_file_events(path)
        if not events:
            continue

        if is_subagent:
            filtered = events
        else:
            events = sorted(events, key=lambda e: e[0])
            turn_start_idx = [i for i, e in enumerate(events) if e[1]]
            if turn_start_idx:
                idx = turn_start_idx[-1]
                cutoff = events[idx][0]
                # 呼び出し元セッションのメインファイルは、開いている最終ターンが構造上必ず
                # レポート実行ターン自身のため、マーカーの有無に関係なく無条件で除外する
                # （flush 競合でマーカーがまだ transcript に無いことがある。docstring 参照）。
                is_invoking_main = (
                    invoking_session_id is not None
                    and file_session_id == invoking_session_id
                )
                has_marker_after = any(is_m and ts >= cutoff for ts, _, is_m in events)
                if is_invoking_main or has_marker_after:
                    # 連続するターン境界列（スラッシュコマンド展開の caveat / command-name /
                    # local-command-stdout の3連続レコード等）は run の先頭まで L を遡らせて
                    # まとめて除外する。遡らないとコマンド起動時のレコードが event_times に残り、
                    # end_display がコマンド入力時刻まで伸びて放置時間の除外が不完全になる。
                    # 自然言語起動（人間プロンプト1レコード）では直前が assistant 等のため
                    # 遡りは発生しない（挙動不変）。
                    while idx > 0 and events[idx - 1][1]:
                        idx -= 1
                    cutoff = events[idx][0]
                    filtered = [e for e in events if e[0] < cutoff]
                    report_cutoffs.append(cutoff)
                else:
                    filtered = events
            else:
                filtered = events

        if not filtered:
            continue
        filtered = sorted(filtered, key=lambda e: e[0])

        segments = [[filtered[0]]]
        for ev in filtered[1:]:
            ts, is_ts, _ = ev
            prev_ts = segments[-1][-1][0]
            new_segment = (not is_subagent and is_ts) or (ts - prev_ts).total_seconds() > gap_max_sec
            if new_segment:
                segments.append([ev])
            else:
                segments[-1].append(ev)

        for seg in segments:
            all_intervals.append((seg[0][0], seg[-1][0]))
        all_event_times.extend(ts for ts, _, _ in filtered)

    all_event_times.sort()
    report_cutoff = min(report_cutoffs) if report_cutoffs else None
    return ActivityScan(intervals=all_intervals, event_times=all_event_times, report_cutoff=report_cutoff)


def active_seconds(intervals, clip_start=None, clip_end=None) -> float:
    """activity interval のリストをマージ（union）し、[clip_start, clip_end] でクリップした合計秒を返す。

    intervals は (start, end) の tuple のリスト（重複・未ソートで良い）。start > end の
    要素は入れ替えて正規化する。clip_start/clip_end は None なら無制限。
    """
    if not intervals:
        return 0.0

    norm = []
    for s, e in intervals:
        if e < s:
            s, e = e, s
        norm.append((s, e))
    norm.sort(key=lambda t: t[0])

    merged = []
    cur_s, cur_e = norm[0]
    for s, e in norm[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))

    total = 0.0
    for s, e in merged:
        cs = s if clip_start is None else max(s, clip_start)
        ce = e if clip_end is None else min(e, clip_end)
        if ce > cs:
            total += (ce - cs).total_seconds()
    return total


# ---------------------------------------------------------------------------
# 単価
# ---------------------------------------------------------------------------

# usd_jpy の既定値（config.json に usd_jpy が無いときのフォールバック）。
DEFAULT_USD_JPY = 160


class ConfigError(Exception):
    """config.json / pricing.json の欠落・破損を表す例外。

    CLI（cost_start / cost_report / cost_status 等）は main() でこれを捕捉し、
    日本語メッセージを stderr に出して非0 exit する（生トレースバックで死なせない）。
    """


def _load_json_file(path, label: str) -> dict:
    """JSON ファイルを読み込む。欠落・破損時は ConfigError（日本語）を送出する。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{label} が見つかりません: {p}")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{label} が壊れています（JSON 解析エラー: {e}）: {p}")
    except OSError as e:
        raise ConfigError(f"{label} を読み込めません（{e}）: {p}")


def load_config() -> dict:
    return _load_json_file(repo_root() / "config" / "config.json", "config.json")


def load_pricing() -> dict:
    return _load_json_file(repo_root() / "config" / "pricing.json", "pricing.json")


def usd_jpy_from_config(config: dict) -> tuple:
    """config から usd_jpy を取り出す。欠落時は (既定160, 警告文) を返す。

    default_scope 等の他キーと同様に .get(..., 既定) で扱い、欠落は致命エラーにしない。
    戻り値: (usd_jpy: float, warning: Optional[str])
    """
    val = config.get("usd_jpy")
    if val is None:
        return DEFAULT_USD_JPY, (
            f"config.json に usd_jpy がありません。参考レートに既定値 {DEFAULT_USD_JPY} 円/USD を使用します。"
        )
    return val, None


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


_BRACKET_SUFFIX_RE = re.compile(r"\s*\[[^\]]*\]\s*")
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def resolve_model(model: Optional[str], pricing: dict) -> Optional[str]:
    """モデル名を pricing.json のキーへ正規化する。未知モデルは None。

    手順: '[1m]' 等の角括弧接尾除去 -> 末尾 '-YYYYMMDD' 除去 -> 完全一致 -> 前方一致（最長優先）。
    """
    if not model:
        return None
    name = _BRACKET_SUFFIX_RE.sub("", model).strip()
    name = _DATE_SUFFIX_RE.sub("", name)

    models = pricing.get("models", {})
    if name in models:
        return name

    candidates = [k for k in models if name.startswith(k)]
    if candidates:
        return max(candidates, key=len)
    return None


def is_pricing_stale(pricing: dict, at: date) -> bool:
    as_of_str = pricing.get("as_of")
    if not as_of_str:
        return False
    as_of = parse_date(as_of_str)
    stale_after = pricing.get("stale_after_days", 90)
    return (at - as_of).days > stale_after


def rate_for(resolved_model: Optional[str], pricing: dict, at: date) -> Optional[dict]:
    """resolved_model（resolve_model() の戻り値）の $/MTok 単価を返す。未知なら None。

    戻り値キー: input, output, write_5m, write_1h, read（全て $/MTok）。
    モデルエントリに cache_write_5m / cache_write_1h / cache_read の明示キーがあれば
    キャッシュ倍率より優先する。
    """
    if not resolved_model:
        return None
    entry = pricing.get("models", {}).get(resolved_model)
    if entry is None:
        return None

    intro = entry.get("intro")
    if intro and "until" in intro:
        until = parse_date(intro["until"])
        if at <= until:
            in_rate, out_rate = intro["input"], intro["output"]
        else:
            in_rate, out_rate = entry["input"], entry["output"]
    else:
        in_rate, out_rate = entry["input"], entry["output"]

    mult = pricing.get("cache_multipliers", {})
    write_5m = entry.get("cache_write_5m", in_rate * mult.get("write_5m", 1.25))
    write_1h = entry.get("cache_write_1h", in_rate * mult.get("write_1h", 2.0))
    read = entry.get("cache_read", in_rate * mult.get("read", 0.1))

    return {"input": in_rate, "output": out_rate, "write_5m": write_5m, "write_1h": write_1h, "read": read}


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

@dataclass
class ModelAgg:
    model: str
    resolved: Optional[str] = None
    input_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    known: bool = True
    billing: str = "included"


@dataclass
class Report:
    models: list = field(default_factory=list)
    total_usd: float = 0.0
    total_jpy: float = 0.0
    unknown_models: list = field(default_factory=list)
    stale: bool = False
    pricing_as_of: Optional[str] = None
    usd_jpy: float = 0.0
    row_count: int = 0
    # Fable（claude-fable-5 等）は従量課金で都度報告が必要、それ以外は Max20x サブスク込みの想定。
    # 表示用に2バケツで小計を持つ（詳細は billing_class() / aggregate() を参照）。
    payg_usd: float = 0.0
    payg_jpy: float = 0.0
    included_usd: float = 0.0
    included_jpy: float = 0.0


def billing_class(agg: "ModelAgg", pricing: dict) -> str:
    """モデルの課金区分（"payg" | "included"）を判定する。

    優先順位: 1) pricing.json の該当モデルエントリの "billing" フラグ
              2) 無ければ raw な model 名（resolved 優先、無ければ生の agg.model）に
                 "fable" を含むかどうかで判定するフォールバック。
    pricing 未登録の将来の fable 系モデルも拾えるよう、フォールバックは resolved が
    無い（＝未知モデル）場合でも生 model 名を見る。未知モデルは cost_usd=0 のため
    金額には影響しないが、表示分類（テーブルの並び順・小計バケツ）は正しくなる。
    """
    entry = pricing.get("models", {}).get(agg.resolved) if agg.resolved else None
    if entry and "billing" in entry:
        return entry["billing"]
    name = agg.resolved or agg.model
    return "payg" if "fable" in (name or "") else "included"


def _extract_cache_creation(usage: dict) -> tuple:
    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and cc:
        w5 = _int_or_zero(cc.get("ephemeral_5m_input_tokens"))
        w1h = _int_or_zero(cc.get("ephemeral_1h_input_tokens"))
        return w5, w1h
    # ネスト欠落時はトップレベル cache_creation_input_tokens を 5m 扱いにフォールバック
    w5 = _int_or_zero(usage.get("cache_creation_input_tokens"))
    return w5, 0


def aggregate(rows: list, pricing: dict, at: date, usd_jpy: float) -> Report:
    """dedup 済み行リストをモデル別に合算し、料金（USD/JPY）を適用する。"""
    by_model: dict = {}
    for row in rows:
        model = row.get("model") or "(unknown)"
        agg = by_model.setdefault(model, ModelAgg(model=model))
        usage = row.get("usage") or {}
        agg.input_tokens += _int_or_zero(usage.get("input_tokens"))
        agg.cache_read_tokens += _int_or_zero(usage.get("cache_read_input_tokens"))
        w5, w1h = _extract_cache_creation(usage)
        agg.cache_write_5m += w5
        agg.cache_write_1h += w1h
        agg.output_tokens += _int_or_zero(usage.get("output_tokens"))

    unknown_models = []
    total_usd = 0.0
    payg_usd = 0.0
    included_usd = 0.0
    for model, agg in by_model.items():
        resolved = resolve_model(model, pricing)
        agg.resolved = resolved
        agg.billing = billing_class(agg, pricing)
        rate = rate_for(resolved, pricing, at) if resolved else None
        if rate is None:
            agg.known = False
            unknown_models.append(model)
            continue
        cost = (
            agg.input_tokens / 1_000_000 * rate["input"]
            + agg.cache_write_5m / 1_000_000 * rate["write_5m"]
            + agg.cache_write_1h / 1_000_000 * rate["write_1h"]
            + agg.cache_read_tokens / 1_000_000 * rate["read"]
            + agg.output_tokens / 1_000_000 * rate["output"]
        )
        agg.cost_usd = cost
        total_usd += cost
        if agg.billing == "payg":
            payg_usd += cost
        else:
            included_usd += cost

    # payg（Fable = 従量課金・要都度報告）をテーブル先頭に表示する
    models_sorted = sorted(by_model.values(), key=lambda a: (0 if a.billing == "payg" else 1, a.model))
    return Report(
        models=models_sorted,
        total_usd=total_usd,
        total_jpy=total_usd * usd_jpy,
        unknown_models=sorted(set(unknown_models)),
        stale=is_pricing_stale(pricing, at),
        pricing_as_of=pricing.get("as_of"),
        usd_jpy=usd_jpy,
        row_count=len(rows),
        payg_usd=payg_usd,
        payg_jpy=payg_usd * usd_jpy,
        included_usd=included_usd,
        included_jpy=included_usd * usd_jpy,
    )


# ---------------------------------------------------------------------------
# 書式
# ---------------------------------------------------------------------------

def fmt_tokens(n) -> str:
    return f"{int(n or 0):,}"


def fmt_usd(x, decimals: int = 4) -> str:
    return f"{float(x or 0):,.{decimals}f}"


def fmt_jpy(x) -> str:
    return f"{float(x or 0):,.0f}"


def fmt_duration(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}時間{m}分"
    if m > 0:
        return f"{m}分{s}秒" if s else f"{m}分"
    return f"{s}秒"


def to_jst(dt) -> datetime:
    """UTC(またはISO文字列) -> JST の aware datetime。"""
    if isinstance(dt, str):
        dt = parse_iso(dt)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST)


_UNSAFE_FILENAME_RE = re.compile(r'[\/\\:*?"<>|\x00-\x1f]')


def make_slug(text: Optional[str], limit: int = 40) -> str:
    """タスク名からファイル名安全な slug を作る（日本語可・危険文字除去・40字上限）。"""
    text = (text or "").strip()
    text = _UNSAFE_FILENAME_RE.sub("", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._-")
    if not text:
        text = "task"
    return text[:limit]


def report_basename(dt_jst: datetime, task_name: Optional[str]) -> str:
    """reports/YYYY/MM/ 配下のベースファイル名（拡張子なし）: YYYYMMDD-HHMM-<slug>。"""
    ts = dt_jst.strftime("%Y%m%d-%H%M")
    slug = make_slug(task_name)
    return f"{ts}-{slug}"


def make_task_id(dt_jst: datetime) -> str:
    return "t-" + dt_jst.strftime("%Y%m%d-%H%M")


# ---------------------------------------------------------------------------
# アトミック書込
# ---------------------------------------------------------------------------

def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------------------
# タスクマーカー
# ---------------------------------------------------------------------------

def active_task_path() -> Path:
    return repo_root() / "var" / "active_task.json"


def load_active_task() -> Optional[dict]:
    p = active_task_path()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_active_task(obj: dict) -> None:
    atomic_write_json(active_task_path(), obj)


def archive_task(obj: dict) -> Path:
    """アクティブマーカーを var/tasks/ へアーカイブし、active_task.json を削除する。"""
    tasks_dir = repo_root() / "var" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_id = obj.get("task_id") or "unknown"
    dest = tasks_dir / f"{task_id}.json"
    atomic_write_json(dest, obj)
    p = active_task_path()
    if p.exists():
        p.unlink()
    return dest


def append_report_log(entry: dict) -> None:
    path = repo_root() / "var" / "log" / "reports.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
