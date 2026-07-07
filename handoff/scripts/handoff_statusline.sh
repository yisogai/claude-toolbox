#!/usr/bin/env bash
# handoff_statusline.sh — context 使用率を色付きで常時表示する statusline。
#   <75%: 緑 ●   75–84%: 黄 ⚠   >=85%: 赤 ⚠
# モデル表示名と cwd のベース名も添える。

INPUT="$(cat)"
DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
PCT=0
[ -n "$DIR" ] && PCT="$(printf '%s' "$INPUT" | bash "$DIR/context_usage.sh" 2>/dev/null)"
case "$PCT" in ''|*[!0-9]*) PCT=0;; esac

# Claude Code が渡す正値（used_percentage）を受け取れた時だけ、セッション毎に PCT を
# キャッシュする。threshold hook には used_percentage が渡されないため、このキャッシュ
# 経由で statusline の表示値と警告判定を一致させる（フォールバック概算値はキャッシュ
# しない — 自己参照で値が固まるのを防ぐため）。
if command -v jq >/dev/null 2>&1; then
  AUTH="$(printf '%s' "$INPUT" | jq -r '.context_window.used_percentage // empty' 2>/dev/null)"
  SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
  [ -z "$SID" ] && SID="$(basename "$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)" .jsonl)"
  if [ -n "$AUTH" ] && [ -n "$SID" ] && [ "$SID" != "." ]; then
    printf '%s' "$PCT" > "${TMPDIR:-/tmp}/claude-context-pct-$SID" 2>/dev/null
  fi
fi

MODEL=""; CWD=""
if command -v jq >/dev/null 2>&1; then
  MODEL="$(printf '%s' "$INPUT" | jq -r '.model.display_name // .model.id // empty' 2>/dev/null)"
  CWD="$(printf '%s' "$INPUT" | jq -r '.workspace.current_dir // .cwd // empty' 2>/dev/null)"
fi

if [ "$PCT" -ge 85 ]; then
  COL=$'\033[31m'; MARK="⚠"
elif [ "$PCT" -ge 75 ]; then
  COL=$'\033[33m'; MARK="⚠"
else
  COL=$'\033[32m'; MARK="●"
fi
RST=$'\033[0m'; DIM=$'\033[2m'

OUT="${COL}${MARK} Context ${PCT}%${RST}"
[ -n "$MODEL" ] && OUT="$OUT ${DIM}|${RST} ${MODEL}"
[ -n "$CWD" ] && OUT="$OUT ${DIM}| $(basename "$CWD")${RST}"
printf '%s' "$OUT"
