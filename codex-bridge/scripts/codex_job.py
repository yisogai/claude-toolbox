#!/usr/bin/env python3
"""codex-bridge のジョブ参照 CLI。

Claude（Fable）のコンテキストを守るため、job.json 全体ではなく**圧縮サマリ**を返すのが
`result` の役割。生データが要るときだけ `--json` を付ける。

サブコマンド:
  status <job-dir>            進捗・結果の 1 画面サマリ（実行中は events.jsonl から推定）
  result <job-dir> [--json]   Claude 向けの圧縮サマリ（最終メッセージは 4,000 字で打ち切り）
  usage [--since ISO] [--json] var/codex_usage.jsonl の集計（モデル別・日別）

終了コード: 0=正常 / 1=引数・入出力エラー / 2=job-dir が存在しない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_lib as lib  # noqa: E402

LAST_MESSAGE_LIMIT = 4000
FAILED_COMMANDS_SHOWN = 20     # H-1: 失敗コマンドは先頭 20 件だけ出し、残りは件数で示す
RESULT_MAX_CHARS = 12000       # H-1: result 全体の上限（Claude のコンテキストを守る）


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def load_job(job_dir: Path):
    """job.json を読む。戻り値は (job|None, state)。

    L-2: 「まだ無い（実行中）」と「壊れている」を区別する。前者は running、後者は corrupt。
    """
    p = job_dir / "job.json"
    if not p.exists():
        return None, "missing"
    try:
        with open(p, "r", encoding="utf-8") as f:
            job = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        lib.eprint(f"警告: job.json が壊れています（読めません）: {e}")
        return None, "corrupt"
    if not isinstance(job, dict):
        lib.eprint("警告: job.json が壊れています（dict ではありません）")
        return None, "corrupt"
    return job, "ok"


def scan_events(path: Path):
    """巨大 events.jsonl でも軽い走査（行数と最終行のみ。全行を保持しない）。"""
    count = 0
    last = b""
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                tail = (tail + chunk)[-65536:]
            for line in reversed(tail.split(b"\n")):
                if line.strip():
                    last = line
                    break
    except OSError:
        return 0, None
    ev = None
    if last:
        try:
            ev = json.loads(last.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            ev = None
    return count, ev


def usage_line(usage, credits) -> str:
    if not usage:
        return "usage: なし"
    return ("usage: input={i} (cached={c}, cache_write={w}) output={o} (reasoning={r}) / credits_est={cr}"
            .format(i=lib.fmt_tokens(usage.get("input_tokens")),
                    c=lib.fmt_tokens(usage.get("cached_input_tokens")),
                    w=lib.fmt_tokens(usage.get("cache_write_input_tokens")),
                    o=lib.fmt_tokens(usage.get("output_tokens")),
                    r=lib.fmt_tokens(usage.get("reasoning_output_tokens")),
                    cr="-" if credits is None else f"{credits:g}"))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    if not job_dir.is_dir():
        lib.eprint(f"エラー: job-dir が存在しません: {job_dir}")
        return 2
    job, state = load_job(job_dir)
    if state == "corrupt":
        print(f"job: {job_dir}")
        print("status: corrupt（job.json が壊れている。events.jsonl / stderr.log を確認する）")
        return 0
    if job is None:
        count, last = scan_events(job_dir / "events.jsonl")
        print(f"job: {job_dir}")
        print("status: running（job.json 未生成）")
        print(f"events: {count} 件")
        if last:
            print(f"最終イベント: type={last.get('type')}")
        else:
            print("最終イベント: なし")
        try:
            mtime = (job_dir / "events.jsonl").stat().st_mtime
            print(f"最終更新: {lib.iso(lib.datetime.fromtimestamp(mtime, lib.timezone.utc))}")
        except OSError:
            pass
        return 0

    print(f"job: {job_dir}")
    print(f"status: {job.get('status')} (exit={job.get('exit_code')}) "
          f"duration={lib.fmt_duration(job.get('duration_sec'))}")
    print(f"mode={job.get('mode')} model={job.get('model')} effort={job.get('effort')} "
          f"write={job.get('write')} thread_id={job.get('thread_id')}")
    print(usage_line(job.get("usage"), job.get("credits_est")))
    print(f"touched_files: {len(job.get('touched_files') or [])} 件 / "
          f"commands: {len(job.get('commands') or [])} 件 / "
          f"warnings: {len(job.get('warnings') or [])} 件")
    return 0


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------

def read_last_message(job) -> str:
    p = job.get("last_message_path")
    if not p or not os.path.exists(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        return f"（最終メッセージを読めませんでした: {e}）"


def cmd_result(args) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    if not job_dir.is_dir():
        lib.eprint(f"エラー: job-dir が存在しません: {job_dir}")
        return 2
    job, state = load_job(job_dir)
    if state == "corrupt":
        lib.eprint(f"エラー: job.json が壊れています（JSON として読めません）: {job_dir}/job.json")
        return 1
    if job is None:
        lib.eprint(f"エラー: job.json がありません（実行中の可能性）: {job_dir}")
        return 1

    if args.as_json:
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0

    out = []
    out.append(f"# codex job: {job.get('status')} (exit={job.get('exit_code')}, "
               f"{lib.fmt_duration(job.get('duration_sec'))})")
    out.append(f"mode={job.get('mode')} model={job.get('model')} effort={job.get('effort')} "
               f"write={job.get('write')} cwd={job.get('cwd')}")
    if job.get("mock"):
        out.append(f"**モック実行（--mock {job.get('mock')}）**: Codex 実機の結果ではない。"
                   "usage / credits_est は架空の値で、使用量台帳にも載せていない。")
    if job.get("queued_sec"):
        out.append(f"（スロット待ち {lib.fmt_duration(job.get('queued_sec'))} は duration に含まない）")
    if job.get("thread_id"):
        out.append(f"thread_id={job.get('thread_id')}（再開: --resume {job.get('thread_id')}）")

    touched = job.get("touched_files") or []
    out.append("")
    out.append(f"## 変更ファイル（{len(touched)} 件）")
    if touched:
        for t in touched:
            out.append(f"- {t.get('path')} ({t.get('kind')})")
    else:
        out.append("- なし")

    cmds = job.get("commands") or []
    failed = [c for c in cmds if c.get("status") == "failed" or (c.get("exit_code") not in (0, None))]
    out.append("")
    out.append(f"## 失敗したコマンド（{len(failed)} / {len(cmds)} 件）")
    if failed:
        for c in failed[:FAILED_COMMANDS_SHOWN]:
            out.append(f"- exit={c.get('exit_code')} status={c.get('status')}: {c.get('command')}")
        if len(failed) > FAILED_COMMANDS_SHOWN:
            out.append(f"- …他 {len(failed) - FAILED_COMMANDS_SHOWN} 件（全件は job.json / events.jsonl）")
    else:
        out.append("- なし")

    errors = job.get("errors") or []
    if errors:
        out.append("")
        out.append("## エラー")
        for e in errors:
            out.append(f"- {e}")

    msg = read_last_message(job)
    out.append("")
    out.append("## 最終メッセージ")
    if msg:
        if len(msg) > LAST_MESSAGE_LIMIT:
            msg = (msg[:LAST_MESSAGE_LIMIT]
                   + f"\n…（以降 {len(msg) - LAST_MESSAGE_LIMIT} 字を省略。全文: "
                     f"{job.get('last_message_path')}）")
        out.append(msg.rstrip())
    else:
        out.append("（なし）")

    if job.get("structured_output") is not None:
        out.append("")
        out.append("## structured_output")
        out.append(json.dumps(job["structured_output"], ensure_ascii=False, indent=2))

    out.append("")
    out.append(usage_line(job.get("usage"), job.get("credits_est")))

    warns = job.get("warnings") or []
    out.append("")
    out.append(f"## 警告（{len(warns)} 件）")
    if warns:
        for w in warns:
            out.append(f"- {w}")
    else:
        out.append("- なし")

    text = "\n".join(out)
    if len(text) > RESULT_MAX_CHARS:
        note = f"\n…（出力が長いため末尾を省略した。全体 {len(text)} 字 / 詳細は job.json）"
        text = text[:RESULT_MAX_CHARS - len(note)] + note
    print(text)
    return 0


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

EPOCH = lib.datetime(1970, 1, 1, tzinfo=lib.timezone.utc)


def adjust_resumed_rows(rows: list) -> list:
    """M-2: `resumed` 行の usage をスレッド累計とみなし、直前行との差分で計上する。

    `--resume` したターンの `turn.completed.usage` がスレッド累計で返ると、台帳をそのまま
    足すと 1 ターン目を二重計上する。同じ `thread_id` の行を時系列に並べ、`resumed` 行は
    直前行との差分（負なら 0）に置き換える。実機で「ターン単体」だと分かったら
    `usage_mode = per_turn`（config / `--usage-mode`）でこの補正を外す。
    """
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[r.get("thread_id") or f"__anon_{i}"].append(i)
    out = list(rows)
    for idxs in groups.values():
        idxs.sort(key=lambda i: (lib.parse_iso(rows[i].get("ts") or "") or EPOCH, i))
        prev = None
        for i in idxs:
            r = rows[i]
            u = lib.normalize_usage(r.get("usage")) or {k: 0 for k in lib.USAGE_FIELDS}
            if r.get("resumed") and prev is not None:
                adj = {k: max(0, u[k] - prev[k]) for k in lib.USAGE_FIELDS}
                new = dict(r)
                new["usage"] = adj
                credits = lib.credits_est(adj, r.get("model") or "")
                if credits is not None:
                    new["credits_est"] = credits
                out[i] = new
            prev = u        # 累計前提なので基準は「補正前の値」
    return out


def cmd_usage(args) -> int:
    path = lib.usage_ledger_path()
    since = lib.parse_iso(args.since) if args.since else None
    if args.since and since is None:
        lib.eprint(f"エラー: --since を解釈できません: {args.since}")
        return 1

    rows = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    if r.get("mock"):
                        continue    # M10: モック実行の架空 usage は集計に混ぜない
                    ts = lib.parse_iso(r.get("ts") or "")
                    if since and (ts is None or ts < since):
                        continue
                    rows.append(r)
        except OSError as e:
            lib.eprint(f"エラー: 台帳を読めません: {e}")
            return 1

    mode = getattr(args, "usage_mode", None) or lib.usage_mode()
    if mode not in lib.USAGE_MODES:
        lib.eprint(f"エラー: --usage-mode は {' | '.join(lib.USAGE_MODES)} のいずれか: {mode}")
        return 1
    if mode == "cumulative":
        rows = adjust_resumed_rows(rows)

    by_model = defaultdict(lambda: {"jobs": 0, "input": 0, "cached": 0, "output": 0, "credits": 0.0})
    by_day = defaultdict(lambda: {"jobs": 0, "credits": 0.0})
    for r in rows:
        u = r.get("usage") or {}
        m = by_model[r.get("model") or "-"]
        m["jobs"] += 1
        m["input"] += int(u.get("input_tokens") or 0)
        m["cached"] += int(u.get("cached_input_tokens") or 0)
        m["output"] += int(u.get("output_tokens") or 0)
        m["credits"] += float(r.get("credits_est") or 0.0)
        ts = lib.parse_iso(r.get("ts") or "")
        day = ts.astimezone(lib.JST).strftime("%Y-%m-%d") if ts else "-"
        by_day[day]["jobs"] += 1
        by_day[day]["credits"] += float(r.get("credits_est") or 0.0)

    if args.as_json:
        print(json.dumps({
            "ledger": str(path),
            "since": args.since,
            "usage_mode": mode,
            "jobs": len(rows),
            "by_model": {k: v for k, v in sorted(by_model.items())},
            "by_day": {k: v for k, v in sorted(by_day.items())},
            "credits_total": round(sum(v["credits"] for v in by_model.values()), 4),
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"台帳: {path}")
    print(f"usage_mode: {mode}"
          + ("（resume 行はスレッド累計とみなし直前行との差分で計上）" if mode == "cumulative"
             else "（台帳の値をそのまま合算）"))
    print(f"ジョブ数: {len(rows)}" + (f"（{args.since} 以降）" if args.since else ""))
    if not rows:
        return 0
    print("")
    print("## モデル別")
    print(f"{'model':<16} {'jobs':>5} {'input':>12} {'cached':>10} {'output':>10} {'credits':>10}")
    for k, v in sorted(by_model.items()):
        print(f"{k:<16} {v['jobs']:>5} {lib.fmt_tokens(v['input']):>12} "
              f"{lib.fmt_tokens(v['cached']):>10} {lib.fmt_tokens(v['output']):>10} "
              f"{v['credits']:>10.2f}")
    print("")
    print("## 日別（JST）")
    for k, v in sorted(by_day.items()):
        print(f"{k}  jobs={v['jobs']:>3}  credits={v['credits']:.2f}")
    print("")
    print(f"credits 合計: {sum(v['credits'] for v in by_model.values()):.2f}"
          "（ChatGPT プランのクレジット概算。単価は config/codex_pricing.json）")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex_job.py", description="codex-bridge のジョブ参照")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="ジョブの進捗・結果サマリ")
    s.add_argument("job_dir")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("result", help="Claude 向けの圧縮サマリ")
    r.add_argument("job_dir")
    r.add_argument("--json", dest="as_json", action="store_true", help="job.json 全体を出力")
    r.set_defaults(func=cmd_result)

    u = sub.add_parser("usage", help="使用量台帳の集計")
    u.add_argument("--since", default=None, help="この時刻以降のみ集計（ISO。naive は JST）")
    u.add_argument("--json", dest="as_json", action="store_true")
    u.add_argument("--usage-mode", dest="usage_mode", default=None,
                   choices=lib.USAGE_MODES,
                   help="resume 行の usage 解釈（既定は config/codex_bridge.json の usage_mode）")
    u.set_defaults(func=cmd_usage)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
