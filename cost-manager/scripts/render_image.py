#!/usr/bin/env python3
"""fable-cost-manager: コストレポートの1枚ペラ PNG カードを描画する。

既定は Pillow 直描画（--via pillow）。--via chrome を指定すると
templates/card.html.tmpl を一時 HTML に展開し、Google Chrome の
headless スクリーンショットで撮影する。失敗時は自動的に Pillow 版へ
フォールバックし、標準エラーに警告を出す。

通常は cost_report.py から `import render_image` して render_card() を呼ぶ。
このスクリプト単体でも --demo でサンプルカードを生成できる。

実行例:
    python3 scripts/render_image.py --demo --out /tmp/sample_card.png
    python3 scripts/render_image.py --demo --via chrome --out /tmp/sample_card_chrome.png
"""

import argparse
import html
import os
import shutil
import string
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib

from PIL import Image, ImageDraw, ImageFont

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

BG = (15, 17, 21)
CARD_BG_TOP = (20, 22, 29)
FG = (242, 242, 242)
MUTED = (166, 172, 189)
LINE = (38, 42, 53)
FOOTER_COLOR = (91, 96, 112)


# ---------------------------------------------------------------------------
# 共通レイアウト計算
# ---------------------------------------------------------------------------

def _card_height(n_models: int, width: int) -> int:
    header_h = 130
    table_header_h = 34
    row_h = 30
    total_block_h = 200
    footer_h = 50
    return header_h + table_header_h + row_h * max(n_models, 1) + total_block_h + footer_h


def _model_row_cells(m) -> tuple:
    write_cell = f"{lib.fmt_tokens(m.cache_write_5m)}/{lib.fmt_tokens(m.cache_write_1h)}"
    if m.known:
        cost_cell = lib.fmt_usd(m.cost_usd, 4)
        name = m.model
    else:
        cost_cell = "—"
        name = f"{m.model}（未計上）"
    return (
        name,
        lib.fmt_tokens(m.input_tokens),
        write_cell,
        lib.fmt_tokens(m.cache_read_tokens),
        lib.fmt_tokens(m.output_tokens),
        cost_cell,
    )


# ---------------------------------------------------------------------------
# Pillow 版
# ---------------------------------------------------------------------------

def _font_loader(config: dict):
    candidates = (config.get("image", {}) or {}).get("font_candidates", [])
    bold_order = list(candidates)
    regular_order = list(reversed(candidates)) if len(candidates) > 1 else list(candidates)

    def load(size: int, bold: bool = False):
        order = bold_order if bold else regular_order
        for path in order:
            try:
                if os.path.exists(path):
                    return ImageFont.truetype(path, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    return load


def _draw_right(draw, x_right, y, text, font, fill):
    w = draw.textlength(text, font=font)
    draw.text((x_right - w, y), text, font=font, fill=fill)


def render_pillow(report: "lib.Report", meta: dict, out_path, config: dict) -> None:
    width = int((config.get("image", {}) or {}).get("width", 1000))
    n_models = max(len(report.models), 1)
    height = _card_height(n_models, width)

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    load_font = _font_loader(config)
    f_title = load_font(32, bold=True)
    f_meta = load_font(16)
    f_th = load_font(13)
    f_td = load_font(15)
    f_total = load_font(52, bold=True)
    f_jpy = load_font(20)
    f_footer = load_font(12)

    pad = 40
    y = pad

    # 上段: タスク名 + 日付/時間帯
    draw.text((pad, y), meta.get("task_name") or "(無題タスク)", font=f_title, fill=FG)
    y += 44
    meta_line = (
        f"{meta.get('date_jst', '')}（JST） "
        f"{meta.get('start_jst', '')} 〜 {meta.get('end_jst', '')}"
        f"（実働 {meta.get('duration', '')}）"
    )
    draw.text((pad, y), meta_line, font=f_meta, fill=MUTED)
    y += 34
    draw.line((pad, y, width - pad, y), fill=LINE, width=1)
    y += 20

    # 中段: モデル別ミニ表
    col_right = {
        "input": width - pad - 420,
        "write": width - pad - 300,
        "read": width - pad - 180,
        "output": width - pad - 90,
        "cost": width - pad,
    }
    headers = [("モデル", pad, "left"), ("入力", col_right["input"], "right"),
               ("C書込", col_right["write"], "right"), ("C読取", col_right["read"], "right"),
               ("出力", col_right["output"], "right"), ("料金(USD)", col_right["cost"], "right")]
    for text, x, align in headers:
        if align == "left":
            draw.text((x, y), text, font=f_th, fill=MUTED)
        else:
            _draw_right(draw, x, y, text, f_th, MUTED)
    y += 22
    draw.line((pad, y, width - pad, y), fill=LINE, width=1)
    y += 8

    for m in report.models:
        name, in_c, write_c, read_c, out_c, cost_c = _model_row_cells(m)
        draw.text((pad, y), name, font=f_td, fill=FG)
        _draw_right(draw, col_right["input"], y, in_c, f_td, FG)
        _draw_right(draw, col_right["write"], y, write_c, f_td, FG)
        _draw_right(draw, col_right["read"], y, read_c, f_td, FG)
        _draw_right(draw, col_right["output"], y, out_c, f_td, FG)
        _draw_right(draw, col_right["cost"], y, cost_c, f_td, FG)
        y += 30
        draw.line((pad, y - 8, width - pad, y - 8), fill=LINE, width=1)

    y += 18

    # 下段: 大きく合計 USD / JPY
    total_usd_text = f"${lib.fmt_usd(report.total_usd, 2)}"
    _draw_right(draw, width - pad, y, total_usd_text, f_total, FG)
    y += 62
    total_jpy_text = f"¥{lib.fmt_jpy(report.total_jpy)}（1 USD = {lib.fmt_jpy(report.usd_jpy)} 円）"
    _draw_right(draw, width - pad, y, total_jpy_text, f_jpy, MUTED)

    # 隅: 単価 as_of
    stale_note = "（単価が古い可能性あり）" if report.stale else ""
    footer_text = f"単価 as_of: {report.pricing_as_of or '(不明)'}{stale_note}"
    fw = draw.textlength(footer_text, font=f_footer)
    draw.text((width - pad - fw, height - 26), footer_text, font=f_footer, fill=FOOTER_COLOR)

    img.save(out_path, format="PNG")


# ---------------------------------------------------------------------------
# Chrome 版
# ---------------------------------------------------------------------------

def _build_card_html(report: "lib.Report", meta: dict, width: int) -> str:
    tmpl_path = lib.code_root() / "templates" / "card.html.tmpl"
    with open(tmpl_path, encoding="utf-8") as f:
        tmpl = string.Template(f.read())

    # HTML 注入対策: task_name・モデル名など動的文字列は必ず html.escape してから埋める。
    # （Pillow 版は直描画のためエスケープ不要。Chrome 版=HTML のみここで実施する）
    rows_html = []
    for m in report.models:
        cells = _model_row_cells(m)  # (name, in_c, write_c, read_c, out_c, cost_c)
        tds = "".join(f"<td>{html.escape(str(c))}</td>" for c in cells)
        rows_html.append(f"        <tr>{tds}</tr>")
    height = _card_height(max(len(report.models), 1), width)
    stale_note = "（単価が古い可能性あり）" if report.stale else ""

    def esc(v) -> str:
        return html.escape(str(v))

    values = {
        # card_width / card_height は CSS の px 数値なのでエスケープしない（数値のみ）
        "card_width": width,
        "card_height": height,
        "task_name": esc(meta.get("task_name") or "(無題タスク)"),
        "date_jst": esc(meta.get("date_jst", "")),
        "start_jst": esc(meta.get("start_jst", "")),
        "end_jst": esc(meta.get("end_jst", "")),
        "duration": esc(meta.get("duration", "")),
        "model_rows_html": "\n".join(rows_html),
        "total_usd": esc(lib.fmt_usd(report.total_usd, 2)),
        "total_jpy": esc(lib.fmt_jpy(report.total_jpy)),
        "usd_jpy": esc(lib.fmt_jpy(report.usd_jpy)),
        "pricing_as_of": esc(report.pricing_as_of or "(不明)"),
        "stale_note": esc(stale_note),
    }
    return tmpl.substitute(values), height


def render_chrome(report: "lib.Report", meta: dict, out_path, config: dict) -> "tuple[bool, str]":
    """Chrome headless でカードを撮影する。戻り値: (成功したか, 警告/エラーメッセージ)。"""
    width = int((config.get("image", {}) or {}).get("width", 1000))
    if not os.path.exists(CHROME_BIN):
        return False, f"Chrome バイナリが見つかりません: {CHROME_BIN}"

    html_str, height = _build_card_html(report, meta, width)

    tmp_dir = tempfile.mkdtemp(prefix="fcm-card-")
    try:
        html_path = os.path.join(tmp_dir, "card.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_str)

        out_path = str(out_path)
        cmd = [
            CHROME_BIN,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            f"--screenshot={out_path}",
            f"file://{html_path}",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=25)
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, f"Chrome 実行に失敗しました: {e}"

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:400]
            return False, f"Chrome がエラー終了しました (code={proc.returncode}): {stderr}"
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return False, "Chrome はスクリーンショットを生成しませんでした"
        return True, ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def render_card(report: "lib.Report", meta: dict, out_path, via: str = "pillow", config=None) -> "str | None":
    """カード PNG を描画する。戻り値は警告メッセージ（無ければ None）。

    via="chrome" で失敗した場合は Pillow へフォールダックし、フォールバックした旨を返す。
    """
    if config is None:
        config = lib.load_config()

    if via == "chrome":
        ok, msg = render_chrome(report, meta, out_path, config)
        if ok:
            return None
        render_pillow(report, meta, out_path, config)
        return f"Chrome 版の生成に失敗したため Pillow 版にフォールバックしました: {msg}"

    render_pillow(report, meta, out_path, config)
    return None


def _demo_report_and_meta():
    # render_md.py の _demo_report_and_meta と同一データを使う（テンプレ・PNG両方で同じ見た目を確認するため）
    render_md_path = Path(__file__).resolve().parent
    sys.path.insert(0, str(render_md_path))
    import render_md as _rmd

    return _rmd._demo_report_and_meta()


def main():
    parser = argparse.ArgumentParser(description="コストレポート PNG カードを描画する（通常は cost_report.py から呼ばれる）。")
    parser.add_argument("--demo", action="store_true", help="サンプルデータでカードを生成する")
    parser.add_argument("--via", choices=["pillow", "chrome"], default="pillow")
    parser.add_argument("--out", default=None, help="出力先パス（省略時は $SCRATCH や cwd に demo_card.png）")
    args = parser.parse_args()

    if not args.demo:
        parser.error("現時点では --demo のみサポートしています（通常利用は cost_report.py から）")

    report, meta = _demo_report_and_meta()
    config = lib.load_config()
    out_path = args.out or "demo_card.png"
    warn = render_card(report, meta, out_path, via=args.via, config=config)
    print(f"書き出しました: {out_path}")
    if warn:
        print(f"警告: {warn}", file=sys.stderr)


if __name__ == "__main__":
    main()
