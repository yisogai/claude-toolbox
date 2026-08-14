# usage-report 実装規約

## 依存
- **python3 標準ライブラリ + `cost_lib`（import）+ matplotlib（任意依存）のみ**。Pillow は使わない。他の外部パッケージを追加しない。
- matplotlib は import 失敗時に PNG をスキップして CSV/md のみ生成する degrade を必ず維持する（exit 0）。チャート描画コードは `scripts/charts.py` に隔離し、`usage_lib.py` から import しない。
- `scripts/digest.py` / `scripts/summarize.py` は**標準ライブラリ + cost_lib のみ**（matplotlib に依存しない）ので `usage_lib.py` から使ってよい。ただし `digest.py` は `usage_lib` を import するため、`usage_lib` 側の import は**関数内の遅延 import** にして循環を避ける。

## cost-manager との関係
- 集計の中核（`iter_usage` / `Accumulator` / `aggregate` / `resolve_model` / `rate_for` / `scan_activity` / `active_seconds` / `atomic_write_*` / `parse_iso` / `to_jst` / `fmt_*`）は `cost-manager/scripts/cost_lib.py` を import して使う。**ロジックを複製しない**（窓フィルタなど、どうしても足りない小物のみ自前実装する）。
  - 例外: `usage_lib.first_real_user_text` は `cost_lib.find_first_user_text` に近い自前実装。cost_lib 側は候補を1件しか返さず、skip 述語（ハーネス注入メッセージを飛ばす条件）を渡す口が無いため、先頭が `<local-command-caveat>` 等だとタイトルが常に `(タイトルなし)` になる。cost-manager のコードは変更しない方針なので、こちらに複数候補走査を持つ。ただし抽出・時刻判定・人間プロンプト判定（`lib._extract_text` / `lib.parse_iso` / `lib._is_human_prompt`）は cost_lib のものを再利用し、判定ロジック自体は複製しない。
- **このツールのコードから cost-manager 配下のファイルを書き換えない**（読むだけ）。単価・為替も cost-manager の `config/pricing.json` / `config/config.json` を読む（このツールは独自の config を持たない）。
  - `config/pricing.json` への新モデル単価の追記は、**人手の運用判断として可**（実際 2026-08-14 に `claude-opus-5` を追記した）。その場合もコードは不変で、`as_of` の更新を伴う。

## 書込・読取
- `~/.claude/projects` は**読み取り専用**（書込・削除・touch 一切禁止）。
- **偽 `claude` を使う縮退テストは必ず `USAGE_REPORT_VAR_DIR` でキャッシュを隔離する**。本番キャッシュ（`usage-report/var/summaries/`）にスタブ要約が残ると、以降の `--summarize` が黙ってそれを再利用し、捏造要約がレポートに出る（実際に検証中へ 176 件混入した）。テスト出力は `--out-dir`、キャッシュは `USAGE_REPORT_VAR_DIR` で、両方を作業用ディレクトリへ向けること。
- 書込先は `usage-report/reports/`（`--out-dir` 指定時はその場所）と、LLM 要約のキャッシュ `usage-report/var/summaries/`（`USAGE_REPORT_VAR_DIR` で差し替え可）のみ（`var/**` は git 管理外）。それ以外の実行時状態を `var/` に置くときも同じ規律（atomic write・キー付きサブディレクトリ）に従う。書込は必ず `lib.atomic_write_text` / `lib.atomic_write_bytes` 経由（CSV は utf-8-sig でエンコードしてから `atomic_write_bytes`、PNG は `savefig` → BytesIO → `atomic_write_bytes`）。

## 時刻
- 内部は UTC aware、表示は JST（`lib.JST`）。`zoneinfo` は使わない（cost_lib に合わせる）。
- 期間は半開区間 `[since, until)`。**日別集計は JST に変換してから日付を切る**（UTC のまま切ると深夜帯が前日に落ちる）。

## 集計の不変条件
- dedup 用の `Accumulator` は**全セッションで単一インスタンスを共有**する（resume/fork のファイル横断重複を潰すため）。セッション別の集計は `acc.rows()` を `source_file → session_id` で振り分けて得る。
- 同じ transcript ファイルを2回 `iter_usage` に流さない（uuid 欠落行が二重計上されうる）。
- **走査順はファイル作成時刻の昇順に固定する**（`_file_order_key`）。ディレクトリ列挙順のままだと fork/resume でコピーされた行の帰属セッションが走査順で入れ替わる（合計は不変でも per-session が変わる）。
- `lib.aggregate` のバケツは生モデル名。日別・モデル別・sessions_by_model は必ず **resolve 名で合算**する（`_merged_models`）。同名バケツを後勝ちで捨てない。
- 単価未収載モデルの $0 計上は、警告だけでなく **summary_card.png / summary.md 合計表 / sessions.csv** にも未計上トークン数と比率を出す。
- セッションの帰属判定は**メイン jsonl の最初の `cwd`**。`encode_cwd` のディレクトリ名は候補絞り込みのヒントにしか使わない（非可逆・誤マッチしうる）。
- サブエージェントは `<sid>/subagents/**/agent-*.jsonl` のみ収集する（`journal.jsonl` を含めない）。
- **タイトル候補はメイン jsonl のみ**から採る（`first_real_user_text` に渡すのは `s.main_path`。孤児 subagent セッションだけ例外）。サブエージェント transcript を混ぜると、ハーネスがエージェントへ渡す指示文（`[SYSTEM NOTIFICATION …]` / `The coordinator sent a message …` / `[structured-output-enforce] …`）が候補を占めてタイトルに漏れる。候補の窓判定は集計側と同じ**半開区間 `[since, until)`** に揃える。
- ハーネス注入タグの判定 `_HARNESS_TAG_RE` は「**ハイフンまたはアンダースコアを含む**小文字始まりタグ」に限定する（`^<[a-z][a-z0-9]*[_-][a-z0-9_-]*(?:\s[^>]*)?/?>`）。区切り文字を必須にしないと `<div>` `<html>` のような素の HTML タグで始まる人間の発話まで弾いてしまう。
- 見出し行だけを採る処理は **`# /` 始まり**（スラッシュコマンドの展開本文）に限定する。`#` 始まり全般に広げると、人間が書いた見出し付き発話の本文まで落ちる。
- **警告（`Agg.warnings`）と注記（`Agg.notes`）を混ぜない**。警告は異常・要注意だけに使い、毎回出る前提の説明は注記に入れる（summary.md の `## 注記` / stdout の `注記:`）。混ぜると本物の警告が薄まる。

## 作業内容サマリ
- 第1段（`digest.py`）は**常時実行・決定論**。同一条件の再実行で `digests.json` を含めバイト一致させる（辞書は `sort_keys=True`、頻度順の同数は名前昇順、レポート生成時刻は入れない）。
- 第2段（`summarize.py`）は `--summarize` でのみ動く。`claude` 不在・非ゼロ終了・タイムアウト・パース失敗は**すべて警告1件で縮退**し exit 0 を保つ（要約が無いだけのレポートを出す）。
- `claude -p` は**バッチ**で呼ぶ（起動オーバーヘッドが17〜26秒あるためセッションごとの逐次呼び出しは不可）。1バッチは最大15セッション / 300KB で、超える規模は**発話を削らずにバッチを増やす**（発話は「何をしたか」を最も直接示す材料。削るのは1セッション単体が1バッチに収まらないときだけで、その場合は警告を1件積む）。`stdin=subprocess.DEVNULL` と `errors="replace"` を必ず付ける（stdin 待ちの数秒／不正 UTF-8 の stdout で全滅するのを避ける）。
- LLM 出力の型は**容器から検査**する（`phases` が数値・文字列単体・辞書でも落ちない・1文字ずつに分解しない）。加えて `usage_report.py` 側で要約処理全体を `except Exception` で受けて縮退する。型検査の網羅に頼らない。
- 要約キャッシュのキーには**ダイジェスト本文のハッシュ**を含める（`--phase-gap-min` を変えると材料が変わるのに mtime/size は変わらないため）。
- **エージェント自身の作業メモ（`.claude/{projects,plans,todos,memory,shell-snapshots,history,statsig,logs,ide}/**`・`/tmp/claude-*/**/scratchpad/**`）は `files`/`dirs` に入れない**（件数だけ `agent_files_total`）。要約の材料が実ソースから逸れる。**除外を `.claude/` 全体に広げない**（`~/.claude/CLAUDE.md`・`skills/**` は作業の成果物、`<repo>/.claude/worktrees/<name>/**` は実ソースそのもの）。worktree のパスは `digest.normalize_path` で元リポジトリのパスへ正規化してから積む。
- コマンド分類の前に**ヒアドキュメント本文を落とす**（`strip_heredocs`）。分割が改行も見るため、コミットメッセージ本文の行がコマンドとして数えられる。
- `sessions.csv` の自由文セル（`title` / `summary`）は先頭が `= + - @` なら `'` を前置する（表計算の数式評価を防ぐ）。
- 要約の入力に**ハーネス注入メッセージを混ぜない**。人間の発話判定は `usage_lib.is_human_utterance`（`cost_lib._is_human_prompt` + `_is_caveat` の再利用）だけを使い、判定を複製しない。
- 要約は**メイン jsonl のみ**から作る（サブエージェント transcript には人間の発話が無く、エージェント向け指示文が混じる）。窓は集計側と同じ半開区間 `[since, until)`。
- プロンプトを変えたら `summarize.PROMPT_VERSION` を上げる（キャッシュキーに含まれる）。
- コマンド分類は**先頭トークン基準**（部分文字列マッチ禁止）。先頭の環境変数代入・ラッパー（`env` `sudo` `fvm` `npx` 等）とパスの basename だけは正規化する（実データは `bin/rspec` / `TZ=… fvm flutter test` が大半で、素朴な第1トークン判定では全部 other に落ちる）。
- `sessions.csv` に列を足すときは**注記行・TOTAL 行のパディング列数**も必ず合わせる（現在19列）。「TOTAL が最終行」「注記行は明細と TOTAL の間」を壊さない。

## チャート
- ダークテーマ固定（surface `#1a1a19` / text `#ffffff` / secondary `#c3c2b7`）。`matplotlib.use("Agg")` を import 直後に呼ぶ。`font.family = "Hiragino Sans"`、`dpi=200`。
- **モデル色は固定割当**（ファミリー基調色 + モデル名から決まる固定の濃淡）。出現順・ランクで塗らない。
- 二軸グラフ禁止。全点数値ラベル禁止（選択的直接ラベルのみ）。積み上げ・隣接バーは surface 色 2px の隙間で分離する。
- 描画後に**バイト長 > 0 と PNG ヘッダの寸法**をコードで検査してから書き出す。

## テスト・検証
- 実データでの検証は `--out-dir` を scratchpad に向けて行い、`reports/` を汚さない。
- 合成 transcript での検証は `FCM_PROJECTS_DIR` を偽の projects ディレクトリに向ける（実データを触らない）。
