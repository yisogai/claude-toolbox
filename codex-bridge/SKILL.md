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
   - レビューなら `review --set-file DIFF=/tmp/x.diff --set FOCUS="…" --set-file CONTEXT=/tmp/ctx.txt`。
   - **中身を制御できない値（diff・レビュー指摘・モデル生成テキスト・ユーザー入力）は必ず `--set-file`**。
     `--set` のインライン引数は自分が書いた短い定型文だけに使う（引用符・バッククォート・`$(...)` が
     シェル解釈され、exit 0 のまま内容が静かに書き換わる。2026-08-25 の deep-review レビューで実証）。
     ファイルは Write ツールで書く（echo / printf / heredoc は引用符衝突で壊れる）。
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

**Web 調査の委譲**（広く浅い一次情報収集。opus リサーチ艦隊の代替）:
- 単発: `codex_run.py --mode task --web-search --prompt "…出典 URL 付きで"`（read-only のまま）。
- **複数観点の並列調査は pool**: 観点ごとに `render_prompt.py research --set QUESTION="…" --set FOCUS="…"`
  でプロンプトを作り、jobs.json の各ジョブに `output_schema_file` として
  `templates/prompts/research.schema.json` を渡し、`codex_pool.py run --web-search …` で実行。
  **統合・裁定だけ**を opus/メインで行う（調査本体に opus fan-out を使わない。2026-08-25 方針）。

**探索・大量読みの委譲**（リポジトリ理解・多数ファイル要約。メイン文脈の肥大防止を兼ねる）:
`render_prompt.py explore --set TARGET="読む対象" --set QUESTIONS="答えるべき問い"` でプロンプトを作り、
read-only ジョブ（単発 exec か pool）＋ `explore.schema.json`。モデルは
`gpt-5.6-luna` / effort medium で足りることが多い。メインは structured_output の evidence パスだけ読む。

**反証検証の委譲**（レビュー指摘・調査クレームの検証）:
`render_prompt.py verify --set CLAIM="…" --set CONTEXT="…"` ＋ `verify.schema.json`（verdict:
confirmed / refuted / plausible）。指摘1件=1ジョブで pool に流す。`/deep-review` はこの配管を使う
pool 駆動版（検証段の opus xhigh を置換済み。統合の opus high のみ Claude 側に残る）。

**クラウド並列実装（best-of-N）**:
```bash
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py submit --cd <対象リポジトリ> --attempts 3 --prompt-file /tmp/p.md
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py list --json / status <task> / diff <task> --attempt N
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_cloud.py apply <task> --attempt N --yes   # --yes なしはドライラン
```
- `--env` は通常省略する。解決順: `--env` → `$CODEX_BRIDGE_CLOUD_ENV` → `var/cloud.json` の
  `environments[--cd のリポジトリの owner/repo]`（origin remote から自動判定）→ top-level `env_id`。
  登録済みリポジトリなら **`--cd <リポジトリ>` を渡すだけで正しい環境に飛ぶ**。1環境=1リポジトリ。
  未登録リポジトリはエラーで止まる（誤った環境への投下防止のため、全体既定の `env_id` は置かない運用）。
- 新しいリポジトリを足すとき: ChatGPT 側（chatgpt.com/codex → Settings → Environments）で GitHub
  連携と環境作成（環境 ID は設定 URL 末尾の 32 桁 hex）→ `var/cloud.json` の `environments` に
  `"owner/repo": "<hex>"` を追記。org リポジトリは GitHub App の org 承認が要る場合がある。
- リポジトリ横断のタスクは cloud では1タスクにできない（1環境=1リポ）。ローカル直列/spawn で行う。
- WIP ブランチ作業中のリポジトリに投げるときは `--branch <既定ブランチ>` を明示する
  （cloud はローカルの checkout 状態でなく push 済みブランチに対して走る）。
- cloud はリポジトリの **push 済みの状態**に対して走る（ローカルの未コミット変更は見えない）。
- `status` はタスクが pending の間、子 codex が非ゼロを返すことがある（→ラッパーは exit 2）。
  完了判定は exit code でなく出力の `[READY]` か `list --json` の `status` で行う。初回タスクは
  コンテナ構築が入るため数分かかる。
- 各案の diff は必ず Claude がレビューして裁定してから apply する。

## 委譲の並列化 — 4方式の使い分け

| 方式 | 使いどころ | 制約 |
|---|---|---|
| ローカル直列（既定） | ローカルの未コミット変更が要る／段階制御・スキーマ合流が要る | **codex exec の多重起動は禁止**（認証 token_invalidated の既知リスク #26303。--max-parallel を上げない） |
| **pool**（codex_pool.py） | 独立した複数ジョブを**ローカルで並列**に（Workflow からの並列委譲・複数リポの同時作業） | app-server 1プロセス多重化なので認証競合なし（実測済み 2026-08-25）。並列度既定 3・上限 4。**usage が台帳に載らない**（プロトコル上未取得）。画像入力は未対応 |
| **spawn**（Codex 内蔵 multi_agent） | 1つの委譲タスクの**内部**に独立サブ作業が複数（多観点レビュー・並列調査） | プロンプトで「collaboration ツール（spawn_agent）で N 体並列に」と明示。成果は親の最終メッセージ経由のみ・目安 2〜4体 |
| **cloud**（codex_cloud.py） | 同一仕様の複数案（--attempts N）／長時間タスクの切り離し／ローカルを占有したくないとき | push 済み GitHub リポジトリ＋環境作成済みが前提。diff レビュー後に apply |

- どの方式にするかの裁定はメイン（Claude）が行う。迷ったらローカル直列。

**pool の使い方**（短命プロセス。常駐させない）:
```bash
# jobs.json: {"jobs":[{"id":"a","cwd":"/絶対パス","prompt":"…","write":false,
#             "model":任意,"effort":任意,"output_schema_file":任意}, …]}（1〜8件）
python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_pool.py run \
  --jobs-file jobs.json --pool-dir <pool-dir> --max-parallel 3 \
  --timeout-sec 1800 --job-timeout-sec 900
# 結果: <pool-dir>/pool.json と <pool-dir>/jobs/<id>/{job.json,last.md,events.jsonl}
```
- 終了コード: 0=全完了 / 2=失敗あり / 3=timeout あり / 4=起動・ハンドシェイク失敗 / 1=引数。
- pool は codex exec と同じ flock スロットを1つ掴む（実行中は exec 系ジョブが待ちになる。逆も然り）。
- 401 / token_invalidated 検出時はプール全体を即中断する（リトライしない）。再ログインをユーザーに報告。
- `--codex-config` と `--web-search` が同じキーを指定した場合は後者（後置の `-c`）が勝つ（last-wins、実測確認済み）。
- 各スレッドで MCP サーバが起動するため立ち上がりに数秒〜十数秒の揺らぎがある。ジョブの
  タイムアウトには余裕を持たせる。

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
