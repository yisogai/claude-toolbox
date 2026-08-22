#!/usr/bin/env python3
"""fable-cost-manager: 週次枠ペーシング（pace）のキャッシュを更新する。

`var/pace/samples.jsonl`（statusline が記録する rate_limits スナップショット）の最新行から
週次枠のリセット時刻と使用率を取り、そのリセット時刻から遡った7日窓について
`FCM_PROJECTS_DIR` 配下の**全プロジェクト**の transcript を dedup 集計する。結果は
`var/pace/cache.json` に atomic write される（statusline はこのキャッシュを読むだけ）。

集計経路は `cost_report.py --scope global` と同じ（`lib.iter_transcripts(glob_all=True)` →
`lib.collect_dedup_rows()` → `lib.aggregate()`）。dedup ロジックは複製していない。

別ライセンスのセッション（license-switch が生成した `.envrc` が効くディレクトリで起動した
セッション）は別の週次枠の消費なので集計から除外する（`--no-exclude-license` で無効化可能）。

通常は statusline（`pace_statusline.sh`）からバックグラウンドで起動されるが、手動実行もできる。
samples.jsonl がまだ無い場合は窓が決まらないため `--resets-at` / `--used` で手動指定できる
（手動検証用）。

終了コード:
    0 = 正常終了（サンプル無しで "no samples" を書いた場合も 0）
    1 = 集計に失敗（config.json / pricing.json の欠落・破損、その他の想定外例外）。
        このとき `{"computed_at":…, "error":…}` の最小キャッシュを atomic に書く
        （書かないと statusline が呼ばれるたびに refresh を再起動し続けるため）。

実行例:
    python3 scripts/pace_refresh.py
    python3 scripts/pace_refresh.py --resets-at 1755840000 --used 41.2   # 手動検証用
    python3 scripts/pace_refresh.py --quiet
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib

WEEK_SEC = 7 * 24 * 3600

# 経過率がこれ未満のときは pace / projected を出さない（0 割・極端な外挿を避ける）。
MIN_ELAPSED_RATIO = 0.01


def valid_resets_at(x) -> bool:
    """resets_at が Unix epoch 秒として妥当か（0 < x < 2**31）。

    statusline がミリ秒値や 0 / 負値を記録してしまった場合に
    `datetime.fromtimestamp` が `ValueError: year out of range` で落ちるのを防ぐ。
    そうしたサンプルは「不正なサンプル」として無視する。
    """
    try:
        v = int(x)
    except (TypeError, ValueError):
        return False
    return 0 < v < 2 ** 31


def _iter_samples(path):
    """samples.jsonl を先頭から読み、有効な seven_day を持つ行を (ts, used, resets_at) で列挙する。

    有効 = ts / used / resets_at がすべて数値で、resets_at が `valid_resets_at()` を満たすこと。
    最終行だけを見ると（`resets_at: null` の行が末尾にあるだけで）手前の有効サンプルを
    取りこぼすため、窓の決定にもこの列挙の末尾を使う。
    """
    out = []
    p = Path(path)
    if not p.exists():
        return out
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                sd = obj.get("seven_day")
                if not isinstance(sd, dict):
                    continue
                try:
                    ts = float(obj.get("ts"))
                    used = float(sd.get("used"))
                    resets_at = int(sd.get("resets_at"))
                except (TypeError, ValueError):
                    continue
                # ts も epoch 秒として妥当な範囲だけ通す（日別スナップショットの
                # datetime.fromtimestamp が範囲外値で落ちるのを防ぐ）
                if not valid_resets_at(resets_at) or not valid_resets_at(ts):
                    continue
                out.append((ts, used, resets_at))
    except OSError:
        return out
    out.sort(key=lambda t: t[0])
    return out


def _project_dir_of(path, pdir: Path):
    """transcript パスから ~/.claude/projects 直下のプロジェクトディレクトリを求める。"""
    try:
        rel = Path(path).relative_to(pdir)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return pdir / rel.parts[0]


def filter_excluded(tfiles, pace_cfg, exclude_license: bool = True):
    """別ライセンス・手動除外指定のセッションを取り除く。

    戻り値: (残った tfiles, 除外したセッション数, 除外した cwd の集合)

    判定はセッション単位。セッションの最初の cwd（メイン jsonl の先頭の "cwd" フィールド。
    無ければ当該ファイル自身の先頭 "cwd"）を取り、
      1. `config.json` の `budget.pace.exclude_cwd_prefixes` のいずれかで始まる
         （`~` は展開し、末尾スラッシュは無視してディレクトリ境界で一致させる）、または
      2. その cwd か祖先ディレクトリに license-switch 生成の `.envrc` がある
    場合に除外する。cwd が読めないセッションは除外しない（保守的に計上する）。
    """
    pdir = lib.projects_dir()
    prefixes = [
        os.path.expanduser(str(p)).rstrip("/") or "/"
        for p in (pace_cfg.get("exclude_cwd_prefixes") or [])
        if p
    ]

    cwd_by_session: dict = {}
    excluded_by_cwd: dict = {}
    kept = []
    excluded_sessions = set()
    excluded_cwds = set()

    for tf in tfiles:
        path = tf.path if isinstance(tf, lib.TFile) else Path(tf)
        proj = _project_dir_of(path, pdir)
        sid = tf.session_id if isinstance(tf, lib.TFile) else Path(path).stem
        key = (str(proj), sid)

        if key in cwd_by_session:
            cwd = cwd_by_session[key]
        else:
            cwd = None
            if proj is not None:
                main = proj / f"{sid}.jsonl"
                if main.exists():
                    cwd = lib.first_cwd_of(main)
            if cwd is None:
                cwd = lib.first_cwd_of(path)
            cwd_by_session[key] = cwd

        if cwd is None:
            kept.append(tf)
            continue

        if cwd in excluded_by_cwd:
            excluded = excluded_by_cwd[cwd]
        else:
            cwd_norm = cwd.rstrip("/") or "/"
            excluded = any(
                cwd_norm == pre or cwd_norm.startswith(pre.rstrip("/") + "/")
                for pre in prefixes
            )
            if not excluded and exclude_license:
                excluded = lib.is_license_switched_dir(cwd)
            excluded_by_cwd[cwd] = excluded

        if excluded:
            excluded_sessions.add(key)
            excluded_cwds.add(cwd)
            continue
        kept.append(tf)

    return kept, len(excluded_sessions), excluded_cwds


def _calibration(samples, rows, pricing, at, window_start, window_end):
    """samples.jsonl の隣接ペア（Δused ≥ 1%）から「1% あたりの USD」を推定する。

    窓内の dedup 済み usage 行を時刻順に並べて累積 USD を作り（走査は1回）、各サンプル時刻で
    切って区間の USD 増分を求め、Δused で割った値の中央値を返す。ペアが 3 未満なら None。
    """
    timed = []
    for row in rows:
        ts = row.get("timestamp")
        if not ts:
            continue
        try:
            dt = lib.parse_iso(ts)
        except (ValueError, TypeError):
            continue
        timed.append((dt.timestamp(), lib.row_cost_usd(row, pricing, at)))
    timed.sort(key=lambda t: t[0])

    times = [t for t, _ in timed]
    cum = []
    acc = 0.0
    for _, usd in timed:
        acc += usd
        cum.append(acc)

    def cum_usd_at(epoch: float) -> float:
        # epoch 以下の行までの累積 USD（二分探索）
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= epoch:
                lo = mid + 1
            else:
                hi = mid
        return cum[lo - 1] if lo > 0 else 0.0

    in_window = [s for s in samples if window_start <= s[0] <= window_end]
    ratios = []
    n_pairs = 0
    for (t1, u1, _), (t2, u2, _) in zip(in_window, in_window[1:]):
        d_used = u2 - u1
        if d_used < 1.0:
            continue
        n_pairs += 1
        d_usd = cum_usd_at(t2) - cum_usd_at(t1)
        if d_usd <= 0:
            continue
        ratios.append(d_usd / d_used)

    if len(ratios) < 3:
        return {"usd_per_pct": None, "n_pairs": n_pairs, "method": "median_of_adjacent_sample_pairs"}
    return {
        "usd_per_pct": statistics.median(ratios),
        "n_pairs": n_pairs,
        "method": "median_of_adjacent_sample_pairs",
    }


FIVE_HOUR_SEC = 5 * 3600


def codex_section(config, window_start: float, window_end: float) -> dict:
    """Codex 使用量レーン（codex-bridge の台帳）の集計。台帳が無ければ None を返す。

    台帳は読むだけで、cost-manager 側から書き込むことは無い。窓は Claude 側の
    seven_day 窓（[window_start, window_end]）をそのまま使う。Codex の週次窓は
    リセット時刻が別なので**近似**であり、notes にその旨を書く。5 時間窓は
    「window_end から遡って 5 時間」とする（Codex 側のリセット時刻は取得できない）。

    `budget.pace.codex_weekly_credits` が設定されていれば % とペースを出す。
    未設定なら `weekly_cap: null` として % は出さない（Codex の枠は絶対値非公開）。
    """
    path = lib.codex_ledger_path(config)
    if not path.exists():
        return None

    pace_cfg = lib.pace_config(config)
    pricing = lib.load_codex_pricing()
    notes = []

    start_dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(window_end, tz=timezone.utc)
    fh_start = datetime.fromtimestamp(max(window_start, window_end - FIVE_HOUR_SEC), tz=timezone.utc)
    stats: dict = {}
    # 台帳は 1 パスだけ読む。窓内集計は行を溜めずにストリームで流し、5 時間窓の行だけを
    # 途中で拾っておく（10 万行の台帳でも保持するのは直近 5 時間分だけ）。
    fh_rows: list = []

    def _window_rows():
        for row in lib.iter_codex_ledger(path, since=start_dt, until=end_dt, stats=stats):
            if row["ts_dt"] >= fh_start:
                fh_rows.append(row)
            yield row

    agg = lib.aggregate_codex(_window_rows(), pricing)
    fh_agg = lib.aggregate_codex(fh_rows, pricing)

    cap = pace_cfg.get("codex_weekly_credits")
    try:
        cap = float(cap) if cap is not None else None
    except (TypeError, ValueError):
        notes.append("codex_weekly_credits が数値ではないため上限未設定として扱いました。")
        cap = None
    if cap is not None and not (cap > 0):
        # 0 以下の上限は % もペースも意味を持たない（負値だと used_pct が負になり、
        # 表示側が `weekly_cap` を truthy と見て None の used_pct を書式化して落ちる）。
        notes.append("codex_weekly_credits が 0 以下のため上限未設定として扱いました。")
        cap = None

    used_pct = pace = projected = None
    if cap:
        used_pct = agg["credits"] / cap * 100.0
        elapsed_ratio = max(0.0, min(1.0, (window_end - window_start) / WEEK_SEC))
        if elapsed_ratio >= MIN_ELAPSED_RATIO:
            pace = (used_pct / 100.0) / elapsed_ratio
            projected = used_pct / elapsed_ratio
    else:
        notes.append(
            "Codex の週次上限は未設定です（budget.pace.codex_weekly_credits）。"
            "枠の絶対値が非公開のため % とペースは出せません。"
        )

    notes.append(
        "窓は Claude の seven_day 窓をそのまま使っています。Codex の週次窓は"
        "リセット時刻が別なので**近似**です。5 時間窓は窓終端から遡った 5 時間です。"
    )
    if agg["unknown_models"]:
        notes.append(
            "codex_pricing.json 未収載かつ credits_est の無いモデルがあります"
            "（クレジットは 0 として扱いました）: " + ", ".join(agg["unknown_models"])
        )
    # NaN/inf のクレジット行は aggregate 側で弾いている。無視行の合計に含めて注記に出す。
    ignored = stats.get("ignored", 0) + agg["ignored"]
    if ignored:
        notes.append(
            f"無視した台帳行 {ignored} 件（mock {stats.get('mock', 0)} / "
            f"usage 無し {stats.get('no_usage', 0)} / 壊れた行 {stats.get('broken', 0)} / "
            f"ts 不正 {stats.get('bad_ts', 0)} / クレジットが NaN・inf {agg['ignored']}）。"
        )
    if stats.get("unreadable"):
        notes.append(f"台帳を読めませんでした: {path}")

    return {
        "window_credits": agg["credits"],
        "window_jobs": agg["jobs"],
        "five_hour_credits": fh_agg["credits"],
        "five_hour_jobs": fh_agg["jobs"],
        "by_model": {
            k: {"credits": v["credits"], "jobs": v["jobs"], "output_tokens": v["output_tokens"]}
            for k, v in agg["by_model"].items()
        },
        "weekly_cap": cap,
        "used_pct": used_pct,
        "pace": pace,
        "projected_end_pct": projected,
        "ledger_path": str(path),
        "ignored_rows": ignored,
        "ignored_detail": {
            **{k: stats.get(k, 0)
               for k in ("broken", "mock", "no_usage", "bad_ts", "out_of_window", "unreadable")},
            "non_finite": agg["ignored"],
        },
        "unknown_models": agg["unknown_models"],
        "notes": notes,
    }


def refresh(args) -> dict:
    """集計してキャッシュ内容の dict を返す（書込は呼び出し側）。"""
    started = time.monotonic()
    notes = []

    config = lib.load_config()
    pricing = lib.load_pricing()
    pace_cfg = lib.pace_config(config)
    usd_jpy, usd_jpy_warn = lib.usd_jpy_from_config(config)
    if usd_jpy_warn:
        notes.append(usd_jpy_warn)

    now = float(args.now) if args.now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)

    samples_path = lib.pace_dir() / "samples.jsonl"
    samples = _iter_samples(samples_path)

    if args.resets_at is not None:
        if not valid_resets_at(args.resets_at):
            raise lib.ConfigError(
                f"--resets-at が Unix epoch 秒の範囲外です: {args.resets_at}"
            )
        resets_at = int(args.resets_at)
        used = float(args.used) if args.used is not None else (samples[-1][1] if samples else 0.0)
        notes.append("窓は手動指定（--resets-at / --used）です。")
    elif samples:
        # 最終有効サンプル（末尾の不正行は _iter_samples で落ちている）
        _, used, resets_at = samples[-1]
    else:
        resets_at, used = None, None

    if resets_at is None:
        return {
            "computed_at": now,
            "duration_sec": round(time.monotonic() - started, 3),
            "window": None,
            "seven_day": None,
            "fable": None,
            "models": {},
            "total_usd": 0.0,
            "unknown_models": [],
            "unknown_tokens": 0,
            "calibration":{"usd_per_pct": None, "n_pairs": 0, "method": "median_of_adjacent_sample_pairs"},
            "samples_n": len(samples),
            # 窓が決まらないので Codex 側も集計できない（台帳の有無に関わらず null）
            "codex": None,
            "notes": ["no samples"],
        }

    start = resets_at - WEEK_SEC
    window_closed = now >= resets_at
    end = float(resets_at) if window_closed else now
    if window_closed:
        notes.append("窓は既に閉じています（resets_at が過去）。経過率は 100% として扱いました。")

    elapsed_ratio = (end - start) / WEEK_SEC
    elapsed_ratio = max(0.0, min(1.0, elapsed_ratio))

    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end, tz=timezone.utc)

    tfiles = list(lib.iter_transcripts(glob_all=True, since=start_dt))
    tfiles, excluded_n, excluded_cwds = filter_excluded(
        tfiles, pace_cfg, exclude_license=not args.no_exclude_license
    )
    if excluded_n:
        notes.append(
            f"別ライセンス/除外指定のセッションを {excluded_n} 件除外しました"
            f"（対象ディレクトリ {len(excluded_cwds)} 件）。"
        )

    stats: dict = {}
    rows = lib.collect_dedup_rows(tfiles, since=start_dt, until=end_dt, stats=stats)
    at = lib.to_jst(now_dt).date()
    report = lib.aggregate(rows, pricing, at=at, usd_jpy=usd_jpy)

    models = {}
    fable_usd = 0.0
    fable_tokens = 0
    unknown_tokens = 0
    for agg in report.models:
        key = agg.resolved or agg.model
        tokens = (
            agg.input_tokens
            + agg.cache_write_5m
            + agg.cache_write_1h
            + agg.cache_read_tokens
            + agg.output_tokens
        )
        entry = models.setdefault(key, {"usd": 0.0, "tokens": 0})
        entry["usd"] += agg.cost_usd
        entry["tokens"] += tokens
        if lib.is_fable_model(agg.model, agg.resolved):
            fable_usd += agg.cost_usd
            fable_tokens += tokens
        if not agg.known:
            unknown_tokens += tokens

    unknown_models = list(report.unknown_models)
    if unknown_models:
        notes.append(
            "pricing.json 未収載のモデルがあり USD 換算できないため Fable 推定は不能です"
            "（tokens のみ計上）: " + ", ".join(unknown_models)
        )
    if report.stale:
        notes.append("pricing.json の as_of が古い可能性があります。")

    total_usd = report.total_usd
    cap_pct = float(pace_cfg.get("fable_cap_pct", 50))
    # 未収載モデルが 1 つでもあると USD の分母（と Fable 分子）が信用できないため、
    # share / est_pct / pace は算出せず null にする（薄色の「F≈0%」で無警告に出すより安全）。
    if unknown_models:
        share = None
        est_pct = None
    else:
        share = (fable_usd / total_usd) if total_usd > 0 else 0.0
        est_pct = used * share

    has_elapsed = elapsed_ratio >= MIN_ELAPSED_RATIO
    pace = (used / 100.0) / elapsed_ratio if has_elapsed else None
    projected_end_pct = used / elapsed_ratio if has_elapsed else None
    has_est = est_pct is not None
    pace_f = (est_pct / (cap_pct * elapsed_ratio)) if (has_est and has_elapsed and cap_pct > 0) else None
    projected_f = (est_pct / elapsed_ratio) if (has_est and has_elapsed) else None
    if not has_elapsed:
        notes.append("窓の経過率が 1% 未満のため pace / 到達見込みは算出していません。")

    calibration = _calibration(samples, rows, pricing, at, start, end)

    # Codex レーン（台帳が無ければ None）。Claude 側の集計とは独立。
    codex = codex_section(config, start, end)

    dropped = stats.get("dropped_no_timestamp", 0)
    if dropped:
        notes.append(f"timestamp 欠落の課金行 {dropped} 件を窓外として除外しました。")

    return {
        "computed_at": now,
        "duration_sec": round(time.monotonic() - started, 3),
        "window": {"start": start, "end": end, "resets_at": resets_at, "closed": window_closed},
        "seven_day": {
            "used": used,
            "elapsed_ratio": elapsed_ratio,
            "pace": pace,
            "projected_end_pct": projected_end_pct,
        },
        "fable": {
            "usd": fable_usd,
            "tokens": fable_tokens,
            "share": share,
            "est_pct": est_pct,
            "cap_pct": cap_pct,
            "pace": pace_f,
            "projected_end_pct": projected_f,
        },
        "models": models,
        "total_usd": total_usd,
        "unknown_models": unknown_models,
        "unknown_tokens": unknown_tokens,
        "calibration": calibration,
        "samples_n": len(samples),
        "rows_n": len(rows),
        "codex": codex,
        "notes": notes,
    }


def _write_error_cache(out, args, message: str) -> None:
    """集計に失敗したときの最小キャッシュ（ネガティブキャッシュ）を書く。

    statusline は cache.json の mtime だけで refresh の要否を判定するため、失敗時に
    何も書かないと statusline 呼び出しごとに refresh が再起動する（無音の再起動ループ）。
    エラー内容を載せた最小キャッシュを残すことで TTL バックオフを効かせる。
    statusline はこの `error` キーを見て F セグメントを `F!`（警告色）にする。
    """
    now = float(args.now) if getattr(args, "now", None) is not None else time.time()
    try:
        lib.atomic_write_json(out, {
            "computed_at": now,
            "error": message,
            "notes": ["集計に失敗しました。pace_refresh.py を手動実行して原因を確認してください。"],
        })
    except OSError as e:
        print(f"エラーキャッシュも書けませんでした: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--resets-at", type=int, default=None,
                        help="週次枠のリセット時刻（Unix epoch 秒）。samples.jsonl より優先（手動検証用）。")
    parser.add_argument("--used", type=float, default=None,
                        help="週次枠の使用率（%%）。--resets-at と併用する（手動検証用）。")
    parser.add_argument("--now", type=float, default=None,
                        help="現在時刻（Unix epoch 秒）。テスト・手動検証用。")
    parser.add_argument("--no-exclude-license", action="store_true",
                        help="license-switch の .envrc による別ライセンス除外を無効にする。")
    parser.add_argument("--quiet", action="store_true", help="stdout へ要約を出さない。")
    args = parser.parse_args()

    out = lib.pace_dir() / "cache.json"

    try:
        cache = refresh(args)
    except lib.ConfigError as e:
        print(f"エラー: {e}", file=sys.stderr)
        _write_error_cache(out, args, f"ConfigError: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - 何で落ちても必ずキャッシュを残す
        print(f"エラー: {type(e).__name__}: {e}", file=sys.stderr)
        _write_error_cache(out, args, f"{type(e).__name__}: {e}")
        sys.exit(1)

    lib.atomic_write_json(out, cache)

    if not args.quiet:
        print(f"pace キャッシュを更新しました: {out}")
        print(f"所要: {cache['duration_sec']} 秒")
        sd = cache.get("seven_day")
        if sd:
            win = cache["window"]
            s = lib.to_jst(datetime.fromtimestamp(win["start"], tz=timezone.utc))
            r = lib.to_jst(datetime.fromtimestamp(win["resets_at"], tz=timezone.utc))
            print(f"窓: {s:%Y-%m-%d %H:%M} 〜 {r:%Y-%m-%d %H:%M}（JST）")
            print(
                f"週次枠: used {sd['used']:.1f}% / 経過 {sd['elapsed_ratio'] * 100:.1f}%"
                + (f" · pace {sd['pace']:.2f} · 週末到達見込み {sd['projected_end_pct']:.0f}%"
                   if sd["pace"] is not None else " · pace —")
            )
            f = cache["fable"]
            if f["est_pct"] is None:
                print(
                    f"Fable: ${f['usd']:.2f} / 全体 ${cache['total_usd']:.2f}"
                    f" → 推定不能（pricing.json 未収載: "
                    f"{', '.join(cache.get('unknown_models') or [])}）"
                )
            else:
                print(
                    f"Fable: ${f['usd']:.2f} / 全体 ${cache['total_usd']:.2f}"
                    f"（share {f['share'] * 100:.1f}%）→ 推定 {f['est_pct']:.1f}% / {f['cap_pct']:.0f}%"
                )
        cx = cache.get("codex")
        if cx:
            # weekly_cap が truthy でも used_pct が None になりうる（不正な上限値）ため両方見る
            cap_s = (
                f" / 上限 {lib.fmt_credits(cx['weekly_cap'])}cr（{cx['used_pct']:.0f}%）"
                if (cx.get("weekly_cap") and cx.get("used_pct") is not None) else " / 上限未設定"
            )
            print(
                f"Codex: {lib.fmt_credits(cx['window_credits'])}cr / {cx['window_jobs']} 件"
                f"（直近5時間 {lib.fmt_credits(cx['five_hour_credits'])}cr）{cap_s}"
            )
        for n in cache.get("notes", []):
            print(f"注記: {n}")


if __name__ == "__main__":
    main()
