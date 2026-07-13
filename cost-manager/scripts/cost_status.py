#!/usr/bin/env python3
"""fable-cost-manager: 進行中タスクの途中経過（消化額・消化率・ペース・ETA）を表示する。

開始マーカー（cost_start.py）があればその基準で、無ければ現在セッション全体を
予算なしで表示する。マーカーの scope.sessions が空のときは cost_report.py と同じく
現在セッション（--session / CLAUDE_CODE_SESSION_ID）へフォールバックする。
--json はフェーズ2の statusline がそのまま読める形式。

終了コード:
    0 = 正常終了
    1 = その他エラー（config/pricing.json の欠落・破損、進行中タスク無し かつ
        現在セッションも特定不能 等）

実行例:
    python3 scripts/cost_status.py
    python3 scripts/cost_status.py --json
    python3 scripts/cost_status.py --session <session-id>
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib

# 経過秒数がこれ未満なら $/h ペース・ETA 計算をゼロ除算扱いで打ち切る
MIN_ELAPSED_SEC_FOR_RATE = 5.0


def _resolve_sessions(active, args) -> list:
    """対象セッション一覧を解決する（cost_report.py と同じフォールバック）。

    マーカーの scope.sessions が空なら --session / CLAUDE_CODE_SESSION_ID へフォールバック。
    それでも特定できなければ空リストを返す（呼び出し側で警告する）。
    """
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
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument(
        "--session", default=None,
        help="対象セッションID（省略時はマーカー登録 or 現在のセッション）",
    )
    args = parser.parse_args()

    try:
        config = lib.load_config()
        pricing = lib.load_pricing()
    except lib.ConfigError as e:
        msg = str(e)
        if args.json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"エラー: {msg}", file=sys.stderr)
        sys.exit(1)

    usd_jpy, usd_jpy_warn = lib.usd_jpy_from_config(config)
    now_utc = datetime.now(timezone.utc)
    now_jst = lib.to_jst(now_utc)

    active = lib.load_active_task()

    if active:
        since_dt = lib.parse_iso(active["started_at"])
        budget_usd = active.get("budget_usd")
        task_name = active.get("task_name")
        task_id = active.get("task_id")
    else:
        since_dt = None
        budget_usd = None
        task_name = None
        task_id = None

    warnings = []
    if usd_jpy_warn:
        warnings.append(usd_jpy_warn)

    # マーカーの scope.sessions が空でも --session / CLAUDE_CODE_SESSION_ID へフォールバック
    sessions = _resolve_sessions(active, args)

    if not sessions:
        if not active:
            # 進行中タスクも現在セッションも無い -> 従来どおり致命エラー
            msg = "進行中タスクなし・現在のセッションも特定できません（CLAUDE_CODE_SESSION_ID 未設定 / --session 未指定）。"
            if args.json:
                print(json.dumps({"error": msg}, ensure_ascii=False))
            else:
                print(msg)
            sys.exit(1)
        # マーカーはあるが対象セッションを特定できない -> 警告して $0 継続
        warnings.append(
            "対象セッションを特定できません（マーカーの scope.sessions が空・--session 未指定・"
            "CLAUDE_CODE_SESSION_ID 未設定）。消化額は $0 と表示されます。"
        )

    tfiles = []
    for s in sessions:
        tfiles.extend(lib.iter_transcripts(session_id=s["session_id"], cwd=s.get("cwd", os.getcwd())))

    if sessions and not tfiles:
        warnings.append(
            "走査対象の transcript ファイルが0件です（セッションID・cwd を確認してください）。"
            "消化額は $0 と表示されます。"
        )

    collect_stats: dict = {}
    rows = lib.collect_dedup_rows(tfiles, since=since_dt, until=now_utc, stats=collect_stats)
    report = lib.aggregate(rows, pricing, at=now_jst.date(), usd_jpy=usd_jpy)

    dropped_no_ts = collect_stats.get("dropped_no_timestamp", 0)
    if dropped_no_ts:
        warnings.append(
            f"timestamp 欠落の課金行 {dropped_no_ts} 件を範囲外として除外しました。"
        )

    if since_dt is not None:
        start_utc = since_dt
    else:
        start_utc = collect_stats.get("earliest_ts") or now_utc

    elapsed_sec = (now_utc - start_utc).total_seconds()

    rate_per_hour = None
    eta_jst = None
    eta_display = "—"
    if elapsed_sec >= MIN_ELAPSED_SEC_FOR_RATE and report.total_usd > 0:
        rate_per_hour = report.total_usd / (elapsed_sec / 3600.0)
        if budget_usd and rate_per_hour > 0 and report.total_usd < budget_usd:
            remain_usd = budget_usd - report.total_usd
            eta_hours = remain_usd / rate_per_hour
            eta_jst = now_jst + timedelta(hours=eta_hours)
            eta_display = eta_jst.strftime("%Y-%m-%d %H:%M")
        elif budget_usd and report.total_usd >= budget_usd:
            eta_display = "予算到達済み"

    pct = (report.total_usd / budget_usd * 100) if budget_usd else None

    if args.json:
        payload = {
            "task_id": task_id,
            "task_name": task_name,
            "has_marker": active is not None,
            "since": start_utc.isoformat().replace("+00:00", "Z"),
            "now": now_utc.isoformat().replace("+00:00", "Z"),
            "elapsed_sec": elapsed_sec,
            "total_usd": report.total_usd,
            "total_jpy": report.total_jpy,
            "usd_jpy": report.usd_jpy,
            "budget_usd": budget_usd,
            "budget_pct": pct,
            "rate_usd_per_hour": rate_per_hour,
            "eta_jst": eta_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00") if eta_jst else None,
            "unknown_models": report.unknown_models,
            "stale": report.stale,
            "warnings": warnings,
            "models": [
                {
                    "model": m.model,
                    "input_tokens": m.input_tokens,
                    "cache_write_5m": m.cache_write_5m,
                    "cache_write_1h": m.cache_write_1h,
                    "cache_read_tokens": m.cache_read_tokens,
                    "output_tokens": m.output_tokens,
                    "cost_usd": m.cost_usd,
                    "known": m.known,
                }
                for m in report.models
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"タスク: {task_name or '(マーカー無し・現在セッション全体)'}")
    if task_id:
        print(f"  task_id: {task_id}")
    print(f"開始: {lib.to_jst(start_utc).strftime('%Y-%m-%d %H:%M:%S')} (JST) / 経過: {lib.fmt_duration(elapsed_sec)}")
    print(f"消化額: ${lib.fmt_usd(report.total_usd, 2)} / ¥{lib.fmt_jpy(report.total_jpy)}")
    if budget_usd:
        print(f"予算: ${lib.fmt_usd(budget_usd, 2)}（消化率 {pct:.1f}%）")
    else:
        print("予算: 未設定")
    if rate_per_hour is not None:
        print(f"ペース: ${lib.fmt_usd(rate_per_hour, 2)}/h")
    else:
        # ペース算出不能の理由を分岐（経過極小か、消化ゼロか）
        if elapsed_sec < MIN_ELAPSED_SEC_FOR_RATE:
            reason = "経過時間が短いため算出できません"
        elif report.total_usd <= 0:
            reason = "消化がまだありません"
        else:
            reason = "算出できません"
        print(f"ペース: — （{reason}）")
    if budget_usd:
        print(f"予算到達 ETA: {eta_display}")
    print("モデル別内訳:")
    if not report.models:
        print("  (データなし)")
    for m in report.models:
        cost_str = lib.fmt_usd(m.cost_usd, 4) if m.known else "—（未計上モデル）"
        print(
            f"  - {m.model}: 入力={lib.fmt_tokens(m.input_tokens)} "
            f"C書込={lib.fmt_tokens(m.cache_write_5m)}/{lib.fmt_tokens(m.cache_write_1h)} "
            f"C読取={lib.fmt_tokens(m.cache_read_tokens)} 出力={lib.fmt_tokens(m.output_tokens)} "
            f"料金=${cost_str}"
        )
    for w in warnings:
        print(f"警告: {w}")
    if report.unknown_models:
        print(f"警告: 未計上モデルあり: {', '.join(report.unknown_models)}")
    if report.stale:
        print("警告: 単価情報が古い可能性があります")


if __name__ == "__main__":
    main()
