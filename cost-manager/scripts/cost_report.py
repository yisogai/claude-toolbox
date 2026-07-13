#!/usr/bin/env python3
"""fable-cost-manager: タスク完了時にコストレポート（Markdown + PNG）を生成する。

範囲は「開始マーカー(cost_start.py) の started_at 〜 now」。マーカーが無ければ現在
セッション全体にフォールバックする。--since/--until を指定すると最優先で上書きする。
既定ではレポート発行後にマーカーを close して var/tasks/ へアーカイブする
（--keep-open で継続）。

終了コード:
    0 = 正常終了
    1 = その他エラー（config/pricing.json の欠落・破損、対象セッション特定不能 等）
    3 = 対象範囲にコストデータが0件（--since/--until やマーカー範囲、--scope を確認）

実行例:
    python3 scripts/cost_report.py --desc "設計レビューとdocs更新"
    python3 scripts/cost_report.py --desc "調査タスク" --scope global
    python3 scripts/cost_report.py --desc "画像なしで確認" --no-image
    python3 scripts/cost_report.py --desc "Chrome版カードで生成" --via chrome
    python3 scripts/cost_report.py --desc "期間指定" --since 2026-07-13T00:00:00Z --until 2026-07-13T06:00:00Z
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib
import render_md
import render_image


def _describe_scope(mode: str, sessions: list) -> str:
    if mode == "global":
        return "global（全プロジェクト走査。無関係なセッションが混入する可能性があります）"
    parts = []
    for s in sessions:
        sid = s.get("session_id") or "?"
        short = sid[:8] if sid != "?" else sid
        parts.append(f"{short}…@{s.get('cwd', '?')}")
    if not parts:
        return "session（対象セッション不明）"
    return "session（" + ", ".join(parts) + "）"


def _resolve_sessions(active, args) -> list:
    if active and active.get("scope", {}).get("sessions"):
        return active["scope"]["sessions"]
    sid = args.session or lib.current_session_id()
    if not sid:
        return []
    return [{"session_id": sid, "cwd": os.getcwd()}]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--desc", default=None, help="タスク内容の要約（1〜2行）。Claude が渡すのが第一。")
    parser.add_argument("--scope", choices=["session", "global"], default=None)
    parser.add_argument("--since", default=None, help="ISO8601（省略時はマーカー開始時刻 or セッション全体）")
    parser.add_argument("--until", default=None, help="ISO8601（省略時は現在時刻）")
    parser.add_argument("--no-image", action="store_true", help="PNG カードを生成しない")
    parser.add_argument("--via", choices=["pillow", "chrome"], default=None, help="画像レンダラ（省略時は config）")
    parser.add_argument("--keep-open", action="store_true", help="マーカーを close せず継続する")
    parser.add_argument("--session", default=None, help="対象セッションID（省略時はマーカー登録 or 現在のセッション）")
    args = parser.parse_args()

    try:
        config = lib.load_config()
        pricing = lib.load_pricing()
    except lib.ConfigError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    usd_jpy, usd_jpy_warn = lib.usd_jpy_from_config(config)

    now_utc = datetime.now(timezone.utc)
    now_jst = lib.to_jst(now_utc)

    active = lib.load_active_task()

    since_dt = lib.parse_iso(args.since) if args.since else None
    until_dt = lib.parse_iso(args.until) if args.until else None
    if since_dt is None and active:
        since_dt = lib.parse_iso(active["started_at"])
    if until_dt is None:
        until_dt = now_utc

    scope_mode = args.scope or (active.get("scope", {}).get("mode") if active else None) or config.get(
        "default_scope", "session"
    )

    if scope_mode == "global":
        tfiles = list(lib.iter_transcripts(glob_all=True, since=since_dt))
        sessions_for_desc = []
    else:
        sessions = _resolve_sessions(active, args)
        if not sessions:
            print(
                "エラー: 対象セッションを特定できません（CLAUDE_CODE_SESSION_ID 未設定・マーカー無し・--session 未指定）。",
                file=sys.stderr,
            )
            sys.exit(1)
        tfiles = []
        for s in sessions:
            tfiles.extend(lib.iter_transcripts(session_id=s["session_id"], cwd=s.get("cwd", os.getcwd())))
        sessions_for_desc = sessions

    collect_stats: dict = {}
    rows = lib.collect_dedup_rows(tfiles, since=since_dt, until=until_dt, stats=collect_stats)

    if not rows:
        msg = "エラー: 対象範囲にコストデータが0件です（--since/--until やマーカー範囲、--scope を確認してください）。"
        # cost_start 直後などで窓がほぼ空（now〜now）のケースはヒントを添える
        if since_dt is not None and (until_dt - since_dt).total_seconds() < 60:
            msg += "\n  ヒント: 開始直後で対象範囲がほぼ空です。作業後に実行するか、--since で範囲を指定してください。"
        print(msg, file=sys.stderr)
        sys.exit(3)

    report = lib.aggregate(rows, pricing, at=now_jst.date(), usd_jpy=usd_jpy)

    # タスク名・タスク内容
    task_name = (active.get("task_name") if active else None) or args.desc or "(タスク名未設定)"
    task_desc = args.desc
    if not task_desc:
        # limit=120: PNG カードの task_desc 表示が3行（_wrap_lines）まで活かせるよう既定80から引き上げ
        task_desc = lib.find_first_user_text(tfiles, since=since_dt, until=until_dt, limit=120) or task_name

    budget_usd = active.get("budget_usd") if active else None

    # 表示用の開始/終了時刻
    # since 明示時はそれを、無指定時は「窓内に採用した生データ行の最早 timestamp」を使う
    # （dedup 採用行=output最大 の timestamp は生データ最早行から数秒〜1分ずれるため）。
    if since_dt is not None:
        start_display_utc = since_dt
    else:
        start_display_utc = collect_stats.get("earliest_ts") or until_dt
    end_display_utc = until_dt

    start_jst = lib.to_jst(start_display_utc)
    end_jst = lib.to_jst(end_display_utc)
    duration_sec = (end_display_utc - start_display_utc).total_seconds()

    meta = {
        "task_name": task_name,
        "date_jst": end_jst.strftime("%Y-%m-%d"),
        "start_jst": start_jst.strftime("%H:%M"),
        "end_jst": end_jst.strftime("%H:%M"),
        "duration": lib.fmt_duration(duration_sec),
        "scope": _describe_scope(scope_mode, sessions_for_desc),
        "task_desc": task_desc,
        "generated_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "budget_usd": budget_usd,
        "stale_after_days": pricing.get("stale_after_days", 90),
    }

    # 出力先
    basename = lib.report_basename(now_jst, task_name)
    out_dir = lib.repo_root() / "reports" / now_jst.strftime("%Y") / now_jst.strftime("%m")
    md_path = out_dir / f"{basename}.md"
    png_path = out_dir / f"{basename}.png"

    md_text = render_md.render_report_md(report, meta)
    lib.atomic_write_text(md_path, md_text)

    image_warn = None
    if not args.no_image:
        via = args.via or (config.get("image", {}) or {}).get("renderer", "pillow")
        image_warn = render_image.render_card(report, meta, png_path, via=via, config=config)

    # reports.jsonl へ追記
    lib.append_report_log(
        {
            "generated_at": now_utc.isoformat().replace("+00:00", "Z"),
            "task_id": active.get("task_id") if active else None,
            "task_name": task_name,
            "scope_mode": scope_mode,
            "since": start_display_utc.isoformat().replace("+00:00", "Z"),
            "until": end_display_utc.isoformat().replace("+00:00", "Z"),
            "total_usd": report.total_usd,
            "total_jpy": report.total_jpy,
            "payg_usd": report.payg_usd,
            "included_usd": report.included_usd,
            "unknown_models": report.unknown_models,
            "md_path": str(md_path),
            "png_path": str(png_path) if not args.no_image else None,
        }
    )

    # マーカー close
    if active and not args.keep_open:
        closed = dict(active)
        closed["status"] = "closed"
        closed["closed_at"] = now_utc.isoformat().replace("+00:00", "Z")
        lib.archive_task(closed)

    # stdout 要約（Claude が要約報告に使う）
    print(f"レポートを生成しました: {md_path}")
    if not args.no_image:
        print(f"カード画像: {png_path}")
        if image_warn:
            print(f"警告: {image_warn}")
    print(f"合計: ${lib.fmt_usd(report.total_usd, 2)} / ¥{lib.fmt_jpy(report.total_jpy)}")
    if usd_jpy_warn:
        print(f"警告: {usd_jpy_warn}")
    dropped_no_ts = collect_stats.get("dropped_no_timestamp", 0)
    if dropped_no_ts:
        print(f"警告: timestamp 欠落の課金行 {dropped_no_ts} 件を範囲外として除外しました（--since/--until 指定時のみ）")
    if report.unknown_models:
        print(f"警告: 未計上モデルあり: {', '.join(report.unknown_models)}")
    if report.stale:
        print("警告: 単価情報が古い可能性があります（pricing.json の as_of を確認してください）")
    if active and not args.keep_open:
        print("マーカーを close しました（var/tasks/ へアーカイブ）")
    elif active and args.keep_open:
        print("マーカーは継続中です（--keep-open）")


if __name__ == "__main__":
    main()
