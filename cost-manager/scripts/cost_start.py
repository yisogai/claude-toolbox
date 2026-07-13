#!/usr/bin/env python3
"""fable-cost-manager: タスク開始マーカーを作成する（cost_report.py の集計範囲の起点）。

実行セッション（env CLAUDE_CODE_SESSION_ID）と現在の cwd を scope.sessions に自動登録する。
既に進行中タスクがある場合は exit 2 で案内する（--force で置換）。

終了コード:
    0 = 正常終了
    1 = その他エラー（config/pricing.json の欠落・破損 等）
    2 = 既に進行中タスクがある（--force で置換可能）

実行例:
    python3 scripts/cost_start.py --task "予算モニタ設計"
    python3 scripts/cost_start.py --task "調査タスク" --budget-usd 20
    python3 scripts/cost_start.py --task "やり直し" --force
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--task", required=True, help="タスク名")
    parser.add_argument("--budget-usd", type=float, default=None, help="予算（USD）。省略可。")
    parser.add_argument("--force", action="store_true", help="既存の進行中タスクを置換する")
    args = parser.parse_args()

    existing = lib.load_active_task()
    if existing and existing.get("status") == "active" and not args.force:
        print(
            f"エラー: 進行中タスク「{existing.get('task_name')}」（{existing.get('task_id')}）が既にあります。\n"
            f"  完了させるには: python3 scripts/cost_report.py --desc \"<要約>\"\n"
            f"  置き換えるには: python3 scripts/cost_start.py --task \"...\" --force",
            file=sys.stderr,
        )
        sys.exit(2)

    now_utc = datetime.now(timezone.utc)
    now_jst = lib.to_jst(now_utc)

    session_id = lib.current_session_id()
    cwd = os.getcwd()
    sessions = [{"session_id": session_id, "cwd": cwd, "added": "start"}] if session_id else []
    if not session_id:
        print(
            "警告: CLAUDE_CODE_SESSION_ID が未設定のため、実行セッションを自動登録できません。"
            "cost_report.py 実行時に --session で明示指定してください。",
            file=sys.stderr,
        )

    try:
        config = lib.load_config()
    except lib.ConfigError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    task_id = lib.make_task_id(now_jst)

    obj = {
        "task_id": task_id,
        "task_name": args.task,
        "budget_usd": args.budget_usd,
        "status": "active",
        "started_at": now_utc.isoformat().replace("+00:00", "Z"),
        "closed_at": None,
        "scope": {"mode": "session", "sessions": sessions},
        "usd_jpy_at_start": config.get("usd_jpy"),
        "thresholds_fired": [],
    }
    lib.save_active_task(obj)

    print(f"タスク開始マーカーを作成しました: {task_id}")
    print(f"  タスク名: {args.task}")
    print(f"  開始時刻: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} (JST)")
    if args.budget_usd is not None:
        print(f"  予算: ${lib.fmt_usd(args.budget_usd, 2)}")
    if session_id:
        print(f"  登録セッション: {session_id} @ {cwd}")


if __name__ == "__main__":
    main()
