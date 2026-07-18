#!/usr/bin/env bash
# model_policy.sh — 層3。モデルポリシーの運用 CLI（/model-policy スキルから Bash 経由で呼ばれる）。
#
# サブコマンド:
#   status（既定）  実効状態（enforce/relaxed/off）・残り分と復帰時刻・どのファイルが効いているか・
#                   各設定値・ハートビート（hook の最終発火）を人間可読の日本語で表示。
#   relax [分]      既定60分・1〜1440にクランプ。relaxed_until = now+分（TTL 失効で自動 enforce 復帰）。
#   reset           relaxed_until=null（即 enforce 復帰）。
#   off             mode="off"（キルスイッチ）。
#   enforce         mode="enforce" かつ relaxed_until=null。
#   exempt [日数]   fable 例外（fable_exempt_subagent_types）の有効期限を now+日数 に設定
#                   （既定14・1〜90にクランプ）。exempt off で期限を即解除（例外は無効化）。
#                   例外リスト自体の編集は policy.json を直接編集する（README §7-4）。
#   --project <sub> 対象を cwd の ./.claude/model-policy.json に切替（タスク/プロジェクト単位スコープ）。
#
# 設計上の厳守事項:
#   - 終了コードは常に 0（エラー時もメッセージを出して 0。スキル経由で呼ばれるため）。
#   - 編集は jq + mktemp→mv のアトミック書き込み。ファイルが無い/壊れているなら内蔵デフォルトを生成。
#   - relax の時刻計算は BSD date（-v +${MIN}M）。GNU date しか無い環境では -d フォールバック。

command -v jq >/dev/null 2>&1 || { echo "jq が見つかりません。model-policy CLI には jq が必要です。"; exit 0; }

# --- 対象ファイルの解決（--project で cwd のプロジェクトファイルへ切替）----------
POLICY_FILE="$HOME/.claude/model-policy/policy.json"
SCOPE_LABEL="ユーザー ($POLICY_FILE)"
if [ "${1:-}" = "--project" ]; then
  POLICY_FILE="./.claude/model-policy.json"
  SCOPE_LABEL="プロジェクト ($POLICY_FILE)"
  shift
fi
SUBCMD="${1:-status}"
ARG="${2:-}"

# --- 内蔵デフォルト JSON（ポリシースキーマ全文）---------------------------------
default_json() {
  cat <<'EOF'
{
  "mode": "enforce",
  "default_model": "opus",
  "allowed": ["opus", "sonnet", "haiku"],
  "on_fable": "deny",
  "deny_fork": true,
  "relaxed_until": null,
  "fable_exempt_subagent_types": [],
  "fable_exempt_until": null
}
EOF
}

# ファイルが無い/壊れているなら内蔵デフォルトで作り直す
ensure_policy() {
  if [ ! -f "$POLICY_FILE" ] || ! jq . "$POLICY_FILE" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$POLICY_FILE")" 2>/dev/null
    default_json > "$POLICY_FILE" 2>/dev/null
  fi
}

# jq でアトミック編集（mktemp→mv）
apply_jq() {
  local filter="$1"; shift
  ensure_policy
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/model-policy.XXXXXX" 2>/dev/null)" || { echo "一時ファイルの作成に失敗しました。"; return 0; }
  if jq "$@" "$filter" "$POLICY_FILE" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$POLICY_FILE" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
    echo "ポリシーファイルの編集に失敗しました: $POLICY_FILE"
  fi
}

# --- ポリシー読み取り（hook と同一の sanitize ロジック。CLI 内の関数は共有 lib ではない）
read_policy_from() {
  # $1 = 読み取るファイル（空文字なら内蔵デフォルトのまま返す）
  MODE="enforce"; DEFMODEL="opus"; ALLOWED="opus sonnet haiku"; ON_FABLE="deny"; DENY_FORK="true"; RUNTIL="0"; EXEMPT=""; EXUNTIL="0"
  local f="$1" parsed
  [ -z "$f" ] && return
  # 1 行 1 フィールドで抽出して行単位で読む（@tsv は空の中間フィールドを
  # IFS=$'\t' read が畳み込んでしまい、exempt リストが空のときずれるため）。
  parsed="$(jq -r '
    (.mode // "enforce"),
    (.default_model // "opus"),
    ((.allowed // ["opus","sonnet","haiku"]) | join(" ")),
    (.on_fable // "deny"),
    (if .deny_fork == null then true else .deny_fork end | tostring),
    (.relaxed_until // 0),
    ((.fable_exempt_subagent_types // []) | join(" ")),
    (.fable_exempt_until // 0)' "$f" 2>/dev/null)"
  [ -z "$parsed" ] && return
  {
    IFS= read -r m; IFS= read -r dm; IFS= read -r al; IFS= read -r onf
    IFS= read -r df; IFS= read -r ru; IFS= read -r ex; IFS= read -r exu
  } <<EOF
$parsed
EOF
  case "$m"   in enforce|off) MODE="$m";; esac
  [ -n "$dm" ] && DEFMODEL="$dm"
  [ -n "$al" ] && ALLOWED="$al"
  case "$onf" in deny|rewrite) ON_FABLE="$onf";; esac
  case "$df"  in true|false)   DENY_FORK="$df";; esac
  case "$ru"  in ''|*[!0-9]*) RUNTIL=0;; *) RUNTIL="$ru";; esac
  EXEMPT="$(printf '%s' "$ex" | tr '[:upper:]' '[:lower:]')"
  case "$exu" in ''|*[!0-9]*) EXUNTIL=0;; *) EXUNTIL="$exu";; esac
}

# ハートビート表示ヘルパ（「N分前」/「未発火」）
heartbeat_str() {
  local f="$1" ts now diff
  if [ -f "$f" ]; then
    ts="$(cat "$f" 2>/dev/null)"
    case "$ts" in ''|*[!0-9]*) echo "未発火（不正な値）"; return;; esac
    now="$(date +%s)"
    diff=$(( (now - ts) / 60 ))
    echo "${diff}分前"
  else
    echo "未発火"
  fi
}

# 未来 epoch を BSD/GNU 両対応で計算
future_epoch() {
  local min="$1" out
  out="$(date -v "+${min}M" +%s 2>/dev/null)"
  [ -z "$out" ] && out="$(date -d "+${min} minutes" +%s 2>/dev/null)"
  printf '%s' "$out"
}

# --- status 表示 ---------------------------------------------------------------
show_status() {
  # 有効ポリシーの解決（hook と同じ順序: プロジェクト → ユーザー → 内蔵デフォルト）
  local eff_file eff_src
  if [ -f "./.claude/model-policy.json" ]; then
    eff_file="./.claude/model-policy.json"; eff_src="プロジェクト (./.claude/model-policy.json)"
  elif [ -f "$HOME/.claude/model-policy/policy.json" ]; then
    eff_file="$HOME/.claude/model-policy/policy.json"; eff_src="ユーザー ($HOME/.claude/model-policy/policy.json)"
  else
    eff_file=""; eff_src="内蔵デフォルト（ポリシーファイルなし）"
  fi
  read_policy_from "$eff_file"

  local now state remain until_h
  now="$(date +%s)"
  if [ "$MODE" = "off" ]; then
    state="off（キルスイッチ ON・全サブエージェント素通し）"
  elif [ "$RUNTIL" -gt "$now" ] 2>/dev/null; then
    remain=$(( (RUNTIL - now + 59) / 60 ))
    until_h="$(date -r "$RUNTIL" '+%H:%M' 2>/dev/null)"
    state="relaxed（緩和中・残り約 ${remain} 分、${until_h} まで）"
  else
    state="enforce（強制中）"
  fi

  echo "=== モデルポリシー状態 ==="
  echo "実効状態      : ${state}"
  echo "有効ファイル  : ${eff_src}"
  echo "--- 設定値 ---"
  echo "mode          : ${MODE}"
  echo "default_model : ${DEFMODEL}"
  echo "allowed       : ${ALLOWED}"
  echo "on_fable      : ${ON_FABLE}"
  echo "deny_fork     : ${DENY_FORK}"
  echo "relaxed_until : ${RUNTIL}（0=緩和なし）"
  # fable 例外（fable_exempt_subagent_types + fable_exempt_until）の可視化。
  # 戻し忘れ/期限切れの検知手段として、残り日数 or 期限切れを明示する。
  if [ -n "$EXEMPT" ]; then
    if [ "$EXUNTIL" -gt "$now" ] 2>/dev/null; then
      ex_days=$(( (EXUNTIL - now + 86399) / 86400 ))
      ex_until_h="$(date -r "$EXUNTIL" '+%m/%d %H:%M' 2>/dev/null)"
      echo "fable例外     : ${EXEMPT}（有効・残り約 ${ex_days} 日、${ex_until_h} まで）"
    else
      echo "fable例外     : ${EXEMPT}（期限切れ→deny 動作。延長は exempt [日数]）"
    fi
  else
    echo "fable例外     : （なし）"
  fi
  echo "--- ハートビート（hook 最終発火）---"
  echo "agent hook    : $(heartbeat_str "$HOME/.claude/model-policy/last-agent-hook")"
  echo "workflow hook : $(heartbeat_str "$HOME/.claude/model-policy/last-workflow-hook")"
  echo "※ サブエージェント（Agent ツール）を使った直後なのに agent hook が「未発火」や"
  echo "  極端に古い場合、hook が配線切れ/ツール名改称等で機能していない可能性があります"
  echo "  （その場合サブエージェントが fable 継承で起動しうる）。README の検知手段を参照。"
}

# --- サブコマンド分岐 ----------------------------------------------------------
case "$SUBCMD" in
  status|"")
    show_status
    ;;
  relax)
    MIN="$ARG"
    case "$MIN" in ''|*[!0-9]*) MIN=60;; esac
    [ "$MIN" -lt 1 ]    && MIN=1
    [ "$MIN" -gt 1440 ] && MIN=1440
    UNTIL="$(future_epoch "$MIN")"
    case "$UNTIL" in
      ''|*[!0-9]*) echo "時刻計算に失敗しました（date コマンドの互換性問題の可能性）。" ;;
      *)
        apply_jq '.relaxed_until = $t' --argjson t "$UNTIL"
        echo "モデルポリシーを ${MIN} 分間緩和しました（対象: ${SCOPE_LABEL}）。"
        echo
        show_status
        ;;
    esac
    ;;
  reset)
    apply_jq '.relaxed_until = null'
    echo "緩和を解除しました（即時 enforce 復帰。対象: ${SCOPE_LABEL}）。"
    echo
    show_status
    ;;
  off)
    apply_jq '.mode = "off"'
    echo "モデルポリシーを無効化しました（キルスイッチ ON。対象: ${SCOPE_LABEL}）。"
    echo
    show_status
    ;;
  enforce)
    apply_jq '.mode = "enforce" | .relaxed_until = null'
    echo "モデルポリシーを enforce に設定しました（対象: ${SCOPE_LABEL}）。"
    echo
    show_status
    ;;
  exempt)
    if [ "$ARG" = "off" ]; then
      apply_jq '.fable_exempt_until = null'
      echo "fable 例外の有効期限を解除しました（登録済み subagent_type も即 deny 動作へ。対象: ${SCOPE_LABEL}）。"
    else
      DAYS="$ARG"
      case "$DAYS" in ''|*[!0-9]*) DAYS=14;; esac
      [ "$DAYS" -lt 1 ]  && DAYS=1
      [ "$DAYS" -gt 90 ] && DAYS=90
      UNTIL="$(future_epoch $(( DAYS * 1440 )))"
      case "$UNTIL" in
        ''|*[!0-9]*) echo "時刻計算に失敗しました（date コマンドの互換性問題の可能性）。" ;;
        *)
          apply_jq '.fable_exempt_until = $t' --argjson t "$UNTIL"
          echo "fable 例外の有効期限を ${DAYS} 日後に設定しました（対象: ${SCOPE_LABEL}）。"
          # リストが空だと期限だけあっても例外は成立しない。気づけるように注意を出す。
          n="$(jq -r '(.fable_exempt_subagent_types // []) | length' "$POLICY_FILE" 2>/dev/null)"
          case "$n" in ''|0) echo "注意: fable_exempt_subagent_types が空です。例外を有効にするには policy.json にサブエージェント名（例: \"fable-advisor\"）を登録してください（README §7-4）。";; esac
          ;;
      esac
    fi
    echo
    show_status
    ;;
  *)
    echo "不明なサブコマンド: ${SUBCMD}"
    echo "使い方: model_policy.sh [--project] {status|relax [分]|reset|off|enforce|exempt [日数]|exempt off}"
    echo
    show_status
    ;;
esac

exit 0
