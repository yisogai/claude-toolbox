#!/usr/bin/env bash
# handoff_threshold_hook.sh — UserPromptSubmit hook。
# 会話の context 使用率が 75 / 85 / 95 % を超えたら、その段階で「1回だけ」
# Claude に handoff 提案を指示する additionalContext を注入する。
#
# 段階管理: セッションごとに「これまで提案した最高段階」を一時ファイルに記録し、
#           同じ/低い段階では再提案しない（= うるさくならない／無視され続けても上の段階で再提示）。
# 安全策:   何があっても exit 0（UserPromptSubmit で exit 2 はプロンプトをブロックするため厳禁）。

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0
DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -z "$DIR" ] && exit 0

# --strict: used_percentage かセッションキャッシュ（statusline 由来の正値）のみ採用。
# transcript からの概算は window サイズ不明（1M モデルで約5倍に過大）のため警告には使わない。
PCT="$(printf '%s' "$INPUT" | bash "$DIR/context_usage.sh" --strict 2>/dev/null)"
case "$PCT" in ''|*[!0-9]*) exit 0;; esac

# 超えている最大の段階を求める
STAGE=0
for s in 75 85 95; do
  if [ "$PCT" -ge "$s" ]; then STAGE="$s"; fi
done
[ "$STAGE" -eq 0 ] && exit 0

# セッション単位の重複抑制
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null)"
[ -z "$SID" ] && SID="unknown"
STATE_FILE="${TMPDIR:-/tmp}/claude-handoff-threshold-$SID"
LAST="$(cat "$STATE_FILE" 2>/dev/null)"
case "$LAST" in ''|*[!0-9]*) LAST=0;; esac

[ "$STAGE" -le "$LAST" ] && exit 0
echo "$STAGE" > "$STATE_FILE" 2>/dev/null

case "$STAGE" in
  75) MSG="【システム通知 / context ${PCT}%】会話の context 使用率が 75% を超えました。この応答ではユーザーの依頼に通常どおり対応したうえで、応答の最後に1回だけ、handoff（/handoff スキルで引き継ぎテキストを生成し /compact に渡す運用）を行うことを提案してください。ユーザーが不要と答えた場合は蒸し返さないこと。" ;;
  85) MSG="【システム通知 / context ${PCT}%】context 使用率が 85% を超えました（自動コンパクトが近づいています）。ユーザーの依頼に対応しつつ、いま handoff を行うことを明確に推奨してください。" ;;
  95) MSG="【システム通知 / context ${PCT}%】context 使用率が 95% を超えました（自動コンパクトが差し迫っています）。最優先で handoff の実行を促し、大きな新規作業は handoff 後に回すよう助言してください。" ;;
esac

jq -n --arg msg "$MSG" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$msg}}'
exit 0
