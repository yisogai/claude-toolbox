#!/usr/bin/env bash
# context_usage.sh — stdin の JSON（hook / statusline 共通）から
# 現在の context 使用率（整数 %）を算出して stdout に出す共通ヘルパー。
#
# 優先順位:
#   1) .context_window.used_percentage（Claude Code が statusline に渡す正値）
#   2) statusline が書いたセッション毎キャッシュ
#      （${TMPDIR:-/tmp}/claude-context-pct-<session_id>、mtime 10分以内のみ採用）
#      hook には used_percentage が渡されないため、statusline の表示値と必ず一致させる。
#      キャッシュは handoff_statusline.sh が「used_percentage を受け取れた時だけ」書く
#      （フォールバック値をキャッシュすると自己参照で値が固まるため書かない）。
#   3) .transcript_path（JSONL）の最後の assistant usage から概算
#      使用トークン = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
#      分母は CLAUDE_CONTEXT_WINDOW（既定 200000）。transcript には window サイズが
#      記録されないため、1M window モデル（例: claude-fable-5[1m]）では過大になる。
#      100% 超は分母想定の誤りとみなし 0 を返す（誤発火より沈黙を選ぶフェイルセーフ）。
#
# 引数 --strict: 3) のフォールバックを使わない（正値かキャッシュが無ければ 0）。
#   threshold hook 用 — 警告の誤発火を原理的に防ぐ。statusline は引数なしで呼ぶ。
#
# 失敗時は必ず 0 を返し exit 0（呼び出し側の hook/statusline を壊さない）。

STRICT=0
[ "$1" = "--strict" ] && STRICT=1

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || { echo 0; exit 0; }
WINDOW="${CLAUDE_CONTEXT_WINDOW:-200000}"

# 1) used_percentage が来ていれば最優先
PCT="$(printf '%s' "$INPUT" | jq -r '.context_window.used_percentage // empty' 2>/dev/null)"
if [ -n "$PCT" ]; then
  printf '%s' "$INPUT" | jq -r '(.context_window.used_percentage // 0) | floor' 2>/dev/null || echo 0
  exit 0
fi

# 2) statusline が書いたセッションキャッシュ（10分以内なら採用）
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
[ -z "$SID" ] && SID="$(basename "$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)" .jsonl)"
if [ -n "$SID" ] && [ "$SID" != "." ]; then
  CACHE="${TMPDIR:-/tmp}/claude-context-pct-$SID"
  if [ -f "$CACHE" ] && [ -n "$(find "$CACHE" -mmin -10 2>/dev/null)" ]; then
    C="$(cat "$CACHE" 2>/dev/null)"
    case "$C" in ''|*[!0-9]*) : ;; *) echo "$C"; exit 0 ;; esac
  fi
fi

# --strict では概算フォールバックを使わない（誤警告防止）
if [ "$STRICT" -eq 1 ]; then echo 0; exit 0; fi

# 3) transcript から概算（分母は CLAUDE_CONTEXT_WINDOW）
TRANSCRIPT="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then echo 0; exit 0; fi

USAGE="$(jq -c 'select(.type=="assistant" and (.message.usage != null)) | .message.usage' "$TRANSCRIPT" 2>/dev/null | tail -1)"
if [ -z "$USAGE" ]; then echo 0; exit 0; fi

TOTAL="$(printf '%s' "$USAGE" | jq -r '((.input_tokens // 0) + (.cache_read_input_tokens // 0) + (.cache_creation_input_tokens // 0))' 2>/dev/null)"
case "$TOTAL" in ''|*[!0-9]*) TOTAL=0;; esac

if [ "$WINDOW" -le 0 ] 2>/dev/null; then WINDOW=200000; fi
PCT=$(( TOTAL * 100 / WINDOW ))
# 100% 超 = 分母（window サイズ）の想定違い。誤警告を避けるため 0 を返す
if [ "$PCT" -gt 100 ]; then echo 0; exit 0; fi
echo "$PCT"
