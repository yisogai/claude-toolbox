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


def build_codex_section(codex) -> str:
    """Codex（codex-bridge の使用量台帳）の参考表を Markdown で組み立てる。

    codex は cost_report.py が作る集計 dict（`credits` / `jobs` / `by_model` /
    `ignored_rows` / `ledger_path` / `notes`）。None または 0 件なら空文字を返す
    （＝レポートに Codex 節を出さない。既存レポートの体裁は変わらない）。
    """
    if not codex or not codex.get("jobs"):
        return ""
    lines = [
        "",
        "## Codex（参考）",
        "",
        "codex-bridge の使用量台帳（`codex_usage.jsonl`）のうち、この範囲の行だけを集計したものです。"
        "クレジットは ChatGPT プランの概算で、USD 合計には含めていません。",
        "",
        "| モデル | 件数 | クレジット | 出力トークン |",
        "| --- | ---: | ---: | ---: |",
    ]
    tot_jobs = tot_out = 0
    tot_cr = 0.0
    by_model = codex.get("by_model") or {}
    for name, v in sorted(by_model.items(), key=lambda kv: -(kv[1].get("credits") or 0)):
        jobs = int(v.get("jobs") or 0)
        out_tok = int(v.get("output_tokens") or 0)
        cr = float(v.get("credits") or 0)
        tot_jobs += jobs
        tot_out += out_tok
        tot_cr += cr
        label = name if v.get("known", True) else f"{name}（単価未収載）"
        lines.append(f"| {label} | {jobs} | {lib.fmt_credits(cr)} | {lib.fmt_tokens(out_tok)} |")
    lines.append(
        f"| **合計** | {tot_jobs} | **{lib.fmt_credits(tot_cr)}** | {lib.fmt_tokens(tot_out)} |"
    )
    lines.append("")
    ignored = codex.get("ignored_rows") or 0
    lines.append(f"- 台帳: `{codex.get('ledger_path')}`" + (f"（無視した行 {ignored} 件）" if ignored else ""))
    for n in codex.get("notes") or []:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def render_report_md(report: "lib.Report", meta: dict, template_path=None) -> str:
    """report(集計結果) と meta(タスクメタ情報) から Markdown 本文を組み立てる。

    meta の必須キー: task_name, date_jst, start_jst, end_jst, duration, scope,
                      task_desc, generated_at_jst
    meta の任意キー（欠落時は既定値でフォールバック）: active_text（既定 "—"）,
                      budget_usd, stale_after_days, codex（Codex 台帳の集計 dict。
                      None または 0 件なら Codex 節を出さない）
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
        "codex_section": build_codex_section(meta.get("codex")),
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
        # 推奨形を反映: --task 相当の短いタスク名（15字程度）。
        "task_name": "請求システム改修",
        "date_jst": now_jst.strftime("%Y-%m-%d"),
        "start_jst": start_jst.strftime("%H:%M"),
        "end_jst": now_jst.strftime("%H:%M"),
        "duration": lib.fmt_duration((now_jst - start_jst).total_seconds()),
        "active_text": "1時間2分（経過の69%）",
        "scope": "session（デモデータ）",
        # 推奨形を反映: --desc 相当の、非エンジニアにも伝わる平易な日本語で1〜2行の要約。
        "task_desc": (
            "お客様への請求書で金額の端数がずれる不具合を直しました。"
            "あわせてキャンペーン割引の表示もわかりやすく整理しています。"
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
