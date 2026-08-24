---
name: codex-bridge
description: Claude Code から OpenAI Codex CLI（codex exec）を非対話・構造化・タイムアウト付きで呼び、実装委譲・レビュー・画像生成・UI 視覚レビュー・Web 調査・クラウド並列実装を行う。「Codex に実装させて」「Codex レビュー」「codex で実装」「Codex に投げて」「Codex の使用量」のほか、「画像を生成」「アイコン/OGP/図版を作って」「この画像を編集」「UI レビュー」「スクショで見た目を確認」「before/after を比べて」「調査を Codex に」「cloud で並列実装」「best-of-N」などと言われたときに使う。画像生成・vision は ChatGPT サブスク枠で完結し Claude 枠を消費しない。全プロジェクトの任意のリポジトリから使える（スクリプトは絶対パスで呼ぶ）。
---

# codex-bridge — Codex CLI への実装委譲・レビュー配管

`/Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/` のスクリプト群
（render_prompt / codex_run / codex_job / ui_screenshot / codex_cloud）を**絶対パスで**呼ぶ。Codex の出力はそのまま読まず、必ず `codex_job.py result` の圧縮サマリを読む
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
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_run.py --mode task|review --job-dir <job-dir> \
     --cd <対象リポジトリ> [--write] [--model gpt-5.6-sol|gpt-5.6-terra|gpt-5.6-luna] \
     [--effort minimal|low|medium|high|xhigh] --prompt-file /tmp/codex-prompt.md \
     [--schema /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/templates/prompts/review.schema.json] \
     --timeout-sec 1800 --idle-timeout-sec 300 [--resume <thread_id>]
   ```
   - **`--write` が無ければ read-only**。書かせるときだけ付ける（レビューには付けない）。
   - モデル既定は `gpt-5.6-terra`（effort `high`）。難しい実装は `gpt-5.6-sol`、機械的な作業は `gpt-5.6-luna`。
   - **並列度は 1**（ChatGPT 認証は並列で token_invalidated が出る既知問題）。`--max-parallel` は上げない。
   - 終了コード: 0=completed / 2=failed / 3=timeout・idle_timeout / 4=codex 不在・認証エラー / 1=その他。
     **4 が出たら導入・ログインの問題**なので、リトライせずユーザーに報告する。

3. **結果を読む**
   ```bash
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_job.py result <job-dir>          # 圧縮サマリ（既定）
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_job.py status <job-dir>          # 実行中の進捗確認
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_job.py result <job-dir> --json   # job.json 全体（必要なときだけ）
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

## マルチモーダル・拡張モード

**画像生成**（アイコン・OGP・図版・ダミー素材。1枚 60〜80 秒、Claude 枠を消費しない）:
```bash
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_run.py --mode imagegen --job-dir <job-dir> \
  --out /絶対パス/name.png --prompt "画像の内容指示（日本語可）" --timeout-sec 600
```
- `--out` は絶対パスの `.png` 必須。`--cd` 指定時は `--out` が cd 配下にあること（既定 cd は out の親）。
- 既存画像の**編集**は `--image <元画像>` を併用（「この画像の◯◯を△△に変えて」）。
- 成否は `job.json` の `status` と `image: {path, bytes, width, height}` で判定（out 未生成なら
  `~/.codex/generated_images/` から自動回収、それも無ければ failed に降格する）。

**UI 視覚レビュー**（screenshot → 指摘 → 修正 → before/after 比較のループ）:
```bash
# 1. 撮影（3 ビューポート: 1440x900 / 768x1024 / 375x812）
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/ui_screenshot.py --url http://localhost:3000 --out-dir <job-dir>/shots
# 2. レビュー（画像は最大4枚。位置は言葉で・座標や色コードは断定させない）
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/render_prompt.py ui-review --set CONTEXT="…" --set FOCUS="…" --out /tmp/p.md
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_run.py --mode review --job-dir <job-dir> --image <shot1> --image <shot2> \
  --prompt-file /tmp/p.md --schema /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/templates/prompts/ui-review.schema.json --timeout-sec 900
# 3. Must/Should/Nice を裁定 → Must を修正（修正指示には画像でなく指摘リスト＋セレクタを渡す）
# 4. 再撮影 → before/after 相対比較（ui-compare + ui-compare.schema.json、--image before --image after）
```
- 反復は上限 3 回。1 周に渡す画像は 2 枚まで（過去周回の画像は捨てテキスト履歴のみ残す）。

**Web 調査の委譲**（広く浅い一次情報収集。Claude の WebSearch 枠温存）:
`codex_run.py --mode task --web-search --prompt "…調査して出典 URL 付きで返す"`（read-only のまま）。

**クラウド並列実装（best-of-N）**（前提: ChatGPT 側で GitHub 連携とクラウド環境 ENV_ID の作成が必要）:
```bash
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py submit --env <ENV_ID> --attempts 3 --prompt-file /tmp/p.md
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py list --json / status <task> / diff <task> --attempt N
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py apply <task> --attempt N --yes   # --yes なしはドライラン
```
- 各案の diff は必ず Claude がレビューして裁定してから apply する。

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
- **画像は `--image` で渡す**（codex_run が `--image=` 結合形に整形して引数吸い込み事故を防ぐ。
  素の `codex exec -i img.png "prompt"` は prompt が画像リストに吸われて無言失敗する）。最大 4 枚、
  長辺 2048px 超は自動縮小。`--image` は `--mode task|review|imagegen` で使え、resume でも渡せる
  （ただし画像入りスレッドはコンテキストが肥大するため短く切る）。
- 環境に OPENAI_API_KEY を置かない運用を維持する（codex_run は既定で子環境から除去する）。
  10 枚超の画像バッチが要るときだけ Images API（別課金）への切替をユーザーに提案する。
