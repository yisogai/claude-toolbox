#!/usr/bin/env bash
# model_policy_reminder_hook.sh — UserPromptSubmit hook（2本目。handoff_threshold_hook と共存）。層4。
#
# 目的:
#   モデルポリシーが「緩和中（relaxed）」のときだけ、毎プロンプトの冒頭に
#   「緩和中・残り時間・戻し方」を additionalContext として注入する。
#   enforce / off のときは無出力（トークンゼロ）。戻し忘れ事故を構造的に防ぐための可視化。
#
# 設計上の厳守事項:
#   - UserPromptSubmit で exit 2 はプロンプトをブロックするため厳禁。何があっても exit 0。
#   - ハートビートは書かない（毎プロンプト発火するため last-agent-hook と混ざらないように。
#     もし将来書くなら last-reminder-hook を使うこと）。
#   - additionalContext は各 hook 独立に加算注入されるため、handoff の閾値通知と共存できる。

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0

# --- ポリシー読み取り（インライン展開。層1 と同一ロジック）----------------------
MODE="enforce"; DEFMODEL="opus"; ALLOWED="opus sonnet haiku"; ON_FABLE="deny"; DENY_FORK="true"; RUNTIL="0"

CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
POLICY_FILE=""
if [ -n "$CWD" ] && [ -f "${CWD}/.claude/model-policy.json" ]; then
  POLICY_FILE="${CWD}/.claude/model-policy.json"
elif [ -f "$HOME/.claude/model-policy/policy.json" ]; then
  POLICY_FILE="$HOME/.claude/model-policy/policy.json"
fi

if [ -n "$POLICY_FILE" ]; then
  PARSED="$(jq -r '
    [ (.mode // "enforce"),
      (.default_model // "opus"),
      ((.allowed // ["opus","sonnet","haiku"]) | join(" ")),
      (.on_fable // "deny"),
      (if .deny_fork == null then true else .deny_fork end | tostring),
      (.relaxed_until // 0)
    ] | @tsv' "$POLICY_FILE" 2>/dev/null)"
  if [ -n "$PARSED" ]; then
    IFS=$'\t' read -r p_mode p_defmodel p_allowed p_onfable p_denyfork p_runtil <<EOF
$PARSED
EOF
    case "$p_mode"    in enforce|off) MODE="$p_mode";; esac
    [ -n "$p_defmodel" ] && DEFMODEL="$p_defmodel"
    [ -n "$p_allowed"  ] && ALLOWED="$p_allowed"
    case "$p_onfable" in deny|rewrite) ON_FABLE="$p_onfable";; esac
    case "$p_denyfork" in true|false)  DENY_FORK="$p_denyfork";; esac
    case "$p_runtil"  in ''|*[!0-9]*) RUNTIL=0;; *) RUNTIL="$p_runtil";; esac
  fi
fi

# --- 状態判定 ------------------------------------------------------------------
NOW="$(date +%s)"
if [ "$MODE" = "off" ]; then
  STATE="off"
elif [ "$RUNTIL" -gt "$NOW" ] 2>/dev/null; then
  STATE="relaxed"
else
  STATE="enforce"
fi

# --- relaxed のときだけリマインダーを注入。それ以外は無出力 exit 0 --------------
if [ "$STATE" = "relaxed" ]; then
  REMAIN=$(( (RUNTIL - NOW + 59) / 60 ))          # 残り分（切り上げ）
  UNTIL_H="$(date -r "$RUNTIL" '+%H:%M' 2>/dev/null)"  # 復帰時刻（BSD date）
  MSG="【モデルポリシー緩和中】サブエージェントのモデル強制が緩和されています（残り約 ${REMAIN} 分、${UNTIL_H} まで）。緩和が不要になったら /model-policy reset で即時 enforce に戻すこと。"
  jq -n --arg msg "$MSG" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$msg}}' 2>/dev/null
fi

exit 0
