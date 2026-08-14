#!/usr/bin/env python3
"""usage-report CLI エントリ。

指定ディレクトリ（既定 cwd）の子孫ディレクトリで実行された全 Claude Code セッションを
期間指定で集計し、CSV 2枚 + PNG 最大4枚 + summary.md を出力する。

終了コード: 0=正常（0セッションでも summary.md を出せば 0） / 1=引数・環境エラー /
2=対象ディレクトリ不存在。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import usage_lib as ul   # noqa: E402
from usage_lib import lib  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="usage_report.py",
        description="ディレクトリ配下の全 Claude Code セッションを期間集計する",
    )
    p.add_argument("--root", default=None, help="集計対象ルート（既定: カレントディレクトリ）")
    p.add_argument("--month", default=None, help="JST 月次（例 2026-07）")
    p.add_argument("--week", default=None, help="JST 週次・月曜始まり（this / last / 2026-W33）")
    p.add_argument("--from", dest="from_s", default=None, help="開始 ISO（naive は JST 解釈、含む）")
    p.add_argument("--to", dest="to_s", default=None, help="終了 ISO（naive は JST 解釈、含まない）")
    p.add_argument("--out-dir", default=None, help="出力先ディレクトリ（既定: reports/YYYY/MM/...）")
    p.add_argument("--format", dest="fmt", default="csv,png,md",
                   help="出力形式のカンマ区切り（csv,png,md）")
    p.add_argument("--no-active", action="store_true", help="実処理時間の算出をスキップ")
    p.add_argument("--label", default=None, help="レポート見出し・出力ディレクトリ名のラベル")
    p.add_argument("--summarize", action="store_true",
                   help="LLM 要約を有効化（既定: 無効。決定論ダイジェストは常に出る）")
    p.add_argument("--summarize-model", default="haiku", help="要約に使うモデル（既定 haiku）")
    p.add_argument("--summarize-timeout", type=float, default=300.0,
                   help="要約の claude 呼び出しタイムアウト秒（既定 300）")
    p.add_argument("--no-summary-cache", action="store_true",
                   help="要約キャッシュを無視して再生成する（書込はする）")
    p.add_argument("--phase-gap-min", type=int, default=30,
                   help="フェーズ分割の空白しきい値（分。既定 30）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.root or os.getcwd()))
    if not os.path.isdir(root):
        print(f"エラー: 対象ディレクトリが存在しません: {root}", file=sys.stderr)
        return 2

    formats = {f.strip().lower() for f in args.fmt.split(",") if f.strip()}
    unknown_fmt = formats - {"csv", "png", "md"}
    if unknown_fmt:
        print(f"エラー: --format に未知の値: {', '.join(sorted(unknown_fmt))}", file=sys.stderr)
        return 1
    if not formats:
        print("エラー: --format が空です。", file=sys.stderr)
        return 1

    try:
        period = ul.resolve_period(args.month, args.week, args.from_s, args.to_s)
        config = lib.load_config()
        pricing = lib.load_pricing()
    except ul.UsageReportError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except lib.ConfigError as exc:
        print(f"エラー: 設定の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    usd_jpy, fx_warn = lib.usd_jpy_from_config(config)
    gap = float(config.get("active_gap_max_sec", 900))
    at = ul.pricing_at(period)
    label = args.label or os.path.basename(root.rstrip(os.sep)) or root

    warnings = []
    if fx_warn:
        warnings.append(fx_warn)
    if period.defaulted:
        print(f"期間フラグが無いため当月（JST {period.label}）を集計します。")

    with_active = not args.no_active

    sessions = ul.collect_sessions(root, warnings)
    agg = ul.aggregate_all(
        root=root, period=period, sessions=sessions, pricing=pricing,
        usd_jpy=usd_jpy, at=at, warnings=warnings,
        with_active=with_active, gap_max_sec=gap,
    )

    out_dir = (
        Path(os.path.abspath(os.path.expanduser(args.out_dir)))
        if args.out_dir
        else ul.default_out_dir(root, label, period.label)
    )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"エラー: 出力ディレクトリを作成できません: {out_dir}（{exc}）", file=sys.stderr)
        return 1

    # --- 作業内容サマリ ---
    # 第1段（決定論ダイジェスト）は常に走らせ、digests.json も --format に関係なく出す。
    phase_gap = max(1, int(args.phase_gap_min))
    digests = ul.build_digests(agg.sessions, period, phase_gap)
    summaries = ul.deterministic_summaries(digests)
    overview = ""
    summary_note = ""
    summarize_line = ""
    llm_sids = set()          # LLM 由来の要約が付いたセッション（カード表示の判断に使う）
    if args.summarize:
        # LLM の出力は任意の形になりうる。型検査を網羅しきる前提に立たず、
        # 要約まわりの例外はすべてここで受けて決定論ダイジェストに縮退する
        # （集計結果そのものを失わないため。exit 0 を維持）。
        res = {}
        try:
            import summarize as sm
            res = sm.summarize(
                digests=digests,
                sessions=agg.sessions,
                model=args.summarize_model,
                cache_dir=ul.summaries_dir(),
                period=period,
                timeout_sec=args.summarize_timeout,
                warnings=warnings,
                use_cache=not args.no_summary_cache,
            )
        except Exception as exc:      # noqa: BLE001 - 要約の失敗でレポートを落とさない
            warnings.append(
                f"LLM 要約の処理に失敗しました（{type(exc).__name__}: {exc}）。"
                "決定論ダイジェストのみでレポートを出力しています。"
            )
        for sid, entry in (res.get("sessions") or {}).items():
            if entry.get("summary"):
                summaries[sid] = entry
                llm_sids.add(sid)
        overview = res.get("overview") or ""
        meta = res.get("meta") or {}
        if meta.get("used") or overview:
            summary_note = (
                f"要約は {meta.get('model')} で {meta.get('generated_at')} に生成"
                "（キャッシュ: var/summaries）。"
            )
        if meta.get("used"):
            summarize_line = (
                f"要約: {meta.get('used')} セッション"
                f"（キャッシュ {meta.get('cached')} 件 / 生成 {meta.get('generated')} 件、"
                f"モデル {meta.get('model')}）"
            )

    written = []
    pdg = out_dir / "digests.json"
    ul.write_text(pdg, ul.digests_json(digests, root, period, phase_gap))
    written.append(pdg)

    if "csv" in formats:
        p1 = out_dir / "sessions.csv"
        ul.write_csv(p1, ul.build_sessions_csv(agg, with_active, summaries))
        written.append(p1)
        p2 = out_dir / "sessions_by_model.csv"
        ul.write_csv(p2, ul.build_sessions_by_model_csv(agg))
        written.append(p2)

    if "png" in formats:
        try:
            import charts
        except Exception as exc:   # matplotlib が無い環境では degrade
            warnings.append(
                f"PNG をスキップしました（matplotlib を読み込めません: {exc}）。"
            )
        else:
            written.extend(_render_charts(
                charts, agg, label, with_active, out_dir, warnings,
                summaries, llm_sids))

    if "md" in formats:
        pmd = out_dir / "summary.md"
        ul.write_text(pmd, ul.build_summary_md(
            agg, label, with_active, summaries, overview, summary_note,
            phase_counts={sid: len(d.get("phases") or []) for sid, d in digests.items()}))
        written.append(pmd)

    # --- stdout 報告 ---
    print("")
    print(f"出力ディレクトリ: {out_dir}")
    for p in written:
        print(f"  - {p.name}")
    print(f"対象: {root}（子孫ディレクトリを含む）")
    print(f"期間: {period.describe()} / ラベル: {period.label}")
    print(f"セッション数: {len(agg.sessions)}")
    print(f"合計コスト（従量仮計算）: ${agg.report.total_usd:,.2f} / "
          f"¥{agg.report.total_jpy:,.0f}")
    note = ul.unknown_note(agg)
    if note:
        print(f"  ※ {note}")
    if with_active:
        print(f"実処理時間: {lib.fmt_duration(agg.active_sec_total)}")
    if summarize_line:
        print(summarize_line)
    if agg.warnings:
        print("警告:")
        for w in agg.warnings:
            print(f"  - {w}")
    if agg.notes:
        print("注記:")
        for n in agg.notes:
            print(f"  - {n}")
    return 0


def _render_charts(charts, agg, label, with_active, out_dir: Path, warnings: list,
                   summaries=None, llm_sids=None) -> list:
    written = []
    active_text = lib.fmt_duration(agg.active_sec_total) if with_active else "—"
    # Top3 は**タイトルを主行**にし、LLM 要約があるときだけ副行として添える
    # （決定論要約は「主要ディレクトリ + 件数」なのでカードの幅では断片になり、
    #  タイトルより情報量が落ちる。決定論要約は CSV / summary.md 側で読ませる）。
    # charts 側のシグネチャは変えず、"タイトル\n要約" の1文字列で渡す。
    llm_sids = llm_sids or set()
    top3 = []
    for s in agg.sessions[:3]:
        text = s.title or "(タイトルなし)"
        if s.session_id in llm_sids:
            summ = ul.summary_of(summaries, s.session_id)
            if summ:
                text = f"{text}\n{summ}"
        top3.append((text, s.report.total_usd))
    unknown_note = ul.unknown_note(agg)

    jobs = [
        ("summary_card.png", lambda: charts.render_summary_card(
            label, agg.period.describe(), agg.root,
            agg.report.total_usd, agg.report.total_jpy,
            len(agg.sessions), active_text, top3,
            unknown_note, ul.unknown_badge(agg))),
        ("daily_cost.png", lambda: charts.render_daily_cost(
            agg.daily, agg.period.since_jst.date(),
            lib.to_jst(agg.period.until - timedelta(seconds=1)).date() + timedelta(days=1))),
        ("repo_breakdown.png", lambda: charts.render_repo_breakdown(agg.repo_costs)),
        ("model_breakdown.png", lambda: charts.render_model_breakdown(ul.model_totals(agg))),
    ]
    for name, fn in jobs:
        if name in ("repo_breakdown.png",) and not agg.repo_costs:
            continue
        if name == "model_breakdown.png" and not agg.report.models:
            continue
        try:
            data = fn()
        except Exception as exc:
            warnings.append(f"{name} の描画に失敗しました: {exc}")
            continue
        w, h = ul.png_size(data)
        if not data or w <= 0 or h <= 0:
            warnings.append(f"{name} の生成結果が不正です（size={len(data)} bytes {w}x{h}）。")
            continue
        path = out_dir / name
        ul.write_png(path, data)
        written.append(path)
    return written


if __name__ == "__main__":
    sys.exit(main())
