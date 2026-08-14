#!/usr/bin/env python3
"""決定論ダイジェストを LLM に渡してセッション要約・期間総括を作る（第2段・第3段）。

方針:
- **標準ライブラリのみ**（``cost_lib`` の atomic write / 時刻ユーティリティは再利用）。
- ``claude -p`` を **1回だけ**呼ぶ（起動オーバーヘッドが 17〜26 秒あり、セッションごとに
  逐次呼ぶと非現実的なため、未キャッシュ分をまとめて1バッチにする）。
- ``claude`` が無い / 失敗 / タイムアウト / パース失敗はすべて**警告を1件積んで縮退**し、
  要約なしでレポートを出す（呼び出し側の exit 0 を維持する）。
- キャッシュは ``usage-report/var/summaries/<key[:2]>/<key>.json``。キーに transcript の
  mtime/size と期間・モデル・プロンプト版を含めるので、内容が変われば自然に無効化される。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_COST_SCRIPTS = _HERE.parent.parent.parent / "cost-manager" / "scripts"
if str(_COST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_COST_SCRIPTS))

import cost_lib as lib  # noqa: E402

PROMPT_VERSION = 3

PROMPT_MAX_PER_SESSION = 20     # LLM に渡す発話の件数
PROMPT_CHARS = 250              # 同・1件あたりの文字数
PHASE_MAX_PER_SESSION = 12      # 同・フェーズ行の件数（多フェーズは主要分だけ渡す）

# argv でプロンプトを渡すため（macOS の ARG_MAX は約 1MB）1回の呼び出しの上限。
# これを超える規模は**発話を削るのではなくバッチを分割**する（発話は「実際に何を
# したか」を最も直接示す材料なので、削ると要約が痩せる）。
BATCH_MAX_BYTES = 300_000
BATCH_MAX_SESSIONS = 15         # 1回の応答 JSON が途中で切れないよう件数も抑える


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------

def _stat_of(path) -> tuple:
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError):
        return (0, 0)


def digest_fingerprint(digest: dict) -> str:
    """ダイジェスト本体のハッシュ。

    ``--phase-gap-min`` を変えるとフェーズ分割（= LLM への入力そのもの）が変わるが、
    transcript の mtime/size は変わらない。材料が変わったのに古い要約が返らないよう、
    ダイジェスト本文そのものをキーに含める。
    """
    try:
        body = json.dumps(digest or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = repr(digest)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def session_cache_key(session_id: str, main_path, period, model: str,
                      digest_fp: str = "", prompt_max: int = PROMPT_MAX_PER_SESSION) -> str:
    mtime_ns, size = _stat_of(main_path)
    seed = "|".join([
        session_id, str(mtime_ns), str(size),
        period.since.isoformat(), period.until.isoformat(),
        model, str(PROMPT_VERSION), digest_fp, str(prompt_max),
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def overview_cache_key(session_keys: list, period, model: str) -> str:
    seed = "|".join(["overview", period.label, model, str(PROMPT_VERSION)]
                    + sorted(session_keys))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / key[:2] / f"{key}.json"


def _read_cache(cache_dir: Path, key: str):
    p = _cache_path(cache_dir, key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _write_cache(cache_dir: Path, key: str, obj: dict) -> None:
    p = _cache_path(cache_dir, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lib.atomic_write_text(p, json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass          # キャッシュが書けないことは致命ではない（次回作り直すだけ）


# ---------------------------------------------------------------------------
# プロンプト
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
あなたは開発作業ログの要約者です。以下は Claude Code の各セッションから機械的に
抽出した材料（人間の発話・編集ファイル・ブランチ・コマンド・サブエージェント）です。
セッションのタイトルは会話の最初期に決まるため実作業を表していません。
**タイトルの言い換えではなく、材料から読み取れる実際の作業**を書いてください。

要件:
- 各セッションについて、**60字以内の日本語1文**で「実際に何をしたか」を書く。
  対象（リポジトリ・機能・ファイル群）と行為（実装・修正・調査・テスト追加・レビュー等）を含める。
- 「フェーズ」が示されているセッションは、フェーズごとの作業も**各40字以内の1行**で書く
  （phases 配列。フェーズが示されていないセッションは phases を空配列にする）。
- 最後に overview として、**このプロンプトに出てくる全セッション（要約対象＋参考として
  示した既要約分）を合わせた期間全体の総括**を **3〜5行**書く（改行区切り。何に時間を
  使ったか・主な成果が分かるように）。「対象なし」「要約済み」のようなメタな報告は書かない。
- 総括では**対象期間を言い換えない**（「〜週間」「N月X日〜」のような期間の要約・推測を書かない）。
  期間は下に明記したものが正であり、材料の時刻から期間を推測しないこと。
- 推測で埋めないこと。材料が乏しいセッションは短くてよい（無理に60字に伸ばさない）。
- **材料に出てこない固有名詞を書かない**（ライブラリ名・フレームワーク名・プラットフォーム名・
  製品名・バージョン番号）。文脈から補完しないこと。例: 材料が「Android のテスト」だけのときに
  「RxJava」「Hilt」等の具体名を足さない。対象を特定できないときは「アプリ側」「一部リポジトリ」の
  ように、材料に書かれている範囲の言葉で書く。書こうとしている固有名詞が材料の中にあるか
  必ず確かめること。
- phases は**必ず文字列の配列**にする（数値・真偽値・文字列単体・オブジェクトにしない）。
  フェーズが示されていないセッションは `"phases": []`。
- 出力は **JSON のみ**。前後に説明文・コードフェンスを付けない。

スキーマ:
{"sessions":[{"id":"<session_id>","summary":"...","phases":["...","..."]}],"overview":"...\\n..."}
"""


def _fmt_counts(items: list, key: str = "name") -> str:
    return ", ".join(f"{d.get(key)}({d.get('count')})" for d in items)


def select_phases(phases: list, limit: int) -> list:
    """多フェーズのセッションから「主要フェーズ」を決定論的に選ぶ。

    worktree を行き来するセッションでは 100 件超のフェーズが出るが、全件を渡しても
    出力側は数行しか返さない（入力だけ膨らんで網羅性は担保されない）。活動量
    （コマンド数 + ファイル数 + 発話数）の多い順に ``limit`` 件を採り、表示順は
    元の時系列に戻す。同数は index 昇順で割る（決定論）。
    """
    if len(phases) <= limit:
        return list(phases)

    def weight(ph):
        c = ph.get("commands") or {}
        n_cmd = sum(v for v in c.values() if isinstance(v, int))
        return (-(n_cmd + len(ph.get("files") or []) + len(ph.get("prompts") or [])),
                ph.get("index") or 0)

    picked = sorted(sorted(phases, key=weight)[:limit], key=lambda p: p.get("index") or 0)
    return picked


def render_digest(d: dict, prompt_max: int = PROMPT_MAX_PER_SESSION) -> str:
    L = []
    A = L.append
    A(f"### session id={d.get('session_id')}")
    if d.get("title"):
        A(f"タイトル(参考): {d['title']}")
    if d.get("start") or d.get("end"):
        A(f"稼働時刻: {str(d.get('start', ''))[:16]}〜{str(d.get('end', ''))[:16]}")
    branches = d.get("branches") or []
    if branches:
        A(f"ブランチ: {', '.join(branches[:8])}（切替 {d.get('switches', 0)} 回）")
    dirs = d.get("dirs") or []
    if dirs:
        A(f"編集ディレクトリ: {_fmt_counts(dirs[:6], 'path')}"
          f" / 編集ファイル数 {d.get('files_total', 0)}")
    files = d.get("files") or []
    if files:
        A("主な編集ファイル: " + ", ".join(x["path"] for x in files[:10]))
    c = d.get("commands") or {}
    A(f"コマンド: test {c.get('test', 0)} / git {c.get('git', 0)} / "
      f"build {c.get('build', 0)} / other {c.get('other', 0)}")
    ex = d.get("examples") or []
    if ex:
        A("代表コマンド:")
        for e in ex:
            A(f"  - {e}")
    if d.get("subagents"):
        A("サブエージェント: " + _fmt_counts(d["subagents"]))
    if d.get("skills"):
        A("スキル: " + _fmt_counts(d["skills"]))
    if d.get("refs"):
        A("参照 issue/PR: " + ", ".join(d["refs"]))
    if d.get("tool_counts"):
        A("ツール利用: " + _fmt_counts(d["tool_counts"][:6]))
    prompts = (d.get("prompts") or [])[:prompt_max]
    if prompts:
        A("人間の発話:")
        for p in prompts:
            A(f"  - {p[:PROMPT_CHARS]}")
    phases = d.get("phases") or []
    if phases:
        shown = select_phases(phases, PHASE_MAX_PER_SESSION)
        if len(shown) < len(phases):
            A(f"フェーズ: 全 {len(phases)} 件のうち主要 {len(shown)} 件"
              f"（phases 配列はここに示した {len(shown)} 件に対応させること）:")
        else:
            A(f"フェーズ（{len(phases)} 件。phases 配列は各フェーズに1行ずつ対応させること）:")
        for ph in shown:
            pc = ph.get("commands") or {}
            A(f"  - {ph.get('index')}: {ph.get('start', '')[:16]}〜{ph.get('end', '')[:16]}"
              f" branch={ph.get('branch') or '-'}"
              f" test={pc.get('test', 0)} git={pc.get('git', 0)} other={pc.get('other', 0)}")
            for f in (ph.get("files") or [])[:5]:
                A(f"      file: {f}")
            for p in (ph.get("prompts") or [])[:2]:
                A(f"      発話: {p}")
    return "\n".join(L)


def period_line(period) -> str:
    """プロンプト先頭に置く対象期間（総括が期間を推測して誤記するのを防ぐ）。"""
    if period is None:
        return ""
    try:
        since = lib.to_jst(period.since).strftime("%Y-%m-%d %H:%M")
        until = lib.to_jst(period.until).strftime("%Y-%m-%d %H:%M")
    except (AttributeError, TypeError, ValueError):
        return ""
    return (f"対象期間: {since} 〜 {until}（JST・終端は含まない / ラベル "
            f"{getattr(period, 'label', '')}）")


def build_prompt(missing_digests: list, known: list, need_overview: bool,
                 prompt_max: int = PROMPT_MAX_PER_SESSION, period=None) -> str:
    """missing_digests: 要約が必要なダイジェスト / known: [(id, title, summary)] 既知分。"""
    L = [_INSTRUCTIONS, ""]
    pl = period_line(period)
    if pl:
        L.append(pl)
        L.append("")
    if missing_digests:
        L.append(f"## 要約対象のセッション（{len(missing_digests)} 件）")
        L.append("")
        for d in missing_digests:
            L.append(render_digest(d, prompt_max))
            L.append("")
    else:
        L.append("## 要約対象のセッション: なし（sessions は空配列にすること）")
        L.append("")
    if need_overview and known:
        L.append("## 参考: 同じ期間の他セッション（要約済み。overview の材料にだけ使う）")
        for sid, title, summ in known:
            L.append(f"- {sid}: {summ or title}")
        L.append("")
    if not need_overview:
        L.append("overview は空文字列でよい。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 出力パース
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def parse_output(text: str):
    """```json フェンス・前後の説明文を剥がして JSON を取り出す。失敗したら None。"""
    if not text:
        return None
    t = text.strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def _phases_from(raw) -> list:
    """LLM が返した phases を文字列リストに正規化する。

    LLM 出力は任意の形になりうる。``phases: 2``（フェーズ「数」との取り違え）で
    TypeError を出したり、``phases: "第1…、第2…"``（文字列単体）を1文字ずつに
    分解したりしないよう、**容器の型を先に検査**する。
    """
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for p in raw:
        if isinstance(p, bool) or not isinstance(p, (str, int, float)):
            continue
        s = str(p).strip()
        if s:
            out.append(s)
    return out


def _entries_from(obj: dict) -> dict:
    """{id: {"summary":..., "phases":[...]}}。スキーマ不一致の要素は個別に捨てる。"""
    out = {}
    sessions = obj.get("sessions") if isinstance(obj, dict) else None
    if isinstance(sessions, dict):
        sessions = list(sessions.values())
    if not isinstance(sessions, (list, tuple)):
        return out
    for item in sessions:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        summ = item.get("summary")
        if not isinstance(sid, str) or not isinstance(summ, str) or not summ.strip():
            continue
        out[sid] = {"summary": summ.strip(), "phases": _phases_from(item.get("phases"))}
    return out


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, model: str, timeout_sec: float, warnings: list):
    exe = shutil.which("claude")
    if not exe:
        warnings.append(
            "claude コマンドが見つからないため LLM 要約をスキップしました"
            "（決定論ダイジェストのみでレポートを出力しています）。"
        )
        return None
    try:
        proc = subprocess.run(
            [exe, "-p", "--model", model, "--output-format", "text", prompt],
            stdin=subprocess.DEVNULL,        # stdin 待ちの数秒を避ける
            capture_output=True, text=True, timeout=timeout_sec,
            # ストリーム途中切断でマルチバイトが分断されると strict デコードは
            # UnicodeDecodeError を投げる（subprocess 内部なので下の except では
            # 捕まらない）。要約の失敗でレポート全体を落とさないため replace する。
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        warnings.append(
            f"LLM 要約がタイムアウトしました（{timeout_sec:g} 秒）。要約なしで続行します"
            "（--summarize-timeout で延ばせます）。"
        )
        return None
    except OSError as exc:
        warnings.append(f"LLM 要約の実行に失敗しました（{exc}）。要約なしで続行します。")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        warnings.append(
            f"LLM 要約が異常終了しました（exit {proc.returncode}: "
            f"{err[-1][:200] if err else 'stderr なし'}）。要約なしで続行します。"
        )
        return None
    obj = parse_output(proc.stdout)
    if obj is None:
        warnings.append(
            "LLM 要約の出力を JSON として解釈できませんでした。要約なしで続行します。"
        )
        return None
    return obj


def _choose_prompt_max(digests: dict, order: list) -> int:
    """1セッション分が1バッチに収まる最大の発話件数を選ぶ（決定論）。

    キャッシュのヒット状況に依存させないため、**期間内の全セッション**を見て決める。
    合計が大きいだけならバッチ分割で対応できるので、ここで落とすのは「1セッション
    単体で 1 バッチに収まらない」極端な場合だけ。
    """
    for pmax in (PROMPT_MAX_PER_SESSION, 8, 3, 0):
        biggest = max(
            (len(render_digest(digests[sid], pmax).encode("utf-8")) for sid in order),
            default=0,
        )
        if biggest <= BATCH_MAX_BYTES:
            return pmax
    return 0


def _split_batches(mds: list, prompt_max: int) -> list:
    """ダイジェスト列を、バイト数と件数の両方で 1 回の呼び出しに収まる塊へ分ける。"""
    batches = []
    cur, cur_bytes = [], 0
    for d in mds:
        size = len(render_digest(d, prompt_max).encode("utf-8"))
        if cur and (cur_bytes + size > BATCH_MAX_BYTES or len(cur) >= BATCH_MAX_SESSIONS):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(d)
        cur_bytes += size
    if cur:
        batches.append(cur)
    return batches


def summarize(digests: dict, sessions: list, model: str, cache_dir, period,
              timeout_sec: float, warnings: list, use_cache: bool = True) -> dict:
    """{"sessions": {sid: {"summary", "phases"}}, "overview": str, "meta": {...}}。"""
    cache_dir = Path(cache_dir)
    order = [s.session_id for s in sessions if s.session_id in digests]
    main_of = {s.session_id: s.main_path for s in sessions}

    # 発話件数の上限は「セッション群全体」から先に決める（キャッシュヒット状況に
    # 依存させると同じ入力でキーが揺れるため）。1セッション分が1バッチに収まる
    # 最大の件数を採り、収まらない規模だけ落とす。
    prompt_max = _choose_prompt_max(digests, order)
    if prompt_max < PROMPT_MAX_PER_SESSION:
        warnings.append(
            f"要約プロンプトが大きすぎるため、LLM に渡す人間の発話を各セッション "
            f"{prompt_max} 件に制限しました（要約の精度が落ちます）。"
        )

    keys = {
        sid: session_cache_key(sid, main_of.get(sid), period, model,
                               digest_fingerprint(digests.get(sid)), prompt_max)
        for sid in order
    }
    okey = overview_cache_key(list(keys.values()), period, model)

    out: dict = {}
    stamps = []
    missing = []
    for sid in order:
        hit = _read_cache(cache_dir, keys[sid]) if use_cache else None
        if hit and hit.get("summary"):
            out[sid] = {"summary": hit["summary"], "phases": hit.get("phases") or []}
            if hit.get("generated_at"):
                stamps.append(hit["generated_at"])
        else:
            missing.append(sid)

    ohit = _read_cache(cache_dir, okey) if use_cache else None
    overview = (ohit or {}).get("overview") or ""
    if ohit and ohit.get("generated_at"):
        stamps.append(ohit["generated_at"])
    need_overview = not overview

    cached_n = len(out)
    generated_n = 0

    if order and (missing or need_overview):
        # 未要約分をバッチに割る。1回の argv・1回の応答 JSON に収まる範囲で
        # まとめ、収まらない規模は**発話を削らずに複数回**呼ぶ。
        batches = _split_batches([digests[sid] for sid in missing], prompt_max) or [[]]
        missed_all = []
        for bi, batch in enumerate(batches):
            last = bi == len(batches) - 1
            known = [
                (sid, digests[sid].get("title", ""), out.get(sid, {}).get("summary", ""))
                for sid in order if sid in out
            ]
            want_overview = need_overview and last
            prompt = build_prompt(batch, known, want_overview, prompt_max, period)
            obj = _call_claude(prompt, model, timeout_sec, warnings)
            if obj is None:
                # 呼び出し自体の失敗は _call_claude が警告済み。ここで件数警告を
                # 重ねると「1件の警告で縮退」という規律が崩れるので積まない。
                continue
            now = datetime.now(lib.JST).strftime("%Y-%m-%d %H:%M:%S")
            entries = _entries_from(obj)
            for d in batch:
                sid = d.get("session_id")
                e = entries.get(sid)
                if not e:
                    missed_all.append(sid)
                    continue
                out[sid] = e
                generated_n += 1
                stamps.append(now)
                _write_cache(cache_dir, keys[sid], {
                    "session_id": sid, "summary": e["summary"], "phases": e["phases"],
                    "model": model, "prompt_version": PROMPT_VERSION, "generated_at": now,
                })
            if want_overview:
                ov = obj.get("overview")
                if isinstance(ov, str) and ov.strip():
                    overview = ov.strip()
                    stamps.append(now)
                    _write_cache(cache_dir, okey, {
                        "overview": overview, "model": model,
                        "prompt_version": PROMPT_VERSION, "generated_at": now,
                    })
        missed = [sid for sid in missed_all if sid and sid not in out]
        if missed:
            warnings.append(
                f"LLM 要約に含まれなかったセッションが {len(missed)} 件あります"
                f"（そのセッションのみ要約なし）: "
                + ", ".join(x[:8] for x in missed[:5])
                + ("…" if len(missed) > 5 else "")
            )

    return {
        "sessions": out,
        "overview": overview,
        "meta": {
            "model": model,
            "used": len(out),
            "cached": cached_n,
            "generated": generated_n,
            "generated_at": max(stamps) if stamps else "",
        },
    }
