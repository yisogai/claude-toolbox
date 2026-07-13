---
name: model-policy
description: サブエージェントのモデル使用ポリシー（fable禁止・opus 既定/sonnet 併用）の確認・一時緩和・復帰・無効化を行うスキル。メインループ専任の fable がサブエージェント（Agent ツール / Workflow の agent()）に割り当てられる/継承される事故を PreToolUse hook でハーネスレベルに防ぎ、作業を opus/sonnet に振り分けて委譲してコストを抑える。ユーザーが「モデルポリシー」「ポリシー確認」「モデル強制の状態」「（サブエージェントの）緩和して」「fableサブエージェントを許可」「fork を使いたい」「ポリシー戻して」「enforce に戻して」「モデル強制を無効化」「キルスイッチ」等と言ったら、明示的に /model-policy と打たれていなくても使う。全プロジェクトから使える（ユーザーレベル hook のため既存・新規リポジトリに自動適用）。
---

# model-policy — サブエージェント・モデル使用ポリシーの運用

## これは何か（仕組みの要約）

メインループを Fable 5（高単価・統括専任）で回しつつ、**サブエージェントが誤って fable で起動すること**をハーネスレベルで防ぐ 4 層システム。ハード強制は「fable 禁止・fork 禁止・model 未指定→opus 書き換え」の 3 点に絞り、opus/sonnet/haiku など fable 以外は素通しする。作業を opus/sonnet に振り分けて委譲する使い分け方針そのものは行動規範層（CLAUDE.md）で担保する。

| 層 | 実体 | 何をするか | 強制 or 規範 |
|---|---|---|---|
| 1 | `model_policy_agent_hook.sh`（PreToolUse "Agent\|Task"） | fork→deny / fable→deny / 未指定・inherit→opus に書き換え / allowed は素通し | **強制** |
| 1b | `model_policy_workflow_hook.sh`（PreToolUse "Workflow"） | script に `agent(` があり `model` 語ゼロ→deny / `model` 値に `fable`→deny | **強制** |
| 2 | `~/.claude/CLAUDE.md` 追記 | fable=統括専任・作業を opus/sonnet に振り分けて委譲・agent() は model 明示・fork 禁止 | 規範（compact 後も残る） |
| 3 | `model_policy.sh`（このスキル） | status / relax / reset / off / enforce の運用 CLI | 運用 |
| 4 | `model_policy_reminder_hook.sh`（UserPromptSubmit） | 緩和中のときだけ「残り時間・戻し方」を注入（enforce 時は無出力=トークンゼロ） | 可視化 |

状態はランタイムの `~/.claude/model-policy/policy.json` で表現し、hook は発火のたび読み直すため**再起動なしで緩和/復元が反映**される。「緩和」は mode ではなく `relaxed_until`（未来 epoch 秒）で表し、**TTL 失効で自動的に enforce へ復帰**する（戻し忘れ事故を構造的に排除）。ファイルが無い/壊れていても hook 内蔵の enforce デフォルトが効く。

## Claude 向け実行手順

CLI は必ず**絶対パス**で Bash から叩く（`settings.json` の permissions.allow に登録済み。`$HOME` 変数だと permissions のリテラル一致から外れて許可プロンプトが出る）。別環境に導入した場合は以下の `/Users/<YOU>` を自分のホームに読み替える（README §3-4 参照）。

```bash
# 状態確認（既定サブコマンド）
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" status

# 一時緩和（分。既定60・上限1440）。fable サブエージェント/fork が必要なときだけ
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" relax 60

# 緩和を即解除して enforce へ
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" reset

# キルスイッチ（全サブエージェント素通し。hook バグ時の応急）
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" off

# enforce に戻す
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" enforce

# タスク/プロジェクト単位で緩和（cwd の ./.claude/model-policy.json を対象に）
bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" --project relax 30
```

運用ルール:
- どのサブコマンドを実行しても、CLI は末尾に `status` 相当を出力する。**その出力を必ずユーザーに日本語で要約報告**すること（実効状態・残り時間・有効ファイル・ハートビート）。
- ユーザーに頼まれて `relax` したら、**そのタスクが終わったら `reset` を促す**こと（TTL でも自動復帰するが、明示的に戻すのが安全）。
- `deny` されたら理由文に従い `model:"opus"` を明示して Agent を再実行する。fable/fork がどうしても必要なら、ユーザーに `/model-policy relax` を依頼する（Claude 自身は relax しない。ユーザーの承認行為）。
- サブエージェントを使った直後に `status` のハートビートが「未発火」や極端に古い場合は、hook が機能していない可能性がある（README の検知手段を参照）。

## settings.json 配線（自己文書化）

この配線は統括側が `~/.claude/settings.json` に投入する（計画書の差分そのまま）。

```jsonc
// permissions.allow に追加（CLI を Bash 経由で許可）
"Bash(bash \"/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh\":*)"

// hooks.PreToolUse を新設
"PreToolUse": [
  { "matcher": "Agent|Task",
    "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_agent_hook.sh" }] },
  { "matcher": "Workflow",
    "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_workflow_hook.sh" }] }
],

// 既存 hooks.UserPromptSubmit の配列に2要素目を追加（handoff の閾値 hook と共存）
{ "matcher": "", "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_reminder_hook.sh" }] }
```

hook スクリプト自体はハーネスが直接実行するため permissions.allow は不要。CLI のみ Bash 経由なので許可を追記する。

## 既知の限界

- **Workflow の一部 agent() だけ model 未指定**のケースは検出できない。層1b は「script 全体に `model` の語が一度でもあるか」しか見ない（agent() 個別の部分パースは誤検知源になるため行わない）。この取りこぼしは層2（CLAUDE.md 規律: 全 agent() に `model:'opus'` を明示）で補完する。
- キー名/ツール名は Claude Code のバージョンで変わりうる。壊れると「エラー」ではなく「**黙って強制が効かなくなる**」方向に倒れる（フェイルオープン設計の裏面）。ハートビート＋カナリアテストで検知する（README §互換性リスク）。
- `model` / `subagent_type` の抽出キーは 2026-07-07 に v2.1.202 で実測確認して固定済み（`tool_input.model` / `tool_input.subagent_type`、未指定時はキー自体が無い）。バージョンアップで変わったら README の手順で再確認する。

## 緊急時手順（3系統の逃げ道）

hook バグで全サブエージェントが起動不能になった場合:

1. **即時・再起動不要**: `bash "/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh" off`（キルスイッチ。hook は毎回ファイルを読むので即反映）。
2. **配線を外す**: `~/.claude/settings.json` の `hooks.PreToolUse` から該当 2 エントリを削除（file watcher で即反映）。
3. **設計上のフェイルオープン**: hook は意図的 deny 以外すべて exit 0 なので、想定外入力は自動的に素通しになる。

逆に、hook が壊れて**強制が効かなくなった**ときの応急処置は、`~/.claude/settings.json` の `env` に `"CLAUDE_CODE_SUBAGENT_MODEL": "opus"` を設定すること（hook と無関係にハーネス側で全サブエージェントを opus 固定。単一モデルになるが fable 排除は維持）。恒久運用では使わない（per-invocation 指定を全上書きし、将来の Sonnet 使い分けを潰すため）。
