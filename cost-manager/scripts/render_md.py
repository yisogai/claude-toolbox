#!/usr/bin/env python3
"""fable-cost-manager: コストレポート本文（Markdown）を templates/report.md.tmpl から描画する。

通常は cost_report.py から `import render_md` して render_report_md() を呼ぶ。
このスクリプト単体でも --demo でサンプルレポートを描画し、テンプレ形式を目視確認できる。

実行例:
    python3 scripts/render_md.py --demo
    python3 scripts/render_md.py --demo --out /tmp/sample_report.md
"""

import argparse
import os
import string
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib


def _template_path() -> Path:
    return lib.code_root() / "templates" / "report.md.tmpl"


def build_model_rows(report: "lib.Report") -> str:
    """モデル別内訳テーブルの本体行（合計行含む）を Markdown で組み立てる。"""
    lines = []
    tot_in = tot_w5 = tot_w1h = tot_read = tot_out = 0
    tot_cost = 0.0
    for m in report.models:
        tot_in += m.input_tokens
        tot_w5 += m.cache_write_5m
        tot_w1h += m.cache_write_1h
        tot_read += m.cache_read_tokens
        tot_out += m.output_tokens
        if m.known:
            cost_str = f"{lib.fmt_usd(m.cost_usd, 4)}"
            tot_cost += m.cost_usd
            name = m.model
        else:
            cost_str = "—"
            name = f"{m.model}（未計上）"
        write_cell = f"{lib.fmt_tokens(m.cache_write_5m)}/{lib.fmt_tokens(m.cache_write_1h)}"
        lines.append(
            f"| {name} | {lib.fmt_tokens(m.input_tokens)} | {write_cell} | "
            f"{lib.fmt_tokens(m.cache_read_tokens)} | {lib.fmt_tokens(m.output_tokens)} | {cost_str} |"
        )
    write_total = f"{lib.fmt_tokens(tot_w5)}/{lib.fmt_tokens(tot_w1h)}"
    lines.append(
        f"| **合計** | {lib.fmt_tokens(tot_in)} | {write_total} | "
        f"{lib.fmt_tokens(tot_read)} | {lib.fmt_tokens(tot_out)} | **{lib.fmt_usd(tot_cost, 4)}** |"
    )
    return "\n".join(lines)


def build_unknown_note(report: "lib.Report") -> str:
    if not report.unknown_models:
        return ""
    names = "、".join(report.unknown_models)
    return f"- ⚠️ 未計上モデル: {names}（pricing.json に単価未定義のため料金集計から除外。トークン数のみ表示）\n"


def build_budget_note(report: "lib.Report", budget_usd) -> str:
    if not budget_usd:
        return ""
    pct = (report.total_usd / budget_usd * 100) if budget_usd else 0
    return f"- 予算 ${lib.fmt_usd(budget_usd, 2)} に対し消化 {pct:.1f}%\n"


def build_stale_note(report: "lib.Report", stale_after_days) -> str:
    if not report.stale:
        return ""
    return f"（⚠️ 単価情報が古い可能性: as_of から{stale_after_days}日超過）"


def render_report_md(report: "lib.Report", meta: dict, template_path=None) -> str:
    """report(集計結果) と meta(タスクメタ情報) から Markdown 本文を組み立てる。

    meta の必須キー: task_name, date_jst, start_jst, end_jst, duration, scope,
                      task_desc, generated_at_jst
    meta の任意キー（欠落時は既定値でフォールバック）: active_text（既定 "—"）,
                      budget_usd, stale_after_days
    """
    tmpl_path = Path(template_path) if template_path else _template_path()
    with open(tmpl_path, encoding="utf-8") as f:
        tmpl = string.Template(f.read())

    values = {
        "task_name": meta.get("task_name") or "(無題タスク)",
        "date_jst": meta.get("date_jst", ""),
        "start_jst": meta.get("start_jst", ""),
        "end_jst": meta.get("end_jst", ""),
        "duration": meta.get("duration", ""),
        "active_text": meta.get("active_text", "—"),
        "scope": meta.get("scope", ""),
        "task_desc": meta.get("task_desc") or "(要約未指定)",
        "model_rows": build_model_rows(report),
        "total_usd": lib.fmt_usd(report.total_usd, 2),
        "total_jpy": lib.fmt_jpy(report.total_jpy),
        "payg_usd": lib.fmt_usd(report.payg_usd, 2),
        "payg_jpy": lib.fmt_jpy(report.payg_jpy),
        "included_usd": lib.fmt_usd(report.included_usd, 2),
        "included_jpy": lib.fmt_jpy(report.included_jpy),
        "usd_jpy": lib.fmt_jpy(report.usd_jpy),
        "unknown_note": build_unknown_note(report),
        "budget_note": build_budget_note(report, meta.get("budget_usd")),
        "pricing_as_of": report.pricing_as_of or "(不明)",
        "stale_note": build_stale_note(report, meta.get("stale_after_days", 90)),
        "generated_at_jst": meta.get("generated_at_jst", ""),
    }
    return tmpl.substitute(values)


def _demo_report_and_meta():
    pricing = lib.load_pricing()
    rows = [
        {
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 120,
                "cache_read_input_tokens": 500000,
                "cache_creation": {"ephemeral_5m_input_tokens": 20000, "ephemeral_1h_input_tokens": 0},
                "output_tokens": 15000,
            },
        },
        {
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 900,
                "cache_read_input_tokens": 1200000,
                "cache_creation": {"ephemeral_5m_input_tokens": 80000, "ephemeral_1h_input_tokens": 0},
                "output_tokens": 40000,
            },
        },
        {
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 40,
                "cache_read_input_tokens": 90000,
                "cache_creation": {"ephemeral_5m_input_tokens": 5000, "ephemeral_1h_input_tokens": 0},
                "output_tokens": 8000,
            },
        },
        {
            "model": "claude-haiku-9-9",  # わざと未知モデルにする（警告表示の確認用）
            "usage": {"input_tokens": 10, "output_tokens": 500},
        },
    ]
    at = date(2026, 7, 13)
    report = lib.aggregate(rows, pricing, at=at, usd_jpy=160)
    now_jst = lib.to_jst(datetime.now(timezone.utc))
    start_jst = now_jst - timedelta(hours=1, minutes=30)
    meta = {
        # 60字級の長いタスク名（PNG カードの2行折り返し・末尾省略表示を確認するため意図的に長くしている）
        "task_name": (
            "デモタスク: fable-cost-manager レポート改修（タイトル折り返し・タスク内容表示・"
            "Fable小計・Retina対応）の動作確認一式"
        ),
        "date_jst": now_jst.strftime("%Y-%m-%d"),
        "start_jst": start_jst.strftime("%H:%M"),
        "end_jst": now_jst.strftime("%H:%M"),
        "duration": lib.fmt_duration((now_jst - start_jst).total_seconds()),
        "active_text": "1時間2分（経過の69%）",
        "scope": "session（デモデータ）",
        # 120字級の長い task_desc（PNG カードの3行折り返し・末尾省略表示を確認するため意図的に長くしている）
        "task_desc": (
            "render_md.py --demo と render_image.py --demo の両方で同一のデモデータを用い、"
            "タイトル2行折り返し・タスク内容3行表示・Fable小計とその他モデル小計の分離表示・"
            "Retina(2x)スケール描画の4点をまとめて目視確認するためのサンプルレポートです。"
        ),
        "generated_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "budget_usd": 20.0,
    }
    return report, meta


def main():
    parser = argparse.ArgumentParser(
        description="コストレポート Markdown を描画する（通常は cost_report.py から呼ばれる）。"
    )
    parser.add_argument("--demo", action="store_true", help="サンプルデータでテンプレ形式を確認する")
    parser.add_argument("--out", default=None, help="出力先パス（省略時は標準出力）")
    args = parser.parse_args()

    if not args.demo:
        parser.error("現時点では --demo のみサポートしています（通常利用は cost_report.py から）")

    report, meta = _demo_report_and_meta()
    text = render_report_md(report, meta)
    if args.out:
        lib.atomic_write_text(args.out, text)
        print(f"書き出しました: {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
