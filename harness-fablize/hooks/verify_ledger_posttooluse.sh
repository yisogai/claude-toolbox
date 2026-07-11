#!/usr/bin/env bash
# verify_ledger_posttooluse.sh — PostToolUse hook。
# settings.json では matcher "Bash" と matcher "Edit|Write" の2エントリから
# 同じこのスクリプトを指す（1本で tool_name により分岐する）。
#
# 目的:
#   1) Bash 実行が「検証行為」（テスト・lint・型チェック等）であれば、
#      台帳ファイル $HOME/.claude/completion-gate/ledger-<session_id>.json に
#      1行1JSON（JSON Lines）で {ts, command(先頭120字), ok, kind} を追記する。
#      kind は "test"（pytest/npm test/vitest/jest/go test/cargo test/make test 等）
#      か "lint"（ruff/tsc/mypy/npm run lint|typecheck 等）のいずれか。
#      .claude/verify.json 宣言コマンドは、宣言側に kind があればそれを使い、
#      無ければ "test" 扱いにする（後方互換: 従来の文字列だけの commands 配列も
#      引き続き "test" 扱いで動く）。
#      台帳は「検証行為をしたかどうか」の記録のみが目的で合否判定はしない
#      （合否判定は隠しテスト等の決定論指標の仕事。docs/harness-design.md #4）。
#   2) Edit/Write でコード拡張子のファイルを編集したら、
#      $HOME/.claude/completion-gate/code-edit-<session_id> を touch する。
#      これにより Stop hook（completion_gate_stop.sh）は transcript を読まずに
#      「code-edit の mtime」と「台帳の最終エントリの ts」の比較だけで武装判定できる。
#
# 設計上の厳守事項（~/.claude/skills/model-policy/scripts/ の hook 群のイディオムを踏襲）:
#   - stdin を INPUT="$(cat)" で受け、jq で抽出。jq 不在なら素通し（exit 0）。
#   - ハートビートはロジックより前・jq の有無に関わらず書く（発火自体の検知のため）。
#   - 本スクリプトは deny/block を一切出さない。常に exit 0。
#     （そもそも公式仕様で PostToolUse は decision control 非対応 = ブロック不可。
#      ツールは既に実行済みのため "Can block? No" — WebFetch で確認済み）
#   - tool_response の形は tool_name によって異なる。Bash の tool_response には
#     exit_code フィールドが無く {stdout, stderr, interrupted} のみ
#     （~/.claude/plugins/marketplaces/claude-plugins-official/plugins/security-guidance/
#      hooks/security_reminder_hook.py のコメントで実測確認済み: "Bash tool_response
#      has no exit_code field (only stdout, stderr, interrupted)"）。
#     そのため ok は interrupted の有無からの best-effort 推定に過ぎず、
#     Stop 側の武装判定はこの ok 値を一切参照しない（エントリの存在と ts だけで判定する）。

set -u

GATE_DIR="$HOME/.claude/completion-gate"

# --- 0. ハートビート（最優先。失敗しても続行）------------------------------------
# ディレクトリが無い等でリダイレクト先オープン自体が失敗すると、末尾の 2>/dev/null
# より先にエラーメッセージが出ることがある（bash はリダイレクトを左から順に設定するため）。
# { ...; } でまとめて包み、複合コマンド全体の stderr を抑止する。
mkdir -p "$GATE_DIR" 2>/dev/null
{ date +%s > "$GATE_DIR/last-ledger-hook"; } 2>/dev/null

INPUT="$(cat)"

# jq 不在なら素通し（セッションを壊すより通す。フェイルオープン）
command -v jq >/dev/null 2>&1 || exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"

SID_RAW="$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)"
SID="$(printf '%s' "$SID_RAW" | tr -cd 'A-Za-z0-9_-')"

# session_id が特定できない/サニタイズ後に空になるなら記録をあきらめる（フェイルオープン）
[ -z "$SID" ] && exit 0

case "$TOOL_NAME" in
Bash)
  COMMAND="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"
  [ -z "$COMMAND" ] && exit 0
  CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"

  IS_VERIFY=false
  KIND=""
  if [ -n "$CWD" ] && [ -f "$CWD/.claude/verify.json" ]; then
    # opt-in: verify.json の commands 配列と部分一致すれば検証行為とみなす
    # （commands 以外のパターンは見ない。粗いパターンへはフォールバックしない＝仕様どおり）
    # 各エントリは文字列（後方互換。kind は "test" 扱い）か
    # {"cmd":"...","kind":"test"|"lint"} のオブジェクトのどちらでもよい。
    while IFS= read -r ventry; do
      [ -z "$ventry" ] && continue
      vc="$(printf '%s' "$ventry" | jq -r '.cmd // ""' 2>/dev/null)"
      vk="$(printf '%s' "$ventry" | jq -r '.kind // "test"' 2>/dev/null)"
      [ -z "$vc" ] && continue
      if printf '%s' "$COMMAND" | grep -Fq -- "$vc" 2>/dev/null; then
        IS_VERIFY=true
        KIND="$vk"
        break
      fi
    done < <(jq -c '.commands[]? // empty
      | if type == "object" then {cmd: (.cmd // .command // ""), kind: (.kind // "test")}
        else {cmd: ., kind: "test"} end' "$CWD/.claude/verify.json" 2>/dev/null)
  else
    # 粗いパターン（.claude/verify.json が無いときのフォールバック）
    TEST_PATTERN='pytest|python3? -m pytest|npm (run )?test|npx (vitest|jest)|go test|cargo test|make test'
    LINT_PATTERN='ruff|tsc|mypy|npm run (lint|typecheck)'
    if printf '%s' "$COMMAND" | grep -Eq "$TEST_PATTERN" 2>/dev/null; then
      IS_VERIFY=true
      KIND="test"
    elif printf '%s' "$COMMAND" | grep -Eq "$LINT_PATTERN" 2>/dev/null; then
      IS_VERIFY=true
      KIND="lint"
    fi
  fi

  [ "$IS_VERIFY" = "true" ] || exit 0
  [ -z "$KIND" ] && KIND="test"

  TS="$(date +%s)"
  CMD120="$(printf '%s' "$INPUT" | jq -r '(.tool_input.command // "")[0:120]' 2>/dev/null)"
  INTERRUPTED="$(printf '%s' "$INPUT" | jq -r '(.tool_response.interrupted // false)' 2>/dev/null)"
  OK="true"
  [ "$INTERRUPTED" = "true" ] && OK="false"

  LEDGER_FILE="$GATE_DIR/ledger-$SID.json"
  LINE="$(jq -cn --argjson ts "$TS" --arg cmd "$CMD120" --argjson ok "$OK" --arg kind "$KIND" \
    '{ts:$ts, command:$cmd, ok:$ok, kind:$kind}' 2>/dev/null)"
  if [ -n "$LINE" ]; then
    { printf '%s\n' "$LINE" >> "$LEDGER_FILE"; } 2>/dev/null
  fi
  ;;

Edit|Write)
  FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)"
  [ -z "$FILE_PATH" ] && exit 0

  BASE="${FILE_PATH##*/}"
  case "$BASE" in
    *.*) EXT="${BASE##*.}" ;;
    *) EXT="" ;;
  esac
  EXT_LC="$(printf '%s' "$EXT" | tr '[:upper:]' '[:lower:]')"

  case "$EXT_LC" in
    md|txt|rst)
      : # ドキュメント系は除外
      ;;
    py|js|jsx|ts|tsx|mjs|cjs|sh|bash|go|rs|rb|java|c|h|cpp|cc|hh|hpp|cs|php|kt|kts|swift|json|yaml|yml|toml|sql|css|scss|less|html|htm|vue|graphql|proto|ini|cfg|conf)
      { touch "$GATE_DIR/code-edit-$SID"; } 2>/dev/null
      ;;
    *)
      : # 未知の拡張子はコード扱いしない（保守的側に倒す）
      ;;
  esac
  ;;

*)
  : # 想定外の tool_name（matcher 変更等）は何もしない
  ;;
esac

exit 0
