---
name: codex-bridge
description: Claude Code から OpenAI Codex CLI（codex exec）を非対話・構造化・タイムアウト付きで呼び、実装委譲とレビューを行う。「Codex に実装させて」「Codex レビュー」「codex で実装」「Codex に投げて」「Codex の使用量」などと言われたときに使う。全プロジェクトの任意のリポジトリから使える（スクリプトは絶対パスで呼ぶ）。
---

# codex-bridge — Codex CLI への実装委譲・レビュー配管

`/Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/` の 3 スクリプトを
**絶対パスで**呼ぶ。Codex の出力はそのまま読まず、必ず `codex_job.py result` の圧縮サマリを読む
（Fable のコンテキストを守るため）。

## 手順

1. **プロンプトを作る**（仕様をテンプレートに埋める。未充足プレースホルダがあれば exit 1）
   ```bash
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/render_prompt.py \
     implement --set OBJECTIVE="…" --set SCOPE="…" --set NON_GOALS="…" \
     --set ACCEPTANCE="…" --set FORBIDDEN="…" --set-file CONTEXT=/tmp/context.md \
     --out /tmp/codex-prompt.md
   ```
   - レビューなら `review --set-file DIFF=/tmp/x.diff --set FOCUS="…" --set CONTEXT="…"`。
   - 対象リポジトリのルートに `templates/AGENTS.md.tmpl` を `AGENTS.md` として置いてあることが前提
     （置いていなければ先にコピーし、リポジトリ固有の規約を末尾に追記する）。

2. **Codex を実行する**（長い。`run_in_background` で回し、`--timeout-sec` を明示する）
   ```bash
   python3 …/codex-bridge/scripts/codex_run.py --mode task|review --job-dir <job-dir> \
     --cd <対象リポジトリ> [--write] [--model gpt-5.6-sol|gpt-5.6-terra|gpt-5.6-luna] \
     [--effort minimal|low|medium|high|xhigh] --prompt-file /tmp/codex-prompt.md \
     [--schema …/templates/prompts/review.schema.json] \
     --timeout-sec 1800 --idle-timeout-sec 300 [--resume <thread_id>]
   ```
   - **`--write` が無ければ read-only**。書かせるときだけ付ける（レビューには付けない）。
   - モデル既定は `gpt-5.6-terra`（effort `high`）。難しい実装は `gpt-5.6-sol`、機械的な作業は `gpt-5.6-luna`。
   - **並列度は 1**（ChatGPT 認証は並列で token_invalidated が出る既知問題）。`--max-parallel` は上げない。
   - 終了コード: 0=completed / 2=failed / 3=timeout・idle_timeout / 4=codex 不在・認証エラー / 1=その他。
     **4 が出たら導入・ログインの問題**なので、リトライせずユーザーに報告する。

3. **結果を読む**
   ```bash
   python3 …/codex-bridge/scripts/codex_job.py result <job-dir>          # 圧縮サマリ（既定）
   python3 …/codex-bridge/scripts/codex_job.py status <job-dir>          # 実行中の進捗確認
   python3 …/codex-bridge/scripts/codex_job.py result <job-dir> --json   # job.json 全体（必要なときだけ）
   ```
   - 使用量は `codex_job.py usage [--since 2026-08-01] [--json]`（「Codex の使用量」と言われたとき）。

4. **報告する**（日本語）
   - status・変更ファイル・失敗したコマンド・警告を必ず伝える。`credits_est` は **ChatGPT プランの
     クレジット概算**であって請求額ではないことを添える。
   - Codex の最終メッセージは AGENTS.md の見出し形式（`## 結果:` …）で返る。**「## 未検証・仮定」と
     「## 提案（未対応）」は握りつぶさず**ユーザーへ伝える。
   - 修正を続けるときは **`--resume <thread_id>`**（`codex_job.py result` の
     `thread_id=…（再開: --resume …）` 行の値）で同じスレッドに Must 指摘だけを渡す。
     `--resume-last` は「同じ cwd の最終更新スレッド」を選ぶため、**間にレビュー実行を挟むと
     レビュー側のスレッドを再開してしまう**。挟んでいないと確信できるとき以外は使わない。

## 原則
- **成否を終了コードで判断しない**。Codex はテストが失敗していても turn が正常完了すれば exit 0 を返す。
  判定は `job.json` の `status`（JSONL イベント由来）で行う。非致命の `error` item（設定の警告など）は
  失敗ではなく `warnings` に出る。
- **`--review-scope` にプロンプトは渡せない**（clap の conflicts_with_all で必ずエラーになる）。
  レビューに指示を与えたいときは `--review-scope` を使わず、`render_prompt.py review` の
  プロンプト方式（`--schema` 併用可）を使う。
- `result` に「モック実行」と出ていたら、それは Codex 実機の結果ではない。ユーザーに実機の結果として
  報告しない。
- Codex の変更は**必ず人か Claude がレビューしてから**採用する。無条件に信用しない。
- 書込先は `codex-bridge/var/` と `--job-dir` のみ。対象リポジトリへの書込は Codex 自身が `--write`
  指定時にサンドボックス（workspace-write）内で行う。
- Codex 未導入・未ログインの環境では `--mock ok` で配管だけを確認できる（実機呼び出しはしない）。
