#!/usr/bin/env bash
# workflow_nudge_prompt.sh — UserPromptSubmit hook。
#
# 目的:
#   ユーザーのプロンプトが「実装系」かつ一定以上の長さのときだけ、
#   ミニ仕様→実装→検証→(複数ファイル変更時は verifier サブエージェント) という
#   手順を1行だけ additionalContext として注入する。該当しない場合は無出力
#   （トークンゼロ原則。~/.claude/skills/model-policy/scripts/model_policy_reminder_hook.sh
#   と同じ思想）。
#
# 設計上の厳守事項:
#   - UserPromptSubmit の exit code 2 はプロンプトそのものをブロックし消してしまうため
#     厳禁（公式仕様: "Blocks prompt processing and erases the prompt"）。
#     本スクリプトは常に exit 0。
#   - stdin を INPUT="$(cat)" で受け、jq で抽出。jq 不在なら素通し（exit 0）。
#   - 文字数判定は bash の ${#var}（ロケール依存で日本語だと不安定になりうる）ではなく
#     jq の length（文字列を Unicode コードポイント列として数える）で行う。
#   - ハートビートは他2本と同じ $HOME/.claude/completion-gate/ に last-nudge-hook として
#     該当有無に関わらず毎回刻む（このスクリプトはタスク仕様で明示的にハートビートを
#     要求されているため、reminder_hook.sh のような「注入時以外は書かない」判断はしない）。

set -u

GATE_DIR="$HOME/.claude/completion-gate"

# --- 0. ハートビート ------------------------------------------------------------
# ディレクトリが無い等でリダイレクト先オープン自体が失敗すると、末尾の 2>/dev/null
# より先にエラーメッセージが出ることがある（bash はリダイレクトを左から順に設定するため）。
# { ...; } でまとめて包み、複合コマンド全体の stderr を抑止する。
mkdir -p "$GATE_DIR" 2>/dev/null
{ date +%s > "$GATE_DIR/last-nudge-hook"; } 2>/dev/null

INPUT="$(cat)"

# jq 不在なら素通し（フェイルオープン）
command -v jq >/dev/null 2>&1 || exit 0

PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
[ -z "$PROMPT" ] && exit 0

LEN="$(printf '%s' "$INPUT" | jq -r '(.prompt // "") | length' 2>/dev/null)"
case "$LEN" in ''|*[!0-9]*) exit 0 ;; esac
[ "$LEN" -ge 12 ] 2>/dev/null || exit 0

printf '%s' "$PROMPT" | grep -Eiq \
  '実装|追加|修正|直して|作って|リファクタ|implement|fix|add|refactor' 2>/dev/null || exit 0

MSG='実装タスクの手順リマインダ: 着手前にミニ仕様（目的/範囲/非目標/完了条件）を書く。完了宣言前に検証を実行し、複数ファイル変更時は verifier サブエージェントを通す。'
OUT="$(jq -cn --arg m "$MSG" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$m}}' 2>/dev/null)"
[ -z "$OUT" ] && exit 0
printf '%s\n' "$OUT"
exit 0
