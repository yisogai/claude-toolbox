#!/usr/bin/env bash
# model_policy_workflow_hook.sh — PreToolUse hook（matcher: "Workflow"）。層1b（ガードレール）。
#
# 目的:
#   Workflow / ultracode の script 内 agent() 呼び出しは、Agent hook（層1）を通らない前提
#（不明のため安全側に倒す）。そこで Workflow ツールの tool_input.script を「静的に文字列検査」し、
#   サブエージェントが fable を継承/指定する2パターンだけを deny する:
#     (1) model キーの引用符/バックティック付き値に fable が入る（例 model: "fable" / "model":"claude-fable-5"）→ deny
#     (2) script に agent( はあるのに model の語が一度も無い → deny（既定で fable 継承になるため）
#   agent( を含まない script は素通し。
#
# 設計上の判断:
#   - 部分パース（agent() 個別の model 有無判定）は誤検知源になるため行わない。
#     agent( の有無は bash の case（*"agent("* 等）で判定する。一部の agent() だけ model 未指定
#     というケースは取りこぼす=既知の限界（層2 CLAUDE.md 規律で補完。SKILL.md に明記）。
#   - 検査(1)は model キーの「引用符/バックティック付き値」だけに絞って fable を照合する
#     （grep -Ei、BSD 互換 ERE）。リポ名 fable-cost-manager・env FABLE_COST_MANAGER_ROOT・
#     prompt 文字列中の fable には反応しない。model 値が未クォート（変数）の場合は照合外＝素通し。
#   - フェイルオープン: script が取れない / jq 不在 / grep 不在・不一致 / off・relaxed のときは素通し（exit 0）。
#   - 意図的な deny の JSON 出力以外は必ず exit 0。

INPUT="$(cat)"

# --- 0. ハートビート（last-workflow-hook。層1 と別ファイルに刻む）----------------
mkdir -p "$HOME/.claude/model-policy" 2>/dev/null
date +%s > "$HOME/.claude/model-policy/last-workflow-hook" 2>/dev/null

# --- debug フラグ時は raw INPUT を workflow-debug.log に追記 ---------------------
[ -f "$HOME/.claude/model-policy/debug" ] && \
  printf '%s\n' "$INPUT" >> "$HOME/.claude/model-policy/workflow-debug.log" 2>/dev/null

command -v jq >/dev/null 2>&1 || exit 0

emit_deny() {
  local reason="$1" out
  out="$(jq -cn --arg r "$reason" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}' 2>/dev/null)" || exit 0
  [ -z "$out" ] && exit 0
  printf '%s\n' "$out"
  exit 0
}

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

# off / relaxed なら素通し
[ "$STATE" = "off" ] && exit 0
[ "$STATE" = "relaxed" ] && exit 0

# --- script の取得（無ければ scriptPath のファイルを読む）----------------------
SCRIPT="$(printf '%s' "$INPUT" | jq -r '.tool_input.script // empty' 2>/dev/null)"
if [ -z "$SCRIPT" ]; then
  SPATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.scriptPath // empty' 2>/dev/null)"
  if [ -n "$SPATH" ] && [ -f "$SPATH" ]; then
    SCRIPT="$(cat "$SPATH" 2>/dev/null)"
  fi
fi
# script が取れなければ素通し（フェイルオープン）
[ -z "$SCRIPT" ] && exit 0

# --- agent( を含む場合のみ検査 -------------------------------------------------
case "$SCRIPT" in
  *"agent("*)
    # (1) model キーの引用符/バックティック付き値に fable → deny
    #     concept: /model["'`]?\s*[:=]\s*["'`][^"'`]*fable/i
    #     BSD grep 互換の ERE（\s は使わず [[:space:]]）。fable-cost-manager /
    #     FABLE_COST_MANAGER_ROOT / prompt 文字列中の fable には反応しない。
    FABLE_MODEL_RE="model[\"'\`]?[[:space:]]*[:=][[:space:]]*[\"'\`][^\"'\`]*fable"
    if printf '%s' "$SCRIPT" | grep -Eiq "$FABLE_MODEL_RE"; then
      emit_deny 'モデルポリシー: Workflow script の agent() で model 値に fable が指定されています。サブエージェントへの fable 割り当ては禁止です。全 agent() の opts.model を "opus"（探索段は "sonnet" 可）に修正して再実行してください。'
    fi
    # (2) model の語が一度も無い → deny
    case "$SCRIPT" in
      *model*) : ;;  # model の語がどこかにある → 取りこぼし（部分パースはしない）。素通し。
      *)
        emit_deny 'モデルポリシー: この Workflow script の agent() 呼び出しに model 指定がありません。Workflow のサブエージェントは既定でメインループのモデル（fable）を継承するため、全 agent() 呼び出しに opts.model を明示して再実行してください（既定 "opus"、探索・finder 段は "sonnet" 可）。'
        ;;
    esac
    ;;
esac

exit 0
