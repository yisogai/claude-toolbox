#!/usr/bin/env python3
"""usage-report のロジック本体。

指定ディレクトリ（root）の子孫ディレクトリで実行された Claude Code セッションを
期間指定で集計し、CSV / Markdown 用のデータ構造を作る。

設計方針:
- コスト計算・dedup・実処理時間の中核は cost-manager の cost_lib を import 再利用する
  （ロジックを複製しない）。ここに書くのは「スコープ列挙・帰属・期間窓・出力整形」だけ。
- ``~/.claude/projects`` は読み取り専用。書込は usage-report/reports/（または
  ``--out-dir`` で明示された出力先）と、LLM 要約のキャッシュ ``var/summaries/`` のみ。
- 時刻は内部 UTC aware、表示は JST（``lib.JST``）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# --- cost_lib の import ------------------------------------------------------
_HERE = Path(__file__).resolve()
TOOLBOX_ROOT = _HERE.parent.parent.parent          # .../claude-toolbox
USAGE_REPORT_ROOT = _HERE.parent.parent            # .../claude-toolbox/usage-report
_COST_SCRIPTS = TOOLBOX_ROOT / "cost-manager" / "scripts"
if str(_COST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_COST_SCRIPTS))

import cost_lib as lib  # noqa: E402

JST = lib.JST


# ---------------------------------------------------------------------------
# 期間の解決
# ---------------------------------------------------------------------------

@dataclass
class Period:
    since: datetime          # UTC aware、含む
    until: datetime          # UTC aware、含まない（半開区間 [since, until)）
    label: str               # "2026-07" / "2026-W33" / "20260701-20260714"
    kind: str                # "month" | "week" | "range"
    defaulted: bool = False  # 期間フラグ無しで当月にフォールバックしたか

    @property
    def since_jst(self) -> datetime:
        return lib.to_jst(self.since)

    @property
    def until_jst(self) -> datetime:
        return lib.to_jst(self.until)

    def describe(self) -> str:
        s = self.since_jst.strftime("%Y-%m-%d %H:%M")
        e = self.until_jst.strftime("%Y-%m-%d %H:%M")
        return f"{s} 〜 {e} (JST, 終端は含まない)"


class UsageReportError(Exception):
    """引数・環境エラー（CLI は exit 1 にする）。"""


def _jst_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=JST)


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(lib.UTC)


def _month_period(spec: str) -> Period:
    m = re.fullmatch(r"(\d{4})-(\d{2})", spec.strip())
    if not m:
        raise UsageReportError(f"--month の書式が不正です: {spec}（例 2026-07）")
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise UsageReportError(f"--month の月が不正です: {spec}")
    start = _jst_midnight(date(y, mo, 1))
    end = _jst_midnight(date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1))
    return Period(_to_utc(start), _to_utc(end), f"{y:04d}-{mo:02d}", "month")


def _week_period(spec: str, now_jst: datetime) -> Period:
    spec = spec.strip()
    if spec in ("this", "last"):
        today = now_jst.date()
        monday = today - timedelta(days=today.weekday())
        if spec == "last":
            monday -= timedelta(days=7)
    else:
        m = re.fullmatch(r"(\d{4})-[Ww](\d{2})", spec)
        if not m:
            raise UsageReportError(
                f"--week の書式が不正です: {spec}（this / last / 2026-W33）"
            )
        y, wk = int(m.group(1)), int(m.group(2))
        try:
            monday = date.fromisocalendar(y, wk, 1)
        except ValueError as exc:
            raise UsageReportError(f"--week の週番号が不正です: {spec}（{exc}）") from exc
    start = _jst_midnight(monday)
    end = _jst_midnight(monday + timedelta(days=7))
    iso = monday.isocalendar()
    return Period(_to_utc(start), _to_utc(end), f"{iso[0]:04d}-W{iso[1]:02d}", "week")


def _parse_user_iso(s: str, what: str) -> datetime:
    s = s.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt = datetime.fromisoformat(s + "T00:00:00")
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageReportError(f"{what} の日時書式が不正です: {s}（{exc}）") from exc
    if dt.tzinfo is None:      # naive は JST とみなす
        dt = dt.replace(tzinfo=JST)
    return _to_utc(dt)


def _range_period(from_s: str, to_s: str) -> Period:
    since = _parse_user_iso(from_s, "--from")
    until = _parse_user_iso(to_s, "--to")
    if until <= since:
        raise UsageReportError("--to は --from より後である必要があります。")
    label = (
        lib.to_jst(since).strftime("%Y%m%d") + "-" + lib.to_jst(until).strftime("%Y%m%d")
    )
    return Period(since, until, label, "range")


def resolve_period(month, week, from_s, to_s, now_jst: Optional[datetime] = None) -> Period:
    """期間フラグから Period を作る。排他チェック込み。どれも無ければ当月（JST）。"""
    now_jst = now_jst or datetime.now(JST)
    used = [bool(month), bool(week), bool(from_s or to_s)]
    if sum(used) > 1:
        raise UsageReportError("--month / --week / --from&--to は同時に指定できません。")
    if month:
        return _month_period(month)
    if week:
        return _week_period(week, now_jst)
    if from_s or to_s:
        if not (from_s and to_s):
            raise UsageReportError("--from と --to は両方指定してください。")
        return _range_period(from_s, to_s)
    p = _month_period(now_jst.strftime("%Y-%m"))
    p.defaulted = True
    return p


def pricing_boundaries_in_period(period: Period, pricing: dict) -> list:
    """期間内に単価改定日（intro 価格の ``until``）をまたぐモデルを [(名前, 日付), ...] で返す。

    単価適用日 ``at`` は期間全体で1つ（仕様どおり）なので、改定日をまたぐ期間では
    全日が片側の単価で計算される。金額がずれる旨を警告に出すために使う。
    """
    start_d = lib.to_jst(period.since).date()
    end_d = lib.to_jst(period.until - timedelta(seconds=1)).date()
    out = []
    for name, entry in (pricing.get("models") or {}).items():
        until_s = ((entry or {}).get("intro") or {}).get("until")
        if not until_s:
            continue
        try:
            until_d = lib.parse_date(until_s)
        except Exception:
            continue
        if start_d <= until_d < end_d:
            out.append((name, until_d))
    return sorted(out, key=lambda t: (t[1], t[0]))


def pricing_at(period: Period, now_jst: Optional[datetime] = None) -> date:
    """単価適用日 = min(今日(JST), 期間終端日(JST))。"""
    now_jst = now_jst or datetime.now(JST)
    end_day = lib.to_jst(period.until - timedelta(seconds=1)).date()
    return min(now_jst.date(), end_day)


# ---------------------------------------------------------------------------
# スコープ列挙・セッション帰属
# ---------------------------------------------------------------------------

_CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _unescape_json_str(raw: bytes) -> Optional[str]:
    try:
        return json.loads(b'"' + raw + b'"')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


@dataclass
class Session:
    session_id: str
    first_cwd: str
    main_path: Optional[Path]
    tfiles: list = field(default_factory=list)   # Path のリスト（main + subagents）
    title: str = ""
    orphan: bool = False
    outside_cwd: bool = False
    # 集計後に埋まる
    rows: list = field(default_factory=list)
    report: object = None
    active_sec: float = 0.0
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None


def candidate_project_dirs(root: str) -> list:
    """~/.claude/projects 直下の候補ディレクトリ（エンコード名の前方一致。誤マッチ込み）。

    ``root="/"`` のとき ``encode_cwd`` は ``"-"`` を返す。素朴に ``enc + "-"`` を前置詞に
    すると ``"--"`` 始まりのディレクトリしか拾えず、実在する ``-Users-...`` が1件も
    候補にならない（無警告で 0 セッション）。enc が既に ``-`` で終わる場合は
    そのものを前置詞に使う。
    """
    pdir = lib.projects_dir()
    enc = lib.encode_cwd(root)
    prefix = enc if enc.endswith("-") else enc + "-"
    out = []
    if not pdir.is_dir():
        return out
    for child in sorted(pdir.iterdir()):
        if not child.is_dir():
            continue
        if child.name == enc or child.name.startswith(prefix):
            out.append(child)
    return out


def _scan_main_meta(path: Path) -> dict:
    """メイン jsonl を1パスで走査し first_cwd / cwd 集合 / タイトル候補を取る。

    全行 json.loads はコストが高いので、cwd は正規表現、ai-title / last-prompt は
    substring 事前判定してから json.loads する。
    """
    first_cwd = None
    cwds = set()
    ai_title = None
    last_prompt = None
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"cwd"' in raw:
                    m = _CWD_RE.search(raw)
                    if m:
                        val = _unescape_json_str(m.group(1))
                        if val:
                            cwds.add(val)
                            if first_cwd is None:
                                first_cwd = val
                if b'"ai-title"' in raw or b'"last-prompt"' in raw:
                    try:
                        obj = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                        ai_title = obj["aiTitle"]          # 最終行を採用
                    elif obj.get("type") == "last-prompt" and obj.get("lastPrompt"):
                        last_prompt = obj["lastPrompt"]
    except OSError:
        return {"first_cwd": None, "cwds": set(), "ai_title": None, "last_prompt": None}
    return {
        "first_cwd": first_cwd,
        "cwds": cwds,
        "ai_title": ai_title,
        "last_prompt": last_prompt,
    }


def _first_cwd_of(path: Path, max_lines: int = 200) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f):
                if i >= max_lines:
                    break
                if b'"cwd"' in raw:
                    m = _CWD_RE.search(raw)
                    if m:
                        val = _unescape_json_str(m.group(1))
                        if val:
                            return val
    except OSError:
        return None
    return None


def _under(child: Optional[str], root: str) -> bool:
    if not child:
        return False
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def _subagent_files(cands: list, sid: str) -> list:
    """全候補ディレクトリから <dir>/<sid>/subagents/**/agent-*.jsonl を集める。"""
    out = []
    for cand in cands:
        base = cand / sid / "subagents"
        if not base.is_dir():
            continue
        out.extend(sorted(base.glob("**/agent-*.jsonl")))
    return out


def collect_sessions(root: str, warnings: list) -> list:
    """root 配下に帰属するセッション一覧を返す（採用行の有無はまだ見ない）。"""
    cands = candidate_project_dirs(root)
    if not cands:
        warnings.append(
            f"~/.claude/projects に {root} 配下の候補ディレクトリが1件もありません"
            "（このディレクトリでのセッション履歴が無い、または projects ディレクトリが"
            "別の場所にある可能性）。集計結果は 0 セッションになります。"
        )
    sessions = []
    all_main_sids = set()
    adopted = {}

    for cand in cands:
        for jf in sorted(cand.glob("*.jsonl")):
            sid = jf.stem
            all_main_sids.add(sid)
            meta = _scan_main_meta(jf)
            first_cwd = meta["first_cwd"]
            if first_cwd is None:
                warnings.append(f"cwd が読めず帰属不能なセッション: {sid}（{jf}）")
                continue
            if not _under(first_cwd, root):
                continue
            outside = any(not _under(c, root) for c in meta["cwds"])
            # lastPrompt にもハーネス注入メッセージが入りうるので、ai-title が
            # 無いときは fallback 経路と同じフィルタ（_clean_title）を通す。
            # 弾かれた場合は空にして、後段の first_real_user_text へ委ねる。
            title = meta["ai_title"] or ""
            if not title and meta["last_prompt"]:
                cleaned = _clean_title(meta["last_prompt"])
                title = "" if cleaned == "(タイトルなし)" else cleaned
            s = Session(
                session_id=sid,
                first_cwd=first_cwd,
                main_path=jf,
                tfiles=[jf],
                title=title,
                outside_cwd=outside,
            )
            adopted[sid] = s
            sessions.append(s)

    for sid, s in adopted.items():
        s.tfiles.extend(_subagent_files(cands, sid))

    # 孤児 subagent（採用セッションに main が無い uuid ディレクトリ）
    for cand in cands:
        for sub in sorted(cand.glob("*/subagents")):
            sid = sub.parent.name
            if sid in adopted:
                continue
            files = sorted(sub.glob("**/agent-*.jsonl"))
            if not files:
                continue
            first_cwd = None
            for f in files:
                first_cwd = _first_cwd_of(f)
                if first_cwd:
                    break
            if not _under(first_cwd, root):
                continue
            existing = next((x for x in sessions if x.session_id == sid), None)
            if existing is not None:
                existing.tfiles.extend(files)
                continue
            sessions.append(
                Session(
                    session_id=sid,
                    first_cwd=first_cwd,
                    main_path=None,
                    tfiles=list(files),
                    title="（別ディレクトリ起動セッションの subagent 分）",
                    orphan=True,
                )
            )

    return sessions


# ---------------------------------------------------------------------------
# usage 集計
# ---------------------------------------------------------------------------

@dataclass
class Aggregation:
    period: Period
    root: str
    sessions: list                      # 採用行を持つ Session（コスト降順）
    all_rows: list
    report: object                      # 全体 Report
    at: date
    usd_jpy: float
    active_sec_total: float
    daily: dict                         # {date(JST): {model: usd}}
    repo_costs: dict                    # {repo: usd}
    dropped_no_timestamp: int
    warnings: list                       # 異常・要注意（数が少ないほど良い）
    cross_session_dups: int = 0          # fork/resume でコピーされ、別セッション側に寄せた行数
    dup_session_ids: list = field(default_factory=list)
    # 注記: 異常ではないが読み手が知っておくべき前提。警告と混ぜると
    # 「毎回出る文」が本物の警告を薄めるため、出力上も別扱いにする。
    notes: list = field(default_factory=list)


def _file_order_key(p) -> tuple:
    """transcript ファイルの走査順キー（作成時刻の昇順 → パス）。

    fork/resume の子セッション jsonl には親の履歴が uuid ごとコピーされている。
    ``lib.Accumulator`` は uuid グローバル排除で「先に走査した側」に行を帰属させるので、
    走査順が変わるとセッション別金額が入れ替わってしまう（合計は不変）。
    ここで**ファイル作成時刻の昇順**に固定することで、
    (1) 実行ごとに結果が変わらない、(2) コピー元＝先に作られた親セッションへ帰属する、
    の 2 点を担保する。作成時刻が取れない環境ではパス順にフォールバックする。
    """
    try:
        st = os.stat(p)
        birth = getattr(st, "st_birthtime", None)
        if birth is None:
            birth = st.st_ctime
    except OSError:
        birth = float("inf")
    return (float(birth), str(p))


def _session_of_file_map(sessions: list) -> dict:
    m = {}
    for s in sessions:
        for p in s.tfiles:
            m[str(p)] = s.session_id
    return m


# ハーネスが user ロールで注入する疑似メッセージの「タグ形式」。
# 実データ（~/.claude/projects の user メッセージ全走査）では
# <task-notification> / <local-command-caveat> / <command-name> /
# <local-command-stdout> / <scheduled-task name=...> / <command-message> /
# <bash-input> / <bash-stdout> などが実在し、種類は Claude Code の更新で増える。
# 個別列挙では追随できないため、形で一括して弾く。ハーネスのタグは例外なく
# ハイフン／アンダースコア区切りの複合語（task-notification, bash-input,
# ide_opened_file …）なので、それを条件にする。こうすると <div> や <html> の
# ような素の HTML タグで始まる人間の発話を巻き込まない。
_HARNESS_TAG_RE = re.compile(r"^<[a-z][a-z0-9]*[_-][a-z0-9_-]*(?:\s[^>]*)?/?>")

# タグを持たない注入メッセージ（ハーネス・hook・自動継続が出す定型文）。
_CAVEAT_PREFIXES = (
    "Caveat: The messages below",
    "Stop hook feedback:",
    "Base directory for this skill:",
    "This session is being continued from",
    "[Request interrupted",
    "[Your previous response",
    "[Image:",
    "Your tool call was malformed",
    "# Autonomous loop",
)


# 定期実行（cron）で起動されたセッションは人間の発話を持たないが、
# 起動タスク名がそのままセッションの内容を表すのでタイトルに使う。
_SCHEDULED_TASK_RE = re.compile(r'^<scheduled-task\s[^>]*name="([^"]+)"')


def _is_caveat(text: str) -> bool:
    """タイトルに出してはいけないハーネス注入メッセージか。"""
    return bool(_HARNESS_TAG_RE.match(text)) or text.startswith(_CAVEAT_PREFIXES)


def is_human_utterance(obj: dict, text: Optional[str]) -> bool:
    """レコードとその本文が「人間が書いた発話」かどうか（要約入力の採否判定）。

    判定は既存ロジックの再利用に徹する（複製しない）:
    - 非発話行（isMeta / isSidechain / tool_result / スラッシュコマンド展開）は
      ``cost_lib._is_human_prompt``
    - ハーネス注入メッセージ（``<task-notification>`` 等）は ``_is_caveat``

    digest.py から呼ぶ。private 関数が将来消えても走査が止まらないよう getattr で退避。
    """
    t = (text or "").strip()
    if not t:
        return False
    is_human_fn = getattr(lib, "_is_human_prompt", None)
    if callable(is_human_fn) and not is_human_fn(obj):
        return False
    return not _is_caveat(t)


def _harness_derived_title(text: str) -> Optional[str]:
    """注入メッセージのうち、内容を表すものからタイトルを作れる場合だけ作る。"""
    m = _SCHEDULED_TASK_RE.match(text)
    if m:
        return f"(定期実行) {m.group(1)}"
    return None


def _clean_title(text: Optional[str], limit: int = 80) -> str:
    """ハーネスが注入する疑似ユーザーメッセージをタイトルに出さない。

    スラッシュコマンドの展開本文（``# /cmd — 概要`` で始まるコマンド定義 Markdown）は
    見出し行だけを採る。そのまま繋ぐとタイトルが定義ファイルのダンプになるため。
    人間が書いた見出し付きの発話は本文まで含めたいので、``# /`` 始まりに限定する。
    """
    t = (text or "").strip()
    if not t or _is_caveat(t):
        return "(タイトルなし)"
    if t.startswith("# /"):
        head = t.split("\n", 1)[0].strip()
        if head.lstrip("#").strip():
            return head[:limit]
    return t.replace("\n", " ")[:limit]


def first_real_user_text(
    tfiles,
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int = 80,
    max_scan: int = 60,
) -> Optional[str]:
    """窓内のユーザーメッセージを古い順に走査し、タイトルに使える最初の text を返す。

    cost_lib.find_first_user_text は候補を1件しか返さないため、先頭が
    ``<local-command-caveat>`` のようなハーネス注入メッセージだとタイトルが常に
    「(タイトルなし)」になる。ここでは候補を max_scan 件まで見て注入系を飛ばす。
    抽出・時刻判定・人間プロンプト判定は cost_lib のものを再利用する（複製しない）。

    採用の優先順は次の3段。人間の発話を必ず優先しつつ、それが無いセッション
    （定期実行やスラッシュコマンド起動だけのセッション）でも内容が分かるようにする。

    1. 人間プロンプト（``lib._is_human_prompt``）で、注入系プレフィックスを持たないもの
    2. ``<scheduled-task name="X">`` から作る ``(定期実行) X``
       — 定期実行セッションは人間の発話を持たないため、タスク名で識別する
    3. それ以外（isMeta 等）で、注入系プレフィックスを持たないもの
       — スラッシュコマンドの展開本文（``# /cmd — 概要``）はここに該当する

    候補は main jsonl のみを渡すこと（呼び出し側の責務）。サブエージェントの
    transcript を混ぜると、ハーネスがエージェントへ渡す指示文が候補を占める。
    """
    candidates = []
    for tf in tfiles:
        path = Path(getattr(tf, "path", tf))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"user"' not in line:
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
                            dt = lib.parse_iso(ts)
                        except (ValueError, TypeError):
                            dt = None
                    # 窓は集計側と同じ半開区間 [since, until) に揃える。
                    if since is not None and dt is not None and dt < since:
                        continue
                    if until is not None and dt is not None and dt >= until:
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    # isMeta / isSidechain / tool_result などの非発話行の判定は、
                    # cost_lib の実データ検証済み判定に委ねる（自前で再実装しない）。
                    # private 関数のため、将来消えても走査が止まらないよう getattr で退避。
                    # ここでは捨てずに優先度を下げるだけにする（上の docstring 参照）。
                    is_human_fn = getattr(lib, "_is_human_prompt", None)
                    human = bool(is_human_fn(obj)) if callable(is_human_fn) else True
                    text = lib._extract_text(msg.get("content"))
                    if text:
                        candidates.append(
                            (dt or datetime.min.replace(tzinfo=lib.UTC), human, text)
                        )
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    head = candidates[:max_scan]

    # 1. 人間の発話（注入系でないもの）
    for _, human, raw in head:
        text = raw.strip()
        if human and text and not _is_caveat(text):
            # 改行の畳み込み・見出し行の抽出は _clean_title に集約する。
            return text[:limit * 4]
    # 2. 注入メッセージのうち内容を表せるもの（定期実行のタスク名）。
    #    人間の発話が無いセッションでは、後続の本文ダンプよりこちらが識別しやすい。
    for _, _, raw in head:
        derived = _harness_derived_title(raw.strip())
        if derived:
            return derived
    # 3. それ以外（isMeta のスラッシュコマンド展開本文など）
    for _, human, raw in head:
        text = raw.strip()
        if not human and text and not _is_caveat(text):
            return text[:limit * 4]
    return None


def _model_tokens(m) -> int:
    return (
        m.input_tokens + m.cache_write_5m + m.cache_write_1h
        + m.cache_read_tokens + m.output_tokens
    )


def _unknown_tokens_of(rep) -> tuple:
    """(単価未収載モデルのトークン数, 全トークン数) を返す。"""
    unk = sum(_model_tokens(m) for m in rep.models if not m.known)
    tot = sum(_model_tokens(m) for m in rep.models)
    return unk, tot


def unknown_tokens(rep) -> int:
    return _unknown_tokens_of(rep)[0]


def unknown_note(agg) -> str:
    """単価未収載モデルがあるときだけ、1行の注記文字列を返す（無ければ空文字）。"""
    unk, tot = _unknown_tokens_of(agg.report)
    if not unk:
        return ""
    pct = (unk / tot * 100) if tot else 0.0
    return (
        f"うち {unk:,} tok（{pct:.1f}%）は単価未収載のため未計上 — 実額はこれより大きい"
    )


def unknown_badge(agg) -> str:
    """summary_card 用の短い注記（長いと右カラムの数値と重なるため別に持つ）。"""
    unk, tot = _unknown_tokens_of(agg.report)
    if not unk:
        return ""
    pct = (unk / tot * 100) if tot else 0.0
    return f"⚠ 実額はこれより大きい（{pct:.1f}% 未計上）"


def aggregate_all(
    root: str,
    period: Period,
    sessions: list,
    pricing: dict,
    usd_jpy: float,
    at: date,
    warnings: list,
    with_active: bool = True,
    gap_max_sec: float = 900.0,
) -> Aggregation:
    file_to_sid = _session_of_file_map(sessions)
    by_sid = {s.session_id: s for s in sessions}

    acc = lib.Accumulator()   # 全セッションで単一インスタンスを共有（resume/fork 重複対策）
    dropped_no_timestamp = 0

    # 走査順を「ファイル作成時刻の昇順」に固定する（_file_order_key の docstring 参照）。
    seen_paths = set()
    ordered_files = []
    for s in sessions:
        for p in s.tfiles:
            key = str(p)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            ordered_files.append(p)
    ordered_files.sort(key=_file_order_key)

    owner_of_key: dict = {}          # dedup キー → 最初に取り込んだセッション
    cross_session_dups = 0
    dup_sids: set = set()
    # (dedup キー, セッション) → そのグループの窓内 timestamp の (最小, 最大)。
    # Accumulator が採用するのは requestId グループのうち output_tokens 最大の行
    # （= ストリーミングの最終行）なので、採用行の ts だけを見ると同一 API 応答の
    # 先頭行より数秒〜1分後ろにずれる。開始時刻はグループの最小 ts から取る。
    # fork でコピーされた親の行を子の開始時刻に混ぜないよう、キーは**セッション込み**。
    key_span: dict = {}

    for p in ordered_files:
        sid_of_file = file_to_sid.get(str(p))
        rows, _ = lib.iter_usage(p, 0)
        for row in rows:
            ts_raw = row.get("timestamp")
            if not ts_raw:
                dropped_no_timestamp += 1
                continue
            try:
                ts = lib.parse_iso(ts_raw)
            except (ValueError, TypeError):
                dropped_no_timestamp += 1
                continue
            if ts < period.since or ts >= period.until:
                continue
            row["_ts"] = ts
            dkey = row.get("requestId") or row.get("uuid")
            if dkey is not None:
                skey = (dkey, sid_of_file)
                span = key_span.get(skey)
                key_span[skey] = (
                    (ts, ts) if span is None
                    else (min(span[0], ts), max(span[1], ts))
                )
                prev = owner_of_key.get(dkey)
                if prev is None:
                    owner_of_key[dkey] = sid_of_file
                elif prev != sid_of_file:
                    # fork/resume でコピーされた行。先に作られた側（＝コピー元）に寄せる。
                    cross_session_dups += 1
                    dup_sids.add(prev)
                    dup_sids.add(sid_of_file)
            acc.add(row)

    all_rows = acc.rows()

    for row in all_rows:
        sid = file_to_sid.get(row.get("source_file"))
        s = by_sid.get(sid)
        if s is None:
            continue
        s.rows.append(row)

    live = [s for s in sessions if s.rows]

    for s in live:
        if not s.title:
            # タイトルの候補は main jsonl のみから採る。サブエージェントの
            # transcript には人間の発話が無く、ハーネスがエージェントへ渡す
            # 指示文（[SYSTEM NOTIFICATION …] / The coordinator sent a message …
            # など）が user ロールで入るため、混ぜると候補列がそれで埋まる。
            title_files = [s.main_path] if s.main_path else s.tfiles
            s.title = _clean_title(
                first_real_user_text(title_files, period.since, period.until, 80)
            )
        s.report = lib.aggregate(s.rows, pricing, at, usd_jpy)
        starts, ends = [], []
        for r in s.rows:
            ts = r.get("_ts")
            if not ts:
                continue
            dkey = r.get("requestId") or r.get("uuid")
            span = key_span.get((dkey, s.session_id)) if dkey is not None else None
            starts.append(span[0] if span else ts)
            ends.append(span[1] if span else ts)
        if starts:
            s.start_utc = min(starts)
            s.end_utc = max(ends)

    report = lib.aggregate(all_rows, pricing, at, usd_jpy)

    # 実処理時間
    active_total = 0.0
    if with_active:
        invoking = lib.current_session_id()
        for s in live:
            scan = lib.scan_activity(
                s.tfiles, gap_max_sec=gap_max_sec, invoking_session_id=invoking
            )
            s.active_sec = lib.active_seconds(scan.intervals, period.since, period.until)
        all_tfiles = []
        seen = set()
        # 全体値は仕様どおり**全セッション**の tfiles をまとめて union を取る
        # （窓内に課金行が無くても活動イベントだけあるセッションを落とさない）。
        for s in sessions:
            for p in s.tfiles:
                if str(p) not in seen:
                    seen.add(str(p))
                    all_tfiles.append(p)
        scan_all = lib.scan_activity(
            all_tfiles, gap_max_sec=gap_max_sec, invoking_session_id=invoking
        )
        active_total = lib.active_seconds(scan_all.intervals, period.since, period.until)

    # 日別×モデル別（JST に変換してから日付を切る）
    daily_rows: dict = {}
    for row in all_rows:
        d = lib.to_jst(row["_ts"]).date()
        daily_rows.setdefault(d, []).append(row)
    daily = {}
    for d, rows in daily_rows.items():
        rep = lib.aggregate(rows, pricing, at, usd_jpy)
        # lib.aggregate のバケツは**生モデル名**なので、resolve 後に同名になる複数
        # バケツ（例 claude-sonnet-5 と claude-sonnet-5-20260101）が並びうる。
        # 辞書内包表記だと後勝ちで片方を捨ててしまうため、必ず加算する。
        bucket: dict = {}
        for m in rep.models:
            name = m.resolved or m.model
            bucket[name] = bucket.get(name, 0.0) + m.cost_usd
        daily[d] = bucket

    # リポジトリ別
    repo_costs: dict = {}
    for s in live:
        repo_costs[repo_of(s, root)] = repo_costs.get(repo_of(s, root), 0.0) + s.report.total_usd

    live.sort(key=lambda s: s.report.total_usd, reverse=True)

    if report.unknown_models:
        unk_tok, tot_tok = _unknown_tokens_of(report)
        pct = (unk_tok / tot_tok * 100) if tot_tok else 0.0
        warnings.append(
            "pricing 未収載のモデルがあり $0 で計上されています: "
            + ", ".join(report.unknown_models)
            + f"（未計上トークン {unk_tok:,} tok = 全体の {pct:.1f}%。"
            "合計コストはその分だけ過小です）"
        )
    if cross_session_dups:
        warnings.append(
            f"fork/resume でコピーされた課金行が {cross_session_dups} 件あり、"
            "コピー元（先に作成された transcript）のセッションに計上しました。"
            "合計・モデル別・日別は影響しませんが、対象セッションの per-session 値は"
            "この規則に依存します: " + ", ".join(sorted(x for x in dup_sids if x))
        )
    if dropped_no_timestamp:
        warnings.append(
            f"timestamp を持たない課金行を {dropped_no_timestamp} 件除外しました"
            "（期間判定不能なため）。"
        )
    orphans = [s for s in live if s.orphan]
    if orphans:
        warnings.append(
            "別ディレクトリ起動セッションの subagent 分を計上しました（"
            + ", ".join(s.session_id for s in orphans)
            + "）。"
        )
    outside = [s for s in live if s.outside_cwd]
    if outside:
        warnings.append(
            f"セッション途中で root 外の cwd が現れたセッションが {len(outside)} 件あります"
            "（セッション単位で計上）: " + ", ".join(s.session_id for s in outside)
        )
    for name, bday in pricing_boundaries_in_period(period, pricing):
        warnings.append(
            f"期間内（{bday.isoformat()}）に {name} の単価改定日がありますが、"
            f"単価適用日は期間全体で1つ（at={at.isoformat()}）です。"
            f"改定日の前後で単価が異なる分だけ金額がずれます"
            "（正確に出したい場合は改定日で期間を分けて2回実行してください）。"
        )
    # 単価表より前の期間を集計する場合、cost_lib の stale 判定
    # （at - as_of > stale_after_days）は負値になり発火しない。過去分を現行単価で
    # 計算していることが黙って通るので、こちら側で明示する。
    # 判定は期間の「開始日」で行う。終端（at）で見ると
    # 「--from 過去日 --to 今日」のように as_of をまたぐ期間で警告が落ちる。
    notes = []
    as_of_s = pricing.get("as_of")
    if as_of_s:
        try:
            as_of_d = lib.parse_date(as_of_s)
        except Exception:
            as_of_d = None
        start_d = lib.to_jst(period.since).date()
        if as_of_d and start_d < as_of_d:
            notes.append(
                f"集計期間の開始日（{start_d.isoformat()}）は単価表の as_of"
                f"（{as_of_d.isoformat()}）より前です。当時の実単価ではなく"
                f"現在の単価表（適用日 at={at.isoformat()}）で計算しています"
                "（単価表は履歴を持たないため、期間中に単価改定・モデル追加が"
                "あった場合はその分ずれます）。"
            )
    if report.stale:
        warnings.append(
            f"pricing.json が古い可能性があります（as_of={report.pricing_as_of}）。"
        )

    return Aggregation(
        period=period,
        root=root,
        sessions=live,
        all_rows=all_rows,
        report=report,
        at=at,
        usd_jpy=usd_jpy,
        active_sec_total=active_total,
        daily=daily,
        repo_costs=repo_costs,
        dropped_no_timestamp=dropped_no_timestamp,
        warnings=warnings,
        cross_session_dups=cross_session_dups,
        dup_session_ids=sorted(x for x in dup_sids if x),
        notes=notes,
    )


def repo_of(s: Session, root: str) -> str:
    try:
        rel = os.path.relpath(s.first_cwd, root)
    except ValueError:
        return "(root)"
    if rel == "." or rel.startswith(".."):
        return "(root)"
    return rel.split(os.sep)[0]


# ---------------------------------------------------------------------------
# 出力（CSV / Markdown）
# ---------------------------------------------------------------------------

def _fmt_hm(seconds: float) -> str:
    # 分は**切り捨て**。lib.fmt_duration（summary.md / PNG 側）と同じ丸めにして、
    # 同じ量が CSV と md で違う値にならないようにする。
    total_min = int(seconds // 60)
    return f"{total_min // 60}:{total_min % 60:02d}"


def _csv_safe(text: str) -> str:
    """表計算ソフトが数式として評価するセル（CSV インジェクション）を無害化する。

    タイトルも要約も transcript の内容（＝人が書いた文字列や LLM 出力）に由来するため、
    ``=HYPERLINK(...)`` のような文字列が入りうる。Excel / Numbers はセル先頭の
    ``= + - @`` を数式の開始と見るので、先頭に ``'`` を足して文字列に固定する。
    """
    t = text or ""
    return "'" + t if t[:1] in ("=", "+", "-", "@") else t


def _jst_str(dt: Optional[datetime]) -> str:
    return lib.to_jst(dt).strftime("%Y-%m-%d %H:%M") if dt else ""


def _tokens_of(rep) -> tuple:
    i = sum(m.input_tokens for m in rep.models)
    w5 = sum(m.cache_write_5m for m in rep.models)
    w1 = sum(m.cache_write_1h for m in rep.models)
    r = sum(m.cache_read_tokens for m in rep.models)
    o = sum(m.output_tokens for m in rep.models)
    return i, w5, w1, r, o


def summary_of(summaries: Optional[dict], sid: str) -> str:
    """要約テキスト（無ければ空文字）。``summaries`` は {sid: {"summary": str, ...}}。"""
    if not summaries:
        return ""
    e = summaries.get(sid) or {}
    return (e.get("summary") or "").replace("\n", " ").strip()


def phases_of(summaries: Optional[dict], sid: str) -> list:
    if not summaries:
        return []
    e = summaries.get(sid) or {}
    out = e.get("phases") or []
    return [str(x).replace("\n", " ").strip() for x in out if str(x).strip()]


def build_sessions_csv(agg: Aggregation, with_active: bool, summaries: Optional[dict] = None) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([
        "session_id", "repo", "title", "first_cwd", "start_jst", "end_jst",
        "active_time", "api_calls", "input_tokens", "cache_write_5m",
        "cache_write_1h", "cache_read", "output_tokens", "total_tokens",
        "cost_usd", "cost_jpy", "unknown_tokens", "models", "summary",
    ])
    for s in agg.sessions:
        i, w5, w1, r, o = _tokens_of(s.report)
        total = i + w5 + w1 + r + o
        # 単価未収載モデルには * を付けて、その行のコストが過小であることを示す。
        models = ";".join(sorted({
            (m.resolved or m.model) + ("" if m.known else "*") for m in s.report.models
        }))
        w.writerow([
            s.session_id, repo_of(s, agg.root),
            _csv_safe(s.title or "(タイトルなし)"), s.first_cwd,
            _jst_str(s.start_utc), _jst_str(s.end_utc),
            _fmt_hm(s.active_sec) if with_active else "",
            len(s.rows), i, w5, w1, r, o, total,
            f"{s.report.total_usd:.4f}", int(round(s.report.total_jpy)),
            unknown_tokens(s.report), models,
            _csv_safe(summary_of(summaries, s.session_id)),
        ])
    # 注記行は「明細の後・TOTAL の前」に置く。仕様上 TOTAL は文字どおり最終行で、
    # tail -1 / 末尾行の読み取りで合計が取れることを保証する（0 セッションでも同様）。
    # 行ごとの丸めのため、明細を SUM しても TOTAL 行とは端数分ずれる。
    w.writerow([
        "# 注記",
        "cost_usd / cost_jpy は行ごとに丸めています。TOTAL 行は丸め前の全体値から"
        "算出しているため、明細を SUM すると端数分（数円程度）ずれます。",
    ] + [""] * 17)
    if with_active:
        w.writerow([
            "# 注記",
            "active_time はセッションごとの実処理時間です。並行実行した時間帯や、"
            "fork でコピーされた親セッションの活動区間が重なるため、列を SUM しても "
            "TOTAL 行（全セッションを union した実処理時間）とは一致しません"
            "（列の合計は常に TOTAL 以上になります）。",
        ] + [""] * 17)
    unk_tok, tot_tok = _unknown_tokens_of(agg.report)
    if unk_tok:
        pct = (unk_tok / tot_tok * 100) if tot_tok else 0.0
        note = (
            f"単価未収載モデル（models 列の *）を $0 で計上しています: "
            f"{', '.join(agg.report.unknown_models)}。未計上トークン {unk_tok:,} tok "
            f"= 全体の {pct:.1f}%。コストはその分だけ過小です。"
        )
        # 表計算で列がずれないよう、ヘッダと同じ列数に揃える。
        w.writerow(["# 注記", note] + [""] * 17)
    gi, gw5, gw1, gr, go = _tokens_of(agg.report)
    w.writerow([
        "TOTAL", "", "", "", "", "",
        _fmt_hm(agg.active_sec_total) if with_active else "",
        len(agg.all_rows), gi, gw5, gw1, gr, go, gi + gw5 + gw1 + gr + go,
        f"{agg.report.total_usd:.4f}", int(round(agg.report.total_jpy)),
        unknown_tokens(agg.report), "", "",
    ])
    return buf.getvalue()


def build_sessions_by_model_csv(agg: Aggregation) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([
        "session_id", "repo", "model", "input_tokens", "cache_write_5m",
        "cache_write_1h", "cache_read", "output_tokens", "cost_usd", "known",
    ])
    for s in agg.sessions:   # 既にコスト降順
        for name, agg_m in _merged_models(s.report):
            w.writerow([
                s.session_id, repo_of(s, agg.root), name,
                agg_m["input"], agg_m["w5"], agg_m["w1"],
                agg_m["read"], agg_m["output"],
                f"{agg_m['usd']:.6f}", "true" if agg_m["known"] else "false",
            ])
    return buf.getvalue()


def _merged_models(rep) -> list:
    """resolve 後のモデル名で合算した内訳を [(name, dict), ...]（名前順）で返す。

    ``lib.aggregate`` のバケツは生モデル名なので、``claude-sonnet-5`` と
    ``claude-sonnet-5-20260101`` のように resolve 後に同名となる複数バケツが並びうる。
    そのまま出すと「1行 = セッション×モデル」が破れ、同名行が重複する。
    """
    merged: dict = {}
    for m in rep.models:
        name = m.resolved or m.model
        d = merged.setdefault(name, {
            "input": 0, "w5": 0, "w1": 0, "read": 0, "output": 0,
            "usd": 0.0, "known": True,
        })
        d["input"] += m.input_tokens
        d["w5"] += m.cache_write_5m
        d["w1"] += m.cache_write_1h
        d["read"] += m.cache_read_tokens
        d["output"] += m.output_tokens
        d["usd"] += m.cost_usd
        d["known"] = d["known"] and bool(m.known)
    return sorted(merged.items(), key=lambda t: t[0])


def model_totals(agg: Aggregation) -> list:
    """全体のモデル別内訳（(name, tokens, usd, known) のリスト、USD 降順）。

    resolve 後に同名となる複数バケツは合算する（_merged_models 参照）。
    """
    out = []
    for name, d in _merged_models(agg.report):
        tokens = d["input"] + d["w5"] + d["w1"] + d["read"] + d["output"]
        out.append((name, tokens, d["usd"], d["known"]))
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def build_summary_md(
    agg: Aggregation,
    label: str,
    with_active: bool,
    summaries: Optional[dict] = None,
    overview: str = "",
    summary_note: str = "",
    phase_counts: Optional[dict] = None,
) -> str:
    L = []
    A = L.append
    A(f"# 使用量レポート: {label}")
    A("")
    A("> コストは全モデルを**従量課金単価で仮計算した参考値**です"
      "（Fable 等は実際にはサブスク込み。本レポートは全量を従量単価で仮計算）。")
    A("")
    A(f"- 対象ディレクトリ: `{agg.root}`（子孫ディレクトリを含む）")
    A(f"- 期間: {agg.period.describe()}")
    A(f"- 単価適用日: {agg.at.isoformat()} / 為替: {agg.usd_jpy} 円/USD"
      f" / pricing as_of: {agg.report.pricing_as_of}")
    A("")
    # LLM 要約が有効なときだけ、期間全体の総括を最初に置く（無効・失敗時は節ごと出さない）。
    if overview:
        A("## この期間の作業")
        A("")
        for line in overview.splitlines():
            if line.strip():
                A(line.rstrip())
        A("")
    A("## 合計")
    A("")
    A("| 項目 | 値 |")
    A("|---|---:|")
    A(f"| コスト（従量仮計算） | ${lib.fmt_usd(agg.report.total_usd, 2)} / "
      f"¥{lib.fmt_jpy(agg.report.total_jpy)} |")
    A(f"| セッション数 | {len(agg.sessions)} |")
    A(f"| API 呼び出し数（dedup 後） | {len(agg.all_rows):,} |")
    i, w5, w1, r, o = _tokens_of(agg.report)
    A(f"| 入力トークン | {lib.fmt_tokens(i)} |")
    A(f"| キャッシュ書込 5m | {lib.fmt_tokens(w5)} |")
    A(f"| キャッシュ書込 1h | {lib.fmt_tokens(w1)} |")
    A(f"| キャッシュ読取 | {lib.fmt_tokens(r)} |")
    A(f"| 出力トークン | {lib.fmt_tokens(o)} |")
    A(f"| トークン合計 | {lib.fmt_tokens(i + w5 + w1 + r + o)} |")
    unk_tok, tot_tok = _unknown_tokens_of(agg.report)
    if unk_tok:
        pct = (unk_tok / tot_tok * 100) if tot_tok else 0.0
        A(f"| **うち未計上トークン（単価未収載）** | **{lib.fmt_tokens(unk_tok)}"
          f"（{pct:.1f}%）** |")
    A(f"| 実処理時間 | {lib.fmt_duration(agg.active_sec_total) if with_active else '（--no-active で未算出）'} |")
    A("")
    if with_active:
        A("> 実処理時間の合計は全セッションの活動区間を **union** した値です。"
          "セッション別（sessions.csv の `active_time` 列・下のセッション一覧）は"
          "並行実行や fork によるコピー分が重なるため、足し上げても上の合計とは"
          "一致しません（常に合計以上になります）。")
        A("")
    if unk_tok:
        A(f"> **注意: 上のコストは過小です。** pricing 未収載のモデル"
          f"（{', '.join(agg.report.unknown_models)}）を $0 で計上しており、"
          f"全トークンの {(unk_tok / tot_tok * 100) if tot_tok else 0:.1f}%"
          f"（{lib.fmt_tokens(unk_tok)}）が金額に反映されていません。")
        A("")

    A("## リポジトリ別")
    A("")
    if agg.repo_costs:
        A("| リポジトリ | コスト USD | 比率 |")
        A("|---|---:|---:|")
        tot = sum(agg.repo_costs.values()) or 1.0
        for name, usd in sorted(agg.repo_costs.items(), key=lambda t: t[1], reverse=True):
            A(f"| {name} | ${lib.fmt_usd(usd, 2)} | {usd / tot * 100:.1f}% |")
    else:
        A("（対象なし）")
    A("")

    A("## モデル別")
    A("")
    if agg.report.models:
        A("| モデル | トークン | コスト USD | 単価収載 |")
        A("|---|---:|---:|---|")
        for name, tokens, usd, known in model_totals(agg):
            A(f"| {name} | {lib.fmt_tokens(tokens)} | ${lib.fmt_usd(usd, 2)} | "
              f"{'○' if known else '×（未収載・$0 計上）'} |")
    else:
        A("（対象なし）")
    A("")

    A("## セッション一覧（コスト降順）")
    A("")
    if agg.sessions:
        # 各セッションの下に要約行（多フェーズはさらにネスト）を添えるため、
        # 表ではなくリストで出す（表の行間に子行は置けないため）。
        for s in agg.sessions:
            title = (s.title or "(タイトルなし)").replace("\n", " ")
            A(f"- `{s.session_id[:8]}` **{repo_of(s, agg.root)}** — {title}")
            A(f"  - {_jst_str(s.start_utc)} 開始 / 実処理 "
              f"{_fmt_hm(s.active_sec) if with_active else '-'} / "
              f"${lib.fmt_usd(s.report.total_usd, 2)}")
            summ = summary_of(summaries, s.session_id)
            if summ:
                A(f"  - 要約: {summ}")
            phases = phases_of(summaries, s.session_id)
            if phases:
                # 要約側のフェーズ行は digest のフェーズを畳んだものなので、
                # 「これで全部」に見せないよう件数の対応を明示する。
                n_digest = (phase_counts or {}).get(s.session_id)
                if n_digest is not None and n_digest != len(phases):
                    A(f"  - フェーズ（ダイジェスト {n_digest} 件 → 要約 {len(phases)} 件）:")
                else:
                    A("  - フェーズ:")
                for i_ph, ph in enumerate(phases, 1):
                    A(f"    - {i_ph}. {ph}")
    else:
        A("この期間に該当するセッションはありません。")
    A("")

    A("## 警告")
    A("")
    if agg.warnings:
        for wmsg in agg.warnings:
            A(f"- {wmsg}")
    else:
        A("- なし")
    A("")

    # 注記は「異常ではないが前提として知っておくべきこと」。警告と分けて出す。
    A("## 注記")
    A("")
    A("- 表中の $ は小数2桁に丸めているため、内訳を足し上げると合計と数セント"
      "ずれることがあります（合計は丸め前の値から算出）。")
    for nmsg in agg.notes:
        A(f"- {nmsg}")
    if summary_note:
        A(f"- {summary_note}")
    A("")
    A(f"生成: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST")
    A("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 書込ヘルパ（すべて lib.atomic_write_* 経由）
# ---------------------------------------------------------------------------

def write_csv(path: Path, text: str) -> None:
    """Excel/Numbers で日本語が化けないよう utf-8-sig で書く。"""
    lib.atomic_write_bytes(path, text.encode("utf-8-sig"))


def write_text(path: Path, text: str) -> None:
    lib.atomic_write_text(path, text)


def write_png(path: Path, data: bytes) -> None:
    lib.atomic_write_bytes(path, data)


def png_size(data: bytes) -> tuple:
    """PNG バイト列から (width, height) を読む（IHDR）。不正なら (0, 0)。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return (0, 0)
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


# ---------------------------------------------------------------------------
# ダイジェスト / 要約（digest.py・summarize.py への薄い入口）
# ---------------------------------------------------------------------------
# digest.py 側は usage_lib を import するため、ここでの import は**関数内の遅延
# import** にして循環を避ける（charts.py と違い matplotlib 依存は無い）。

def build_digests(sessions: list, period: Period, phase_gap_min: int = 30) -> dict:
    import digest
    return digest.build_digests(sessions, period, phase_gap_min)


def digests_json(digests: dict, root: str, period: Period, phase_gap_min: int) -> str:
    import digest
    return digest.digests_json(digests, root, period, phase_gap_min)


def deterministic_summaries(digests: dict) -> dict:
    """LLM 無しの既定要約（{sid: {"summary": str, "phases": []}}）。"""
    import digest
    return {
        sid: {"summary": digest.deterministic_summary(d), "phases": []}
        for sid, d in digests.items()
    }


def summaries_dir() -> Path:
    """LLM 要約キャッシュの置き場（``usage-report/var/summaries``）。

    ``USAGE_REPORT_VAR_DIR`` で差し替えられる。偽 ``claude`` を使う縮退テストが
    本番キャッシュへスタブ要約を書き込むと、以降の実行が黙ってそれを再利用して
    捏造要約を出す（実際に検証中へ混入した）。テストは必ずこの環境変数で隔離する
    （cost_lib の ``FCM_PROJECTS_DIR`` / ``FABLE_COST_MANAGER_ROOT`` と同じ流儀）。
    """
    override = os.environ.get("USAGE_REPORT_VAR_DIR")
    base = Path(override).expanduser() if override else USAGE_REPORT_ROOT / "var"
    return base / "summaries"


def default_out_dir(root: str, label: str, period_label: str, now_jst: Optional[datetime] = None) -> Path:
    now_jst = now_jst or datetime.now(JST)
    name = f"{now_jst.strftime('%Y%m%d-%H%M')}-{lib.make_slug(label)}-{period_label}"
    return USAGE_REPORT_ROOT / "reports" / now_jst.strftime("%Y") / now_jst.strftime("%m") / name
