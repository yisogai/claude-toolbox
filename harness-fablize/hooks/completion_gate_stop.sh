#!/usr/bin/env bash
# completion_gate_stop.sh — Stop hook。
#
# 目的:
#   セッション中にコードファイルの Edit/Write があったのに、その後にテスト実行
#   （kind=="test" の検証行為。pytest/npm test 等）をした記録が無いまま完了しようと
#   した場合にだけ、Claude に検証実行を促す（block）。lint/型チェック（kind=="lint"）
#   のみでは武装解除しない（docs/harness-design.md #4: gate は anti-未検証であって
#   anti-虚偽完了ではない）。判定材料は verify_ledger_posttooluse.sh が書く
#   code-edit-<session_id>（mtime）と ledger-<session_id>.json（kind=="test" の
#   エントリのうち最新の ts）のみで、transcript は読まない。
#
# block 方式の選定根拠（公式 hooks 仕様を WebFetch で確認: code.claude.com/docs/en/hooks）:
#   ドキュメントの "Decision control" 表で Stop / SubagentStop の decision pattern は
#   "Top-level decision"、キーは decision:"block" と reason
#   （hookSpecificOutput に包まず、JSON のトップレベルに decision/reason を置く方式。
#   PostToolUse や UserPromptSubmit と共通の枠組み）。
#   Stop は exit code 2 でも継続を阻止できる（"Prevents Claude from stopping,
#   continues the conversation"）が、このリポジトリの hook 群は
#   「意図した出力（deny/block の JSON 等）以外は常に exit 0」を統一イディオムにしている
#   （~/.claude/skills/model-policy/scripts/ 参照）。exit コードに意味を持たせると
#   将来のバージョン差や set -u 由来の予期しない非ゼロ終了と区別しづらくなるため、
#   本スクリプトは exit 2 を使わず、stdout に {"decision":"block","reason":...} を
#   JSON で出したうえで必ず exit 0 する方式を採用する。
#
# 設計上の厳守事項:
#   - キルスイッチ（$HOME/.claude/completion-gate/state.json の mode:"off"）が無くても
#     正しく動く必要がある（eval では未インストール環境が主）。ディレクトリ作成不可等で
#     何もできない場合は、armed 判定に必要な code-edit ファイルがそもそも存在しない
#     という経路を通って自然に exit 0 に落ちる（安全側）。
#   - stdin の stop_hook_active（Stop hook 自身の再帰発火を防ぐための公式フラグ）が
#     true なら即 exit 0（無限ループ防止）。
#   - 武装条件は AND: code-edit ファイルが存在 かつ その mtime より新しい
#     kind=="test" の台帳エントリが無い場合のみ block。kind フィールドが無い
#     古いエントリ（後方互換）は "test" 扱いにする。
#   - ブロックはセッション毎最大2回（blocks-<session_id> カウンタ）。
#   - 状態ファイルの掃除（48時間より古い ledger-*/code-edit-*/blocks-*）はベストエフォート。
#     失敗しても以降の処理・exit 0 経路に影響させない。

set -u

GATE_DIR="$HOME/.claude/completion-gate"

# --- 0. ハートビート ------------------------------------------------------------
# ディレクトリが無い等でリダイレクト先オープン自体が失敗すると、末尾の 2>/dev/null
# より先にエラーメッセージが出ることがある（bash はリダイレクトを左から順に設定するため）。
# { ...; } でまとめて包み、複合コマンド全体の stderr を抑止する。
mkdir -p "$GATE_DIR" 2>/dev/null
{ date +%s > "$GATE_DIR/last-gate-hook"; } 2>/dev/null

# --- 0.5 古い状態ファイルの掃除（ベストエフォート、失敗しても続行）----------------
{
  find "$GATE_DIR" -maxdepth 1 -type f \
    \( -name 'ledger-*' -o -name 'code-edit-*' -o -name 'blocks-*' \) \
    -mtime +2 -delete
} >/dev/null 2>&1 || true

INPUT="$(cat)"

# jq 不在なら素通し（block の JSON を安全に組み立てられないため。フェイルオープン）
command -v jq >/dev/null 2>&1 || exit 0

# --- 1. キルスイッチ --------------------------------------------------------------
# state.json 不在は enforce 扱い。壊れた JSON / 想定外値も enforce 扱い（安全側）。
STATE_FILE="$GATE_DIR/state.json"
MODE="enforce"
if [ -f "$STATE_FILE" ]; then
  M="$(jq -r '.mode // "enforce"' "$STATE_FILE" 2>/dev/null)"
  [ -n "$M" ] && MODE="$M"
fi
[ "$MODE" = "off" ] && exit 0

# --- 2. 無限ループ防止（公式仕様: stop_hook_active）--------------------------------
STOP_ACTIVE="$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)"
[ "$STOP_ACTIVE" = "true" ] && exit 0

# --- 3. セッション特定 --------------------------------------------------------------
SID_RAW="$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)"
SID="$(printf '%s' "$SID_RAW" | tr -cd 'A-Za-z0-9_-')"
[ -z "$SID" ] && exit 0

CODE_EDIT_FILE="$GATE_DIR/code-edit-$SID"
LEDGER_FILE="$GATE_DIR/ledger-$SID.json"
BLOCKS_FILE="$GATE_DIR/blocks-$SID"

# --- 4. 武装判定（AND）--------------------------------------------------------------
# code-edit ファイルが無ければ、そもそもコード編集が無かった（or 記録できなかった）
# ので武装しない。
[ -f "$CODE_EDIT_FILE" ] || exit 0

CODE_MTIME="$(stat -f %m "$CODE_EDIT_FILE" 2>/dev/null || stat -c %Y "$CODE_EDIT_FILE" 2>/dev/null)"
case "$CODE_MTIME" in ''|*[!0-9]*) exit 0 ;; esac

# 台帳の全行を走査し、kind=="test" のエントリのうち最新の ts を求める。
# kind フィールドが無い古いエントリ（後方互換）は "test" 扱いにする。
# 最終行だけを見ないのは、コード編集後に pytest → lint の順で実行した場合、
# 最終行が lint（kind=="lint"）でも直前の pytest 実行は有効な検証行為のため。
# 1行ずつ jq にかけることで、途中に壊れた行があっても他の行の判定に影響しない。
LAST_TEST_VERIFY_TS=0
if [ -f "$LEDGER_FILE" ]; then
  while IFS= read -r LINE; do
    [ -z "$LINE" ] && continue
    K="$(printf '%s' "$LINE" | jq -r '.kind // "test"' 2>/dev/null)"
    [ "$K" = "test" ] || continue
    V="$(printf '%s' "$LINE" | jq -r '.ts // empty' 2>/dev/null)"
    case "$V" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$V" -gt "$LAST_TEST_VERIFY_TS" ] 2>/dev/null; then
      LAST_TEST_VERIFY_TS="$V"
    fi
  done < "$LEDGER_FILE"
fi

# 台帳の最新のテスト実行（kind=="test"）が code-edit の mtime より新しければ武装しない。
# 同秒（タイの場合）は「まだ検証されていない」扱い（安全側＝block に倒す）。
# lint のみ（kind=="lint"）を実行しても、ここではカウントされず武装解除されない。
if [ "$LAST_TEST_VERIFY_TS" -gt "$CODE_MTIME" ] 2>/dev/null; then
  exit 0
fi

# --- 5. ブロック回数上限（セッション毎 最大2回）--------------------------------------
BLOCKS_COUNT="$(cat "$BLOCKS_FILE" 2>/dev/null)"
case "$BLOCKS_COUNT" in ''|*[!0-9]*) BLOCKS_COUNT=0 ;; esac
if [ "$BLOCKS_COUNT" -ge 2 ] 2>/dev/null; then
  exit 0
fi

NEW_COUNT=$((BLOCKS_COUNT + 1))
{ printf '%s' "$NEW_COUNT" > "$BLOCKS_FILE"; } 2>/dev/null

REASON='コードを変更したが、その後にテストを実行した記録がない（lint/型チェックのみでは解除されない）。テストを実行して結果を確認してから完了すること。検証手段がないと判断した場合は、その理由と残存リスクを最終報告に明記した上で完了してよい。'
OUT="$(jq -cn --arg r "$REASON" '{decision:"block", reason:$r}' 2>/dev/null)"
[ -z "$OUT" ] && exit 0
printf '%s\n' "$OUT"
exit 0
