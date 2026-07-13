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

# タイトル（タスク名）表示設定。マーカー無し運用ではタスク名＝タスク内容（長文）になりやすいため
# 32pt/最大2行から縮小・行数拡張した。行送り34pxは元の32pt/42px比（約1.3125）を26ptに適用した値。
TITLE_FONT_SIZE = 26
TITLE_LINE_H = 34
TITLE_MAX_LINES = 3


# ---------------------------------------------------------------------------
# 共通レイアウト計算
# ---------------------------------------------------------------------------

def _card_height(
    n_models: int,
    width: int,
    n_title_lines: int = 1,
    n_desc_lines: int = 0,
    total_block_h: int = 240,
) -> int:
    """カード全体の論理高さ(px)を返す（scale とは独立。実px化は _ScaledDraw.S() が担う）。

    n_title_lines: タイトルの折り返し行数（最大3。1行超過分は論理34px/行（TITLE_LINE_H）で加算）。
    n_desc_lines: task_desc の折り返し行数（0=非表示。表示時は論理24px/行＋余白8pxを加算）。
    total_block_h: 下段合計ブロックに確保する論理高さ。既定240は「Fable(payg)>0 かつ
    included>0」の最も背の高いケース基準（render_pillow 側が分岐に応じて実測値を渡す）。
    引数はキーワード既定値付きで拡張しているため、chrome 側（render_chrome /
    _build_card_html）の既存呼び出し（位置引数2つのみ）は無改造で動作する。

    header_h の基礎値 152 は「タイトル1行（26pt/TITLE_LINE_H=34px）＋メタ情報行（経過）＋
    実処理時間行」の基本3行分（meta["active_text"] は常に存在するため実処理時間行は常時
    描画される）。旧値160は32pt/42px行送り基準だったため、行送りを34pxへ縮小した差分
    （42-34=8px）だけ引き下げている。
    """
    header_h = 152 + max(n_title_lines - 1, 0) * TITLE_LINE_H + (n_desc_lines * 24 + 8 if n_desc_lines else 0)
    table_header_h = 34
    row_h = 30
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


class _ScaledDraw:
    """論理座標系のまま呼び出せる、Retina(2x等)スケール描画用の薄いラッパ。

    定数群（pad・行送り等）は既存の論理pxのまま維持し、実ピクセル化だけをここに集約する。
    フォントは font() が最初から実サイズ（S(size)）で読み込む（キャッシュ付き）ため、
    textlength() は常に実px同士の比較になり、論理pxとの混在は起きない。
    """

    def __init__(self, draw: "ImageDraw.ImageDraw", scale: float, load_font) -> None:
        self._draw = draw
        self._scale = scale
        self._load_font = load_font
        self._font_cache: dict = {}

    def S(self, v: float) -> int:
        """論理px -> 実px。"""
        return int(round(v * self._scale))

    def font(self, size: int, bold: bool = False):
        key = (size, bold)
        f = self._font_cache.get(key)
        if f is None:
            f = self._load_font(self.S(size), bold=bold)
            self._font_cache[key] = f
        return f

    def text(self, xy, s: str, font, fill) -> None:
        x, y = xy
        self._draw.text((self.S(x), self.S(y)), s, font=font, fill=fill)

    def text_right(self, x_right: float, y: float, s: str, font, fill) -> None:
        w = self._draw.textlength(s, font=font)
        self._draw.text((self.S(x_right) - w, self.S(y)), s, font=font, fill=fill)

    def line(self, xy, fill, width: int = 1) -> None:
        x0, y0, x1, y1 = xy
        self._draw.line(
            (self.S(x0), self.S(y0), self.S(x1), self.S(y1)), fill=fill, width=max(1, self.S(width))
        )

    def measure_px(self, s: str, font) -> float:
        """実px単位での文字列幅（折り返し判定用）。"""
        return self._draw.textlength(s, font=font)

    def measure_logical(self, s: str, font) -> float:
        """論理px単位での文字列幅。text()/text_right() が受け取る x 座標（pad・width と同じ
        論理px系）と直接足し引きするための変換版（measure_px の実px結果を scale で割る）。
        """
        return self._draw.textlength(s, font=font) / self._scale


def _wrap_lines(measure, text: str, max_px: float, max_lines: int) -> list:
    """text を文字単位で折り返し max_lines 行に収める（日本語向け・禁則処理なし）。

    measure(s) は実px幅を返す callable。改行は空白に置換してから折り返す。
    max_lines を超える分がある場合は最終行を "…" が max_px に収まるまで縮めて付与する。
    1文字も入らない極端なケース（max_px が極端に小さい等）でも無限ループしない
    （`or not cur` で強制的に1文字は積む）。
    """
    text = (text or "").replace("\r", " ").replace("\n", " ")
    if not text:
        return []

    lines: list = []
    cur = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        candidate = cur + ch
        if measure(candidate) <= max_px or not cur:
            cur = candidate
            i += 1
        else:
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                break

    if cur and len(lines) < max_lines:
        lines.append(cur)

    if i < n:
        # 収まりきらない残りがある -> 最終行を "…" 付きで max_px に収める
        lines = lines[:max_lines] or [""]
        last = lines[-1]
        while last and measure(last + "…") > max_px:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines


def _truncate_ellipsis(measure, text: str, max_px: float) -> str:
    """1行のテキストを max_px に収まるよう末尾省略(…)する（収まっていればそのまま返す）。

    _wrap_lines() の末尾省略ロジックと同じ考え方を1行版として切り出したもの。
    テーブルのモデル名列が動的レイアウトの残り幅に収まらない場合の省略に使う。
    """
    if not text or measure(text) <= max_px:
        return text
    s = text
    while s and measure(s + "…") > max_px:
        s = s[:-1]
    return (s + "…") if s else "…"


def render_pillow(report: "lib.Report", meta: dict, out_path, config: dict) -> None:
    img_conf = config.get("image", {}) or {}
    width = int(img_conf.get("width", 1000))
    scale = max(1.0, min(float(img_conf.get("scale", 2)), 4.0))
    n_models = max(len(report.models), 1)

    load_font = _font_loader(config)
    pad = 40

    # 下段合計ブロックの行送り定数（高さ計算 _bottom_block_h() と実描画の両方で共有し、
    # 数値の二重管理を避ける）。
    SUB_LABEL_H = 22   # ラベル行 -> ヒーロー行
    HERO_H = 76        # ヒーロー行 -> JPY換算行
    JPY_H = 30         # JPY換算行 -> 次要素（included行 / 罫線+合計行 / ここで終端）
    INCLUDED_H = 26    # その他モデル参考行（included_usd>0 のときのみ加算）
    CLEARANCE_H = 16   # 罫線+合計行手前のクリアランス（payg>0 ケースのみ）
    TAIL_PAD = 70      # 最終行から footer までの余白（旧固定値 total_block_h=240 を
                       # 「payg>0 かつ included>0」ケース= 170 + 70 として踏襲）

    def _bottom_block_h() -> int:
        """下段合計ブロックの必要高さ(total_block_h)を描画分岐に応じて計算する。"""
        r = SUB_LABEL_H + HERO_H + JPY_H
        if report.payg_usd > 0:
            if report.included_usd > 0:
                r += INCLUDED_H
            r += CLEARANCE_H
        return r + TAIL_PAD

    # ①フォント準備（計測・本描画とも同じ _ScaledDraw.font() 経由で実サイズを共有する）
    #   ②計測用ダミー Draw でタイトル/desc の行数を先に確定する（高さ計算に必要なため）
    dummy_draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    probe = _ScaledDraw(dummy_draw, scale, load_font)
    max_text_px = probe.S(width) - 2 * probe.S(pad)

    title_text = meta.get("task_name") or "(無題タスク)"
    title_lines = _wrap_lines(
        lambda s: probe.measure_px(s, probe.font(TITLE_FONT_SIZE, bold=True)),
        title_text, max_text_px, max_lines=TITLE_MAX_LINES,
    ) or ["(無題タスク)"]

    task_desc = (meta.get("task_desc") or "").strip()
    task_name_stripped = (meta.get("task_name") or "").strip()
    desc_lines: list = []
    if task_desc and task_desc != task_name_stripped:
        desc_lines = _wrap_lines(
            lambda s: probe.measure_px(s, probe.font(16)), task_desc, max_text_px, max_lines=3
        )

    # ③ _card_height で高さ計算 -> ④実画像生成
    height = _card_height(
        n_models, width,
        n_title_lines=len(title_lines), n_desc_lines=len(desc_lines),
        total_block_h=_bottom_block_h(),
    )

    img = Image.new("RGB", (probe.S(width), probe.S(height)), BG)
    draw = ImageDraw.Draw(img)
    sd = _ScaledDraw(draw, scale, load_font)

    f_title = sd.font(TITLE_FONT_SIZE, bold=True)
    f_desc = sd.font(16)
    f_meta = sd.font(16)
    f_th = sd.font(13)
    f_td = sd.font(15)
    f_sub_label = sd.font(15)
    f_hero = sd.font(64, bold=True)
    f_hero_jpy = sd.font(20)
    f_sub_other = sd.font(14)
    f_grand = sd.font(22)
    f_footer = sd.font(12)

    # ⑤描画: 上段 タスク名（最大2行折り返し）
    y = pad
    for line in title_lines:
        sd.text((pad, y), line, font=f_title, fill=FG)
        y += TITLE_LINE_H

    # タスク内容（あれば MUTED 16pt 最大3行。空 or task_name と同一なら省略）
    if desc_lines:
        for line in desc_lines:
            sd.text((pad, y), line, font=f_desc, fill=MUTED)
            y += 24
        y += 8

    meta_line = (
        f"{meta.get('date_jst', '')}（JST） "
        f"{meta.get('start_jst', '')} 〜 {meta.get('end_jst', '')}"
        f"（経過 {meta.get('duration', '')}）"
    )
    sd.text((pad, y), meta_line, font=f_meta, fill=MUTED)
    y += 30

    # 実処理時間（active_text は meta に常に存在。計算不能時は "—"）
    active_line = f"実処理 {meta.get('active_text', '—')}"
    sd.text((pad, y), active_line, font=f_meta, fill=MUTED)
    y += 34
    sd.line((pad, y, width - pad, y), fill=LINE, width=1)
    y += 20

    # 中段: モデル別ミニ表（aggregate() のソートにより payg=Fable が先頭に来る）
    # 列は固定x座標ではなく実測幅ベースの動的レイアウトにする（キャッシュ書込 5m/1h 併記等で
    # 値が長くなっても隣列と接触しないように）。ヘッダ文字列と全行の値のうち描画幅が最大の
    # ものを probe/measure で実測し、右端の列（料金）から順に「右揃え位置 − 実測最大幅 −
    # 最小ギャップ」で1列ずつ左へ確定していく。ヘッダ・全行を同じ x 群で描画するため
    # 右揃えの基準は常に統一される。
    rows_cells = [_model_row_cells(m) for m in report.models]
    COL_GAP = 24  # 列間の最小ギャップ(論理px)

    def _col_max_px(header: str, values: list) -> float:
        # 論理px（col_right の x 座標系）で測る。measure_px は実px（scale後）を返すため
        # ここでは measure_logical を使う（実px のまま引き算すると scale>1 で列が過度に
        # 圧迫される unit ミスマッチになるので注意）。
        w = sd.measure_logical(header, f_th)
        for v in values:
            w = max(w, sd.measure_logical(v, f_td))
        return w

    cost_right = width - pad
    cost_w = _col_max_px("料金(USD)", [c[5] for c in rows_cells])
    output_right = cost_right - cost_w - COL_GAP
    output_w = _col_max_px("出力", [c[4] for c in rows_cells])
    read_right = output_right - output_w - COL_GAP
    read_w = _col_max_px("C読取", [c[3] for c in rows_cells])
    write_right = read_right - read_w - COL_GAP
    write_w = _col_max_px("C書込", [c[2] for c in rows_cells])
    input_right = write_right - write_w - COL_GAP
    input_w = _col_max_px("入力", [c[1] for c in rows_cells])
    # モデル名列は残り幅（pad 〜 入力列の左端の手前）を使い、収まらなければ末尾省略する。
    name_max_px = input_right - input_w - COL_GAP - pad

    col_right = {
        "input": input_right,
        "write": write_right,
        "read": read_right,
        "output": output_right,
        "cost": cost_right,
    }
    headers = [("モデル", pad, "left"), ("入力", col_right["input"], "right"),
               ("C書込", col_right["write"], "right"), ("C読取", col_right["read"], "right"),
               ("出力", col_right["output"], "right"), ("料金(USD)", col_right["cost"], "right")]
    for text, x, align in headers:
        if align == "left":
            sd.text((x, y), text, font=f_th, fill=MUTED)
        else:
            sd.text_right(x, y, text, font=f_th, fill=MUTED)
    y += 22
    sd.line((pad, y, width - pad, y), fill=LINE, width=1)
    y += 8

    for name, in_c, write_c, read_c, out_c, cost_c in rows_cells:
        name_draw = _truncate_ellipsis(lambda s: sd.measure_logical(s, f_td), name, name_max_px)
        sd.text((pad, y), name_draw, font=f_td, fill=FG)
        sd.text_right(col_right["input"], y, in_c, font=f_td, fill=FG)
        sd.text_right(col_right["write"], y, write_c, font=f_td, fill=FG)
        sd.text_right(col_right["read"], y, read_c, font=f_td, fill=FG)
        sd.text_right(col_right["output"], y, out_c, font=f_td, fill=FG)
        sd.text_right(col_right["cost"], y, cost_c, font=f_td, fill=FG)
        y += 30
        sd.line((pad, y - 8, width - pad, y - 8), fill=LINE, width=1)

    y += 18

    # 下段: Fable を最大表示（従量課金・要都度報告）、その他は参考、合計は別途表示
    if report.payg_usd > 0:
        sd.text_right(width - pad, y, "Fable（従量課金・要都度報告）", font=f_sub_label, fill=MUTED)
        y += SUB_LABEL_H
        sd.text_right(width - pad, y, f"${lib.fmt_usd(report.payg_usd, 2)}", font=f_hero, fill=FG)
        y += HERO_H
        sd.text_right(
            width - pad, y,
            f"¥{lib.fmt_jpy(report.payg_jpy)}（1 USD = {lib.fmt_jpy(report.usd_jpy)} 円）",
            font=f_hero_jpy, fill=MUTED,
        )
        y += JPY_H
        if report.included_usd > 0:
            sd.text_right(
                width - pad, y,
                f"その他モデル（Max20x込み・参考）: ${lib.fmt_usd(report.included_usd, 2)} / "
                f"¥{lib.fmt_jpy(report.included_jpy)}",
                font=f_sub_other, fill=MUTED,
            )
            y += INCLUDED_H
        y += CLEARANCE_H
        sd.line((pad, y - 8, width - pad, y - 8), fill=LINE, width=1)
        sd.text_right(
            width - pad, y,
            f"合計  ${lib.fmt_usd(report.total_usd, 2)} / ¥{lib.fmt_jpy(report.total_jpy)}",
            font=f_grand, fill=FG,
        )
    else:
        # Fable コスト0（opus/sonnet 等のみ）: ヒーローは合計に切替え、Fable行は「なし」を小さく添える
        sd.text_right(
            width - pad, y, "合計（Max20x サブスク込み・従量課金なし）", font=f_sub_label, fill=MUTED
        )
        y += SUB_LABEL_H
        sd.text_right(width - pad, y, f"${lib.fmt_usd(report.total_usd, 2)}", font=f_hero, fill=FG)
        y += HERO_H
        sd.text_right(
            width - pad, y,
            f"¥{lib.fmt_jpy(report.total_jpy)}（1 USD = {lib.fmt_jpy(report.usd_jpy)} 円）",
            font=f_hero_jpy, fill=MUTED,
        )
        y += JPY_H
        sd.text_right(width - pad, y, "Fable（従量課金）: なし", font=f_sub_other, fill=MUTED)

    # 隅: 単価 as_of
    stale_note = "（単価が古い可能性あり）" if report.stale else ""
    footer_text = f"単価 as_of: {report.pricing_as_of or '(不明)'}{stale_note}"
    sd.text_right(width - pad, height - 26, footer_text, font=f_footer, fill=FOOTER_COLOR)

    img.save(out_path, format="PNG")


# ---------------------------------------------------------------------------
# Chrome 版
# ---------------------------------------------------------------------------
# 凍結: これは pillow 版の失敗時フォールバック専用の簡易版。task_desc 表示・
# Fable/その他小計・Retina(2x) 対応等の新機能は pillow 版のみに実装している。
# 正となるレイアウトは常に render_pillow() 側。二重メンテを避けるため本関数と
# card.html.tmpl はこれ以上拡張しない方針とする。

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
