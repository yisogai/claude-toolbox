# codex-bridge — Claude Code から Codex CLI を非対話で呼ぶ配管

Claude Code（メイン = Fable、または Workflow 内の opus ドライバ）から OpenAI Codex CLI の
`codex exec` を **非対話・構造化・タイムアウト付き**で呼び出し、実装委譲とレビューを行う共通配管。

Codex 未契約・未インストールの環境でも、同梱のモック（`tests/mock_codex.py`）で**配管全体を
テストできる**。モック分岐は「起動するバイナリの差し替え」だけで、ストリーミング・タイムアウト・
kill・`job.json` 生成は本物と同じコードを通る。

```
codex-bridge/
├── README.md / SKILL.md
├── scripts/     codex_run.py（実行ドライバ）/ codex_job.py（結果参照）/ render_prompt.py / codex_lib.py
├── templates/   AGENTS.md.tmpl / prompts/{implement.md,review.md,review.schema.json} / codex.config.toml.example
├── config/      codex_pricing.json（ChatGPT プランのクレジット単価）
├── workflows/   implement-review-loop.js（Workflow テンプレート。コピーして使う）
├── tests/       test_codex_bridge.py / mock_codex.py
└── var/         events 台帳・ロック（git 追跡外）
```

---

## 導入

```bash
npm i -g @openai/codex          # Codex CLI（安定版 0.149.0 で検証した仕様）
codex login                     # ChatGPT プランでログイン（API キー課金にしない）
cp templates/codex.config.toml.example ~/.codex/config.toml   # 既存があれば差分をマージ
```

対象リポジトリのルートに作業規約を置く（Codex は AGENTS.md を「最も近い 1 枚」だけ読む。上書き型・32KiB 上限）:

```bash
cp templates/AGENTS.md.tmpl <対象リポジトリ>/AGENTS.md
# リポジトリ固有の規約は AGENTS.md 末尾（「ここから下はリポジトリ固有の規約」）に追記する
```

スキルとして使う場合は `install.sh codex-bridge`（リポジトリ直下）で `~/.claude/skills/` へ配置する。

### cmux シムの注意

作者環境では PATH 先頭に cmux の CLI シム（`.../cmux-cli-shims/<uuid>/codex`）があり、これは実
バイナリではない。`codex_run.py` は PATH 走査時に **パスに `cmux-cli-shims` を含むものを除外**して
実バイナリを探す（除外したことは `job.json` の `warnings` に残る）。明示したいときは
`--codex-bin <path>` か環境変数 `CODEX_BIN` を使う（優先順位: `--codex-bin` > `CODEX_BIN` > PATH）。

---

## 使い方

```bash
# 1) プロンプトを組み立てる（未充足プレースホルダがあれば exit 1）
python3 scripts/render_prompt.py implement \
  --set OBJECTIVE="…" --set SCOPE="…" --set NON_GOALS="…" \
  --set ACCEPTANCE="…" --set FORBIDDEN="…" --set-file CONTEXT=/tmp/context.md \
  --out /tmp/codex-prompt.md

# 2) Codex を実行する（長時間。バックグラウンド実行が前提）
python3 scripts/codex_run.py --mode task --job-dir /tmp/job1 --cd /path/to/repo --write \
  --model gpt-5.6-terra --effort high --prompt-file /tmp/codex-prompt.md \
  --timeout-sec 1800 --idle-timeout-sec 300

# 3) 結果を読む（Claude に渡すのは result の圧縮サマリ）
python3 scripts/codex_job.py status /tmp/job1     # 実行中の進捗
python3 scripts/codex_job.py result /tmp/job1     # 圧縮サマリ
python3 scripts/codex_job.py usage --since 2026-08-01
```

`--job-dir` に生成されるもの: `events.jsonl`（JSONL の生ログ）/ `stderr.log` / `last.md`（最終メッセージ）/
`job.json`（結果まとめ。アトミック書込）。**4 つとも実行開始時に消してから作り直す**ので、同じ
job-dir を使い回しても前回の結果が今回のものとして残ることはない。`job.json` には `queued_sec`
（スロット待ち。`duration_sec` には含めない）と `mock`（モック実行のシナリオ名 / 実機なら `null`）も入る。

### codex_run.py の主なオプション

| オプション | 既定 | 意味 |
|---|---|---|
| `--mode task\|review\|imagegen` | 必須 | 用途タグ（`job.json` / 台帳に残る）。`imagegen` は画像生成モード（下記） |
| `--write` | off | 付けると `-s workspace-write`。無ければ `-s read-only` |
| `--model` | `gpt-5.6-terra` | `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` |
| `--effort` | `high` | `-c model_reasoning_effort=<値>`（minimal…xhigh） |
| `--schema <path>` | なし | `--output-schema`。`last.md` を JSON パースして `structured_output` へ |
| `--timeout-sec` | 3600 | 壁時計タイムアウト |
| `--idle-timeout-sec` | 600 | 無イベント許容秒 |
| `--resume <id>` / `--resume-last` | なし | `codex exec resume` で同じスレッドを続ける（**`--resume <thread_id>` を推奨**。下記） |
| `--review-scope` | なし | `uncommitted` / `base:<ref>` / `commit:<sha>`（Codex ネイティブレビュー = 散文。**プロンプトは渡せない**） |
| `--max-parallel` | 1 | `var/locks/` のスロット数。**上げない**（下記の落とし穴） |
| `--allow-api-key` | off | `OPENAI_API_KEY` を子環境に残す（従量課金になる） |
| `--mock <scenario>` | なし | 実機を使わず配管を回す（下記） |
| `--image <path>` | なし | 入力画像（repeatable、最大4枚。png/jpg/jpeg/gif/webp）。argv には **`--image=` 結合形**で渡す（可変長 `-i` がプロンプトや stdin 指示 `-` を吸う事故の構造的回避）。darwin では長辺 2048px 超を `<job-dir>/images/` に自動縮小 |
| `--out <path>` | なし | `imagegen` 専用・必須。絶対パスの `.png`。`--cd` 配下にあること（workspace-write は cd 配下にしか書けない） |
| `--web-search` | off | `-c web_search="live"` を付与（`--review-scope` とは併用不可・警告して外す） |

**終了コード**: `0`=completed / `2`=failed / `3`=timeout・idle_timeout / `4`=codex 不在・認証エラー /
`1`=その他（error・killed・引数エラー）。

### マルチモーダル・拡張（2026-08-24 追加。実測根拠は docs/research/2026-08-24-chatgpt-multimodal-usage.md）

- **imagegen モード**: Codex 組み込みの image_gen ツール（`$imagegen`、gpt-image-2）で画像を生成し
  `--out` に保存する。プロンプトは `$imagegen <指示>。…に保存して。` に自動合成。サンドボックスは
  常に workspace-write、`--timeout-sec` 既定はこのモードのみ 600。turn.completed でも `--out` に
  有効な PNG/JPEG が無ければ `~/.codex/generated_images/` から回収を試み、失敗なら status=failed に
  降格する。`job.json` に `image: {path, bytes, width, height}` が載る。`--image` 併用で既存画像の
  編集もできる。ChatGPT プラン枠を消費し（画像ターンはテキスト比 3〜5 倍速）、API キー不要。
- **ui_screenshot.py**: headless Chrome で複数ビューポート（既定 1440x900 / 768x1024 / 375x812）を
  直列撮影し、PNG 検証済みのパスを stdout に出す。終了コード 0=全成功 / 1=引数 / 2=全滅 /
  3=一部成功 / 4=chrome 不在。UI レビューは ui-review / ui-compare テンプレート（+ schema）と
  `--mode review --image …` で回す（運用ループは SKILL.md）。
- **codex_cloud.py**: `codex cloud`（EXPERIMENTAL）の薄いラッパー。`submit --env <ENV_ID>
  --attempts N`（best-of-N）→ `list --json` / `status` / `diff --attempt N` → `apply --yes`。
  `--yes` なしの apply はドライラン。前提: ChatGPT 側で GitHub 連携とクラウド環境の作成。
- **codex_pool.py**（2026-08-25 追加）: `codex app-server` 1プロセスに複数ジョブを thread として
  並行投入するローカル並列プール。1プロセス = AuthManager 1個なので ChatGPT 認証の
  プロセス間競合が構造的に起きない（設計根拠と実測は docs/research/2026-08-25-codex-local-parallel.md）。
  短命プロセス（常駐 broker にしない。codex-plugin-cc #540 の轍の回避）。どの異常経路でも
  running の job.json を残さない。codex exec と同じ flock スロットで相互排他。
  制約: usage がプロトコル上取れず台帳未計上／画像入力未対応／並列度上限 4。

---

## 落とし穴（一次情報で確認。2026-08-22 / Codex CLI 0.149.0）

- **終了コードで成否を判断しない**。exit 1 になるのは致命エラー通知 / `turn.failed` / 中断 / server
  request 失敗のみで、**テストが失敗していても turn が正常完了すれば exit 0**。判定は `--json` の
  JSONL イベント（`turn.completed` / `turn.failed` / `error`）で行う。`codex_run.py` はこの方針で
  `job.json` の `status` を決め、`turn.completed` に到達せず終了した場合は `error` にする。
- **`--full-auto` は削除済み**。渡すと即エラーになるので使わない。
- **`-s` / `-C` / `-p` はサブコマンド（`resume` / `review`）より前**に置く必要がある。
- **プロンプトは stdin 推奨**（`-` を渡す）。非 TTY で空出力になる既知問題 #19945 があるため、
  さらに `-o <file>` で最終メッセージをファイルにも落とす。`-o` が空だった場合、`codex_run.py` は
  `agent_message` イベントから `last.md` を復元して警告を残す。
- **`exec review` は `--output-schema` を無視する既知バグ**がある。構造化レビューが要るときは通常の
  `exec` に `templates/prompts/review.md` を渡す方式（既定）を使い、`--review-scope` は「Codex
  ネイティブレビュー（散文）」のオプトインとして使う。`--review-scope` 指定時は `--output-schema`
  を付けない実装になっている（`--schema` を併用すると「外した」旨を `warnings` に残す）。
- **`review` サブコマンドにプロンプトは渡せない**。`ReviewArgs` の `--uncommitted` / `--base` /
  `--commit` は `conflicts_with_all = [..., "prompt"]` なので、`review --uncommitted -` は clap の
  ArgumentConflict で必ず失敗する。`codex_run.py` は `--review-scope` 指定時だけ末尾の `-` を付けず、
  stdin を即クローズし、「プロンプトは無視した」を `warnings` に残す。
- **`--resume-last` は「同じ cwd の最終更新スレッド」を選ぶ**。同じ worktree でレビュー実行を挟むと
  レビュー側のスレッドを再開してしまうため、**同じ cwd で他の Codex 実行を挟んでいないときだけ**使う。
  実装スレッドへ確実に戻すには `codex_job.py result` が出す `thread_id` を控えて
  `--resume <thread_id>` を使う（`workflows/implement-review-loop.js` はこの方式）。
- **`--cd` が git 管理外**だと codex は起動時に弾く。`codex_run.py` は `.git` を上へ辿って判定し、
  管理外なら `--skip-git-repo-check` を自動で付ける（付けたことは `warnings` に残る）。
- **上位から止められたとき**（SIGTERM / SIGHUP）も `job.json`（`status=killed`）を書き、プロセス
  グループを終了させ、スロットを解放してから終了する。ロックは `flock` なので、異常終了しても
  カーネルが自動解放する（ロックファイルは残るが、それが占有を意味することはない）。
- **並列実行は 1**。ChatGPT 認証で並列に走らせると token_invalidated が出る（#26303、0.136 時点）。
  `--max-parallel` の既定は 1 で、スロットが空くまで 5 秒間隔で待ち、`--timeout-sec` に達したら exit 3。
- **`OPENAI_API_KEY` があると無言で API 従量課金に切り替わる**（#20099）。`codex_run.py` は
  `--allow-api-key` が無い限り子プロセス環境から `OPENAI_API_KEY` / `CODEX_API_KEY` を削除する。
  加えて `~/.codex/config.toml` を **TOML としてパース**し、`forced_login_method` が `"chatgpt"`
  でなければ `job.json` の `warnings` に残す（公式の危険値は `"api"`。`"api"` / `"apikey"` なら
  「従量課金を強制する設定」と明示。
  読めない・パースできない場合もその旨を残す。**設定ファイルは変更しない**）。
- **非致命の `error` item は失敗ではない**。`exec_events.rs` の `Item::Error` は
  "non-fatal error surfaced as an item" と定義されている（ConfigWarning / DeprecationNotice 等）。
  `codex_run.py` はこれを `warnings` に回し、`status` は `turn.completed` 到達を優先する。
  `failed` になるのは top-level `turn.failed`、または `turn.completed` 未到達での top-level `error`。
- **バックグラウンド起動（`&` / `run_in_background`）では SIGINT が無視される**。シェルが
  バックグラウンドジョブの SIGINT を無効化するため、Ctrl-C 相当では止まらない。停止は
  **SIGTERM**（`kill <pid>`）で行う。`codex_run.py` は SIGTERM / SIGHUP を捕まえて子プロセス
  グループを終了させ、`job.json`（`status=killed`）を書いてからスロットを解放する。
- **失敗コマンドにも記録上限がある**。成功 50 件 / 失敗 50 件が上限で、超えた分は件数だけ
  `warnings` に残る（全件は `events.jsonl`）。`codex_job.py result` は失敗コマンドを先頭 20 件＋
  「他 M 件」に丸め、出力全体も 12,000 字で打ち切る（Claude のコンテキストを守るため）。
- **`--review-scope` の ref は `--base=<ref>` の `=` 形**で渡す。分離形（`--base <ref>`）だと
  `-` で始まる ref を clap がフラグと誤認して失敗する（clap レプリカで確認済み）。
- **`--schema` は実行前に検証する**。存在しない / ファイルでない / JSON として読めない場合は
  codex を起動せず exit 1。
- **プロンプトの不正な UTF-8 は置換して読む**（`--prompt-file` / stdin）。置換したことは
  `warnings` に残る。読めない場合だけ exit 1。
- **起動・引数エラーは JSONL に出ない**。イベントが 2 件以下で終わった失敗では、`stderr.log` の
  末尾 600 字を `errors`（＝`codex_job.py result` の「## エラー」）に載せる。

### `--resume` と使用量台帳（M-2）

台帳の行には `resumed`（`--resume` / `--resume-last` で走ったか）と `resume_of`（再開したスレッド）が
入る。`--resume` したターンの `turn.completed.usage` が**スレッド累計**で返る場合、台帳をそのまま
足すと 1 ターン目を二重計上するため、`codex_job.py usage` は既定（`usage_mode = cumulative`）で
同じ `thread_id` の行を時系列に並べ、`resumed` 行を**直前行との差分**（負なら 0）で計上する。

実機検証で「ターン単体で返る」と分かったら、`config/codex_bridge.json` の `usage_mode` を
`per_turn` にする（`codex_job.py usage --usage-mode per_turn` / 環境変数
`CODEX_BRIDGE_USAGE_MODE` でも切り替えられる）。この解釈は **[未確認]**。

### クレジット概算について

`credits_est` は `config/codex_pricing.json`（ChatGPT プランのクレジット単価 / 1M tokens）からの
**概算**であり、請求額ではない。計上ルールは以下の解釈に基づく（いずれも [未確認]。実機検証で確定させる）:

- `input_tokens` は `cached_input_tokens` を内包すると解釈し、非キャッシュ入力 = `input - cached`。
- `cache_write_input_tokens` は input 単価で加算。
- `reasoning_output_tokens` は `output_tokens` に内包されると解釈し、**二重計上しない**。
- `gpt-5.6-sol` の 100 / 10 / 500 は 2026-11-21 までの販促価格（定価相当は `list` を参照）。

---

## モックでの動作確認（Codex 不要）

モックの `usage` は架空の値なので、**本物の `var/` を汚さないよう `CODEX_BRIDGE_ROOT` で隔離する**
（`--mock` 実行は使用量台帳に追記しない実装だが、`var/locks/` は使うため）。`--cd` は git 管理下の
ディレクトリにしておく（管理外だと `--skip-git-repo-check` の警告が付く）。

```bash
WORK=$(mktemp -d) && git -C "$WORK" init -q          # 動作確認用の git リポジトリ
ROOT=$(mktemp -d) && mkdir -p "$ROOT/var"            # var/ の隔離先
CODEX_BRIDGE_ROOT="$ROOT" python3 scripts/codex_run.py \
  --mode task --job-dir /tmp/mockjob --cd "$WORK" --mock ok --prompt "テスト"
python3 scripts/codex_job.py result /tmp/mockjob     # 「モック実行」と明示される
```

`job.json` の `mock` フィールドにシナリオ名が入り、使用量台帳（`var/codex_usage.jsonl`）には
追記されない。台帳に `mock` 非 null の行が混ざっていても `codex_job.py usage` は集計から除外する。

| シナリオ | 検証できること |
|---|---|
| `ok` | file_change 2 件 + command_execution 2 件 + 非致命 error item + agent_message + `turn.completed`。実際に `<cd>/MOCK_TOUCHED.txt` を書く |
| `failed` | `turn.failed` → status=failed / exit 2 |
| `hang` | `thread.started` 後に無応答 → idle timeout でプロセスグループごと停止（exit 3） |
| `slow` | 1 秒おきにイベント → 壁時計 timeout（exit 3） |
| `exit0_no_turn` | イベント途中打ち切りで exit 0 → status=error（exit 1） |
| `schema` | `last.md` に schema 準拠 JSON → `structured_output` に載る |
| `garbage` | 非 JSON 行・不正 UTF-8 混入でも落ちず `warnings` に記録して完走 |
| `envdump` | 子プロセスの環境変数を `<cd>/MOCK_ENV.json` にダンプ（API キー削除の検証用） |
| `escape` | `setsid` で孫を残したまま無応答 → 壁時計 timeout でも `job.json` が出て終了する |
| `manycmds` | command_execution 60 件（55 件目だけ失敗）→ 上限 50 でも失敗は必ず残る |
| `manyfails` | command_execution 2,000 件が全部失敗 → 失敗側の上限 50 で打ち切り、`result` は 12,000 字以内 |
| `startup_error` | イベント無し + stderr のみで exit 2 → 原因が `errors` / `result` に出る |
| `partial_change` | file_change の in_progress / failed は `touched_files` に入れない |
| `toplevel_error` | top-level `error` のみで `turn.completed` 未到達 → status=failed |
| `error_then_complete` | top-level `error` の後に `turn.completed` → status=completed |

### テスト

```bash
python3 -m unittest discover -s codex-bridge/tests -v     # リポジトリルートから
# 単体で: python3 -m unittest discover -s tests -v （codex-bridge/ 内から）
```

86 ケース・約 35 秒。実バイナリを一切呼ばない（`var/` は `CODEX_BRIDGE_ROOT`、Codex 設定は
`CODEX_HOME` で一時ディレクトリへ逃がすため、本物の `var/` と `~/.codex/` は汚さない）。

---

## 契約後の実機検証チェックリスト

モックで検証できるのは配管まで。**2026-08-24 に実機（codex-cli 0.149.1 / ChatGPT Pro）で検証済み**。結果:

- [x] `codex --version` = 0.149.1。PATH 解決は cmux シムを除外し実バイナリ（nodenv 経由）を選択（warnings に除外記録）
- [x] `--model gpt-5.6-sol` / `terra` / `luna` すべて受理
- [x] `-c model_reasoning_effort=xhigh` 受理（sol/xhigh で確認）
- [x] `--output-schema` 実効（`structured_output` に schema 準拠 JSON。E2E レビューでも verdict/findings を取得）
- [ ] `exec review` の `--output-schema` 無視バグの再確認（未実施。現行方針＝review は通常 exec + review プロンプトで回避済みのため優先度低）
- [x] `turn.completed` の `usage` は 5 フィールド全部返る
- [x] `reasoning_output_tokens` は `output_tokens` に**内包**（出力 131 tok 中 reasoning 72、可視出力 121 バイトと整合）→ credits 計上の前提どおり
- [x] 並列 2 で token_invalidated（#26303）は**再現せず**（両方 completed・認証維持）。`--max-parallel` は 2 まで実績あり（それ以上は未検証）
- [x] テスト失敗を含む依頼で exit 0 かつ status=completed（command_execution failed を記録しつつ turn 正常完了）→ 成否判定は job.json で行う前提どおり
- [ ] 長時間タスクでの idle timeout 誤発火（未検証。実タスク投入時に `--idle-timeout-sec` を大きめから始める）
- [x] `--resume` 時の `turn.completed.usage` は**ターン差分**（累計ではない）→ `config/codex_bridge.json` を `usage_mode = "per_turn"` に切替済み
- [ ] `forced_login_method = "api"` で従量課金になることの確認（未実施。危険側の検証のため意図的にスキップ。`"chatgpt"` を設定済み）

検証ログ: セッション scratchpad `mikken/`（j1〜j7, e2e-*）。E2E: terra `--write` によるバグ修正（touched_files 1 件・テスト実過を統括側で検収）→ 構造化レビュー（approve / findings 0）。この日の消費合計 2.99 クレジット。
