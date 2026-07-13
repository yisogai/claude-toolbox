---
name: cost-manager
description: fable-cost-manager でタスク単位のコスト（USD/JPY・モデル別内訳）を計測・可視化する。「コスト計測開始」「予算を設定」「コストレポート」「今回いくらかかった」「料金を出して」「コスト集計」「消化状況」「予算どれくらい使った」などと言われたときに使う。全プロジェクトの任意のリポジトリから使える（スクリプトは絶対パスで呼ぶ）。
---

# cost-manager — タスク単位コスト計測・レポート

`/Users/<YOU>/.claude/skills/cost-manager` の3スクリプトを絶対パスで呼び、タスク単位のトークン使用量・料金（USD + 参考JPY）を計測する。開始マーカーで範囲を区切り（無ければ現在セッション全体にフォールバック）、完了時に Markdown + PNG の2点セットでレポートを出力する。

## 手順
1. **計測開始**（「コスト計測開始」「予算を設定」等）:
   ```bash
   python3 /Users/<YOU>/.claude/skills/cost-manager/scripts/cost_start.py --task "<短い名前>" [--budget-usd N]
   ```
   - `--task`: タスク名（短く）。`--budget-usd`: 予算（USD、省略可）。
   - 既に進行中タスクがある場合は exit 2 で確認を促してくる。ユーザーに置き換えてよいか確認してから `--force` を付けて再実行する。

2. **途中経過確認**（「消化状況」「予算どれくらい使った」等）:
   ```bash
   python3 /Users/<YOU>/.claude/skills/cost-manager/scripts/cost_status.py
   ```
   - 消化額（USD/JPY）・消化率・経過時間・$/h ペース・予算到達 ETA・モデル別内訳が出力される。結果をユーザーに日本語で要約報告し、[原則]の提案基準に従って助言する。

3. **完了レポート**（「コストレポート」「今回いくらかかった」「料金を出して」「コスト集計」等）:
   ```bash
   python3 /Users/<YOU>/.claude/skills/cost-manager/scripts/cost_report.py --desc "<今回の作業の要約1-2行>" [--since ISO] [--scope session|global]
   ```
   - **`--desc` には Claude が今回の作業内容を1〜2行の日本語で要約して必ず渡す**こと（省略すると要約の質が落ちる）。
   - 範囲は開始マーカーの started_at〜now（無ければセッション全体）。`--since`/`--until` で明示的に上書き可能。スコープ既定は `session`（マーカー登録セッション + subagents）、`global` は全プロジェクト走査（無関係セッション混入の可能性を伴う）。
   - 実行後、生成された Markdown / PNG の**パス**と**合計 USD/JPY**、**実処理時間**を必ずユーザーに日本語で報告する。

## 原則
- `~/.claude/projects` は読み取り専用。書込は本リポジトリの `var/` と `reports/` のみ。破壊的操作は行わない。
- `cost_status.py` の結果に応じて提案する:
  - 消化ペースが速い（ETA が近い・$/h が高い）→ 「オーケストレーションを Opus に落とす」ことを提案する。
  - 予算に余裕がある → `/model-policy relax` の利用を提案する（model-policy スキルへ橋渡し）。
