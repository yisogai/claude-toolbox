#!/usr/bin/env bash
# license_statusline.sh — handoff_statusline.sh の出力の末尾に、アクティブなアカウント/
# ライセンスのセグメントを追加する合成 wrapper（handoff 側は無改変のまま呼び出す）。
#   🔑 <name>           license-switch（.envrc の CLAUDE_LICENSE_NAME）適用時。黄色強調
#   🔑 env              license-switch 外の env 認証（ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY /
#                       CLAUDE_CODE_OAUTH_TOKEN のいずれかが設定されている）
#   ⚿ <メールlocal部>   メインの /login アカウント（~/.claude.json の oauthAccount）。薄色
#
# 設計:
#   - 表示は「env 上でどのライセンスを意図しているか」。実課金アカウントのサーバー側検証では
#     ない（oauth トークン失効時は無言でメインにフォールバックする既知挙動があるため）。
#   - email は ~/.claude.json（数百KB）の毎回パースを避け、mtime をキーに $TMPDIR へキャッシュ。
#   - statusline はエラーを表に出さない（失敗した要素は黙って省く）。

INPUT="$(cat)"

BASE=""
HANDOFF="$HOME/.claude/skills/handoff/scripts/handoff_statusline.sh"
[ -f "$HANDOFF" ] && BASE="$(printf '%s' "$INPUT" | bash "$HANDOFF" 2>/dev/null)"

RST=$'\033[0m'; DIM=$'\033[2m'; YEL=$'\033[33m'

main_account_label() {
  local cfg="$HOME/.claude.json" cache="${TMPDIR:-/tmp}/claude-license-statusline-email"
  [ -f "$cfg" ] || return 0
  local mtime line email label
  mtime="$(stat -f %m "$cfg" 2>/dev/null || stat -c %Y "$cfg" 2>/dev/null)"
  if [ -f "$cache" ]; then
    IFS= read -r line < "$cache" 2>/dev/null || true
    case "$line" in "$mtime "*) printf '%s' "${line#"$mtime" }"; return 0;; esac
  fi
  command -v jq >/dev/null 2>&1 || return 0
  email="$(jq -r '.oauthAccount.emailAddress // empty' "$cfg" 2>/dev/null)"
  label="${email%%@*}"
  [ -n "$label" ] && printf '%s %s' "$mtime" "$label" > "$cache" 2>/dev/null
  printf '%s' "$label"
}

if [ -n "${CLAUDE_LICENSE_NAME:-}" ]; then
  SEG="${YEL}🔑 ${CLAUDE_LICENSE_NAME}${RST}"
elif [ -n "${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  SEG="${YEL}🔑 env${RST}"
else
  LABEL="$(main_account_label)"
  SEG=""
  [ -n "$LABEL" ] && SEG="${DIM}⚿ ${LABEL}${RST}"
fi

if [ -n "$BASE" ] && [ -n "$SEG" ]; then
  printf '%s %s %s' "$BASE" "${DIM}|${RST}" "$SEG"
else
  printf '%s' "${BASE}${SEG}"
fi
