#!/usr/bin/env python3
"""usage-report の PNG 描画（matplotlib）。

matplotlib は任意依存。import に失敗した場合は呼び出し側が PNG をスキップして
CSV / Markdown だけを生成する（exit 0）ため、このモジュールは import 失敗を
握りつぶさずそのまま送出する。

デザイン規範（dataviz スキル準拠）:
- ダークテーマ固定。surface #1a1a19 / text #ffffff / secondary #c3c2b7。
- モデル色は**固定割当**（順序・ランクで塗らない）。凡例必須。
- 積み上げ・隣接バーは surface 色 2px の gap で分離（枠線ではなく隙間で分ける）。
- 二軸禁止。全点数値ラベル禁止（選択的な直接ラベルのみ）。
"""

from __future__ import annotations

import io
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")   # GUI backend（既定 macosx）を避ける。import 直後に必須。

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# --- パレット ---------------------------------------------------------------
SURFACE = "#1a1a19"
TEXT = "#ffffff"
TEXT_SECONDARY = "#c3c2b7"
GRID = "#3a3a37"

BLUE = "#3987e5"
ORANGE = "#d95926"
AQUA = "#199e70"
YELLOW = "#c98500"
GRAY = "#c3c2b7"

DPI = 200


# 同一ファミリーに複数世代（opus-5 と opus-4-8 等）が同時に出ると 1 色では区別できない。
# ファミリー基調色を保ちつつ、**モデル名から決まる固定の濃淡**を割り当てる（出現順・
# ランクでは決めないので、フィルタしても色は動かない = recolor-on-filter 回避）。
FAMILY_SHADES = {
    "fable": [BLUE, "#7fb4f0"],
    "opus": [ORANGE, "#f2915f"],
    "sonnet": [AQUA, "#57c9a3"],
    "haiku": [YELLOW, "#e8b74a"],
    "other": [GRAY, "#8a897f"],
}

# 実データで出現する主要モデルは濃淡まで固定する（レポート間で色がぶれないように）。
MODEL_SHADE_INDEX = {
    "claude-fable-5": 0,
    "claude-opus-5": 0,
    "claude-opus-4-8": 1,
    "claude-sonnet-5": 0,
    "claude-sonnet-4-5": 1,
    "claude-haiku-4-5": 0,
}


def model_family(name: str) -> str:
    n = (name or "").lower()
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in n:
            return fam
    return "other"


def model_color(name: str) -> str:
    """モデル名 → 固定色。ランク・出現順では塗らない（recolor-on-filter 回避）。"""
    shades = FAMILY_SHADES[model_family(name)]
    idx = MODEL_SHADE_INDEX.get(name)
    if idx is None:
        # 未知モデルも名前から決まる安定したインデックスにする（md5 は実行間で不変）。
        import hashlib
        idx = int(hashlib.md5((name or "").encode("utf-8")).hexdigest(), 16)
    return shades[idx % len(shades)]


def _apply_style():
    plt.rcParams.update({
        "font.family": "Hiragino Sans",
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": TEXT,
        "axes.labelcolor": TEXT_SECONDARY,
        "xtick.color": TEXT_SECONDARY,
        "ytick.color": TEXT_SECONDARY,
        "axes.edgecolor": GRID,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.grid": False,
        "figure.dpi": DPI,
        "savefig.dpi": DPI,
    })


def _fig_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def _clean_axes(ax, keep_x=True):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    if not keep_x:
        ax.spines["bottom"].set_visible(False)


def _trim(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# 1. summary_card.png（スタットカード。チャートなし）
# ---------------------------------------------------------------------------

def render_summary_card(
    label: str,
    period_text: str,
    root: str,
    total_usd: float,
    total_jpy: float,
    session_count: int,
    active_text: str,
    top_sessions: list,   # [(title, usd), ...] 最大3件
    unknown_note: str = "",    # 単価未収載の注記（全文。カード下部に出す）
    unknown_badge: str = "",   # 同・短縮版（ヒーロー数値の直下に出す）
) -> bytes:
    _apply_style()
    fig = plt.figure(figsize=(10, 6))
    fig.patch.set_facecolor(SURFACE)

    fig.text(0.06, 0.90, f"使用量サマリ  {label}", fontsize=20, color=TEXT, va="top")
    fig.text(0.06, 0.845, period_text, fontsize=11, color=TEXT_SECONDARY, va="top")
    fig.text(0.06, 0.805, _trim(root, 64), fontsize=10, color=TEXT_SECONDARY, va="top")

    fig.text(0.06, 0.68, f"${total_usd:,.2f}", fontsize=62, color=TEXT, va="center")
    fig.text(0.06, 0.545, f"約 ¥{total_jpy:,.0f}（従量課金だったと仮定した参考値）",
             fontsize=12, color=TEXT_SECONDARY, va="center")

    if unknown_badge:
        # ヒーロー数値だけが独り歩きしないよう、金額のすぐ下に過小である旨を出す。
        # 右カラム（セッション数・実処理時間）と重ならないよう短文に留める。
        fig.text(0.06, 0.475, unknown_badge, fontsize=11, color=ORANGE, va="center")

    fig.text(0.62, 0.70, "セッション数", fontsize=11, color=TEXT_SECONDARY, va="center")
    fig.text(0.62, 0.645, f"{session_count}", fontsize=26, color=TEXT, va="center")
    fig.text(0.62, 0.565, "実処理時間", fontsize=11, color=TEXT_SECONDARY, va="center")
    fig.text(0.62, 0.510, active_text, fontsize=26, color=TEXT, va="center")

    fig.add_artist(plt.Line2D([0.06, 0.94], [0.44, 0.44], color=GRID, linewidth=1))

    fig.text(0.06, 0.385, "高コストセッション Top3", fontsize=12, color=TEXT_SECONDARY, va="center")
    y = 0.325
    if top_sessions:
        # テキストは "タイトル" か "タイトル\n要約"。要約は主行より小さく・淡く出す
        # （主行を要約で置き換えると 30 字で途中切れになり、タイトルより読めない）。
        for text, usd in top_sessions[:3]:
            title, _, sub = (text or "").partition("\n")
            fig.text(0.06, y, _trim(title, 30), fontsize=13, color=TEXT, va="center")
            fig.text(0.94, y, f"${usd:,.2f}", fontsize=13, color=TEXT, va="center", ha="right")
            if sub.strip():
                fig.text(0.06, y - 0.035, _trim(sub.strip(), 46), fontsize=9.5,
                         color=TEXT_SECONDARY, va="center")
            y -= 0.085
    else:
        fig.text(0.06, y, "（対象セッションなし）", fontsize=13, color=TEXT_SECONDARY, va="center")

    fig.text(0.06, 0.035, "コストは全モデルを従量課金単価で仮計算した参考値",
             fontsize=9, color=TEXT_SECONDARY, va="center")
    if unknown_note:
        fig.text(0.06, 0.085, "⚠ " + unknown_note, fontsize=9, color=ORANGE, va="center")
    return _fig_bytes(fig)


# ---------------------------------------------------------------------------
# 2. daily_cost.png（JST 日別 × モデル別の積み上げ棒）
# ---------------------------------------------------------------------------

FIG_W_DAILY = 12.0        # インチ
_AXES_FRACTION = 0.86     # tight_layout 後に軸が figure 幅に占める概算比率


def _bin_days(days: list) -> tuple:
    """日リストを本数が読める粒度に畳む。

    戻り値: (bins, kind)。bins は [(label, [その bin に含まれる日]), ...]。
    --from/--to で年単位のレンジを指定されると日別の棒は 1px 未満になり
    「コストがゼロ」に見えてしまうため、62日超は週次、400日超は月次に落とす。
    """
    n = len(days)
    if n <= 62:
        return [(d.strftime("%m/%d"), [d]) for d in days], "日別"
    if n <= 400:
        bins = []
        for i in range(0, n, 7):
            chunk = days[i:i + 7]
            bins.append((chunk[0].strftime("%m/%d") + "〜", chunk))
        return bins, "週別（7日ごと）"
    bins = []
    cur_key = None
    for d in days:
        key = (d.year, d.month)
        if key != cur_key:
            bins.append((f"{d.year:04d}/{d.month:02d}", []))
            cur_key = key
        bins[-1][1].append(d)
    return bins, "月別"


def render_daily_cost(daily: dict, since_date, until_date) -> bytes:
    """daily: {date: {model: usd}}。since_date <= d < until_date の全日を x に出す。

    日数が多いときは週次・月次にビン化する（1本あたりの幅が確保できないため）。
    """
    _apply_style()
    days = []
    d = since_date
    while d < until_date:
        days.append(d)
        d += timedelta(days=1)
    if not days:
        days = sorted(daily.keys())

    bins, kind = _bin_days(days)
    labels = [b[0] for b in bins]

    all_models = sorted({m for v in daily.values() for m in v})
    # bin ごとの合計を先に作る
    series = {}
    for m in all_models:
        series[m] = [
            sum(daily.get(dd, {}).get(m, 0.0) for dd in chunk) for _, chunk in bins
        ]
    # 全期間 $0 の系列（単価未収載モデル等）は色面が 1px も描かれないので凡例から外す。
    models = [m for m in all_models if sum(series[m]) > 0]
    zero_models = [m for m in all_models if m not in models]

    fig, ax = plt.subplots(figsize=(FIG_W_DAILY, 5.8))
    bottoms = [0.0] * len(bins)
    width = 0.72

    # 隣接バーの分離線はポイント単位。dpi=200 では 2pt = 5.6px 相当あり、
    # 棒が細いと塗りを食い潰して「棒が消える」ため、実効幅から算出して細い時は 0 にする。
    slot_px = FIG_W_DAILY * DPI * _AXES_FRACTION / max(1, len(bins))
    bar_px = slot_px * width
    lw = 2 * 72.0 / DPI if bar_px >= 8 else 0.0

    for m in models:
        vals = series[m]
        ax.bar(
            range(len(bins)), vals, width=width, bottom=bottoms,
            color=model_color(m), edgecolor=SURFACE, linewidth=lw, label=m,
        )
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    title = f"コスト推移（JST・{kind}・モデル別積み上げ）"
    ax.set_title(title, fontsize=15, color=TEXT, pad=44, loc="left")
    ax.set_ylabel("USD")
    step = max(1, -(-len(bins) // 16))   # ラベルは最大 16 本まで
    idx = [i for i in range(len(bins)) if i % step == 0]
    ax.set_xticks(idx)
    ax.set_xticklabels([labels[i] for i in idx], rotation=45, ha="right", fontsize=9)
    ax.set_xlim(-0.7, len(bins) - 0.3)
    top = max(bottoms) if bottoms else 0.0
    if top > 0:
        ax.set_ylim(0, top * 1.12)       # 凡例を軸外に出しても天井余白を明示的に確保
    ax.yaxis.grid(True, linewidth=0.8, color=GRID)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    if models:
        # 凡例は**軸の外（上）**へ。loc="lower left" にすることで凡例本体が
        # プロット領域ではなく上方向へ伸び、左寄りの高いバーと重ならない。
        ax.legend(
            frameon=False, labelcolor=TEXT_SECONDARY, fontsize=10,
            ncols=min(4, len(models)), loc="lower left", bbox_to_anchor=(0, 1.01),
        )
    if zero_models:
        ax.text(
            0, -0.30, "（$0 のため非表示: " + ", ".join(zero_models) + " — 単価未収載）",
            transform=ax.transAxes, fontsize=9, color=TEXT_SECONDARY, va="top",
        )
    fig.tight_layout()
    return _fig_bytes(fig)


# ---------------------------------------------------------------------------
# 3. repo_breakdown.png（リポジトリ別コスト横棒・単一色）
# ---------------------------------------------------------------------------

def render_repo_breakdown(repo_costs: dict) -> bytes:
    _apply_style()
    items = sorted(repo_costs.items(), key=lambda t: t[1], reverse=True)
    if len(items) >= 9:   # 9件以上は上位8 + その他に畳む
        head, tail = items[:8], items[8:]
        items = head + [("その他", sum(v for _, v in tail))]
    names = [n for n, _ in items]
    vals = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(10, max(3.2, 0.62 * len(items) + 2.0)))
    ypos = list(range(len(items)))[::-1]
    ax.barh(ypos, vals, height=0.62, color=BLUE, edgecolor=SURFACE, linewidth=2)
    ax.set_yticks(ypos)
    ax.set_yticklabels([_trim(n, 28) for n in names], fontsize=11, color=TEXT)
    ax.set_xlabel("USD")
    ax.set_title("リポジトリ別コスト", fontsize=15, color=TEXT, pad=16, loc="left")
    top = max(vals) if vals else 1.0
    ax.set_xlim(0, top * 1.18 if top > 0 else 1.0)
    for y, v in zip(ypos, vals):
        ax.text(v + top * 0.015, y, f"${v:,.2f}", va="center", fontsize=10, color=TEXT)
    ax.xaxis.grid(True, linewidth=0.8, color=GRID)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    ax.tick_params(axis="y", length=0)
    fig.tight_layout()
    return _fig_bytes(fig)


# ---------------------------------------------------------------------------
# 4. model_breakdown.png（モデル別コスト横棒・固定モデル色）
# ---------------------------------------------------------------------------

def render_model_breakdown(models: list) -> bytes:
    """models: [(name, tokens, usd, known), ...]（USD 降順）。"""
    _apply_style()
    names = [m[0] for m in models]
    vals = [m[2] for m in models]
    toks = [m[1] for m in models]

    fig, ax = plt.subplots(figsize=(10, max(3.2, 0.7 * len(models) + 2.0)))
    ypos = list(range(len(models)))[::-1]
    ax.barh(ypos, vals, height=0.6,
            color=[model_color(n) for n in names], edgecolor=SURFACE, linewidth=2)
    ax.set_yticks(ypos)
    ax.set_yticklabels([_trim(n, 24) for n in names], fontsize=11, color=TEXT)
    ax.set_xlabel("USD")
    ax.set_title("モデル別コスト", fontsize=15, color=TEXT, pad=16, loc="left")
    top = max(vals) if vals else 1.0
    ax.set_xlim(0, top * 1.30 if top > 0 else 1.0)
    for y, v, t, known in zip(ypos, vals, toks, [m[3] for m in models]):
        note = "" if known else "（単価未収載）"
        ax.text(v + top * 0.015, y, f"${v:,.2f}{note}", va="center", fontsize=10, color=TEXT)
        ax.text(v + top * 0.015, y - 0.30, f"{t:,} tok", va="center",
                fontsize=8, color=TEXT_SECONDARY)
    ax.xaxis.grid(True, linewidth=0.8, color=GRID)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    ax.tick_params(axis="y", length=0)
    if names:
        # 凡例は軸の外（下）に置く。バーに直接ラベルが付いているため重なりを作らない。
        ax.legend(
            handles=[Patch(facecolor=model_color(n), label=n) for n in names],
            frameon=False, labelcolor=TEXT_SECONDARY, fontsize=9,
            ncols=min(4, len(names)), loc="upper center", bbox_to_anchor=(0.5, -0.16),
        )
    fig.tight_layout()
    return _fig_bytes(fig)
