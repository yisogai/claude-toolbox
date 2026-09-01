#!/usr/bin/env python3
"""codex-bridge の共通処理。

- ルート解決（`CODEX_BRIDGE_ROOT` でテストから差し替え可能）
- アトミック書込（mktemp → os.replace）
- codex バイナリ解決（cmux シム除外）
- 単価表の読込とクレジット概算
- 使用量台帳（`var/codex_usage.jsonl`）への O_APPEND 追記

python3 標準ライブラリのみを使う。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

#: PATH 走査で除外するパス断片（cmux の CLI シムは実バイナリではない）
SHIM_MARKERS = ("cmux-cli-shims",)


# ---------------------------------------------------------------------------
# ルート解決
# ---------------------------------------------------------------------------

def code_root() -> Path:
    """スクリプト自身の実位置基準のリポジトリ内ルート（templates/ の解決に使う）。"""
    return Path(__file__).resolve().parent.parent


def root() -> Path:
    """`config/` `var/` の親ルート。テストは `CODEX_BRIDGE_ROOT` で差し替える。"""
    env = os.environ.get("CODEX_BRIDGE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return code_root()


def var_dir() -> Path:
    return root() / "var"


def locks_dir() -> Path:
    return var_dir() / "locks"


def usage_ledger_path() -> Path:
    return var_dir() / "codex_usage.jsonl"


def rate_limits_ledger_path() -> Path:
    return var_dir() / "codex_rate_limits.jsonl"


def pricing_path() -> Path:
    """単価表。`CODEX_BRIDGE_ROOT` 側に無ければコード同梱のものへフォールバックする。"""
    p = root() / "config" / "codex_pricing.json"
    if p.exists():
        return p
    return code_root() / "config" / "codex_pricing.json"


def config_path() -> Path:
    """任意設定（`usage_mode` 等）。`CODEX_BRIDGE_ROOT` 側に無ければコード同梱へフォールバック。"""
    p = root() / "config" / "codex_bridge.json"
    if p.exists():
        return p
    return code_root() / "config" / "codex_bridge.json"


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


USAGE_MODES = ("cumulative", "per_turn")


def usage_mode() -> str:
    """`--resume` 時の usage 解釈（M-2）。

    優先順位: 環境変数 `CODEX_BRIDGE_USAGE_MODE` > `config/codex_bridge.json` の `usage_mode` >
    既定 `cumulative`（resume 行はスレッド累計とみなして直前行との差分で計上する）。
    実機検証で「ターン単位」だと分かったら `per_turn` に切り替える。
    """
    for cand in (os.environ.get("CODEX_BRIDGE_USAGE_MODE"), load_config().get("usage_mode")):
        if cand in USAGE_MODES:
            return cand
    return "cumulative"


def templates_dir() -> Path:
    return code_root() / "templates"


def codex_home() -> Path:
    """Codex の設定ディレクトリ（`CODEX_HOME` があれば優先。既定 `~/.codex`）。"""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


ROLLOUT_TAIL_BYTES = 512 * 1024


def scan_rollout(thread_id: str | None, budget_sec: float = 1.5) -> dict:
    """rollout jsonl の最後の token_count を best-effort で取得する。"""
    out = {"usage": None, "rate_limits": None, "path": None}
    if not thread_id:
        return out
    started = time.monotonic()
    pattern = f"sessions/*/*/*/rollout-*-{thread_id}.jsonl"
    for path in sorted(codex_home().glob(pattern)):
        if time.monotonic() - started > budget_sec:
            break
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - ROLLOUT_TAIL_BYTES))
                lines = f.read().split(b"\n")
        except OSError:
            continue
        out["path"] = str(path)
        for raw in reversed(lines):
            if b'"token_count"' not in raw:
                continue
            try:
                payload = (json.loads(raw.decode("utf-8", "replace")).get("payload") or {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            out["usage"] = normalize_usage(info.get("total_token_usage"))
            out["rate_limits"] = normalize_rate_limits(payload.get("rate_limits"), "rollout.token_count")
            return out
    return out


# ---------------------------------------------------------------------------
# 時刻
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str):
    """ISO 文字列を aware datetime にする。naive は JST 解釈。失敗時は None。"""
    if not s:
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


# ---------------------------------------------------------------------------
# アトミック書込
# ---------------------------------------------------------------------------

def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path, obj) -> None:
    """O_APPEND で 1 行追記する（同時実行でも行が混ざらないようにまとめて 1 write）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# codex バイナリ解決
# ---------------------------------------------------------------------------

def _is_executable(p: Path) -> bool:
    try:
        st = p.stat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(str(p), os.X_OK)


def is_shim(path: str) -> bool:
    return any(m in str(path) for m in SHIM_MARKERS)


def resolve_codex_bin(explicit: str | None = None, path_env: str | None = None):
    """codex 実バイナリを解決する。

    優先順位: `--codex-bin` > env `CODEX_BIN` > PATH 走査。
    PATH 走査では `cmux-cli-shims` を含むパスを除外する（実バイナリではないため）。
    戻り値は (path or None, skipped_shims:list[str])。
    """
    skipped: list[str] = []
    for cand in (explicit, os.environ.get("CODEX_BIN")):
        if cand:
            p = Path(cand).expanduser()
            if _is_executable(p):
                return str(p.resolve()), skipped
            return None, skipped
    raw = path_env if path_env is not None else os.environ.get("PATH", "")
    for d in raw.split(os.pathsep):
        if not d:
            continue
        p = Path(d) / "codex"
        if not _is_executable(p):
            continue
        if is_shim(str(p)):
            skipped.append(str(p))
            continue
        return str(p), skipped
    return None, skipped


# ---------------------------------------------------------------------------
# 単価・クレジット概算
# ---------------------------------------------------------------------------

def load_pricing(path=None) -> dict:
    p = Path(path) if path else pricing_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"as_of": None, "unit": "credits_per_mtok", "models": {}, "notes": []}


USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

USAGE_ALIASES = {
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
}


def normalize_usage(usage) -> dict | None:
    """turn.completed の usage を 5 フィールドの int dict に正規化する。"""
    if not isinstance(usage, dict):
        return None
    src = dict(usage)
    for camel, snake in USAGE_ALIASES.items():
        if snake not in src and camel in src:
            src[snake] = src[camel]
    out = {}
    for k in USAGE_FIELDS:
        v = src.get(k)
        try:
            out[k] = int(v) if v is not None else 0
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _rl_window(window):
    if not isinstance(window, dict):
        return None
    return {
        "used_percent": window.get("used_percent", window.get("usedPercent")),
        "window_minutes": window.get("window_minutes", window.get("windowDurationMins")),
        "resets_at": window.get("resets_at", window.get("resetsAt")),
    }


def normalize_rate_limits(raw, source: str) -> dict | None:
    """RateLimitSnapshot を snake_case にする。窓種は window_minutes で判別する。

    rate_limits はアカウント単位のスナップショットであり、usage のような差分は取らない。
    primary / secondary は特定の窓種へ固定して解釈しない。
    """
    if not isinstance(raw, dict):
        return None
    credits = raw.get("credits")
    return {
        "limit_id": raw.get("limit_id", raw.get("limitId")),
        "limit_name": raw.get("limit_name", raw.get("limitName")),
        "primary": _rl_window(raw.get("primary")),
        "secondary": _rl_window(raw.get("secondary")),
        "credits": {
            "has_credits": credits.get("has_credits", credits.get("hasCredits")),
            "unlimited": credits.get("unlimited"),
            "balance": credits.get("balance"),
        } if isinstance(credits, dict) else None,
        "plan_type": raw.get("plan_type", raw.get("planType")),
        "rate_limit_reached_type": raw.get("rate_limit_reached_type", raw.get("rateLimitReachedType")),
        "spend_control_reached": raw.get("spend_control_reached", raw.get("spendControlReached")),
        "source": source,
        "observed_at": iso(now_utc()),
    }


def merge_rate_limits(prev, new):
    """疎な更新に含まれる None では、直前のスナップショットを消さない。"""
    if prev is None:
        return new
    if new is None:
        return prev
    out = dict(prev)
    for key, value in new.items():
        if value is not None:
            out[key] = value
    return out


def append_rate_limits(payload: dict) -> None:
    """rate_limits スナップショットを専用台帳へ追記する。"""
    rate_limits = payload.get("rate_limits")
    if not rate_limits or payload.get("mock"):
        return
    try:
        append_jsonl(rate_limits_ledger_path(), {
            "ts": payload["ended_at"], "job_dir": payload.get("job_dir"),
            "mode": payload.get("mode"), "model": payload.get("model"),
            "status": payload.get("status"), "rate_limits": rate_limits,
        })
    except OSError as exc:
        payload.setdefault("warnings", []).append(f"rate_limits 台帳への追記に失敗した: {exc}")


def credits_est(usage, model: str, pricing=None):
    """ChatGPT プランのクレジット概算（credits）。単価不明なら None。

    計上ルール（いずれも [未確認]。README / config の notes に明記）:
    - `input_tokens` は cached を内包すると解釈し、非キャッシュ入力 = input - cached。
    - `cache_write_input_tokens` は input 単価で加算。
    - `reasoning_output_tokens` は `output_tokens` に内包すると解釈し、二重計上しない。
    """
    u = normalize_usage(usage)
    if u is None:
        return None
    pr = pricing if pricing is not None else load_pricing()
    m = (pr.get("models") or {}).get(model)
    if not m:
        return None
    fresh_input = max(0, u["input_tokens"] - u["cached_input_tokens"])
    total = (
        fresh_input * float(m.get("input", 0))
        + u["cache_write_input_tokens"] * float(m.get("input", 0))
        + u["cached_input_tokens"] * float(m.get("cached_input", 0))
        + u["output_tokens"] * float(m.get("output", 0))
    ) / 1_000_000.0
    return round(total, 4)


# ---------------------------------------------------------------------------
# 表示ヘルパ
# ---------------------------------------------------------------------------

def fmt_tokens(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "-"


def fmt_duration(sec) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "-"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def eprint(*a) -> None:
    print(*a, file=sys.stderr)
