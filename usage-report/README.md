# usage-report

指定ディレクトリの**子孫ディレクトリで実行された全 Claude Code セッション**（サブエージェント transcript を含む）を期間指定で集計し、**CSV 2枚 + PNG 最大4枚 + summary.md** を出力する CLI。

cost-manager が「1タスク単位の計測」なのに対し、本ツールは「**ディレクトリ配下 × 期間**の棚卸し」を担当する。dedup・単価・実処理時間の中核ロジックは `cost-manager/scripts/cost_lib.py` を import して再利用しており、複製していない。

## 使い方

```bash
python3 <toolbox>/usage-report/scripts/usage_report.py \
  [--root DIR]                  # 集計対象ルート。既定: カレントディレクトリ
  [--month YYYY-MM]             # JST 月次（例 2026-07）
  [--week this|last|YYYY-Www]   # JST 週次・月曜始まり（ISO週。例 2026-W33）
  [--from ISO] [--to ISO]       # 任意期間。JST 解釈（naive は JST）。[from, to) 半開区間
  [--out-dir DIR]               # 出力先の上書き
  [--format csv,png,md]         # 既定: 全部。カンマ区切りで部分選択
  [--no-active]                 # 実処理時間の算出をスキップ（同一 --format 条件で約1/2に短縮）
  [--label TEXT]                # 見出し・出力ディレクトリ名のラベル（既定: root の basename）
  [--summarize]                 # LLM による作業内容要約を有効化（既定: 無効）
  [--summarize-model MODEL]     # 要約に使うモデル（既定: haiku）
  [--summarize-timeout SEC]     # 要約の claude 呼び出しタイムアウト秒（既定: 300）
  [--no-summary-cache]          # 要約キャッシュを無視して再生成（書込はする）
  [--phase-gap-min MIN]         # フェーズ分割の空白しきい値（分。既定: 30）
```

- 期間フラグは排他（`--month` / `--week` / `--from`&`--to` のいずれか1系統）。`--from`/`--to` は両方必須。
- 期間フラグが無ければ**当月（JST）**を集計し、その旨を stdout に出す。
- 既定の出力先: `usage-report/reports/YYYY/MM/<YYYYMMDD-HHMM>-<label>-<期間ラベル>/`
- 終了コード: `0`=正常（0セッションでも summary.md を出せば 0） / `1`=引数・環境エラー / `2`=対象ディレクトリ不存在

例:

```bash
# 7月分（案件ルート配下すべて）
python3 .../usage_report.py --root ~/Documents/medirom/projects/Lav/git --month 2026-07
# 今週（カレントディレクトリ配下）
python3 .../usage_report.py --week this
# 期間指定・CSV だけ・高速
python3 .../usage_report.py --from 2026-07-01 --to 2026-07-15 --format csv --no-active
```

## 出力

| ファイル | 内容 |
|---|---|
| `sessions.csv` | 1行=1セッション（コスト降順、最終行に `TOTAL`）。utf-8-sig |
| `sessions_by_model.csv` | 1行=セッション×モデル。未知モデルは `known=false` |
| `summary_card.png` | 合計 USD/JPY・セッション数・実処理時間・高コスト Top3 のスタットカード（Top3 は**タイトル**を主行に出し、`--summarize` の LLM 要約があるときだけ小さい副行で要約を添える） |
| `daily_cost.png` | JST 日別 × モデル別の積み上げ棒（y=USD。長期間は週次/月次にビン化） |
| `repo_breakdown.png` | リポジトリ別コスト横棒（9件以上は上位8+その他） |
| `model_breakdown.png` | モデル別コスト横棒（固定モデル色） |
| `summary.md` | `## この期間の作業`（`--summarize` 時のみ）・合計表・リポジトリ別・モデル別・セッション一覧（各セッションの下に要約とフェーズ内訳）・`## 警告`・`## 注記` |
| `digests.json` | 決定論ダイジェスト（`--format` に関係なく常に出力。要約の入力材料そのもの） |

`sessions.csv` の列（19列）: `session_id, repo, title, first_cwd, start_jst, end_jst, active_time, api_calls, input_tokens, cache_write_5m, cache_write_1h, cache_read, output_tokens, total_tokens, cost_usd, cost_jpy, unknown_tokens, models, summary`

- `summary` は作業内容の要約。`--summarize` 時は LLM が書いた1文、無効時は決定論的な短縮形（`<主要ディレクトリ> を編集(Nファイル) / テストM回 / ブランチ: …`）。

- `unknown_tokens` は単価未収載モデルのトークン数（その行のコストが過小である量）。`models` 列では未収載モデルに `*` が付く。
- 行順は「明細行（コスト降順）→ `# 注記` 行（1〜3本）→ `TOTAL`」。`TOTAL` は文字どおり最終行なので `tail -1` で合計が取れる。注記には行ごとの丸め（明細の SUM は TOTAL 行と端数分ずれる）・`active_time` の重なり・未計上トークン（あれば）を書く。
- `start_jst` / `end_jst` は同一 API 応答（requestId グループ）の窓内 timestamp の最小・最大。dedup 採用行はストリーミング最終行なので、その ts だけを使うと開始が最大1分ほど後ろにずれる。
- `active_time` はセッション単位の実処理時間。並行実行や fork のコピー分が重なるため、列を SUM しても TOTAL 行（全セッションの union）とは一致しない（常に TOTAL 以上）。

## 作業内容サマリ

`ai-title` は会話の早い段階で決まり以降ほぼ更新されないため、Bash 200 回・Edit 50 回規模のセッションでは実作業を表さない。そこで2段構えで「実際に何をしたか」を出す。

1. **決定論ダイジェスト（常時・無料）** — `scripts/digest.py`。メイン jsonl を1パス走査し、人間の発話・編集ファイル/ディレクトリ・Bash コマンドの分類（test/git/build/other）・ブランチ・サブエージェント・スキル・参照 issue/PR・フェーズ分割を抽出して `digests.json` に書く。`sessions.csv` の `summary` 列と `summary.md` にはこの決定論的な短縮形が入る。
2. **LLM 要約（`--summarize` でオプトイン）** — `scripts/summarize.py`。未キャッシュ分をまとめて `claude -p` に渡し、セッションごとの1文要約・フェーズ内訳・期間全体の総括（`## この期間の作業`）を得る。呼び出しは**セッション数によらず最小限のバッチ数**（1バッチ最大15セッション / 300KB）で、100 セッション超の期間でも人間の発話を削らずに済ませる（実測: 133 セッションで6回）。プロンプト先頭に対象期間を明記し、総括が期間を言い換えないようにしている。

- **フェーズ分割**は決定論的に行う（`gitBranch` の変化、または直前レコードとの時刻差が `--phase-gap-min` 分以上）。フェーズが1つしかないセッションは内訳を出さない。
- **キャッシュ**は `usage-report/var/summaries/<key[:2]>/<key>.json`。キーは `session_id + メイン jsonl の mtime/size + 期間 + モデル + プロンプト版 + **ダイジェスト本文のハッシュ**` の SHA-256 なので、transcript が伸びたときも `--phase-gap-min` を変えて材料（フェーズ分割）が変わったときも自然に無効化される。全件ヒットすれば `claude` を呼ばない（実測: 初回 約112秒 → キャッシュヒット 約12秒）。
- **要約の入力にハーネス注入メッセージを混ぜない**。人間の発話の判定は `usage_lib.is_human_utterance`（= `cost_lib._is_human_prompt` + `_is_caveat`）を再利用する。
- **エージェント自身の作業メモは材料から外す**。`.claude/` 配下でもハーネスが管理する内部ディレクトリ（`projects` / `plans` / `todos` / `memory` / `shell-snapshots` / `history` / `statsig` / `logs` / `ide`）と `/tmp/claude-*/**/scratchpad/**` への編集だけを `files` / `dirs` から外し、件数を `agent_files_total` に残す（これらが上位を占めると実ソースが押し出され、要約が「何をしたか」を表さなくなる）。`~/.claude/CLAUDE.md` や `~/.claude/skills/**` は実作業の成果物なので**除外しない**。
- **worktree のパスは元リポジトリへ正規化する**。EnterWorktree が作る `<repo>/.claude/worktrees/<name>/…` は元リポジトリの同じファイルを別パスで編集しているだけなので、`.claude/worktrees/<name>/` を取り除いたパスで `files` / `dirs` に積む（worktree ごとにディレクトリが散らばるのを防ぐ）。
- `claude` が無い / 失敗 / タイムアウト / 出力が JSON として読めない場合は、**警告を1件積んで要約なしで続行**する（exit 0 のまま、レポートは従来どおり出る）。LLM の返す JSON は型が崩れうる（`phases` が数値・文字列など）ため、型は容器から検査し、想定外の例外も呼び出し側で受けて決定論ダイジェストに縮退する。
- **`--summarize` は人間の発話（各250字）と Bash コマンド例を `claude -p` へ送る**。API キー・トークン等を発話やコマンドに書いていた期間では、それが外部モデルへ渡り、`digests.json`（レポート出力先）と `var/summaries/` のキャッシュにも平文で残る。共有前に中身を確認すること。

## 集計の考え方

- **セッションの帰属は cwd が正**。`~/.claude/projects` のディレクトリ名（`encode_cwd` の非可逆エンコード）は候補の絞り込みヒントにしか使わず、確定判定はメイン jsonl の**最初の `cwd`** が `--root` 配下かどうかで行う。候補が1件も無いときは警告を出す（`--root /` のような浅いパスも `-` で始まる全ディレクトリを候補にして正しく拾う）。
- **サブエージェント transcript を必ず含める**。実測（Lav/git 2026-07 を main のみ／subagents のみに分けて集計）では、サブエージェント側が**コストの 31.6%**（$721.28 / $2,282.76）・**トークンの 44.0%**・**dedup 後 API 呼び出し件数の 82.2%** を占める（2026-W33 でも 32.7% / 44.1% / 72.2%）。7割前後になるのは API 呼び出し件数（実測 72〜82%）で、コストは3割強・トークンは4割強。落とすと無視できない量が欠ける。`<sid>/subagents/**/agent-*.jsonl` を全候補ディレクトリから収集する（worktree 移動でサイドカーが別プロジェクトディレクトリに散るため）。`journal.jsonl` は対象外。
- **dedup は全セッションで単一の `Accumulator` を共有**。requestId（無ければ uuid）単位で `output_tokens` 最大の行を採用し、uuid はグローバルに排除する（resume / fork のファイル横断重複対策）。
- 期間窓は行の `timestamp` で `[since, until)` を判定。timestamp を持たない課金行は除外し、件数を警告に出す。
- **日別集計は JST に変換してから日付を切る**（UTC のまま切ると深夜帯が前日に落ちる）。
- 単価適用日は `min(今日(JST), 期間終端日(JST))`。sonnet-5 の導入価格（〜2026-08-31）が期間に応じて正しく効く。**期間全体で1つ**なので、単価改定日をまたぐ `--from`/`--to` では全日が片側の単価になる（検出して警告に出す。正確に出すなら改定日で期間を分けて2回実行する）。
- **コストは全モデルを従量課金単価で仮計算した参考値**。billing（payg/included）区分は使わない（Fable 等は実際にはサブスク込み）。
- `pricing.json` に単価が無いモデルは $0 計上になる（新モデルが出て単価が追記されるまでの間に起きる）。その場合は**未計上トークン数と比率**を stdout・summary.md・summary_card.png・sessions.csv に出す（合計コストはその分だけ過小）。解消するには cost-manager の `config/pricing.json` に単価を追記する（人手の運用作業。このツールのコードからは書き換えない）。
- **警告（`## 警告`）と注記（`## 注記`）は分けて出す**。警告は「異常・要注意」（未収載モデル・fork/resume の帰属・timestamp 欠落・単価改定日またぎなど）、注記は「異常ではないが前提として知っておくべきこと」。毎回出る文を警告に混ぜると本物の警告が薄まるため、出力面（summary.md のセクション / stdout の `警告:` と `注記:`）で分離している。
- 単価表の `as_of` より前の期間を集計する場合、当時の実単価ではなく現在の単価表で計算している旨を**注記**に出す（警告ではない）。判定は期間の**開始日** < `as_of`（終端で見ると `--from 過去日 --to 今日` のような期間で落ちる）。summary.md の `## 注記` には、これに加えて「表中の $ は2桁丸めのため内訳の足し上げが合計と数セントずれる」が常時出る。
- fork/resume でコピーされた課金行は**先に作成された transcript のセッション**に寄せる（走査順をファイル作成時刻の昇順に固定）。合計・モデル別・日別は影響を受けないが、per-session 値はこの規則に依存するため警告に出る。
- `daily_cost.png` は日数が多いとビン化する（62日超=週次、400日超=月次）。

## 依存

- python3 標準ライブラリ
- `cost-manager/scripts/cost_lib.py`（import 再利用。cost-manager 側のファイルは変更しない）
- matplotlib（**任意**。無ければ PNG をスキップして CSV/md のみ生成し exit 0）

## 制約

- `~/.claude/projects` は読み取り専用。書込先は `usage-report/reports/`（`--out-dir` 指定時はその場所）と、LLM 要約のキャッシュ `usage-report/var/summaries/` のみ（`var/**` は git 管理外）。
- セッション途中で cwd が変わっても帰属はセッション単位（起動 cwd）で決める。root 外の cwd が現れた場合は警告に出す。
- 同じ cwd で走った無関係なセッションは区別できない（cost-manager の `--scope global` と同型の制約）。
