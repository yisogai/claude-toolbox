# harness アンインストール手順

`install.sh --apply` で `~/.claude` に適用したハーネス v1 を取り除く手順。
install.sh 自体にはアンインストール用のフラグは無い（フラグ追加の代わりに、
以下の手動手順とキルスイッチによる即時無効化の2段階で対応する）。

## 0. まず止めたいだけなら: キルスイッチ（推奨・最速）

ファイル・hooks 配線を残したまま、completion-gate（Stop hook のブロック）だけを
即座に無効化できる。

```bash
python3 -c 'import json; json.dump({"mode":"off"}, open("'"$HOME"'/.claude/completion-gate/state.json","w"))'
```

再度有効化する場合は `mode` を `"enforce"` に戻す（または `state.json` を削除すれば
次回 Stop hook 発火時に enforce 扱いで再作成される）。

verify_ledger_posttooluse.sh（台帳記録）と workflow_nudge_prompt.sh（nudge 注入）は
キルスイッチの対象外（副作用が軽微なため）。完全に止めたい場合は下記の手動手順で
hooks エントリごと削除すること。

## 1. settings.json から hooks エントリを削除する

`~/.claude/settings.json` の `.hooks` から、install.sh が追加した以下の4エントリを
削除する（jq でもエディタでの手動編集でも良い）。他の既存 hooks（完了通知音などの Stop hook、
他スキルの UserPromptSubmit、PreToolUse 等）は削除しないこと。

- `.hooks.PostToolUse` のうち `matcher == "Bash"` かつ command が
  `.../hooks/verify_ledger_posttooluse.sh` のエントリ
- `.hooks.PostToolUse` のうち `matcher == "Edit|Write"` かつ command が
  `.../hooks/verify_ledger_posttooluse.sh` のエントリ
- `.hooks.Stop` のうち command が `.../hooks/completion_gate_stop.sh` のエントリ
- `.hooks.UserPromptSubmit` のうち command が `.../hooks/workflow_nudge_prompt.sh` のエントリ

jq でまとめて削除する例（`<REPO>` はこのリポジトリの絶対パスに置換。install.sh が
バックアップした `backup-opus-fable-harness-<timestamp>/settings.json` があれば、
それを見ながら差分を取ると確実）。

注意: エントリごと `select` で消す形（`.hooks[0].command != $vl` など）は使わないこと。
同じ matcher のエントリに他ツールの hook が同居している場合（例: PostToolUse Bash に
cmux が同居）、それらを巻き添えで消してしまう。以下のように hooks 配列から該当
コマンドだけを抜き、空になったエントリのみ削除する（2026-07-31 の撤去で実証済みの手順）:

```bash
REPO="<REPO>"  # harness-fablize を配置したディレクトリの絶対パスに置換
jq --arg vl "$REPO/hooks/verify_ledger_posttooluse.sh" \
   --arg cg "$REPO/hooks/completion_gate_stop.sh" \
   --arg wn "$REPO/hooks/workflow_nudge_prompt.sh" '
  def prune($cmds): map(.hooks = (.hooks | map(select([.command] | inside($cmds) | not))))
    | map(select(.hooks | length > 0));
  .hooks.PostToolUse = ((.hooks.PostToolUse // []) | prune([$vl]))
  | .hooks.Stop = ((.hooks.Stop // []) | prune([$cg]))
  | .hooks.UserPromptSubmit = ((.hooks.UserPromptSubmit // []) | prune([$wn]))
' "$HOME/.claude/settings.json" > "$HOME/.claude/settings.json.tmp" \
  && mv "$HOME/.claude/settings.json.tmp" "$HOME/.claude/settings.json"
```

削除後、`grep -c 'fablize/harness/hooks' ~/.claude/settings.json` が 0 であること、
他の配線（model-policy / cmux / handoff / auto_journal / 通知音）が残っていることを確認する。

適用後、`jq empty ~/.claude/settings.json` で valid JSON であることを確認すること。

## 2. CLAUDE.md から節を削除する（または元に戻す）

install.sh は `~/.claude/CLAUDE.md` に「## 作業プロトコル（全タスク共通）」節を追加している
（既存の同節があれば置換、無ければ末尾に追加）。

最も確実な方法は、install.sh が apply 前に取ったバックアップから復元すること。

```bash
# apply 時に表示されたバックアップディレクトリを使う
cp "$HOME/.claude/backup-opus-fable-harness-<timestamp>/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
```

バックアップが無い場合は、`~/.claude/CLAUDE.md` を手動編集し、「## 作業プロトコル」節を
丸ごと削除する（次の `## ` 見出し、または末尾までがその節の範囲）。他の既存節は変更しない。

## 3. agents / workflows を削除する

```bash
rm -f "$HOME/.claude/agents/verifier.md" "$HOME/.claude/agents/implementer.md"
rm -f "$HOME/.claude/agents/fable-advisor.md"
rm -f "$HOME/.claude/workflows/implement-verified.js" "$HOME/.claude/workflows/deep-review.js"
# workflows/ ディレクトリが空になり、他の workflow を置いていないなら削除してもよい
rmdir "$HOME/.claude/workflows" 2>/dev/null || true
```

fable-advisor.md を削除する場合は、model-policy スキル側の例外登録
（`~/.claude/model-policy/policy.json` の `fable_exempt_subagent_types`）からも
`"fable-advisor"` を外すこと（残っていても実害はないが、存在しない agent への
例外が残るのは紛らわしい）。

他の verifier.md / implementer.md / 同名 workflow をこのハーネス以外の用途で
使っていないか確認してから削除すること（同名で別内容のファイルを上書きしていた
場合は、そのファイルもバックアップディレクトリに退避されている）。

## 4. completion-gate の状態・ランタイムファイルを削除する（任意）

キルスイッチ状態ファイルとランタイム生成物（台帳・ハートビート等）を完全に消したい場合。
hooks エントリ（手順1）を削除済みであれば、このディレクトリは新規に書き込まれなくなる。

```bash
rm -rf "$HOME/.claude/completion-gate"
```

## 5. --switch-model を使っていた場合

`settings.json` の `"model"` を元の値（例: `claude-fable-5[1m]`）に手動で戻す。
バックアップに元の値が残っているので、そこから確認して書き戻すのが確実。

```bash
# 確認
jq -r '.model' "$HOME/.claude/backup-opus-fable-harness-<timestamp>/settings.json"
# 書き戻し（<元の値> を上の出力に置換）
jq --arg m "<元の値>" '.model = $m' "$HOME/.claude/settings.json" \
  > "$HOME/.claude/settings.json.tmp" && mv "$HOME/.claude/settings.json.tmp" "$HOME/.claude/settings.json"
```

## 6. このドキュメントの範囲外（別系統の配線）

以下は install.sh の管轄外であり、この手順では消えない。混同しないこと。

- **model-policy スキル**（`~/.claude/skills/model-policy/`）: PreToolUse 2本
  （Agent|Task / Workflow）と UserPromptSubmit の reminder、状態ファイル
  `~/.claude/model-policy/policy.json`。停止は `model_policy.sh off`（キルスイッチ）または
  settings.json から該当3エントリを削除。手順は model-policy スキルの README を参照。
- handoff / cost-manager / license-switch / auto_journal / cmux の各配線（ハーネスと独立）。

## 7. 確認

- `jq empty ~/.claude/settings.json` で valid JSON。
- `grep -c '## 作業プロトコル' ~/.claude/CLAUDE.md` が 0。
- 新しいセッションで hooks が発火しない（`~/.claude/completion-gate/last-*-hook` が
  更新されなくなる）ことを確認する。
