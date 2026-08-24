#!/usr/bin/env python3
"""fable-cost-manager: Codex（ChatGPT プラン）の公式 usage をサンプリングする。

`GET https://chatgpt.com/backend-api/wham/usage` を叩き、週次窓の `used_percent` など
**数値だけ**を `var/pace/codex_official_samples.jsonl` に追記する。pace（週次枠ペーシング）の
Codex 節はこのサンプルを使って「% / 経過 % / ペース」を出す。

一次情報（2026-08-24 実機確認）:
    ヘッダ  Authorization: Bearer <access_token> / chatgpt-account-id: <account_id>
    認証元  $CODEX_HOME/auth.json（既定 ~/.codex/auth.json）の tokens.access_token / tokens.account_id
    応答    {"plan_type": …, "rate_limit": {"primary_window": {"used_percent", …}, "secondary_window": …}, …}

[未検証] このエンドポイントは非公開 API であり、予告なく変わりうる。`used_percent` は
整数丸めの可能性が高い（週内に約 46 クレジット消費済みでも 0 が返る実測あり）。

プライバシー / 安全要件（テストで検証している）:
    - `email` / `user_id` / `account_id` / `access_token` をサンプル・キャッシュ・標準出力・
      標準エラー・例外メッセージのどこにも書かない。応答からは数値のみを抜き出す。
    - auth.json は**読むだけ**。書込・chmod・コピーは一切しない（トークンの更新は codex CLI の仕事）。
    - 送信先は https://chatgpt.com のみ。

環境変数:
    CODEX_HOME                    auth.json の親ディレクトリ（既定 ~/.codex）
    FCM_CODEX_OFFICIAL_FIXTURE    テスト専用。HTTP を発行せずこのファイルの内容を応答として返す。
                                  形式: {"status": 200, "body": "<本文>"} / {"error": "timeout"} /
                                  {"sleep": <秒>, ...}（遅い応答の再現）。
                                  フィクスチャを使ったサンプルには `"fixture": true` が付き、
                                  pace の注記にも「[テスト] フィクスチャ応答を使用」が出る
                                  （本番で無警告に効かないようにするため）。

タイムアウト: `timeout_sec` は **1 操作あたり**（urllib のソケットタイムアウト）。DNS や
複数アドレスへの接続で積み上がるため、`timeout_sec * 3` を**全体の壁時計期限**として別に
見張り、超過したら諦める（`OVERALL_TIMEOUT_FACTOR`）。

実行例:
    python3 scripts/codex_official.py            # スロットル付きで 1 回サンプリング
    python3 scripts/codex_official.py --json --force

終了コード: 0 = 正常（スロットルでスキップした場合も 0） / 1 = 取得失敗
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_lib as lib

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "fable-cost-manager/1.0"
SAMPLES_NAME = "codex_official_samples.jsonl"

# config/config.json の budget.pace.codex_official が欠けている場合の既定値。
OFFICIAL_DEFAULTS = {
    "enabled": True,
    "min_interval_sec": 900,
    "timeout_sec": 10,
    "max_age_sec": 21600,  # 6 時間
}

# plan_type として保存を許す文字（想定外の文字列＝識別子混入を保存しないための保険）。
_PLAN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# 例外メッセージに載せる最大長。
MAX_MSG = 200

# `timeout_sec` は 1 操作あたりの上限なので、全体の壁時計期限はその何倍まで許すか。
OVERALL_TIMEOUT_FACTOR = 3

WEEK_SEC = 7 * 24 * 3600
# 窓幅として受け入れる上限（週次窓の 8 倍まで）。ミリ秒・マイクロ秒・ナノ秒単位や
# 1e12 のような外れ値を弾き、`datetime.fromtimestamp()` を落とさないための保険。
MAX_WINDOW_SEC = WEEK_SEC * 8

# サンプル jsonl の最大行数。超えたら先頭半分を切り詰める（ローテーション）。
MAX_SAMPLE_LINES = 5000

# 後方読みで参照する末尾バイト数（`cost_lib.read_last_jsonl_line` と同じ考え方）。
TAIL_BYTES = 65536


class OfficialError(Exception):
    """公式 usage の取得失敗。メッセージにトークン・識別子は含めない。"""


def official_config(pace_cfg) -> dict:
    """budget.pace.codex_official を既定値とマージして返す（欠落キーは OFFICIAL_DEFAULTS）。"""
    merged = dict(OFFICIAL_DEFAULTS)
    raw = (pace_cfg or {}).get("codex_official")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None:
                merged[k] = v
    return merged


def official_enabled(cfg) -> tuple:
    """`enabled` の判定。戻り値 `(有効か, 型警告メッセージ|None)`。

    JSON の真偽値 `true` のみを真とする（`"false"` / `"true"` / `1` などの文字列・数値は
    偽として扱い、注記に型警告を出す）。文字列 `"false"` が truthy と解釈されて本番で
    無警告にサンプリングが走るのを防ぐため。
    """
    raw = cfg.get("enabled", True)
    if isinstance(raw, bool):
        return raw, None
    return False, (
        "budget.pace.codex_official.enabled が真偽値ではないため無効として扱いました"
        f"（{type(raw).__name__}）。true / false で指定してください。"
    )


def _num(v):
    """int / float だけを通す（bool・文字列・None は None）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v


def valid_epoch(x) -> bool:
    """Unix epoch 秒として妥当か（0 < x < 2**31）。

    ミリ秒・マイクロ秒・ナノ秒単位の値や 0 / 負値 / NaN / inf を弾く。これらを
    `datetime.fromtimestamp()` に渡すと ValueError / OverflowError で refresh 全体が落ちる。
    """
    v = _num(x)
    return v is not None and 0 < v < 2 ** 31


def valid_window_span(x) -> bool:
    """窓幅（秒）として妥当か（0 < x <= MAX_WINDOW_SEC）。"""
    v = _num(x)
    return v is not None and 0 < v <= MAX_WINDOW_SEC


def default_auth_path() -> Path:
    """codex CLI の auth.json（`$CODEX_HOME/auth.json`、既定 `~/.codex/auth.json`）。"""
    home = os.environ.get("CODEX_HOME")
    base = Path(os.path.expanduser(home)) if home else Path.home() / ".codex"
    return base / "auth.json"


def _read_auth(auth_path):
    """auth.json から (access_token, account_id) を読む。**読むだけ**。

    戻り値のトークンは HTTP ヘッダにしか使わない（ログ・例外・保存には流さない）。
    """
    p = Path(auth_path) if auth_path else default_auth_path()
    if not p.exists():
        raise OfficialError(
            f"codex の auth.json が見つかりません: {p}"
            "（codex CLI でログインしてください）"
        )
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise OfficialError(f"codex の auth.json を JSON として読めません: {p}") from None
    except OSError as e:
        raise OfficialError(
            f"codex の auth.json を読めません: {p}（{type(e).__name__}）"
        ) from None
    if not isinstance(obj, dict) or not isinstance(obj.get("tokens"), dict):
        raise OfficialError("codex の auth.json に tokens がありません。")
    tokens = obj["tokens"]
    access = tokens.get("access_token")
    account = tokens.get("account_id")
    if not isinstance(access, str) or not access:
        raise OfficialError("codex の auth.json に access_token がありません。")
    if not isinstance(account, str) or not account:
        raise OfficialError("codex の auth.json に account_id がありません。")
    return access, account


def fixture_path():
    """テスト専用フィクスチャのパス（未設定なら None）。"""
    return os.environ.get("FCM_CODEX_OFFICIAL_FIXTURE") or None


def _urllib_fetcher(url, headers, timeout):
    """既定の fetcher。chatgpt.com へ GET し (status, body) を返す。

    テスト時は `FCM_CODEX_OFFICIAL_FIXTURE` が設定されていれば HTTP を発行しない。
    `{"sleep": <秒>}` で遅い応答も再現できる（壁時計の見張りのテスト用）。
    """
    fixture = fixture_path()
    if fixture:
        with open(fixture, "r", encoding="utf-8") as f:
            spec = json.load(f)
        nap = _num(spec.get("sleep"))
        if nap and nap > 0:
            time.sleep(min(nap, 60))
        if spec.get("error"):
            raise TimeoutError(str(spec["error"])[:MAX_MSG])
        return int(spec.get("status", 200)), str(spec.get("body", ""))

    import urllib.error
    import urllib.request

    if not url.startswith("https://chatgpt.com/"):
        # 送信先は chatgpt.com のみ（呼び出し側の事故防止）
        raise OfficialError("送信先が https://chatgpt.com ではありません。")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 本文には識別子が含まれうるので読み捨て、ステータスだけを返す
        try:
            e.read()
        except Exception:  # noqa: BLE001
            pass
        return int(e.code), ""


def _overall_deadline(timeout_sec) -> float:
    """1 操作の timeout から全体の壁時計期限（秒）を決める。"""
    t = _num(timeout_sec)
    if t is None or t <= 0:
        t = OFFICIAL_DEFAULTS["timeout_sec"]
    return float(t) * OVERALL_TIMEOUT_FACTOR


def _call_with_deadline(fn, args, deadline_sec: float):
    """`fn(*args)` を別スレッドで実行し、`deadline_sec` を超えたら諦める。

    `timeout_sec` は 1 操作あたりの上限でしかなく、DNS 解決や複数アドレスへの接続で
    実時間は積み上がる。壁時計（`time.monotonic()`）で全体を見張り、超過したら
    `OfficialError` にする。取り残したスレッドは daemon なので、プロセス終了時に消える
    （refresh は数秒で終わるので放置してよい）。
    """
    box = {}

    def _run():
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - 呼び出し側で型ごとに包み直す
            box["exc"] = e

    th = threading.Thread(target=_run, name="codex-official-fetch", daemon=True)
    started = time.monotonic()
    th.start()
    th.join(deadline_sec)
    if th.is_alive():
        raise OfficialError(
            f"公式 usage の取得が全体の壁時計期限（{deadline_sec:.0f} 秒）を超えたため"
            "中断しました。"
        )
    if "exc" in box:
        raise box["exc"]
    if time.monotonic() - started > deadline_sec:
        raise OfficialError(
            f"公式 usage の取得が全体の壁時計期限（{deadline_sec:.0f} 秒）を超えました。"
        )
    return box.get("value")


def _shape(payload: dict) -> dict:
    """応答から**数値のみ**を抜き出す。識別子（email / user_id / account_id）は捨てる。"""
    rl = payload.get("rate_limit")
    if not isinstance(rl, dict):
        raise OfficialError("公式 usage の応答に rate_limit がありません。")

    def window(w):
        if not isinstance(w, dict):
            return None
        used = _num(w.get("used_percent"))
        span = _num(w.get("limit_window_seconds"))
        reset = _num(w.get("reset_at"))
        if used is None or span is None or reset is None:
            return None
        return {"used_percent": used, "limit_window_seconds": span, "reset_at": reset}

    primary = window(rl.get("primary_window"))
    if primary is None:
        raise OfficialError("公式 usage の応答の primary_window に必要な数値がありません。")

    plan = payload.get("plan_type")
    if not (isinstance(plan, str) and _PLAN_RE.match(plan)):
        plan = None

    return {"plan_type": plan, "primary": primary, "secondary": window(rl.get("secondary_window"))}


def fetch_official(auth_path=None, timeout_sec=10.0, fetcher=None) -> dict:
    """公式 usage を 1 回取得し、数値だけの dict を返す。

    戻り値: `{"plan_type": str|None, "primary": {...}, "secondary": {...}|None}`
    失敗時は `OfficialError`（メッセージにトークン・識別子は載らない）。
    """
    access, account = _read_auth(auth_path)
    headers = {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    fn = fetcher or _urllib_fetcher
    used_fixture = fetcher is None and bool(fixture_path())
    try:
        status, body = _call_with_deadline(
            fn, (USAGE_URL, headers, timeout_sec), _overall_deadline(timeout_sec)
        )
    except OfficialError:
        raise
    except TimeoutError:
        raise OfficialError(f"公式 usage の取得がタイムアウトしました（{timeout_sec} 秒）。") from None
    except Exception as e:  # noqa: BLE001
        # 例外の中身（URL・本文・ヘッダ）にトークンが混ざりうるので型名だけを出す
        raise OfficialError(f"公式 usage の取得に失敗しました（{type(e).__name__}）。") from None
    finally:
        # ローカル変数からトークンを落とす（traceback のフレームに残さない）
        access = account = headers = None

    if status == 401 or status == 403:
        raise OfficialError(
            f"公式 usage の取得に失敗しました（HTTP {status}）。"
            "トークンが期限切れの可能性があります。codex CLI を一度実行すると回復することがあります。"
        )
    if status != 200:
        raise OfficialError(f"公式 usage の取得に失敗しました（HTTP {status}）。")

    try:
        payload = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        # JSONDecodeError だけでは足りない: 4,300 桁超の整数リテラルは ValueError、
        # 極端に深いネストは RecursionError になる（台帳パーサと同じ扱い）。
        raise OfficialError("公式 usage の応答を JSON として読めません。") from None
    if not isinstance(payload, dict):
        raise OfficialError("公式 usage の応答が JSON オブジェクトではありません。")
    shaped = _shape(payload)
    if used_fixture:
        # 本番でフィクスチャが無警告に効かないよう、由来を必ず残す（読み側が注記を出す）。
        shaped["fixture"] = True
    return shaped


def official_samples_path() -> Path:
    return lib.pace_dir() / SAMPLES_NAME


def _valid_sample(obj) -> bool:
    """サンプル 1 行が使える形か（ts / used_percent / 窓がすべて妥当な数値か）。

    窓（`limit_window_seconds` / `reset_at`）と `ts` の範囲もここで弾く。不正行が
    最終行に居座ると、スロットルの間ずっと悪いサンプルが再利用されるため。
    """
    if not isinstance(obj, dict):
        return False
    if not valid_epoch(obj.get("ts")):
        return False
    primary = obj.get("primary")
    if not isinstance(primary, dict):
        return False
    if _num(primary.get("used_percent")) is None:
        return False
    return (valid_window_span(primary.get("limit_window_seconds"))
            and valid_epoch(primary.get("reset_at")))


def read_official_samples(path=None, tail_bytes: int = TAIL_BYTES) -> list:
    """サンプル jsonl の**有効な最終行だけ**を `[レコード]` で返す（無ければ `[]`）。

    末尾 `tail_bytes` だけを後方に読む（`cost_lib.read_last_jsonl_line` と同じ考え方）。
    表示・スロットルに必要なのは常に最新の 1 件なので、行数が増えても一定時間で終わる。
    壊れた行・数値でない行・窓や ts が範囲外の行は飛ばして手前の有効行を採用する。
    """
    p = Path(path) if path else official_samples_path()
    try:
        size = p.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    try:
        with open(p, "rb") as f:
            start = max(0, size - tail_bytes)
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        # 先頭は途中で切れている可能性があるため捨てる
        lines = lines[1:]
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            # 壊れた行・巨大整数リテラル・極端なネストで集計全体を落とさない
            continue
        if _valid_sample(obj):
            return [obj]
    return []


def _last_ts(path) -> float:
    """有効な最終行の ts（不正行はスロットル判定に使わない）。無ければ None。"""
    samples = read_official_samples(path)
    return samples[-1]["ts"] if samples else None


def _rotate_samples(path: Path) -> None:
    """行数が `MAX_SAMPLE_LINES` を超えたら先頭半分を切り詰める（atomic write）。

    サンプルは最新 1 件しか使わないため、履歴は上限を決めて捨ててよい。
    切り詰めは `cost_lib.atomic_write_text`（mktemp → os.replace）で行う。
    """
    try:
        # 行数を数えるだけでも全読みになるので、明らかに小さいファイルは触らない
        # （有効なサンプル 1 行は 100 バイト以上ある）。
        if path.stat().st_size < MAX_SAMPLE_LINES * 32:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_SAMPLE_LINES:
        return
    keep = lines[len(lines) // 2:]
    try:
        lib.atomic_write_text(path, "".join(keep))
    except OSError:
        return


def sample_official(auth_path=None, samples_path=None, cfg=None, now=None,
                    force=False, fetcher=None):
    """スロットル付きで 1 回サンプリングし、追記したレコードを返す。

    直近サンプルから `min_interval_sec` 未満なら取得せず None を返す（`force=True` で無視）。
    取得に失敗した場合は `OfficialError` を送出する（呼び出し側で注記にする）。
    """
    cfg = official_config({"codex_official": cfg}) if cfg is not None else dict(OFFICIAL_DEFAULTS)
    now = float(now) if now is not None else time.time()
    path = Path(samples_path) if samples_path else official_samples_path()

    if not force:
        try:
            min_interval = float(cfg.get("min_interval_sec") or 0)
        except (TypeError, ValueError):
            min_interval = OFFICIAL_DEFAULTS["min_interval_sec"]
        last = _last_ts(path)
        if last is not None and (now - last) < min_interval:
            return None

    try:
        timeout = float(cfg.get("timeout_sec") or OFFICIAL_DEFAULTS["timeout_sec"])
    except (TypeError, ValueError):
        timeout = OFFICIAL_DEFAULTS["timeout_sec"]

    # 壁時計の見張り（fetch 側にも同じ期限があるが、呼び出し全体としても超過を検出する）。
    deadline = _overall_deadline(timeout)
    started = time.monotonic()
    shaped = fetch_official(auth_path=auth_path, timeout_sec=timeout, fetcher=fetcher)
    if time.monotonic() - started > deadline:
        raise OfficialError(
            f"公式 usage の取得が全体の壁時計期限（{deadline:.0f} 秒）を超えました。"
        )
    rec = {"ts": now, **shaped}

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # 1 行追記は O_APPEND の 1 回 write で原子性が保てる（4KB 未満）。
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    _rotate_samples(path)
    return rec


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="取得したサンプルを JSON で出力する")
    parser.add_argument("--force", action="store_true", help="スロットルを無視して取得する")
    args = parser.parse_args()

    try:
        config = lib.load_config()
    except lib.ConfigError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    cfg = official_config(lib.pace_config(config))

    enabled, warn = official_enabled(cfg)
    if warn:
        print(f"警告: {warn}", file=sys.stderr)
    if not enabled:
        # README の「false で完全に無効」と挙動を一致させる（--force でも取得しない）。
        print(
            "エラー: Codex 公式 usage の取得は config で無効化されています"
            "（budget.pace.codex_official.enabled = false。--force でも取得しません）。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        rec = sample_official(cfg=cfg, force=args.force)
    except OfficialError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - 例外の中身は出さない（トークン混入の恐れ）
        print(f"エラー: 公式 usage の取得に失敗しました（{type(e).__name__}）。", file=sys.stderr)
        sys.exit(1)

    if rec is None:
        msg = "直近サンプルから min_interval_sec 未満のためスキップしました（--force で強制取得）。"
        if args.json:
            print(json.dumps({"skipped": True, "reason": msg}, ensure_ascii=False))
        else:
            print(msg)
        return

    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        p = rec["primary"]
        print(f"plan: {rec['plan_type']} / used {p['used_percent']}% "
              f"/ 窓 {p['limit_window_seconds']}s / reset_at {p['reset_at']}")


if __name__ == "__main__":
    main()
