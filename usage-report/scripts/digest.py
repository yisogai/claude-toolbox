#!/usr/bin/env python3
"""セッションの「実際に何をしたか」を決定論的に抽出する（第1段）。

``ai-title`` は会話の早い段階で決まり以降ほぼ更新されないため、Bash 200 回・
Edit 50 回規模のセッションでは実作業を表さない。ここではメイン jsonl を1パス
走査して、要約に必要な材料（人間の発話・編集ファイル・ブランチ・コマンド・
サブエージェント・スキル）だけを抜き出す。

設計方針:
- **標準ライブラリのみ**（matplotlib に依存する charts.py とは独立）。時刻・
  人間プロンプト判定などの既存ロジックは ``usage_lib`` / ``cost_lib`` を再利用し、
  複製しない。
- 対象は**メイン jsonl のみ**。サブエージェント transcript には人間の発話が無く、
  ハーネスがエージェントへ渡す指示文が user ロールで入るため混ぜない。
- 窓は集計側と同じ半開区間 ``[since, until)``。
- 出力は JSON 化して ``digests.json`` に落とすため、順序は必ず決定論的にする
  （頻度順の同数は名前昇順で割る）。

``usage_lib`` を import するが、``usage_lib`` 側はこのモジュールを関数内で遅延
import するため循環にならない。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import usage_lib as ul

lib = ul.lib

# 全行 json.loads は重い（メイン jsonl だけで数百 MB）ので、まず正規表現で
# timestamp / gitBranch を拾い、詳細が要る行（user か tool_use を含む行）だけを
# json.loads する。
_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
_BRANCH_RE = re.compile(rb'"gitBranch"\s*:\s*"((?:[^"\\]|\\.)*)"')
_SNAPSHOT_RE = re.compile(rb'^\s*\{"type"\s*:\s*"file-history-snapshot"')

_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
_AGENT_TOOLS = ("Agent", "Task")

PROMPT_LIMIT = 300          # 発話1件あたりの文字数上限
PROMPT_MAX = 40             # 発話の件数上限
FILES_MAX = 30
DIRS_MAX = 10
REFS_MAX = 10
TOOLS_MAX = 10
EXAMPLES_MAX = 6
EXAMPLE_LIMIT = 120

_REF_RE = re.compile(r"#(\d+)")

_CMD_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")

# ヒアドキュメント（`git commit -F - <<'EOF' … EOF`・`gh issue create --body …`）の本文は
# コマンドではない。改行で分割するため、本文の各行が1コマンドとして分類されてしまう
# （日本語の本文に `flutter test` 等が出てくると test 回数が水増しされる）。
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command: str) -> str:
    """ヒアドキュメント本文（開始行の次〜終端タグ行）を取り除く。"""
    if "<<" not in (command or ""):
        return command or ""
    lines = (command or "").split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for _q, tag in _HEREDOC_RE.findall(line):
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1        # 終端タグ行も落とす（無ければループ終了）
    return "\n".join(out)

# エージェント自身の作業メモ（自動メモリ・スクラッチパッド・プラン）は「何をしたか」では
# ないため、files/dirs から除外する（要約の材料になると実ソースを押しのけるため）。
# 別枠 ``agent_files_total`` として件数だけ残す。
#
# 除外は `.claude/` 全体ではなく**ハーネスが管理する内部ディレクトリだけ**に限る。
# `.claude/` 配下には実作業の成果物（`~/.claude/CLAUDE.md`・`skills/**`・`settings.json`、
# および EnterWorktree が作る `<repo>/.claude/worktrees/<name>/**` の実ソース）が同居
# するため、パス中に `/.claude/` があるだけで落とすと実作業そのものが材料から消える。
_AGENT_MEMO_DIRS = "projects|plans|todos|memory|shell-snapshots|history|statsig|logs|ide"
_AGENT_PATH_RE = re.compile(
    r"(?:^|/)\.claude/(?:" + _AGENT_MEMO_DIRS + r")/"   # 自動メモリ・プラン・履歴
    # ハーネスの一時作業領域の scratchpad（tmp 配下でも作業対象リポジトリはあり得るので、
    # scratchpad セグメントを必須にして誤除外を避ける）
    r"|^/(?:private/)?tmp/claude-[^/]*/(?:[^/]+/)*scratchpad/"
)

# EnterWorktree が作る worktree（`<repo>/.claude/worktrees/<name>/…`）は、元リポジトリの
# 同じファイルを別パスで編集しているだけなので、元リポジトリのパスに正規化してから
# files/dirs に積む（worktree ごとに別ディレクトリとして散らばるのを防ぐ）。
_WORKTREE_RE = re.compile(r"/\.claude/worktrees/[^/]+/")


def normalize_path(path: str) -> str:
    """worktree 配下の編集パスを元リポジトリのパスへ正規化する。"""
    if not path:
        return path
    return _WORKTREE_RE.sub("/", path, count=1)


def is_agent_path(path: str) -> bool:
    """エージェントの作業メモ（memory / scratchpad / plans）かどうか。"""
    return bool(_AGENT_PATH_RE.search(path or ""))


# ---------------------------------------------------------------------------
# コマンド分類
# ---------------------------------------------------------------------------

# 先頭に付く環境変数代入・ラッパーは読み飛ばして「実際のコマンド」を先頭トークンに
# する。実データでは `bin/rspec …` / `RAILS_ENV=development bin/rake …` /
# `TZ=Asia/Tokyo fvm flutter test …` が大半を占め、素朴に第1トークンだけを見ると
# ほぼすべてが other に落ちる（分類が用を成さない）。
_WRAPPERS = ("env", "sudo", "time", "nice", "xargs", "npx", "fvm", "dotenv")


def _normalize_tokens(seg: str) -> list:
    toks = seg.strip().split()
    while toks:
        t = toks[0]
        if "=" in t and not t.startswith("/") and "/" not in t.split("=")[0]:
            toks = toks[1:]           # 先頭の環境変数代入
            continue
        if t in _WRAPPERS:
            toks = toks[1:]
            continue
        if t == "bundle" and toks[1:2] == ["exec"] and toks[2:3] != ["rspec"]:
            toks = toks[2:]           # `bundle exec rspec` は仕様どおり test 判定に残す
            continue
        break
    if toks:
        # `bin/rspec` `./bin/rspec` `/usr/bin/git` はコマンド名で判定する
        # （部分文字列マッチではなく、パスの basename を取るだけ）。
        toks = [toks[0].rsplit("/", 1)[-1]] + toks[1:]
    return toks


def _segment_category(seg: str) -> str:
    """コマンド1セグメントの分類。先頭トークン基準（部分文字列マッチはしない）。"""
    toks = _normalize_tokens(seg)
    if not toks:
        return ""
    t0 = toks[0]
    rest = toks[1:]
    if t0 in ("rspec", "parallel_rspec"):
        return "test"
    if t0 in ("rake", "make") and rest[:1] in (["test"], ["spec"]):
        return "test"
    if t0 == "xcodebuild" and "test" in rest:
        return "test"
    # Android の wrapper（`./gradlew testDebugUnitTest` 等）。タスク名に test を含めば test。
    if t0 in ("gradle", "gradlew"):
        return "test" if any("test" in x.lower() for x in rest) else "build"
    # test（build より先に見る。`make test` / `npm run test` を build に取られないため）
    if t0 in ("pytest", "rspec", "jest", "vitest"):
        return "test"
    if t0 in ("go", "cargo") and rest[:1] == ["test"]:
        return "test"
    if t0 == "flutter" and rest[:1] == ["test"]:
        return "test"
    if t0 in ("npm", "yarn", "pnpm") and "test" in rest[:2]:
        return "test"
    if t0 == "bundle" and rest[:2] == ["exec", "rspec"]:
        return "test"
    if t0 == "make" and rest[:1] == ["test"]:
        return "test"
    if t0 in ("git", "gh"):
        return "git"
    if t0 == "make":
        return "build"
    if t0 in ("docker", "xcodebuild"):
        return "build"
    if t0 in ("npm", "yarn", "pnpm") and "build" in rest[:2]:
        return "build"
    if t0 == "flutter" and rest[:1] == ["build"]:
        return "build"
    return "other"


def classify_command(command: str) -> str:
    """複合コマンド（``cd X && cmd``）は各セグメントの先頭トークンを見て分類する。"""
    cats = set()
    for seg in _CMD_SPLIT_RE.split(strip_heredocs(command)):
        c = _segment_category(seg)
        if c:
            cats.add(c)
    for c in ("test", "git", "build"):
        if c in cats:
            return c
    return "other"


# ---------------------------------------------------------------------------
# 走査
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> str:
    return lib.to_jst(dt).isoformat() if dt else ""


def _top(counter: Counter, n: int) -> list:
    """頻度順（同数は名前昇順）で上位 n 件を [{"path"/"name", "count"}] 用に返す。"""
    return sorted(counter.items(), key=lambda t: (-t[1], t[0]))[:n]


def _short(text: str, limit: int) -> str:
    t = " ".join((text or "").split())
    return t[:limit]


def _scan_records(path: Path, period) -> list:
    """メイン jsonl の窓内レコードを、要約に必要な情報だけの dict 列にして返す。"""
    recs = []
    try:
        f = open(path, "rb")
    except OSError:
        return recs
    with f:
        for raw in f:
            # file-history-snapshot 行は top-level に timestamp を持たず（入れ子の
            # スナップショット時刻が正規表現に拾われる）、発話もツール利用も持たない。
            # レコードとして数えるとフェーズ境界だけがずれるので読み飛ばす。
            if _SNAPSHOT_RE.search(raw):
                continue
            m = _TS_RE.search(raw)
            if not m:
                continue
            try:
                ts = lib.parse_iso(m.group(1).decode("ascii", errors="replace"))
            except (ValueError, TypeError):
                continue
            if ts < period.since or ts >= period.until:
                continue
            branch = None
            bm = _BRANCH_RE.search(raw)
            if bm:
                try:
                    branch = json.loads(b'"' + bm.group(1) + b'"')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    branch = None
            rec = {
                "ts": ts, "branch": branch or None,
                "prompt": None, "files": [], "agent_files": [], "cmds": [],
                "tools": [], "subagents": [], "skills": [],
            }
            need = b'"tool_use"' in raw or b'"type":"user"' in raw or b'"type": "user"' in raw
            if need:
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    _fill_from_obj(rec, obj)
            recs.append(rec)
    recs.sort(key=lambda r: r["ts"])
    return recs


def _fill_from_obj(rec: dict, obj: dict) -> None:
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return
    if obj.get("type") == "user":
        text = lib._extract_text(msg.get("content"))
        if ul.is_human_utterance(obj, text):
            rec["prompt"] = _short(text, PROMPT_LIMIT)
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "(unknown)"
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        rec["tools"].append(name)
        if name in _EDIT_TOOLS:
            fp = inp.get("file_path") or inp.get("notebook_path")
            if isinstance(fp, str) and fp:
                # worktree のパスは元リポジトリへ正規化してから判定・集計する。
                fp = normalize_path(fp)
                # エージェントの作業メモは別枠（件数のみ）。要約材料には載せない。
                rec["agent_files" if is_agent_path(fp) else "files"].append(fp)
        elif name == "Bash":
            cmd = inp.get("command")
            if isinstance(cmd, str) and cmd.strip():
                rec["cmds"].append(cmd)
        elif name in _AGENT_TOOLS:
            st = inp.get("subagent_type")
            rec["subagents"].append(st if isinstance(st, str) and st else "(unknown)")
        elif name == "Skill":
            sk = inp.get("skill")
            rec["skills"].append(sk if isinstance(sk, str) and sk else "(unknown)")


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def _commands_block(cmds: list) -> tuple:
    """(counts dict, examples list) を返す。"""
    counts = {"test": 0, "git": 0, "build": 0, "other": 0}
    per_cat: dict = {"test": Counter(), "git": Counter(), "build": Counter(), "other": Counter()}
    for c in cmds:
        cat = classify_command(c)
        counts[cat] += 1
        per_cat[cat][_short(c, EXAMPLE_LIMIT)] += 1
    examples = []
    # 各分類の最頻コマンドから1件ずつ拾い（分類の網羅を優先）、余りを2件目で埋める。
    for rank in (0, 1):
        for cat in ("test", "git", "build", "other"):
            top = _top(per_cat[cat], rank + 1)
            if len(top) > rank and len(examples) < EXAMPLES_MAX:
                item = top[rank][0]
                if item not in examples:
                    examples.append(item)
    return counts, examples


def _phase_split(recs: list, phase_gap_min: int) -> list:
    """ブランチ変化・一定時間以上の空白でフェーズに分ける（レコード列のリストを返す）。"""
    if not recs:
        return []
    gap = phase_gap_min * 60
    phases = [[recs[0]]]
    last_branch = recs[0]["branch"]
    for prev, cur in zip(recs, recs[1:]):
        new = False
        if cur["branch"] is not None and last_branch is not None and cur["branch"] != last_branch:
            new = True
        if (cur["ts"] - prev["ts"]).total_seconds() >= gap:
            new = True
        if cur["branch"] is not None:
            last_branch = cur["branch"]
        if new:
            phases.append([cur])
        else:
            phases[-1].append(cur)
    return phases


def _phase_info(index: int, chunk: list) -> dict:
    files = Counter()
    cmds = []
    prompts = []
    branch = ""
    for r in chunk:
        if not branch and r["branch"]:
            branch = r["branch"]
        for p in r["files"]:
            files[p] += 1
        cmds.extend(r["cmds"])
        if r["prompt"] and len(prompts) < 2:
            prompts.append(_short(r["prompt"], 150))
    counts, _ = _commands_block(cmds)
    return {
        "index": index,
        "start": _iso(chunk[0]["ts"]),
        "end": _iso(chunk[-1]["ts"]),
        "branch": branch,
        "files": [p for p, _c in _top(files, 5)],
        "commands": counts,
        "prompts": prompts,
    }


def build_digest(session, period, phase_gap_min: int = 30) -> dict:
    """1セッションの決定論ダイジェストを作る。メイン jsonl のみを読む。"""
    recs = _scan_records(session.main_path, period) if session.main_path else []

    prompts = []
    files = Counter()
    dirs = Counter()
    agent_files = Counter()
    cmds = []
    tools = Counter()
    subagents = Counter()
    skills = Counter()
    branches = []
    switches = 0
    last_branch = None

    for r in recs:
        if r["prompt"] and len(prompts) < PROMPT_MAX:
            prompts.append(r["prompt"])
        for p in r["files"]:
            files[p] += 1
            dirs[str(Path(p).parent)] += 1
        for p in r.get("agent_files") or []:
            agent_files[p] += 1
        cmds.extend(r["cmds"])
        for t in r["tools"]:
            tools[t] += 1
        for s in r["subagents"]:
            subagents[s] += 1
        for s in r["skills"]:
            skills[s] += 1
        b = r["branch"]
        if b:
            if b not in branches:
                branches.append(b)
            if last_branch is not None and b != last_branch:
                switches += 1
            last_branch = b

    counts, examples = _commands_block(cmds)

    refs = []
    for text in prompts + [_short(c, EXAMPLE_LIMIT) for c in cmds]:
        for m in _REF_RE.finditer(text):
            v = "#" + m.group(1)
            if v not in refs:
                refs.append(v)
            if len(refs) >= REFS_MAX:
                break
        if len(refs) >= REFS_MAX:
            break

    chunks = _phase_split(recs, phase_gap_min)
    # フェーズが1つしかないセッションは空にする（要約側の入力を減らすため）。
    phases = [_phase_info(i + 1, c) for i, c in enumerate(chunks)] if len(chunks) > 1 else []

    return {
        "session_id": session.session_id,
        "title": session.title or "",
        # 窓内の実レコードの開始・終了（LLM に期間感覚を渡すため。無ければ空文字）。
        "start": _iso(recs[0]["ts"]) if recs else "",
        "end": _iso(recs[-1]["ts"]) if recs else "",
        "prompts": prompts,
        "files": [{"path": p, "count": c} for p, c in _top(files, FILES_MAX)],
        "files_total": len(files),
        # エージェント自身の作業メモ（.claude/** ・ /tmp/claude-*/**）は材料から外し件数だけ残す
        "agent_files_total": len(agent_files),
        "dirs": [{"path": p, "count": c} for p, c in _top(dirs, DIRS_MAX)],
        "commands": counts,
        "examples": examples,
        "branches": branches,
        "switches": switches,
        "subagents": [{"name": n, "count": c} for n, c in _top(subagents, TOOLS_MAX)],
        "skills": [{"name": n, "count": c} for n, c in _top(skills, TOOLS_MAX)],
        "refs": refs,
        "tool_counts": [{"name": n, "count": c} for n, c in _top(tools, TOOLS_MAX)],
        "phases": phases,
    }


# ---------------------------------------------------------------------------
# 決定論的な短縮要約（第2段が無効なときに sessions.csv / summary.md に出す）
# ---------------------------------------------------------------------------

def _width(s: str) -> int:
    """全角=2 / 半角=1 の表示幅。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in s)


def _fit(s: str, units: int) -> str:
    if _width(s) <= units:
        return s
    out = []
    w = 0
    for ch in s:
        cw = _width(ch)
        # 末尾に付ける「…」は全角（幅2）なので、その分を必ず空けてから切る
        # （units - 1 で切ると半角境界で units + 1 になり上限を1単位超える）。
        if w + cw > units - 2:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def _short_dir(path: str) -> str:
    parts = [p for p in Path(path).parts if p not in ("/",)]
    return "/".join(parts[-2:]) if parts else path


def deterministic_summary(digest: dict, max_units: int = 160) -> str:
    """`<主要ディレクトリ> を編集(Nファイル) / テストM回 / ブランチ: a, b` 形式。

    ``max_units`` は表示幅（全角80字 = 160 単位）。長い要素から切り詰める。
    """
    parts = []
    dirs = digest.get("dirs") or []
    n_files = digest.get("files_total") or len(digest.get("files") or [])
    if dirs and n_files:
        parts.append(f"{_fit(_short_dir(dirs[0]['path']), 60)} を編集({n_files}ファイル)")
    elif n_files:
        parts.append(f"{n_files}ファイルを編集")
    elif digest.get("agent_files_total"):
        # 実ファイルの編集が無く作業メモだけのセッション（調査・レビュー中心）
        parts.append(f"作業メモ{digest['agent_files_total']}ファイル")
    cmds = digest.get("commands") or {}
    if cmds.get("test"):
        parts.append(f"テスト{cmds['test']}回")
    if cmds.get("git"):
        parts.append(f"git {cmds['git']}回")
    branches = digest.get("branches") or []
    if branches:
        parts.append("ブランチ: " + _fit(", ".join(branches[:3]), 60))
    out = " / ".join(parts)
    return _fit(out, max_units) if out else ""


def build_digests(sessions: list, period, phase_gap_min: int = 30) -> dict:
    """セッションID → digest。``sessions`` は採用済みセッション（agg.sessions）。"""
    return {
        s.session_id: build_digest(s, period, phase_gap_min)
        for s in sessions
    }


def digests_json(digests: dict, root: str, period, phase_gap_min: int) -> str:
    """digests.json の本文。生成時刻は含めない（同一条件の再実行でバイト一致させる）。"""
    payload = {
        "root": root,
        "period": {
            "label": period.label,
            "kind": period.kind,
            "since": lib.to_jst(period.since).isoformat(),
            "until": lib.to_jst(period.until).isoformat(),
        },
        "phase_gap_min": phase_gap_min,
        "sessions": [digests[k] for k in sorted(digests)],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
