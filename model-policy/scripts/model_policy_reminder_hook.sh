#!/usr/bin/env bash
# model_policy_reminder_hook.sh — UserPromptSubmit hook（2本目。handoff_threshold_hook と共存）。層4。
#
# 目的:
#   次の3条件のいずれかのときだけ、毎プロンプトの冒頭に additionalContext を注入する:
#   (1) 緩和中（relaxed）: 残り時間・戻し方（従来動作）
#   (2) settings.json の恒久 model が fable: メインモデル・ドリフト警告（opus 運用への計器）
#   (3) fable 例外（fable_exempt_until）の失効48時間前: 失効予告
#   平常時（enforce・model=opus・例外余裕あり）は無出力（トークンゼロ）。
#   戻し忘れ/ドリフト/失効事故を構造的に防ぐための可視化。
#
# 設計上の厳守事項:
#   - UserPromptSubmit で exit 2 はプロンプトをブロックするため厳禁。何があっても exit 0。
#   - ハートビートは書かない（毎プロンプト発火するため last-agent-hook と混ざらないように。
#     もし将来書くなら last-reminder-hook を使うこと）。
#   - additionalContext は各 hook 独立に加算注入されるため、handoff の閾値通知と共存できる。

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0

# --- ポリシー読み取り（インライン展開。層1 と同一ロジック）----------------------
MODE="enforce"; DEFMODEL="opus"; ALLOWED="opus sonnet haiku"; ON_FABLE="deny"; DENY_FORK="true"; RUNTIL="0"; EXEMPT=""; EXUNTIL="0"

CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
POLICY_FILE=""
if [ -n "$CWD" ] && [ -f "${CWD}/.claude/model-policy.json" ]; then
  POLICY_FILE="${CWD}/.claude/model-policy.json"
elif [ -f "$HOME/.claude/model-policy/policy.json" ]; then
  POLICY_FILE="$HOME/.claude/model-policy/policy.json"
fi

if [ -n "$POLICY_FILE" ]; then
  # 1 行 1 フィールド抽出（@tsv の空フィールド畳み込み回避。層1 と同一イディオム）
  PARSED="$(jq -r '
    (.mode // "enforce"),
    (.default_model // "opus"),
    ((.allowed // ["opus","sonnet","haiku"]) | join(" ")),
    (.on_fable // "deny"),
    (if .deny_fork == null then true else .deny_fork end | tostring),
    (.relaxed_until // 0),
    ((.fable_exempt_subagent_types // []) | join(" ")),
    (.fable_exempt_until // 0)' "$POLICY_FILE" 2>/dev/null)"
  if [ -n "$PARSED" ]; then
    {
      IFS= read -r p_mode; IFS= read -r p_defmodel; IFS= read -r p_allowed
      IFS= read -r p_onfable; IFS= read -r p_denyfork; IFS= read -r p_runtil
      IFS= read -r p_exempt; IFS= read -r p_exuntil
    } <<EOF
$PARSED
EOF
    case "$p_mode"    in enforce|off) MODE="$p_mode";; esac
    [ -n "$p_defmodel" ] && DEFMODEL="$p_defmodel"
    [ -n "$p_allowed"  ] && ALLOWED="$p_allowed"
    case "$p_onfable" in deny|rewrite) ON_FABLE="$p_onfable";; esac
    case "$p_denyfork" in true|false)  DENY_FORK="$p_denyfork";; esac
    case "$p_runtil"  in ''|*[!0-9]*) RUNTIL=0;; *) RUNTIL="$p_runtil";; esac
    EXEMPT="$(printf '%s' "$p_exempt" | tr '[:upper:]' '[:lower:]')"
    case "$p_exuntil" in ''|*[!0-9]*) EXUNTIL=0;; *) EXUNTIL="$p_exuntil";; esac
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

# --- 注入メッセージの組み立て（該当なしなら無出力 exit 0 = 平常時トークンゼロ）---
MSGS=""

# (1) 緩和中リマインダー（従来動作）
if [ "$STATE" = "relaxed" ]; then
  REMAIN=$(( (RUNTIL - NOW + 59) / 60 ))          # 残り分（切り上げ）
  UNTIL_H="$(date -r "$RUNTIL" '+%H:%M' 2>/dev/null)"  # 復帰時刻（BSD date）
  MSGS="【モデルポリシー緩和中】サブエージェントのモデル強制が緩和されています（残り約 ${REMAIN} 分、${UNTIL_H} まで）。緩和が不要になったら /model-policy reset で即時 enforce に戻すこと。"
fi

# (2) メインモデル・ドリフト計器: 恒久設定（settings.json の model）が fable のままなら警告。
#     Opus メイン運用への移行後、/model や設定編集で fable が恒久化されたら毎プロンプトで
#     気づかせる（セッション内の一時昇格 /model fable は settings.json に残らない想定）。
SETTINGS_MODEL="$(jq -r '.model // ""' "$HOME/.claude/settings.json" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
case "$SETTINGS_MODEL" in
  *fable*)
    MSGS="${MSGS:+$MSGS
}【メインモデル注意】settings.json の恒久 model が fable です。運用方針はメイン=opus（fable はセッション内 /model fable の一時昇格と fable-advisor のみ）。意図的な設定でなければ claude-opus-4-8[1m] へ戻すこと。"
    ;;
esac

# (3) fable 例外のまもなく失効警告（残り48時間未満のときだけ。平常時は無出力を保つ）
if [ -n "$EXEMPT" ] && [ "$EXUNTIL" -gt "$NOW" ] 2>/dev/null; then
  EX_REMAIN_H=$(( (EXUNTIL - NOW) / 3600 ))
  if [ "$EX_REMAIN_H" -lt 48 ]; then
    MSGS="${MSGS:+$MSGS
}【fable例外まもなく失効】fable 例外（${EXEMPT}）が残り約 ${EX_REMAIN_H} 時間で失効し、以降 fable-advisor は deny されます。Fable の課金条件（サブスク内か従量か）を確認のうえ、継続するなら model_policy.sh exempt 14 で延長すること。"
  fi
fi

[ -n "$MSGS" ] && \
  jq -n --arg msg "$MSGS" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$msg}}' 2>/dev/null

exit 0
