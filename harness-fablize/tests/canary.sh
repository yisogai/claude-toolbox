#!/usr/bin/env bash
# canary.sh — harness/hooks/*.sh の単体カナリア（実モデル呼び出しなし）。
#
# 各テストは偽の HOME（mktemp -d）に env HOME=... で分離して実行し、
# 実際の ~/.claude には一切触れない。stdin に偽の hook JSON を食わせて
# stdout/exit code を検証するだけのユニットテスト形式。
#
# 使い方: bash harness/tests/canary.sh
# 全ケース green なら exit 0、1件でも fail があれば exit 1。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../hooks"
LEDGER_HOOK="$HOOKS_DIR/verify_ledger_posttooluse.sh"
GATE_HOOK="$HOOKS_DIR/completion_gate_stop.sh"
NUDGE_HOOK="$HOOKS_DIR/workflow_nudge_prompt.sh"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); printf '  PASS: %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL: %s\n' "$1"; }

# --- JSON ビルダー（jq -cn で組み立てる。手書きエスケープのバグを避ける）----------

json_edit() {
  # $1=session_id $2=cwd $3=file_path
  jq -cn --arg s "$1" --arg c "$2" --arg f "$3" \
    '{session_id:$s, cwd:$c, tool_name:"Edit",
      tool_input:{file_path:$f, old_string:"a", new_string:"b"},
      tool_response:{filePath:$f}}'
}

json_bash() {
  # $1=session_id $2=cwd $3=command
  jq -cn --arg s "$1" --arg c "$2" --arg cmd "$3" \
    '{session_id:$s, cwd:$c, tool_name:"Bash",
      tool_input:{command:$cmd},
      tool_response:{stdout:"ok", stderr:"", interrupted:false}}'
}

json_stop() {
  # $1=session_id $2=cwd $3=stop_hook_active(true/false)
  jq -cn --arg s "$1" --arg c "$2" --argjson a "$3" \
    '{session_id:$s, cwd:$c, hook_event_name:"Stop", stop_hook_active:$a}'
}

json_prompt() {
  # $1=session_id $2=cwd $3=prompt
  jq -cn --arg s "$1" --arg c "$2" --arg p "$3" \
    '{session_id:$s, cwd:$c, hook_event_name:"UserPromptSubmit", prompt:$p}'
}

# --- 実行ヘルパ ------------------------------------------------------------------

run_hook() {
  # $1=fake_home $2=script $3=json_stdin ; stdout と exit code をグローバルに残す
  local fake_home="$1" script="$2" input="$3"
  RUN_OUT="$(printf '%s' "$input" | HOME="$fake_home" bash "$script" 2>/tmp/canary_stderr.$$)"
  RUN_RC=$?
  rm -f "/tmp/canary_stderr.$$"
}

new_fake_home() {
  mktemp -d "${TMPDIR:-/tmp}/harness-canary-home.XXXXXX"
}

new_fake_proj() {
  mktemp -d "${TMPDIR:-/tmp}/harness-canary-proj.XXXXXX"
}

echo "== canary: verify_ledger_posttooluse.sh / completion_gate_stop.sh / workflow_nudge_prompt.sh =="

# ------------------------------------------------------------------------------
# ケース1: docs 編集のみ → gate が block しない
# ------------------------------------------------------------------------------
{
  HOME1="$(new_fake_home)"
  PROJ1="$(new_fake_proj)"
  SID="sess-docs-only"

  run_hook "$HOME1" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ1" "README.md")"
  if [ "$RUN_RC" -ne 0 ]; then
    fail "case1: ledger hook (Edit README.md) exit code = $RUN_RC (expected 0)"
  elif [ -f "$HOME1/.claude/completion-gate/code-edit-$SID" ]; then
    fail "case1: code-edit-$SID が作られてしまった（README.md は除外対象のはず）"
  else
    pass "case1: docs 編集では code-edit マーカーが作られない"
  fi

  run_hook "$HOME1" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ1" false)"
  if [ -n "$RUN_OUT" ]; then
    fail "case1: docs 編集のみなのに Stop hook が出力あり: $RUN_OUT"
  elif [ "$RUN_RC" -ne 0 ]; then
    fail "case1: Stop hook exit code = $RUN_RC (expected 0)"
  else
    pass "case1: docs 編集のみ → gate は block しない"
  fi
}

# ------------------------------------------------------------------------------
# ケース2: コード編集+検証なし → block（1回目・2回目）、3回目は素通し
# ------------------------------------------------------------------------------
{
  HOME2="$(new_fake_home)"
  PROJ2="$(new_fake_proj)"
  SID="sess-no-verify"

  run_hook "$HOME2" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ2" "app.py")"
  if [ ! -f "$HOME2/.claude/completion-gate/code-edit-$SID" ]; then
    fail "case2: code-edit-$SID が作られなかった（app.py はコード拡張子のはず）"
  else
    pass "case2: app.py 編集で code-edit マーカーが作られる"
  fi

  # 1回目: block されるはず
  run_hook "$HOME2" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ2" false)"
  DECISION1="$(printf '%s' "$RUN_OUT" | jq -r '.decision // empty' 2>/dev/null)"
  if [ "$RUN_RC" -eq 0 ] && [ "$DECISION1" = "block" ]; then
    pass "case2: 1回目は block される"
  else
    fail "case2: 1回目が block されなかった (rc=$RUN_RC out=$RUN_OUT)"
  fi

  # 2回目: まだ block されるはず
  run_hook "$HOME2" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ2" false)"
  DECISION2="$(printf '%s' "$RUN_OUT" | jq -r '.decision // empty' 2>/dev/null)"
  if [ "$RUN_RC" -eq 0 ] && [ "$DECISION2" = "block" ]; then
    pass "case2: 2回目も block される"
  else
    fail "case2: 2回目が block されなかった (rc=$RUN_RC out=$RUN_OUT)"
  fi

  # 3回目: 上限超過で素通しのはず
  run_hook "$HOME2" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ2" false)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case2: 3回目は上限超過で素通し（無出力）"
  else
    fail "case2: 3回目が素通しにならなかった (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース3: コード編集+検証あり → block しない
# ------------------------------------------------------------------------------
{
  HOME3="$(new_fake_home)"
  PROJ3="$(new_fake_proj)"
  SID="sess-verified"

  run_hook "$HOME3" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ3" "app.py")"
  # code-edit の mtime と台帳 ts が同秒だと「未検証」扱い（安全側）になり得るため、
  # テストの時間分解能起因の flaky を避けるために1秒空ける。
  sleep 1
  run_hook "$HOME3" "$LEDGER_HOOK" "$(json_bash "$SID" "$PROJ3" "pytest -q")"

  if [ ! -f "$HOME3/.claude/completion-gate/ledger-$SID.json" ]; then
    fail "case3: pytest 実行が台帳に記録されなかった"
  else
    pass "case3: pytest 実行が台帳に記録される"
  fi

  run_hook "$HOME3" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ3" false)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case3: コード編集+検証あり → block しない"
  else
    fail "case3: 検証済みなのに block された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース4: stop_hook_active=true → block しない
# ------------------------------------------------------------------------------
{
  HOME4="$(new_fake_home)"
  PROJ4="$(new_fake_proj)"
  SID="sess-stop-active"

  run_hook "$HOME4" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ4" "app.py")"
  run_hook "$HOME4" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ4" true)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case4: stop_hook_active=true → block しない（無限ループ防止）"
  else
    fail "case4: stop_hook_active=true なのに block された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース5: キルスイッチ off → block しない
# ------------------------------------------------------------------------------
{
  HOME5="$(new_fake_home)"
  PROJ5="$(new_fake_proj)"
  SID="sess-killswitch"

  mkdir -p "$HOME5/.claude/completion-gate"
  printf '%s' '{"mode":"off"}' > "$HOME5/.claude/completion-gate/state.json"

  run_hook "$HOME5" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ5" "app.py")"
  run_hook "$HOME5" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ5" false)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case5: キルスイッチ off → block しない"
  else
    fail "case5: キルスイッチ off なのに block された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース6: nudge — 実装系プロンプト → 注入あり / 質問プロンプト → 無出力
# ------------------------------------------------------------------------------
{
  HOME6="$(new_fake_home)"
  PROJ6="$(new_fake_proj)"
  SID="sess-nudge"

  IMPL_PROMPT="ログイン画面のバリデーションを実装してほしい。エラーメッセージの文言もついでに直してください。"
  run_hook "$HOME6" "$NUDGE_HOOK" "$(json_prompt "$SID" "$PROJ6" "$IMPL_PROMPT")"
  CTX="$(printf '%s' "$RUN_OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null)"
  if [ "$RUN_RC" -eq 0 ] && [ -n "$CTX" ]; then
    pass "case6: 実装系プロンプト（30字以上）→ additionalContext 注入あり"
  else
    fail "case6: 実装系プロンプトなのに注入されなかった (rc=$RUN_RC out=$RUN_OUT)"
  fi

  Q_PROMPT="これは何ですか？"
  run_hook "$HOME6" "$NUDGE_HOOK" "$(json_prompt "$SID" "$PROJ6" "$Q_PROMPT")"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case6: 質問プロンプト（短い・キーワードなし）→ 無出力"
  else
    fail "case6: 質問プロンプトなのに出力があった (rc=$RUN_RC out=$RUN_OUT)"
  fi

  SHORT_IMPL_PROMPT="直して"
  run_hook "$HOME6" "$NUDGE_HOOK" "$(json_prompt "$SID" "$PROJ6" "$SHORT_IMPL_PROMPT")"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case6: 実装キーワードだが30字未満 → 無出力"
  else
    fail "case6: 30字未満の実装プロンプトなのに注入された (rc=$RUN_RC out=$RUN_OUT)"
  fi

  LONG_NONIMPL_PROMPT="今日の天気について教えてください。傘は必要でしょうか、念のため確認したいです。"
  run_hook "$HOME6" "$NUDGE_HOOK" "$(json_prompt "$SID" "$PROJ6" "$LONG_NONIMPL_PROMPT")"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case6: 30字以上だが実装キーワードなし → 無出力"
  else
    fail "case6: 実装キーワードが無いのに注入された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース7（追加）: verify.json opt-in の commands 配列と部分一致するコマンドが
# 台帳に記録される（粗いパターンに一致しないコマンドでも検証行為とみなされる）
# ------------------------------------------------------------------------------
{
  HOME7="$(new_fake_home)"
  PROJ7="$(new_fake_proj)"
  SID="sess-verifyjson"

  mkdir -p "$PROJ7/.claude"
  printf '%s' '{"commands":["./scripts/check.sh"]}' > "$PROJ7/.claude/verify.json"

  run_hook "$HOME7" "$LEDGER_HOOK" "$(json_bash "$SID" "$PROJ7" "./scripts/check.sh --all")"
  LEDGER7="$HOME7/.claude/completion-gate/ledger-$SID.json"
  if [ -f "$LEDGER7" ] && grep -q "check.sh" "$LEDGER7"; then
    pass "case7: .claude/verify.json の commands 部分一致で台帳に記録される"
  else
    fail "case7: verify.json opt-in コマンドが台帳に記録されなかった"
  fi
}

# ------------------------------------------------------------------------------
# ケース8（追加）: 状態ディレクトリ・jq 不在等が理由で例外的に動かない場合でも
# gate hook は例外を投げず exit 0 する（フェイルオープンの回帰確認: 存在しない
# セッションに対する Stop 呼び出し）
# ------------------------------------------------------------------------------
{
  HOME8="$(new_fake_home)"
  PROJ8="$(new_fake_proj)"
  SID="sess-never-touched"

  run_hook "$HOME8" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ8" false)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case8: 一度も Edit/Write していないセッションの Stop → block しない"
  else
    fail "case8: 未使用セッションなのに block された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース9（追加）: コード編集 + lint のみ実行（kind="lint"）→ 台帳に記録はされるが
# 武装解除されず block される（lint だけでは検証済み扱いにしない）
# ------------------------------------------------------------------------------
{
  HOME9="$(new_fake_home)"
  PROJ9="$(new_fake_proj)"
  SID="sess-lint-only"

  run_hook "$HOME9" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ9" "app.py")"
  sleep 1
  run_hook "$HOME9" "$LEDGER_HOOK" "$(json_bash "$SID" "$PROJ9" "ruff check .")"

  LEDGER9="$HOME9/.claude/completion-gate/ledger-$SID.json"
  KIND9="$(tail -n 1 "$LEDGER9" 2>/dev/null | jq -r '.kind // empty' 2>/dev/null)"
  if [ "$KIND9" = "lint" ]; then
    pass "case9: ruff 実行が台帳に kind=lint で記録される"
  else
    fail "case9: ruff 実行の台帳エントリの kind が想定と異なる (got=$KIND9)"
  fi

  run_hook "$HOME9" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ9" false)"
  DECISION9="$(printf '%s' "$RUN_OUT" | jq -r '.decision // empty' 2>/dev/null)"
  if [ "$RUN_RC" -eq 0 ] && [ "$DECISION9" = "block" ]; then
    pass "case9: コード編集 + lint のみ実行 → block される"
  else
    fail "case9: lint のみなのに block されなかった (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

# ------------------------------------------------------------------------------
# ケース10（追加）: コード編集 + pytest 実行（kind="test"）→ 台帳に記録され、
# block されない
# ------------------------------------------------------------------------------
{
  HOME10="$(new_fake_home)"
  PROJ10="$(new_fake_proj)"
  SID="sess-pytest-only"

  run_hook "$HOME10" "$LEDGER_HOOK" "$(json_edit "$SID" "$PROJ10" "app.py")"
  sleep 1
  run_hook "$HOME10" "$LEDGER_HOOK" "$(json_bash "$SID" "$PROJ10" "pytest -q")"

  LEDGER10="$HOME10/.claude/completion-gate/ledger-$SID.json"
  KIND10="$(tail -n 1 "$LEDGER10" 2>/dev/null | jq -r '.kind // empty' 2>/dev/null)"
  if [ "$KIND10" = "test" ]; then
    pass "case10: pytest 実行が台帳に kind=test で記録される"
  else
    fail "case10: pytest 実行の台帳エントリの kind が想定と異なる (got=$KIND10)"
  fi

  run_hook "$HOME10" "$GATE_HOOK" "$(json_stop "$SID" "$PROJ10" false)"
  if [ "$RUN_RC" -eq 0 ] && [ -z "$RUN_OUT" ]; then
    pass "case10: コード編集 + pytest 実行 → block されない"
  else
    fail "case10: pytest 実行済みなのに block された (rc=$RUN_RC out=$RUN_OUT)"
  fi
}

echo ""
echo "== 結果: PASS=$PASS FAIL=$FAIL =="

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
exit 0
