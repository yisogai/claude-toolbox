#!/usr/bin/env bash
# install.sh — ハーネス v1（opus-fable harness）を ~/.claude へ日常利用向けに配備する。
#
# 使い方:
#   install.sh [--dry-run|--apply] [--switch-model] [-h|--help]
#
#   --dry-run       既定。実際には何も変更せず、変更差分のプレビューだけを表示する。
#   --apply         実際に適用する。適用前に対象ファイルを
#                   $HOME/.claude/backup-opus-fable-harness-<timestamp>/ へバックアップする。
#   --switch-model  $HOME/.claude/settings.json の "model" を claude-opus-4-8[1m] に変更する。
#                   明示しない限り model フィールドには一切触れない
#                   （Fable トライアル終了タイミングはユーザーが決めるため）。
#
# 配備内容:
#   - ~/.claude/agents/{verifier,implementer,fable-advisor}.md ← agents/ からコピー
#   - ~/.claude/workflows/{implement-verified,deep-review}.js ← workflows/ からコピー
#   - ~/.claude/settings.json の hooks に3エントリを追加（PostToolUse x2, Stop, UserPromptSubmit。
#     command はこのリポジトリ内の hooks/*.sh への絶対パス。コピーではなく参照）
#   - ~/.claude/CLAUDE.md に「## 作業プロトコル」節を追加する（既存があれば置換、
#     無ければ末尾に追加。lib/merge_claude_md.py が担当）
#   - ~/.claude/completion-gate/state.json が無ければ {"mode":"enforce"} を作成（キルスイッチ初期化）
#
# 対象ホームは $HOME 環境変数から解決する（実 HOME を書き換えずに検証したい場合は
# HOME=<偽ホーム> install.sh --apply のように env 経由で渡す）。
#
# 必須コマンド: bash, jq, python3。無ければ明確なエラーを出して exit 1 する。
set -u

# ─── 0. 環境チェック ──────────────────────────────────────────────────────────
die() {
  echo "エラー: $*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || die "jq が必要です（brew install jq 等でインストールしてください）。"
command -v python3 >/dev/null 2>&1 || die "python3 が必要です（CLAUDE.md の節置換に使用します）。"

# ─── 1. 引数解析 ──────────────────────────────────────────────────────────────
MODE="dry-run"
SWITCH_MODEL=0

usage() {
  cat <<'EOF'
使い方: install.sh [--dry-run|--apply] [--switch-model] [-h|--help]

  --dry-run       既定。変更差分のプレビューのみ（実変更なし）。
  --apply         実際に ~/.claude へ適用する（適用前にバックアップを取る）。
  --switch-model  settings.json の model を claude-opus-4-8[1m] に変更する
                   （--apply と併用したときのみ実際に書き込まれる）。
  -h, --help      このヘルプを表示する。
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --apply) MODE="apply" ;;
    --switch-model) SWITCH_MODEL=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "エラー: 不明なオプション: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# ─── 2. パス解決 ──────────────────────────────────────────────────────────────
# このスクリプト自身のディレクトリ = <repo>/harness。REPO_ROOT はその親。
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$SCRIPT_DIR" ] || die "リポジトリのパスを特定できませんでした。"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)"
[ -n "$REPO_ROOT" ] || die "リポジトリルートを特定できませんでした。"

HOOKS_DIR="$SCRIPT_DIR/hooks"
AGENTS_SRC_DIR="$SCRIPT_DIR/agents"
WORKFLOWS_SRC_DIR="$SCRIPT_DIR/workflows"
CLAUDE_MD_SRC_DIR="$SCRIPT_DIR/claude-md"
MERGE_PY="$SCRIPT_DIR/lib/merge_claude_md.py"

VERIFY_LEDGER_HOOK="$HOOKS_DIR/verify_ledger_posttooluse.sh"
COMPLETION_GATE_HOOK="$HOOKS_DIR/completion_gate_stop.sh"
WORKFLOW_NUDGE_HOOK="$HOOKS_DIR/workflow_nudge_prompt.sh"

# 配備元ファイルの存在確認（リポジトリの部分チェックアウト等を検知）
for f in \
  "$VERIFY_LEDGER_HOOK" "$COMPLETION_GATE_HOOK" "$WORKFLOW_NUDGE_HOOK" \
  "$AGENTS_SRC_DIR/verifier.md" "$AGENTS_SRC_DIR/implementer.md" \
  "$WORKFLOWS_SRC_DIR/implement-verified.js" "$WORKFLOWS_SRC_DIR/deep-review.js" \
  "$CLAUDE_MD_SRC_DIR/opus-fable-protocol.md" \
  "$MERGE_PY"
do
  [ -f "$f" ] || die "配備元ファイルが見つかりません: $f （リポジトリが不完全な可能性があります）"
done

# 対象（$HOME はこのプロセスの環境変数。実 HOME を汚さず検証したい場合は
# HOME=<偽ホーム> を付けて呼び出すこと）
CLAUDE_DIR="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
CLAUDE_MD_FILE="$CLAUDE_DIR/CLAUDE.md"
AGENTS_DEST_DIR="$CLAUDE_DIR/agents"
WORKFLOWS_DEST_DIR="$CLAUDE_DIR/workflows"
GATE_DIR="$CLAUDE_DIR/completion-gate"
GATE_STATE_FILE="$GATE_DIR/state.json"

[ -f "$SETTINGS_FILE" ] || die "対象ファイルが見つかりません: $SETTINGS_FILE （先に ~/.claude/settings.json を用意してください）"
[ -f "$CLAUDE_MD_FILE" ] || die "対象ファイルが見つかりません: $CLAUDE_MD_FILE （先に ~/.claude/CLAUDE.md を用意してください）"
jq empty "$SETTINGS_FILE" >/dev/null 2>&1 || die "$SETTINGS_FILE が valid JSON ではありません。"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$CLAUDE_DIR/backup-opus-fable-harness-$TIMESTAMP"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness-install.XXXXXX")" || die "一時ディレクトリを作成できませんでした。"
cleanup() { rm -rf "$STAGE_DIR"; }
trap cleanup EXIT

echo "== harness install.sh (mode=$MODE, switch-model=$SWITCH_MODEL) =="
echo "対象: HOME=$HOME"
echo "リポジトリ: $REPO_ROOT"
echo

# ─── 3. ステージング（新しい内容を STAGE_DIR に作る。まだどこにも適用しない） ─────

# 3a. agents / workflows（単純コピー）
mkdir -p "$STAGE_DIR/agents" "$STAGE_DIR/workflows"
cp "$AGENTS_SRC_DIR/verifier.md" "$STAGE_DIR/agents/verifier.md" || die "ステージングに失敗: agents/verifier.md"
cp "$AGENTS_SRC_DIR/implementer.md" "$STAGE_DIR/agents/implementer.md" || die "ステージングに失敗: agents/implementer.md"
cp "$AGENTS_SRC_DIR/fable-advisor.md" "$STAGE_DIR/agents/fable-advisor.md" || die "ステージングに失敗: agents/fable-advisor.md"
cp "$WORKFLOWS_SRC_DIR/implement-verified.js" "$STAGE_DIR/workflows/implement-verified.js" || die "ステージングに失敗: workflows/implement-verified.js"
cp "$WORKFLOWS_SRC_DIR/deep-review.js" "$STAGE_DIR/workflows/deep-review.js" || die "ステージングに失敗: workflows/deep-review.js"

# 3b. settings.json（jq で hooks を差分マージ。冪等: 同一 command が既にあれば追加しない）
ENTRIES_JSON="$(jq -n \
  --arg vl "$VERIFY_LEDGER_HOOK" \
  --arg cg "$COMPLETION_GATE_HOOK" \
  --arg wn "$WORKFLOW_NUDGE_HOOK" \
  '[
    {event:"PostToolUse", matcher:"Bash", command:$vl},
    {event:"PostToolUse", matcher:"Edit|Write", command:$vl},
    {event:"Stop", matcher:"", command:$cg},
    {event:"UserPromptSubmit", matcher:"", command:$wn}
  ]'
)"
[ -n "$ENTRIES_JSON" ] || die "hooks エントリの構築に失敗しました（jq）。"

# 冪等判定は (matcher, command) の組で行う。PostToolUse の Bash / Edit|Write は
# どちらも同じ verify_ledger_posttooluse.sh を command に持つため、command だけで
# 判定すると2つ目が誤って「重複」扱いされ追加されなくなる（実測で確認済み）。
NEW_SETTINGS="$(jq --argjson entries "$ENTRIES_JSON" '
  reduce $entries[] as $e (.;
    (.hooks[$e.event] // []) as $arr
    | ( $arr | map(select(.matcher == $e.matcher)) | map(.hooks[]?.command) | any(. == $e.command) ) as $exists
    | if $exists then .
      else .hooks[$e.event] = ($arr + [{matcher: $e.matcher, hooks: [{type:"command", command: $e.command}]}])
      end
  )
' "$SETTINGS_FILE")"
[ -n "$NEW_SETTINGS" ] || die "settings.json のマージに失敗しました（jq）。"

if [ "$SWITCH_MODEL" -eq 1 ]; then
  NEW_SETTINGS="$(printf '%s' "$NEW_SETTINGS" | jq '.model = "claude-opus-4-8[1m]"')"
  [ -n "$NEW_SETTINGS" ] || die "model フィールドの書き換えに失敗しました（jq）。"
fi

printf '%s\n' "$NEW_SETTINGS" > "$STAGE_DIR/settings.json"
jq empty "$STAGE_DIR/settings.json" >/dev/null 2>&1 || die "生成した settings.json が valid JSON ではありません（内部エラー）。"

# 3c. CLAUDE.md（python3 で節置換・挿入。冪等）
NEW_CLAUDE_MD="$(python3 "$MERGE_PY" "$CLAUDE_MD_FILE" "$CLAUDE_MD_SRC_DIR/opus-fable-protocol.md")"
RC=$?
[ "$RC" -eq 0 ] && [ -n "$NEW_CLAUDE_MD" ] || die "CLAUDE.md のマージに失敗しました（merge_claude_md.py, rc=$RC）。"
# $() はコマンド置換で末尾の改行をすべて取り除くため、printf '%s\n' で1つ復元する
# （merge_claude_md.py の出力は常に末尾改行1つで正規化されている）。
printf '%s\n' "$NEW_CLAUDE_MD" > "$STAGE_DIR/CLAUDE.md"

# 3d. completion-gate/state.json（キルスイッチ初期化。既存があれば内容は変更しない）
if [ -f "$GATE_STATE_FILE" ]; then
  cp "$GATE_STATE_FILE" "$STAGE_DIR/gate-state.json"
  GATE_STATE_IS_NEW=0
else
  printf '%s\n' '{"mode": "enforce"}' > "$STAGE_DIR/gate-state.json"
  GATE_STATE_IS_NEW=1
fi

# ─── 4. 差分プレビュー（両モード共通で表示。dry-run はここで終了） ────────────────

diff_file() {
  # $1=表示名 $2=現在のファイル(無ければ /dev/null 相当) $3=ステージ後のファイル
  local label="$1" cur="$2" staged="$3"
  echo "--- $label ---"
  if [ ! -f "$cur" ]; then
    echo "(新規作成)"
    diff -u /dev/null "$staged" | tail -n +3
  else
    if diff -q "$cur" "$staged" >/dev/null 2>&1; then
      echo "(変更なし)"
    else
      diff -u "$cur" "$staged" | tail -n +3
    fi
  fi
  echo
}

echo "########## 変更差分プレビュー ##########"
echo
diff_file "agents/verifier.md" "$AGENTS_DEST_DIR/verifier.md" "$STAGE_DIR/agents/verifier.md"
diff_file "agents/implementer.md" "$AGENTS_DEST_DIR/implementer.md" "$STAGE_DIR/agents/implementer.md"
diff_file "agents/fable-advisor.md" "$AGENTS_DEST_DIR/fable-advisor.md" "$STAGE_DIR/agents/fable-advisor.md"
diff_file "workflows/implement-verified.js" "$WORKFLOWS_DEST_DIR/implement-verified.js" "$STAGE_DIR/workflows/implement-verified.js"
diff_file "workflows/deep-review.js" "$WORKFLOWS_DEST_DIR/deep-review.js" "$STAGE_DIR/workflows/deep-review.js"
diff_file "settings.json" "$SETTINGS_FILE" "$STAGE_DIR/settings.json"
diff_file "CLAUDE.md" "$CLAUDE_MD_FILE" "$STAGE_DIR/CLAUDE.md"

echo "--- completion-gate/state.json ---"
if [ "$GATE_STATE_IS_NEW" -eq 1 ]; then
  echo "(新規作成: {\"mode\": \"enforce\"})"
else
  echo "(既存あり。内容は変更しない: $(cat "$GATE_STATE_FILE" 2>/dev/null))"
fi
echo

echo "--- model フィールド ---"
if [ "$SWITCH_MODEL" -eq 1 ]; then
  echo "変更する: claude-opus-4-8[1m] へ切り替え"
else
  echo "変更しない（--switch-model 未指定）"
fi
echo

if [ "$MODE" = "dry-run" ]; then
  echo "########## dry-run のため、実際の変更は行っていません ##########"
  echo "適用するには: install.sh --apply"
  exit 0
fi

# ─── 5. 適用（--apply） ───────────────────────────────────────────────────────
echo "########## 適用します（--apply） ##########"
echo

mkdir -p "$BACKUP_DIR" || die "バックアップディレクトリを作成できませんでした: $BACKUP_DIR"

backup_if_exists() {
  # $1=対象ファイル $2=BACKUP_DIR からの相対パス
  local src="$1" rel="$2" dest
  [ -f "$src" ] || return 0
  dest="$BACKUP_DIR/$rel"
  mkdir -p "$(dirname "$dest")" || die "バックアップ先ディレクトリを作成できませんでした: $(dirname "$dest")"
  cp "$src" "$dest" || die "バックアップに失敗しました: $src -> $dest"
  echo "バックアップ: $src -> $dest"
}

backup_if_exists "$SETTINGS_FILE" "settings.json"
backup_if_exists "$CLAUDE_MD_FILE" "CLAUDE.md"
backup_if_exists "$AGENTS_DEST_DIR/verifier.md" "agents/verifier.md"
backup_if_exists "$AGENTS_DEST_DIR/implementer.md" "agents/implementer.md"
backup_if_exists "$AGENTS_DEST_DIR/fable-advisor.md" "agents/fable-advisor.md"
backup_if_exists "$WORKFLOWS_DEST_DIR/implement-verified.js" "workflows/implement-verified.js"
backup_if_exists "$WORKFLOWS_DEST_DIR/deep-review.js" "workflows/deep-review.js"
backup_if_exists "$GATE_STATE_FILE" "completion-gate/state.json"
echo

mkdir -p "$AGENTS_DEST_DIR" || die "作成できませんでした: $AGENTS_DEST_DIR"
mkdir -p "$WORKFLOWS_DEST_DIR" || die "作成できませんでした: $WORKFLOWS_DEST_DIR"
mkdir -p "$GATE_DIR" || die "作成できませんでした: $GATE_DIR"

cp "$STAGE_DIR/agents/verifier.md" "$AGENTS_DEST_DIR/verifier.md" || die "配備に失敗: agents/verifier.md"
cp "$STAGE_DIR/agents/implementer.md" "$AGENTS_DEST_DIR/implementer.md" || die "配備に失敗: agents/implementer.md"
cp "$STAGE_DIR/agents/fable-advisor.md" "$AGENTS_DEST_DIR/fable-advisor.md" || die "配備に失敗: agents/fable-advisor.md"
cp "$STAGE_DIR/workflows/implement-verified.js" "$WORKFLOWS_DEST_DIR/implement-verified.js" || die "配備に失敗: workflows/implement-verified.js"
cp "$STAGE_DIR/workflows/deep-review.js" "$WORKFLOWS_DEST_DIR/deep-review.js" || die "配備に失敗: workflows/deep-review.js"
echo "配備しました: agents/{verifier,implementer,fable-advisor}.md, workflows/{implement-verified,deep-review}.js"

# settings.json / CLAUDE.md はアトミックに置換（同一ディレクトリへ書いてから mv）
cp "$STAGE_DIR/settings.json" "$SETTINGS_FILE.tmp.$$" || die "settings.json の書き込みに失敗しました。"
mv "$SETTINGS_FILE.tmp.$$" "$SETTINGS_FILE" || die "settings.json の置換に失敗しました。"
echo "更新しました: $SETTINGS_FILE"

cp "$STAGE_DIR/CLAUDE.md" "$CLAUDE_MD_FILE.tmp.$$" || die "CLAUDE.md の書き込みに失敗しました。"
mv "$CLAUDE_MD_FILE.tmp.$$" "$CLAUDE_MD_FILE" || die "CLAUDE.md の置換に失敗しました。"
echo "更新しました: $CLAUDE_MD_FILE"

if [ "$GATE_STATE_IS_NEW" -eq 1 ]; then
  cp "$STAGE_DIR/gate-state.json" "$GATE_STATE_FILE" || die "キルスイッチ初期化に失敗しました: $GATE_STATE_FILE"
  echo "作成しました: $GATE_STATE_FILE ({\"mode\":\"enforce\"})"
else
  echo "既存のキルスイッチ状態を維持: $GATE_STATE_FILE"
fi

echo
echo "########## 適用完了 ##########"
echo "バックアップ: $BACKUP_DIR"
