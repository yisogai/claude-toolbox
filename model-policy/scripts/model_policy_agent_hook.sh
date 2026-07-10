#!/usr/bin/env bash
# model_policy_agent_hook.sh — PreToolUse hook（matcher: "Agent|Task"）。層1（強制）。
#
# 目的:
#   サブエージェント起動ツール（Agent / エイリアス Task）の tool_input を検査し、
#   「メインループ専任モデル fable がサブエージェントに割り当てられる／継承される」事故を
#   ハーネスレベルで防ぐ。強制は 3 点に絞る:
#     (a) fork サブエージェント（親=fable を必ず継承）を deny
#     (b) model に fable を含む指定を deny（on_fable=rewrite なら opus へ書き換え）
#     (c) model 未指定 / inherit を updatedInput で default_model（opus）へ書き換え
#   opus/sonnet/haiku など fable 以外は素通し（安価モデルの明示指定を壊さないため）。
#   opus 既定・sonnet 併用方針は CLAUDE.md の行動規範層（層2）で担保し、hook では
#   fable 排除に必要な最小限だけをハード強制する。
#
# 設計上の厳守事項（この環境の慣習）:
#   - stdin を INPUT="$(cat)" で受け、jq で抽出。jq 不在なら素通し（exit 0）。
#   - 意図的な deny の JSON 出力以外、あらゆる経路で exit 0（セッションを壊すより通す=フェイルオープン）。
#   - 素通しは「無出力 exit 0」。allow の JSON は原則出さない（他の permission 判定を握り潰さないため。
#     ただし model 書き換えが必要なときだけ permissionDecision:"allow"+updatedInput を出す）。
#   - ポリシー読み取りロジックは各 hook にインライン展開する（共有 lib は作らない。
#     sibling 欠損で強制が黙って死ぬ経路を排除するため）。
#   - 壊れた JSON・想定外値は sanitize して内蔵デフォルト（enforce/opus/deny/deny_fork）へ倒す。
#
# 検知の仕組み（§11 ハートビート）:
#   このスクリプトは「ツール名改称・配線切れ」等で黙って発火しなくなる方向に壊れうる。
#   そのため発火のたびに last-agent-hook へ epoch 秒を刻み、/model-policy status で
#   「サブエージェントを使ったのに発火が古い/無い」を検知できるようにする。

INPUT="$(cat)"

# --- 0. ハートビート（最優先・状態判定より前。失敗しても続行）------------------
mkdir -p "$HOME/.claude/model-policy" 2>/dev/null
date +%s > "$HOME/.claude/model-policy/last-agent-hook" 2>/dev/null

# --- 1. debug フラグがあれば raw INPUT を記録（tool_input の実キー名確認用）------
[ -f "$HOME/.claude/model-policy/debug" ] && \
  printf '%s\n' "$INPUT" >> "$HOME/.claude/model-policy/agent-debug.log" 2>/dev/null

# jq 不在なら素通し（セッションを壊すより通す）
command -v jq >/dev/null 2>&1 || exit 0

# --- deny / rewrite の出力ヘルパ（このファイル内でのみ使用。共有 lib ではない）----
emit_deny() {
  # 理由付き拒否。理由はモデルに返り、モデルが自己修正して再試行できる。
  # jq が失敗したら壊れた出力を出さずに素通し（フェイルオープン）。
  local reason="$1" out
  out="$(jq -cn --arg r "$reason" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}' 2>/dev/null)" || exit 0
  [ -z "$out" ] && exit 0
  printf '%s\n' "$out"
  exit 0
}
emit_rewrite() {
  # tool_input 全体を取り、model キーだけ DEFMODEL へ差し替えて updatedInput で返す
  #（updatedInput は完全置換だが、tool_input 全体を返すので prompt 等の他フィールドを失わない）。
  # jq が途中で失敗したら壊れた updatedInput を出さずに無出力 exit 0。
  local new_input out
  new_input="$(printf '%s' "$INPUT" | jq -c --arg m "$DEFMODEL" '.tool_input | .model = $m' 2>/dev/null)" || exit 0
  [ -z "$new_input" ] && exit 0
  out="$(jq -cn --argjson ui "$new_input" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",updatedInput:$ui}}' 2>/dev/null)" || exit 0
  [ -z "$out" ] && exit 0
  printf '%s\n' "$out"
  exit 0
}

# --- ポリシー読み取り（インライン展開）----------------------------------------
# 内蔵デフォルト（ファイル欠損・破損・想定外値のときの安全側の値）
MODE="enforce"; DEFMODEL="opus"; ALLOWED="opus sonnet haiku"; ON_FABLE="deny"; DENY_FORK="true"; RUNTIL="0"

# 解決順: $CWD/.claude/model-policy.json → $HOME/.claude/model-policy/policy.json → 内蔵デフォルト。
# ファイル間マージはしない＝最初に見つかった 1 ファイルだけを読む（フィールドマージは jq のデフォルトで行う）。
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)"
POLICY_FILE=""
if [ -n "$CWD" ] && [ -f "${CWD}/.claude/model-policy.json" ]; then
  POLICY_FILE="${CWD}/.claude/model-policy.json"
elif [ -f "$HOME/.claude/model-policy/policy.json" ]; then
  POLICY_FILE="$HOME/.claude/model-policy/policy.json"
fi

if [ -n "$POLICY_FILE" ]; then
  # 1 回の jq で全フィールドをタブ区切りで抽出。
  # deny_fork は false が正当値なので `// true` は使えない（jq の // は false も空扱い）。
  #   → `if .deny_fork == null then true else .deny_fork end` で欠損/null のときだけ true。
  # 壊れた JSON なら jq が失敗 → PARSED が空 → 内蔵デフォルトを維持（安全側）。
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
    # sanitize（想定外値は内蔵デフォルトへ）
    case "$p_mode"    in enforce|off) MODE="$p_mode";; esac
    [ -n "$p_defmodel" ] && DEFMODEL="$p_defmodel"
    [ -n "$p_allowed"  ] && ALLOWED="$p_allowed"
    case "$p_onfable" in deny|rewrite) ON_FABLE="$p_onfable";; esac
    case "$p_denyfork" in true|false)  DENY_FORK="$p_denyfork";; esac
    case "$p_runtil"  in ''|*[!0-9]*) RUNTIL=0;; *) RUNTIL="$p_runtil";; esac
  fi
fi

# --- 状態判定: off / relaxed / enforce ----------------------------------------
NOW="$(date +%s)"
if [ "$MODE" = "off" ]; then
  STATE="off"
elif [ "$RUNTIL" -gt "$NOW" ] 2>/dev/null; then
  STATE="relaxed"
else
  STATE="enforce"
fi

# --- 2. off / relaxed なら何もしない（強制解除）--------------------------------
[ "$STATE" = "off" ] && exit 0
[ "$STATE" = "relaxed" ] && exit 0

# --- 3. MODEL / SUBTYPE 抽出 ---------------------------------------------------
# キー名は 2026-07-07 に実測確認済み（Claude Code の PreToolUse stdin を debug ログで採取）:
#   tool_name="Agent"、tool_input のキーは model / subagent_type / prompt / description。
#   model 未指定時はキー自体が存在しない（.model // "" で空文字になる）。
# キー名が変わった場合（→強制が黙って無効化）は README の互換性リスク節の手順で再確認すること。
MODEL="$(printf '%s' "$INPUT" | jq -r '.tool_input | (.model // "")' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
SUBTYPE="$(printf '%s' "$INPUT" | jq -r '.tool_input | (.subagent_type // "")' 2>/dev/null | tr '[:upper:]' '[:lower:]')"

# --- 4. fork かつ deny_fork → deny --------------------------------------------
if [ "$SUBTYPE" = "fork" ] && [ "$DENY_FORK" = "true" ]; then
  emit_deny 'モデルポリシー: fork サブエージェントは親（メインループ=fable）のモデルを継承するため禁止です。subagent_type を明示した通常の Agent 呼び出しに model:"opus"（既定）または "sonnet"（調査等の定型作業）を付けて実行してください。fork がどうしても必要な場合は、ユーザーに /model-policy relax の実行を依頼してください。'
fi

# --- 5. MODEL に fable を含む → on_fable に従う（deny / rewrite）---------------
case "$MODEL" in
  *fable*)
    if [ "$ON_FABLE" = "rewrite" ]; then
      emit_rewrite
    else
      emit_deny 'モデルポリシー違反: サブエージェントへの fable 割り当ては禁止です（fable はメインループの統括専任）。model:"opus"（既定）または "sonnet"（定型作業）を指定して Agent を再実行してください。一時的に fable サブエージェントが必要な場合は、ユーザーに /model-policy relax の実行を依頼してください。'
    fi
    ;;
esac

# --- 6. MODEL が allowed のいずれかを部分一致で含む → 素通し --------------------
# 例: "claude-opus-4-8" は "opus" を含むので通す。空文字はここでは一致しない。
for a in $ALLOWED; do
  case "$MODEL" in
    *"$a"*) exit 0 ;;
  esac
done

# --- 7. MODEL が空 / inherit → updatedInput で default_model へ書き換え --------
if [ -z "$MODEL" ] || [ "$MODEL" = "inherit" ]; then
  emit_rewrite
fi

# --- 8. それ以外（fable でない未知値）→ 素通し（フェイルオープン）--------------
exit 0
