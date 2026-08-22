---
name: cost-manager
description: fable-cost-manager でタスク単位のコスト（USD/JPY・モデル別内訳）と週次枠のペースを計測・可視化する。「コスト計測開始」「予算を設定」「コストレポート」「今回いくらかかった」「料金を出して」「コスト集計」「消化状況」「予算どれくらい使った」「ペース確認」「週次枠」「使い切り状況」「枠どれくらい余ってる」などと言われたときに使う。全プロジェクトの任意のリポジトリから使える（スクリプトは絶対パスで呼ぶ）。
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
   python3 /Users/<YOU>/.claude/skills/cost-manager/scripts/cost_report.py --task "<短いタスク名>" --desc "<平易な要約1-2行>" [--since ISO] [--scope session|global]
   ```
   - **`--task` には15字程度の短いタスク名**を渡す（PNG カードのタイトルになる。非エンジニアが見出しとしてひと目で分かる短さにする）。
   - **`--desc` には非エンジニアにも伝わる平易な日本語で1〜2行の要約**を渡す（省略すると要約の質が落ちる）。専門用語・PR番号・関数名・コード識別子は避けるか、使う場合は括弧で平易な補足を添える。「何をして、何がどう良くなったか」が伝わる表現にする。
   - 範囲は開始マーカーの started_at〜now（無ければセッション全体）。`--since`/`--until` で明示的に上書き可能。スコープ既定は `session`（マーカー登録セッション + subagents）、`global` は全プロジェクト走査（無関係セッション混入の可能性を伴う）。
   - 実行後、生成された Markdown / PNG の**パス**と**合計 USD/JPY**、**実処理時間**を必ずユーザーに日本語で報告する。

4. **ペース確認**（「ペース確認」「週次枠」「使い切り状況」「枠どれくらい余ってる」等）:
   ```bash
   python3 /Users/<YOU>/.claude/skills/cost-manager/scripts/pace_report.py
   ```
   - 週次枠 / Fable サブ枠 / 5時間枠の現在のペース、窓の開始・終了（JST）、このペースでの週末到達%、
     モデル別 USD/tokens、較正、日別サンプル履歴、推奨行が出力される。
   - キャッシュが古い・無いときは `--refresh` を付けて同期集計する（実データで数秒〜十数秒かかる）。
   - exit 3（サンプル未取得）のときは、statusline に `pace_statusline.sh` が組み込まれていない可能性を
     ユーザーに伝える（README の「statusLine への組み込み」）。
   - 出力の推定は**未検証の仮定 A1〜A3**に基づく参考値であることを添えて報告する。
   - 「未収載モデルがあるため Fable 推定不能」が出たら、Fable のペースは報告せず
     `config/pricing.json` に当該モデルの単価を追加する必要があることを伝える。
     「直近の集計が失敗しています」が出たら、その理由をそのまま伝える。
   - 「Codex」節が出るのは codex-bridge の使用量台帳がある場合だけ。Codex の枠は絶対値が非公開の
     ため既定では**消費クレジットと件数のみ**（% は出せない）。実測で上限が分かったときに
     `config/config.json` の `budget.pace.codex_weekly_credits` を設定すると % とペースが出る、
     と伝える。窓が Claude 側の seven_day 窓の流用（＝近似）であることも添える。

## 原則
- `~/.claude/projects` は読み取り専用。書込は本リポジトリの `var/` と `reports/` のみ。破壊的操作は行わない。
- `cost_status.py` の結果に応じて提案する:
  - 消化ペースが速い（ETA が近い・$/h が高い）→ 「オーケストレーションを Opus に落とす」ことを提案する。
  - 予算に余裕がある → `/model-policy relax` の利用を提案する（model-policy スキルへ橋渡し）。
