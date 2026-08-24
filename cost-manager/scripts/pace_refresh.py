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
import codex_official
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


def official_lane(pace_cfg: dict, now: float, notes: list):
    """Codex 公式 usage をサンプリングし、cache 用の `official` dict を返す（無ければ None）。

    - `budget.pace.codex_official.enabled` が偽なら何もしない（サンプルも読まない）。
    - 取得の失敗（auth.json 不在・401・タイムアウト・不正 JSON）は notes に 1 行入れて
      続行する。refresh 全体を失敗させない。
    - 直近サンプルが `max_age_sec` より古ければ `stale: true` を立てる。

    プライバシー: ここで扱うのは `codex_official` が数値だけに削ぎ落としたレコードのみ。
    例外は型名か `OfficialError` の自前メッセージだけを notes に載せる（トークン・識別子を
    載せないため、例外オブジェクトの中身は展開しない）。
    """
    cfg = codex_official.official_config(pace_cfg)
    enabled, enabled_warn = codex_official.official_enabled(cfg)
    if enabled_warn:
        notes.append(enabled_warn)
    if not enabled:
        return None

    try:
        codex_official.sample_official(cfg=cfg, now=now)
    except codex_official.OfficialError as e:
        notes.append(f"Codex 公式 usage を取得できませんでした: {e}")
    except Exception as e:  # noqa: BLE001 - 中身は出さない（トークン混入の恐れ）
        notes.append(
            f"Codex 公式 usage を取得できませんでした（{type(e).__name__}）。"
        )

    samples = codex_official.read_official_samples()
    if not samples:
        # ファイルはあるのに有効な行が 1 件も無い＝すべてスキップされた（窓・ts が範囲外、
        # 壊れた行）。黙って Codex 節を落とすと原因が分からないので注記を出す。
        try:
            has_lines = codex_official.official_samples_path().stat().st_size > 0
        except OSError:
            has_lines = False
        if has_lines:
            notes.append(
                "Codex 公式 usage の有効なサンプルがありません"
                "（窓が不正・ts が不正な行はスキップしました）。"
            )
        return None
    last = samples[-1]
    p = last.get("primary") or {}
    used_pct = codex_official._num(p.get("used_percent"))
    span = codex_official._num(p.get("limit_window_seconds"))
    reset_at = codex_official._num(p.get("reset_at"))
    sampled_at = codex_official._num(last.get("ts"))
    if used_pct is None or sampled_at is None:
        notes.append("Codex 公式 usage のサンプルが不正なため無視しました。")
        return None
    # 窓は `datetime.fromtimestamp()` に渡るので、極端値（NaN / inf / 1e12 / ミリ秒・
    # マイクロ秒・ナノ秒単位）をここで弾く。弾かないと refresh 全体が落ち、Claude 側の
    # 集計まで巻き添えでエラーキャッシュになる。
    if not (codex_official.valid_window_span(span)
            and codex_official.valid_epoch(reset_at)
            and codex_official.valid_epoch(sampled_at)
            and valid_resets_at(reset_at - span)
            and (reset_at - span) <= reset_at):
        notes.append("Codex 公式 usage のサンプルの窓が不正なため無視しました。")
        return None
    used_pct = float(used_pct)
    span = float(span)
    reset_at = float(reset_at)
    sampled_at = float(sampled_at)
    if last.get("fixture"):
        notes.append(
            "[テスト] フィクスチャ応答を使用した Codex 公式 usage サンプルです"
            "（FCM_CODEX_OFFICIAL_FIXTURE）。実際の使用量ではありません。"
        )

    window_start = reset_at - span
    end = min(now, reset_at)
    elapsed_ratio = max(0.0, min(1.0, (end - window_start) / span))
    has_elapsed = elapsed_ratio >= MIN_ELAPSED_RATIO
    try:
        max_age = float(cfg.get("max_age_sec") or codex_official.OFFICIAL_DEFAULTS["max_age_sec"])
    except (TypeError, ValueError):
        max_age = codex_official.OFFICIAL_DEFAULTS["max_age_sec"]
    stale = (now - sampled_at) > max_age
    if stale:
        notes.append(
            "Codex 公式 usage のサンプルが古いままです"
            f"（{lib.fmt_duration(max(0.0, now - sampled_at))}前）。"
        )

    return {
        "used_pct": used_pct,
        "window_start": window_start,
        "reset_at": reset_at,
        "elapsed_ratio": elapsed_ratio,
        "pace": (used_pct / (elapsed_ratio * 100.0)) if has_elapsed else None,
        "projected_end_pct": (used_pct / elapsed_ratio) if has_elapsed else None,
        "plan_type": last.get("plan_type"),
        "sampled_at": sampled_at,
        "stale": stale,
        "secondary": last.get("secondary"),
    }


def codex_section(config, window_start: float, window_end: float, official=None) -> dict:
    """Codex 使用量レーン（codex-bridge の台帳）の集計。

    台帳が無く公式 usage も無ければ None を返す（従来どおり Codex 節を出さない）。

    窓:
      - 公式 usage（`official`）があるときは**公式窓**（window_start = reset_at −
        limit_window_seconds 〜 現在）で集計する。呼び出し側がその窓を渡す。
      - 無いときは従来どおり Claude の seven_day 窓の**近似**で、notes にその旨を書く。
      - 5 時間窓は「window_end から遡って 5 時間」（Codex 側の 5 時間リセット時刻は
        公式応答の secondary_window にあるが、台帳側の窓としては使っていない）。

    上限（% / ペースの分母）:
      - 手動 `budget.pace.codex_weekly_credits` を最優先（`cap_source: "manual"`）。
      - 無ければ公式 % からの自動較正 `weekly_cap_est`（`cap_source: "estimated"`）。
        `weekly_cap_est = 窓内クレジット ÷ (used_pct / 100)`。`used_pct < 1` では算出しない。
    """
    path = lib.codex_ledger_path(config)
    if not path.exists() and official is None:
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

    # 公式 % からの自動較正（窓内クレジット ÷ 使用率）。整数丸めのため誤差が大きい。
    cap_est = None
    if official is not None:
        o_used = official.get("used_pct")
        if o_used is not None and o_used >= 1.0 and agg["credits"] > 0:
            cap_est = agg["credits"] / (o_used / 100.0)
            notes.append(
                f"週次上限の自動較正: 公式 {o_used:.0f}% と窓内 "
                f"{lib.fmt_credits(agg['credits'])}cr から約 {lib.fmt_credits(cap_est)}cr と推定"
                "（used_percent は整数丸めのため誤差が大きい）。"
            )
        elif o_used is not None and o_used < 1.0:
            notes.append(
                "公式 used_percent が 1% 未満のため週次上限の較正はできません"
                "（% が小さすぎて較正不能）。"
            )
        else:
            notes.append("窓内の台帳クレジットが 0 のため週次上限の較正はできません。")

    used_pct = pace = projected = None
    cap_source = None
    if cap:
        cap_source = "manual"
    elif cap_est and cap_est > 0:
        cap_source = "estimated"
    cap_used = cap if cap_source == "manual" else (cap_est if cap_source == "estimated" else None)

    if cap_used:
        used_pct = agg["credits"] / cap_used * 100.0
        # 経過率の分母は窓の実幅。公式窓が 7 日以外（Codex 側の窓変更・別プラン）でも
        # 公式行の経過率・ペースと矛盾しないよう、`reset_at - window_start` を使う。
        span = WEEK_SEC
        if official is not None:
            o_span = (official.get("reset_at") or 0) - (official.get("window_start") or 0)
            if o_span and o_span > 0:
                span = o_span
        elapsed_ratio = max(0.0, min(1.0, (window_end - window_start) / span))
        if elapsed_ratio >= MIN_ELAPSED_RATIO:
            pace = (used_pct / 100.0) / elapsed_ratio
            projected = used_pct / elapsed_ratio
    else:
        notes.append(
            "Codex の週次上限は未設定です（budget.pace.codex_weekly_credits）。"
            "枠の絶対値が非公開のため % とペースは出せません。"
        )

    if official is not None:
        notes.append(
            "窓は Codex の**公式窓**（公式 usage の reset_at − limit_window_seconds 〜 現在）"
            "で集計しています。5 時間窓は窓終端から遡った 5 時間です。"
        )
    else:
        notes.append(
            "窓は Claude の seven_day 窓をそのまま使っています。Codex の週次窓は"
            "リセット時刻が別なので**近似**です。5 時間窓は窓終端から遡った 5 時間です。"
        )
    if not path.exists():
        notes.append(f"Codex の使用量台帳がまだありません: {path}")
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
        "weekly_cap_est": cap_est,
        "cap_source": cap_source,
        "used_pct": used_pct,
        "pace": pace,
        "projected_end_pct": projected,
        "official": official,
        "window_start": window_start,
        "window_end": window_end,
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

    # 引数の検証はネットワーク（official_lane）より**前**に行う。無効な引数で
    # 落ちると決まっているのに公式 usage を叩くのは無駄で、スロットル枠も消費する。
    if args.resets_at is not None and not valid_resets_at(args.resets_at):
        raise lib.ConfigError(
            f"--resets-at が Unix epoch 秒の範囲外です: {args.resets_at}"
        )

    # Codex 公式 usage（ネットワーク）。失敗しても notes に 1 行入れて続行する。
    # Claude 側のサンプル（窓）とは独立に成立するので、窓が決まらない場合でも使う。
    official = official_lane(pace_cfg, now, notes)
    if official is not None:
        codex_window = (official["window_start"], min(now, official["reset_at"]))
    else:
        codex_window = None

    if args.resets_at is not None:
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
            # Claude 側の窓が決まらないので Claude の集計はできないが、公式 usage があれば
            # Codex 側は公式窓だけで成立する（台帳も公式窓で集計する）。
            "codex": (codex_section(config, codex_window[0], codex_window[1], official=official)
                      if codex_window else None),
            "notes": ["no samples"] + notes,
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

    # Codex レーン（台帳も公式 usage も無ければ None）。Claude 側の集計とは独立。
    # 公式 usage があれば公式窓で、無ければ従来どおり Claude の seven_day 窓の近似で集計する。
    cx_start, cx_end = codex_window if codex_window else (start, end)
    codex = codex_section(config, cx_start, cx_end, official=official)

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
            o = cx.get("official")
            if o:
                print(
                    f"Codex 公式: used {o['used_pct']:.0f}% / 経過 "
                    f"{o['elapsed_ratio'] * 100:.0f}%"
                    + (f" · pace {o['pace']:.2f}" if o.get("pace") is not None else " · pace —")
                    + f"（plan: {o.get('plan_type') or '不明'}"
                    + ("・サンプルが古い" if o.get("stale") else "")
                    + "）"
                )
        for n in cache.get("notes", []):
            print(f"注記: {n}")


if __name__ == "__main__":
    main()
