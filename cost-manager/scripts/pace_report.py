#!/usr/bin/env python3
"""fable-cost-manager: 週次枠ペーシング（pace）を人間向けに表示する。

`var/pace/cache.json`（`pace_refresh.py` が作る集計結果）と `var/pace/samples.jsonl`
（statusline が記録した rate_limits スナップショット）を読み、週次枠・Fable サブ枠・
5時間枠の現在のペースと「このペースでの週末到達%」を表示する。使用率は最新サンプル、
Fable のシェアはキャッシュから取り、経過率・ペースは実行時刻で再計算する。

終了コード:
    0 = 正常終了
    1 = config.json / pricing.json の欠落・破損
    3 = 表示に必要なデータが無い（サンプル未取得。statusline を数分動かしてから再実行）

実行例:
    python3 scripts/pace_report.py
    python3 scripts/pace_report.py --refresh      # 同期で集計してから表示
    python3 scripts/pace_report.py --json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib
import pace_refresh

WEEK_SEC = pace_refresh.WEEK_SEC
FIVE_HOUR_SEC = 5 * 3600
MIN_ELAPSED_RATIO = pace_refresh.MIN_ELAPSED_RATIO


def _load_cache():
    p = lib.pace_dir() / "cache.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _daily_snapshots(samples_path):
    """各 JST 日の最終サンプルの seven_day.used を [(date, used)] で返す（古い順）。"""
    by_day = {}
    for ts, used, _ in pace_refresh._iter_samples(samples_path):
        d = lib.to_jst(datetime.fromtimestamp(ts, tz=timezone.utc)).date()
        prev = by_day.get(d)
        if prev is None or ts >= prev[0]:
            by_day[d] = (ts, used)
    return [(d, by_day[d][1]) for d in sorted(by_day)]


def build(now: float, args) -> dict:
    config = lib.load_config()
    pace_cfg = lib.pace_config(config)
    cache = _load_cache() or {}
    # cache.json のキーは値が null でありうる（サンプル無し・集計失敗時）。
    # `.get(k, {})` は「キーがあって値が null」を既定値に置き換えないため、必ず `or` で正規化する。
    fab = cache.get("fable") or {}
    win = cache.get("window") or {}
    sd_cache = cache.get("seven_day") or {}
    unknown_models = list(cache.get("unknown_models") or [])
    samples_path = lib.pace_dir() / "samples.jsonl"
    last = lib.read_last_jsonl_line(samples_path)
    valid_samples = pace_refresh._iter_samples(samples_path)

    band = pace_cfg.get("on_pace_band") or [0.8, 1.1]
    band_lo, band_hi = float(band[0]), float(band[1])
    cap_pct = float(pace_cfg.get("fable_cap_pct", 50))

    resets_at = used = None
    source = None
    if valid_samples:
        # 末尾に resets_at 欠落の行があっても、手前の有効サンプルを使う
        _, used, resets_at = valid_samples[-1]
        source = "samples.jsonl"
    if resets_at is None and win.get("resets_at") is not None and sd_cache.get("used") is not None:
        if pace_refresh.valid_resets_at(win["resets_at"]):
            resets_at = int(win["resets_at"])
            used = float(sd_cache["used"])
            source = "cache.json"

    if resets_at is None:
        return {"ok": False, "reason": "no_samples"}

    start = resets_at - WEEK_SEC
    closed = now >= resets_at
    end = float(resets_at) if closed else now
    elapsed_ratio = max(0.0, min(1.0, (end - start) / WEEK_SEC))
    has_elapsed = elapsed_ratio >= MIN_ELAPSED_RATIO

    share = fab.get("share")
    cap_pct = float(fab.get("cap_pct") or cap_pct)
    est_pct = (used * share) if (share is not None and not unknown_models) else None

    out = {
        "ok": True,
        "now": now,
        "used_source": source,
        "window": {"start": start, "end": end, "resets_at": resets_at, "closed": closed},
        "elapsed_ratio": elapsed_ratio,
        "band": [band_lo, band_hi],
        "seven_day": {
            "used": used,
            "pace": (used / 100.0) / elapsed_ratio if has_elapsed else None,
            "projected_end_pct": used / elapsed_ratio if has_elapsed else None,
        },
        "fable": {
            "share": share,
            "cap_pct": cap_pct,
            "est_pct": est_pct,
            "pace": (est_pct / (cap_pct * elapsed_ratio))
            if (est_pct is not None and has_elapsed and cap_pct > 0) else None,
            "projected_end_pct": (est_pct / elapsed_ratio)
            if (est_pct is not None and has_elapsed) else None,
            "usd": fab.get("usd"),
            "tokens": fab.get("tokens"),
        },
        "five_hour": None,
        "models": cache.get("models") or {},
        "total_usd": cache.get("total_usd"),
        "unknown_models": unknown_models,
        "unknown_tokens": cache.get("unknown_tokens"),
        "calibration": cache.get("calibration"),
        "cache_computed_at": cache.get("computed_at"),
        "cache_duration_sec": cache.get("duration_sec"),
        "cache_error": cache.get("error"),
        "cache_notes": list(cache.get("notes") or []),
        # Codex レーン（pace_refresh.py が台帳から集計したものをそのまま渡す。
        # 台帳が無い／サンプル無しで窓が決まらない場合は null）。
        "codex": cache.get("codex") or None,
        "samples_n": cache.get("samples_n"),
        "daily": [(d.isoformat(), u) for d, u in _daily_snapshots(samples_path)],
    }

    if isinstance(last, dict) and isinstance(last.get("five_hour"), dict):
        fh = last["five_hour"]
        try:
            if not pace_refresh.valid_resets_at(fh.get("resets_at")):
                raise ValueError("resets_at が範囲外")
            fh_reset = int(fh["resets_at"])
            fh_used = float(fh["used"])
            fh_elapsed = max(0.0, min(1.0, (min(now, fh_reset) - (fh_reset - FIVE_HOUR_SEC)) / FIVE_HOUR_SEC))
            out["five_hour"] = {"used": fh_used, "resets_at": fh_reset, "elapsed_ratio": fh_elapsed}
        except (KeyError, TypeError, ValueError):
            pass

    out["recommendations"] = _recommend(out)
    return out


def _recommend(d: dict) -> list:
    band_lo, band_hi = d["band"]
    rec = []
    if d.get("cache_error"):
        rec.append(
            f"直近の集計が失敗しています（{d['cache_error']}）。"
            "pace_refresh.py を手動実行して原因を確認してください。"
        )
    if d.get("unknown_models"):
        rec.append(
            "未収載モデルがあるため Fable 推定不能: "
            + ", ".join(d["unknown_models"])
            + "。pricing.json に単価を追加せよ。"
        )
    sd = d["seven_day"]
    if sd["projected_end_pct"] is None:
        rec.append("窓の経過率が小さいためペース判断はまだできません（数時間後に再確認）。")
    else:
        proj = sd["projected_end_pct"]
        if sd["pace"] < band_lo:
            rec.append(
                f"週末到達見込み {proj:.0f}% → 枠が {max(0.0, 100 - proj):.0f}% 余る見込み。"
                "サブエージェントの並列度を上げるか effort を上げる余地があります。"
            )
        elif sd["pace"] > band_hi:
            rec.append(
                f"週末到達見込み {proj:.0f}% → 期限前に枯渇する見込み。"
                "並列度を落とすか、重い作業を次の窓へ回すことを検討してください。"
            )
        else:
            rec.append(f"週末到達見込み {proj:.0f}% → 想定どおりのペースです（band {band_lo}〜{band_hi}）。")

    f = d["fable"]
    if d.get("unknown_models"):
        pass  # Fable 推定が不能なので推奨は出さない（上の行で理由を出している）
    elif f["pace"] is None:
        rec.append("Fable のシェアが未集計です（pace_refresh.py を実行してください）。")
    elif f["pace"] > band_hi:
        rec.append(
            f"Fable は上限 {f['cap_pct']:.0f}% に対し {f['pace']:.2f}x で進行 → "
            "effort を下げるか、機械的な作業の opus 委譲を増やしてください。"
        )
    elif f["pace"] < band_lo:
        rec.append(
            f"Fable は上限 {f['cap_pct']:.0f}% に対し {f['pace']:.2f}x で進行 → "
            "メインループを fable のまま使う余地があります。"
        )
    else:
        rec.append(f"Fable は上限 {f['cap_pct']:.0f}% に対し {f['pace']:.2f}x で進行 → 想定どおりです。")
    return rec


NOTES = [
    "[未検証] A1: 週次枠の消費は各モデルの USD 換算コストに比例すると仮定しています"
    "（Fable のシェア = fable_usd / total_usd。単価は pricing.json）。",
    "[未検証] A2: Fable の 50% 上限は同じ seven_day 窓に対する比率だと仮定しています。",
    "[未検証] A3: 窓の開始は resets_at − 7日（5時間枠は −5時間）だと仮定しています。",
    "仕様: license-switch の .envrc が効くディレクトリで起動したセッション（= 別ライセンス／別枠）は"
    "集計から除外しています（config の budget.pace.exclude_cwd_prefixes も同様に除外）。",
]


def render_text(d: dict) -> str:
    if not d.get("ok"):
        return "サンプルがまだありません（statusline を数分動かしてから再実行してください）。"

    def jst(epoch):
        return lib.to_jst(datetime.fromtimestamp(epoch, tz=timezone.utc)).strftime("%Y-%m-%d %H:%M")

    L = []
    w = d["window"]
    L.append("週次枠ペーシング（pace）")
    L.append("")
    remain = max(0.0, w["resets_at"] - d["now"])
    L.append(
        f"窓: {jst(w['start'])} 〜 {jst(w['resets_at'])}（JST） / "
        f"経過 {d['elapsed_ratio'] * 100:.0f}% / 残り {lib.fmt_duration(remain)}"
        + ("（窓は閉じています）" if w["closed"] else "")
    )
    sd = d["seven_day"]
    pace_s = f"{sd['pace']:.2f}" if sd["pace"] is not None else "—"
    proj_s = f"{sd['projected_end_pct']:.0f}%" if sd["projected_end_pct"] is not None else "—"
    L.append(f"週次枠 : used {sd['used']:.1f}% · ペース {pace_s} · 週末到達見込み {proj_s}")

    f = d["fable"]
    if d.get("unknown_models"):
        L.append(
            "Fable  : 推定不能（pricing.json 未収載: " + ", ".join(d["unknown_models"]) + "）"
        )
    elif f["est_pct"] is None:
        L.append("Fable  : 未集計（pace_refresh.py を実行してください）")
    else:
        fp = f"{f['pace']:.2f}" if f["pace"] is not None else "—"
        fpj = f"{f['projected_end_pct']:.0f}%" if f["projected_end_pct"] is not None else "—"
        L.append(
            f"Fable  : 推定 {f['est_pct']:.1f}% / 上限 {f['cap_pct']:.0f}%"
            f"（シェア {f['share'] * 100:.1f}% · ${lib.fmt_usd(f['usd'] or 0, 2)}）"
            f" · ペース {fp} · 週末到達見込み {fpj}"
        )
    fh = d.get("five_hour")
    if fh:
        L.append(f"5時間枠: used {fh['used']:.1f}% · 経過 {fh['elapsed_ratio'] * 100:.0f}%")

    models = d.get("models") or {}
    if models:
        L.append("")
        L.append("モデル別（窓内・全プロジェクト・dedup 済）")
        for name, v in sorted(models.items(), key=lambda kv: -(kv[1].get("usd") or 0)):
            L.append(f"  {name:<22} ${lib.fmt_usd(v.get('usd') or 0, 2):>8}  {lib.fmt_tokens(v.get('tokens')):>14} tok")
        if d.get("total_usd") is not None:
            L.append(f"  {'合計':<21} ${lib.fmt_usd(d['total_usd'], 2):>8}")

    cx = d.get("codex")
    if cx:
        L.append("")
        L.append("Codex（codex-bridge の使用量台帳より・参考）")
        cap = cx.get("weekly_cap")
        # 古いキャッシュや不正な上限値では cap があっても used_pct が None になりうる
        if cap and cx.get("used_pct") is not None:
            pace_c = f"{cx['pace']:.2f}" if cx.get("pace") is not None else "—"
            proj_c = f"{cx['projected_end_pct']:.0f}%" if cx.get("projected_end_pct") is not None else "—"
            L.append(
                f"  窓内   : {lib.fmt_credits(cx['window_credits'])}cr / {cx['window_jobs']} 件"
                f"（上限 {lib.fmt_credits(cap)}cr の {cx['used_pct']:.0f}%）"
                f" · ペース {pace_c} · 週末到達見込み {proj_c}"
            )
        else:
            L.append(
                f"  窓内   : {lib.fmt_credits(cx['window_credits'])}cr / {cx['window_jobs']} 件"
                "（上限未設定のため % は出せません）"
            )
        L.append(
            f"  5時間窓: {lib.fmt_credits(cx['five_hour_credits'])}cr / "
            f"{cx.get('five_hour_jobs', 0)} 件"
        )
        by_model = cx.get("by_model") or {}
        for name, v in sorted(by_model.items(), key=lambda kv: -(kv[1].get("credits") or 0)):
            L.append(
                f"    {name:<20} {lib.fmt_credits(v.get('credits')):>10}cr  "
                f"{v.get('jobs', 0):>3} 件  {lib.fmt_tokens(v.get('output_tokens')):>12} out-tok"
            )
        L.append(f"  無視した行: {cx.get('ignored_rows', 0)} 件 / 台帳: {cx.get('ledger_path')}")
        for n in cx.get("notes") or []:
            L.append(f"  注記: {n}")

    cal = d.get("calibration") or {}
    L.append("")
    if cal.get("usd_per_pct"):
        L.append(f"較正: 週次枠 1% あたり ${lib.fmt_usd(cal['usd_per_pct'], 3)}（隣接サンプル {cal.get('n_pairs')} ペアの中央値）")
    else:
        L.append(f"較正: サンプルペア不足のため未算出（Δused≥1% のペア {cal.get('n_pairs', 0)} 件、3 件以上で算出）")

    daily = d.get("daily") or []
    if daily:
        L.append("")
        L.append("サンプル履歴（各日の最終スナップショット）")
        for day, u in daily[-8:]:
            L.append(f"  {day}  {u:.1f}%")

    L.append("")
    L.append("推奨")
    for r in d.get("recommendations", []):
        L.append(f"  - {r}")

    if d.get("cache_computed_at"):
        L.append("")
        L.append(
            f"キャッシュ: {jst(d['cache_computed_at'])}（集計 {d.get('cache_duration_sec')} 秒 / "
            f"サンプル {d.get('samples_n')} 件）"
        )
    L.append("")
    L.append("注記")
    for n in list(d.get("cache_notes") or []) + NOTES:
        L.append(f"  - {n}")
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument("--refresh", action="store_true", help="同期で pace_refresh 相当を実行してから表示する")
    parser.add_argument("--now", type=float, default=None, help="現在時刻（epoch 秒）。テスト用。")
    args = parser.parse_args()

    import time

    now = args.now if args.now is not None else time.time()

    if args.refresh:
        # --refresh は pace_refresh.py と同じ失敗時の振る舞いにする（対称性）。
        # 失敗を握りつぶすと statusline が cache.json の mtime を見て refresh を
        # 再起動し続けるため、必ずネガティブキャッシュを残して終了する。
        ns = argparse.Namespace(
            resets_at=None, used=None, now=now, no_exclude_license=False, quiet=True
        )
        out = lib.pace_dir() / "cache.json"
        try:
            cache = pace_refresh.refresh(ns)
        except lib.ConfigError as e:
            print(f"エラー: {e}", file=sys.stderr)
            pace_refresh._write_error_cache(out, ns, f"ConfigError: {e}")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001 - 何で落ちても必ずキャッシュを残す
            print(f"エラー: {type(e).__name__}: {e}", file=sys.stderr)
            pace_refresh._write_error_cache(out, ns, f"{type(e).__name__}: {e}")
            sys.exit(1)
        lib.atomic_write_json(out, cache)

    try:
        d = build(now, args)
    except lib.ConfigError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(render_text(d))

    if not d.get("ok"):
        sys.exit(3)


if __name__ == "__main__":
    main()
