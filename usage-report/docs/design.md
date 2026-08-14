# usage-report 設計（v1・実装済み）

本書は実装後の状態を記述する。仕様と実装が食い違ったら**実装が正**。

## 目的
指定ディレクトリ（既定 cwd）の**子孫ディレクトリで実行された全 Claude Code セッション**を期間指定（月/週/任意）で集計し、トークン使用量・従量課金換算コスト（USD/JPY）・作業内容サマリを **CSV 2枚 + PNG 最大4枚 + summary.md** に出力する。

## 非目標
- リアルタイム監視・予算アラート（cost-manager の領分）
- 走査結果のキャッシュ（v2 候補。現状は実測 10 秒程度なので不要）

## 構成
```
usage-report/
├── SKILL.md            # グローバルスキル定義
├── README.md           # 使い方・出力仕様
├── CLAUDE.md           # 実装規約
├── .gitignore
├── docs/design.md      # 本書
├── scripts/
│   ├── usage_report.py # CLI エントリ（argparse + 出力オーケストレーション）
│   ├── usage_lib.py    # 期間解決・スコープ列挙・帰属・集計・CSV/md 生成
│   ├── digest.py       # 決定論ダイジェスト抽出（標準ライブラリのみ）
│   ├── summarize.py    # LLM 要約 + キャッシュ（標準ライブラリのみ）
│   └── charts.py       # matplotlib 描画（任意依存を隔離）
├── reports/.gitkeep
└── var/summaries/    # LLM 要約のキャッシュ（git 管理外。第1段だけなら書き込まない）
```

## 処理の流れ

1. **期間解決**（`usage_lib.resolve_period`）
   - `--month` / `--week` / `--from`&`--to` は排他。どれも無ければ当月（JST）で `Period.defaulted=True`。
   - すべて JST で境界を作り、内部は UTC aware の半開区間 `[since, until)` にする。
   - 単価適用日 `at = min(今日(JST), 期間終端日(JST))`（`pricing_at`）。sonnet-5 の導入価格（〜2026-08-31）が期間に応じて効く。
   - `at` は期間全体で1つなので、単価改定日（`intro.until`）をまたぐ期間では全日が片側の単価になる。`pricing_boundaries_in_period` で検出し、対象モデル・改定日・`at` を警告に出す（正確に出すなら改定日で期間を分けて2回実行する）。

2. **候補プロジェクトディレクトリの絞り込み**（`candidate_project_dirs`）
   - `enc = lib.encode_cwd(root)` として、`~/.claude/projects` 直下で `name == enc` または `name.startswith(enc + "-")` のディレクトリ。
   - これは**誤マッチしうる**（`/…/git-old` も拾う）。確定判定は次の cwd 検証で行う。
   - 前置詞は `enc` が `-` で終わるなら `enc` そのもの、でなければ `enc + "-"`。`--root /` は `enc == "-"` になり、素朴に `enc + "-"` とすると `--` 始まりのディレクトリしか拾えず**無警告で 0 セッション**になるため（実在するのは `-Users-...`）。
   - 候補が 0 件のときは警告を出す（履歴が無いのか、絞り込みが外れたのかを読み手が判別できるように）。

3. **セッション帰属の確定**（`collect_sessions`）
   - 候補ディレクトリ直下の `*.jsonl` がメインセッション。1パスで走査し（`_scan_main_meta`）、
     - **最初の `cwd`**（起動ディレクトリ）→ `root` 配下なら採用
     - 全 `cwd` の集合 → root 外が混じっていれば警告フラグ
     - 最後の `ai-title` / `last-prompt` → タイトル候補
   - cwd の抽出は正規表現（全行 `json.loads` を避ける）、タイトルは substring 事前判定してから `json.loads`。
   - タイトルの優先順:
     1. 最後の `ai-title.aiTitle`
     2. `last-prompt.lastPrompt`（`_clean_title` のフィルタを通す。注入メッセージなら破棄して次へ）
     3. `usage_lib.first_real_user_text`（採用セッションのみ・遅延評価）— 窓内のユーザーメッセージを古い順に最大 `max_scan=60` 件見て、(a) 人間の発話（`lib._is_human_prompt`）で注入系でないもの → (b) `<scheduled-task name="X">` から作る `(定期実行) X` → (c) それ以外（isMeta のスラッシュコマンド展開本文など）の順に採る
     4. `(タイトルなし)`
   - タイトル候補を採る対象は**メイン jsonl のみ**（`s.main_path`。孤児 subagent セッションのみ例外的に `tfiles`）。サブエージェントの transcript には人間の発話が無く、ハーネスがエージェントへ渡す指示文（`[SYSTEM NOTIFICATION …]` / `The coordinator sent a message …` / `[structured-output-enforce] …`）が user ロールで入るため、混ぜると候補列がそれで埋まりタイトルに漏れる。
   - 候補の窓判定は集計側と同じ**半開区間 `[since, until)`**（`ts < since` と `ts >= until` を捨てる）。右端を閉区間にすると期間の境界行がタイトル候補に混じる。
   - 注入メッセージの判定は個別列挙ではなく、**「ハイフンまたはアンダースコアを含む小文字始まりのタグで始まる」正規表現**（`_HARNESS_TAG_RE` = `^<[a-z][a-z0-9]*[_-][a-z0-9_-]*(?:\s[^>]*)?/?>`。`<task-notification>` `<scheduled-task ...>` `<bash-input>` `<ide_opened_file>` 等を一括で弾く）＋タグを持たない定型文のプレフィックス列（`Stop hook feedback:` / `[Request interrupted` / `Base directory for this skill:` 等）で行う。種類は Claude Code の更新で増えるため列挙では追随できないが、区切り文字を必須にすることで `<div>` `<html>` のような素の HTML タグで始まる人間の発話は巻き込まない。
   - **`# /` で始まる**テキスト（スラッシュコマンドの展開本文）についてのみ、**見出し行だけ**を採る（コマンド定義 Markdown の本文がタイトルにダンプされるのを防ぐ）。`#` 始まり全般に広げると、人間が書いた見出し付き発話の本文まで落ちてしまうため、`# /` に限定する。
   - `cost_lib.find_first_user_text` は候補を1件しか返さず、先頭が注入メッセージだと常に `(タイトルなし)` になるため使わない（`first_real_user_text` を自前で持つ理由は `CLAUDE.md` 参照）。
   - サブエージェント: 採用 `sid` について**全候補ディレクトリ**の `<dir>/<sid>/subagents/**/agent-*.jsonl` を収集（worktree 移動でサイドカーが別ディレクトリに散るため）。`journal.jsonl` はパターン上マッチしない。
   - **孤児 subagent**: 採用セッションに無い `<uuid>/subagents/**/agent-*.jsonl` で、先頭の cwd が root 配下のものを `session_id=uuid` の部分セッションとして計上し、警告に載せる。root 外なら無視。

4. **usage 集計**（`aggregate_all`）
   - 全 tfile を `lib.iter_usage(path, 0)` で読み、`timestamp` が窓内の行だけを**単一の `lib.Accumulator`** に `add()`（全セッションで共有）。timestamp 欠落・不正行は捨てて件数を数える。
   - 同一パスを2回流さないよう `seen_paths` で抑止（uuid 欠落行の二重計上防止）。
   - **走査順はファイル作成時刻の昇順に固定**（`_file_order_key`）。fork/resume の子 jsonl には親の履歴が uuid ごとコピーされており、`Accumulator` は「先に走査した側」に行を帰属させるため、走査順が変わるとセッション別金額が入れ替わる（合計は不変）。作成時刻順にすることで、実行間で結果が変わらず、かつコピー元＝親セッションに帰属する。件数と対象 session_id は警告に出す。
   - `acc.rows()` を `source_file → session_id` で振り分け、セッション別 rows と全体 rows を得る。
   - 料金は `lib.aggregate(rows, pricing, at, usd_jpy)` をセッション別・全体・日別（JST 日付 × モデル）で適用。
   - `lib.aggregate` のバケツは**生モデル名**なので、resolve 後に同名となる複数バケツ（`claude-sonnet-5` と `claude-sonnet-5-20260101` など）が並びうる。日別・モデル別・sessions_by_model は必ず resolve 名で**合算**する（`_merged_models`）。辞書内包表記の後勝ちで一方を捨てると、合計表と日別グラフが食い違う。
   - `start_jst` / `end_jst` は**採用行そのものの ts ではなく、同一 dedup キー（requestId）グループの窓内 ts の min/max** から取る（`key_span`）。`Accumulator` が採用するのはグループ中 `output_tokens` 最大の行＝ストリーミング最終行なので、採用行だけ見ると開始が数秒〜1分後ろにずれる。キーは `(dedup キー, session_id)` にして、fork でコピーされた親の行が子の開始時刻を前へ引っ張らないようにする。
   - 実処理時間は `lib.scan_activity` → `lib.active_seconds`（セッション別と、全 tfile をまとめた全体値）。全体は interval union なので並行実行を二重計上しない。`--no-active` でこの走査を丸ごと省く（**同一 `--format` 条件で約1/2**。実測は下記「実測」節）。

5. **作業内容サマリ**（`digest.py` / `summarize.py`）
   - `ai-title` は会話の早い段階で決まり以降ほぼ更新されない（実測: 1セッションで123〜146回記録されるが値はユニーク1〜2種）。Bash 200 回・Edit 50 回規模のセッションではタイトルが実作業を表さないため、**タイトルとは独立に「何をしたか」を出す**。
   - **第1段（常時・決定論・無料）**: `digest.build_digest` が**メイン jsonl のみ**を1パス走査し、人間の発話（最大40件×300字）・編集ファイル/ディレクトリ・Bash コマンドの分類と代表例・ブランチと切替回数・サブエージェント・スキル・参照 issue/PR・ツール頻度・フェーズを抽出する。窓は集計側と同じ半開区間 `[since, until)`。結果は `digests.json`（`--format` に関係なく常に出力）と `sessions.csv` の `summary` 列・`summary.md` に入る。
     - 走査は「まず正規表現で `timestamp` / `gitBranch` を拾い、詳細が要る行（`user` か `tool_use` を含む行）だけ `json.loads`」する。全行 `json.loads` はメイン jsonl だけで数百 MB あり実行時間が跳ねる（実測の追加コストは約1.5秒）。
     - 人間の発話判定は `usage_lib.is_human_utterance`（`cost_lib._is_human_prompt` + `_is_caveat`）に集約し、判定を複製しない。これによりハーネス注入メッセージ（`<task-notification>` 等）が要約の入力に混ざらない（実データ216件で混入0）。
     - コマンド分類は**先頭トークン基準**。ただし先頭の環境変数代入とラッパー（`env` `sudo` `time` `npx` `fvm` `bundle exec` 等）を読み飛ばし、パスの basename を取る正規化だけ行う。実データは `bin/rspec …` / `TZ=Asia/Tokyo fvm flutter test …` / `RAILS_ENV=… bin/rake …` が大半で、素朴な第1トークン判定では test/build がほぼ 0 件に落ちる（実測で確認）。
     - **フェーズ分割**は `gitBranch` の変化、または直前レコードとの時刻差が `--phase-gap-min` 分（既定30）以上で区切る。フェーズが1つしかないセッションは `phases` を空にする（要約側の入力を減らすため）。
     - **エージェント自身の作業メモは `files` / `dirs` から除外**する。件数だけ `agent_files_total` に残す。実測（Lav/git 2026-07）では除外前、21セッション中7件で「主要ディレクトリ」がメモリ / scratchpad になり、LLM に渡す上位10ファイルの45%がこれらだった。tmp 側は `scratchpad` セグメントを必須にして、tmp 配下に置いた作業対象リポジトリを誤除外しない。
     - 除外の範囲は **`.claude/` 配下のハーネス内部ディレクトリだけ**（`projects` `plans` `todos` `memory` `shell-snapshots` `history` `statsig` `logs` `ide`）。`/.claude/` を含むパスを一律に落とすと、`~/.claude/CLAUDE.md` `~/.claude/skills/**`（設定・スキル整備そのものが作業）や、EnterWorktree の `<repo>/.claude/worktrees/<name>/**` の実ソースまで消える。実測（Lav/git 2026-07）で一律除外にすると除外183件中83件が worktree の実ソースで、3セッションは残ファイル0件になり、`summary` 列が「作業メモ31ファイル」のような事実と異なる表現になっていた。
     - **worktree のパスは元リポジトリへ正規化**する（`<repo>/.claude/worktrees/<name>/x` → `<repo>/x`）。同じファイルの編集が worktree ごとに別ディレクトリへ散らばるのを防ぎ、`dirs` 集計を実リポジトリの構造に一致させる。
     - **ヒアドキュメント本文はコマンドとして数えない**。`_CMD_SPLIT_RE` は改行でも分割するため、`git commit -F - <<'EOF' … EOF` の本文の各行が1コマンド扱いになり、本文中の `flutter test` 等で test 回数が水増しされていた。分類前に `strip_heredocs` で本文（開始行の次〜終端タグ行）を落とす。
   - **第2段（`--summarize` でオプトイン）**: `summarize.summarize` が未キャッシュ分をまとめて `claude -p --model <model> --output-format text` に渡す。`claude -p` は1回あたり17〜26秒の起動オーバーヘッドがあり、セッションごとに逐次呼ぶと非現実的（21セッションで9分）。`stdin=subprocess.DEVNULL` を付けないと stdin 待ちで数秒無駄になる。出力は ```` ```json ```` フェンスで包まれることがあるため、フェンス剥がし → `json.loads` → 「最初の `{` から最後の `}`」フォールバックの順で解釈する。
     - **バッチ分割**: 1回の呼び出しは最大15セッション / 300KB（argv 長と応答 JSON が途中で切れるリスクの両方を抑える）。これを超える規模は**発話を削らずに複数回**呼ぶ（実測: 133セッションで6回）。以前は発話件数を 20→8→3→0 と落としていたが、`--root ~/Documents --month 2026-07` では pmax=0（発話ゼロ）が採用され、材料が無警告で消えていた。1セッション単体が1バッチに収まらない場合だけ発話件数を落とし、そのときは警告を1件積む。
     - **プロンプトに対象期間を明記**する（`対象期間: since 〜 until（JST・終端は含まない）`）。入れないと総括がフェーズの時刻から期間を推測し、月次レポートを「2週間」と誤記する（実測）。各セッションの `稼働時刻` も渡す。
     - **多フェーズは主要フェーズだけ渡す**（`select_phases`、最大12件。活動量順に採り時系列に戻す決定論選択）。全件渡しても返るのは数行で、入力だけ膨らむため。summary.md 側は「ダイジェスト N 件 → 要約 M 件」と対応を明示する。
     - **材料に無い固有名詞を禁じる**（`PROMPT_VERSION` 3 で追加）。「推測で埋めない」だけでは足りず、モデルは文脈から技術名を補完する。実測（Lav/git 2026-07・haiku・21セッション）で、要約中の英字トークンがダイジェスト本文に出現するかを機械検査したところ、禁止前は 3/21 件が裏付けの無い語を含んでいた（Android のテストという材料だけから `RxJava`、`アプリ側` から `iOS`/`Flutter`、`Rails 7.2` から `LTS`）。ライブラリ・フレームワーク・プラットフォーム・製品・バージョン番号を材料外で書かないよう明示したところ **0/21 件**になり、平均文字数は 70→52 字に縮んだが具体性は落ちていない（削れたのは捏造部分で、実在の対象はむしろ細かくなった）。
     - **LLM 出力の型は容器から検査する**（`_phases_from`）。`phases` が数値・真偽値・文字列単体・辞書で返っても落ちず、1文字ずつ分解もしない。加えて `usage_report.py` 側で要約処理全体を `except Exception` で受け、決定論ダイジェストに縮退する（型検査の網羅に頼らない二重化）。`subprocess.run` は `errors="replace"` を付ける（不正 UTF-8 の stdout で `UnicodeDecodeError` が出るとレポートが1件も出せなくなるため）。
   - **縮退**: `claude` 不在（`shutil.which`）・非ゼロ終了・タイムアウト（既定300秒）・パース失敗・スキーマ不一致は、すべて**警告を1件積んで要約なしで続行**する（exit 0 を維持）。個々のセッションが応答から欠けた場合も、そのセッションだけ要約なしにして全体は捨てない。
   - **キャッシュ**: `var/summaries/<key[:2]>/<key>.json`。キーは `session_id + メイン jsonl の mtime_ns/size + 期間の since/until + モデル + PROMPT_VERSION + ダイジェスト本文のハッシュ + 発話件数上限` の SHA-256。ダイジェスト本文を含めるのは、`--phase-gap-min` を変えると LLM への入力（フェーズ分割）が変わるのに mtime/size は変わらず、古い要約が黙って再利用されるため。transcript が伸びれば自然に無効化される。総括は全セッションキーから別キーで持つ。全件ヒットなら `claude` を呼ばない（実測: 初回112秒 → キャッシュヒット11.6秒）。プロンプトを変えたら `PROMPT_VERSION` を上げる。

6. **出力**
   - CSV は `csv.writer`（CRLF）で組み立て、utf-8-sig にエンコードして `lib.atomic_write_bytes`。
   - PNG は `savefig` → BytesIO → ヘッダ検査（`png_size`）→ `lib.atomic_write_bytes`。
   - summary.md / digests.json は `lib.atomic_write_text`。
   - `summary.md` のセッション一覧は**表ではなくリスト**にしている。各セッションの下に要約行、多フェーズならさらにネストしたフェーズ内訳を置くため（Markdown の表は行間に子行を置けない）。数値（開始時刻・実処理・コスト）は各項目の第1子行に残す。
   - `summary_card.png` の Top3 は**タイトルを主行**に出し、`--summarize` の LLM 要約があるときだけ副行（小さめ・淡色）で要約を添える（`charts.render_summary_card` のシグネチャは変えず、`"タイトル\n要約"` の1文字列を渡す）。要約でタイトルを置き換えると 30 字幅で途中切れになり、タイトルより読めなくなる（決定論要約は「主要ディレクトリ + 件数」なので特に情報量が落ちる）。決定論要約は `sessions.csv` / `summary.md` で読ませる。
   - **警告（`Agg.warnings`）と注記（`Agg.notes`）は別物として持ち、別の面に出す**。警告は異常・要注意（未収載モデル・fork/resume の帰属・timestamp 欠落行・単価改定日またぎ・root 外 cwd・孤児 subagent・pricing stale）、注記は「異常ではないが前提として知っておくべきこと」。summary.md では `## 警告` と `## 注記`、stdout では `警告:` と `注記:` に分ける。毎回出る文を警告に混ぜると本物の警告が薄まるため。
   - `## 注記` には、常時出る「表中の $ は小数2桁に丸めているため内訳の足し上げが合計と数セントずれる」に加え、`Agg.notes` の各行（現在は as_of の件）を並べる。stdout 側は `Agg.notes` のみ（丸めの文は md 固有）。

## コストの意味
- **全モデルを従量課金単価で仮計算した参考値**。`billing`（payg/included）区分は使わない（Fable 等は実際にはサブスク込み）。summary.md 冒頭と PNG に注記を入れる。
- `pricing.json` 未収載のモデルは $0 計上。`Report.unknown_models` と**未計上トークン数・その比率**を、読み手が最初に見る面すべてに出す: stdout / summary.md（合計表の「うち未計上トークン」行 + 表直下の注意書き）/ summary_card.png（ヒーロー数値直下の短い警告 + 下部の全文）/ model_breakdown.png のバーラベル / sessions.csv（`unknown_tokens` 列・`models` 列の `*`・注記行）/ sessions_by_model.csv の `known` 列。
- 未収載は「新モデルが出てから単価が追記されるまで」の一時状態。単価の追記は cost-manager の `config/pricing.json` に対する**人手の運用作業**で、このツールのコードからは cost-manager 配下を書き換えない。2026-08-14 に `claude-opus-5`（input $5 / output $25 per MTok）を追記済みで、現在の実データでは未収載モデルは 0 件（`unknown_tokens` は全行 0、警告にも出ない）。
- 単価表の `as_of` より前の期間を集計する場合、cost_lib の stale 判定（`at - as_of > stale_after_days`）は負値になり発火しないため、こちら側で「当時の実単価ではなく現在の単価表で計算している」旨を出す（`at` と `as_of` を併記）。これは異常ではなく前提なので**警告ではなく注記**（`Agg.notes` → `## 注記` / stdout の `注記:`）。判定は期間の**開始日** < `as_of` で行う（終端で見ると `--from 過去日 --to 今日` のように as_of をまたぐ期間で落ちる）。

## チャートのデザイン規範（dataviz 準拠）
- ダークテーマ固定。surface `#1a1a19` / text `#ffffff` / secondary `#c3c2b7` / grid `#3a3a37`。
- モデル色は固定割当。ファミリー基調色（fable=blue `#3987e5` / opus=orange `#d95926` / sonnet=aqua `#199e70` / haiku=yellow `#c98500` / その他=gray）に加え、**同一ファミリーの複数世代を区別するためモデル名から決まる固定の濃淡**を持つ（例: `claude-opus-4-8` は `#f2915f`）。出現順・ランクでは塗らない（recolor-on-filter 回避）。
- 積み上げ・隣接バーは surface 色 2px の隙間で分離（枠線を描かない）。二軸禁止。全点数値ラベル禁止（バー端の直接ラベルのみ）。
- 凡例は2系列以上で必須。**必ず軸の外**に置く（横棒は下、日別積み上げは上に `loc="lower left", bbox_to_anchor=(0, 1.01)`）。軸内 upper-left に置くと凡例本体が下方向＝プロット領域へ伸び、左寄りの高いバーと重なる。
- 日別積み上げは日数で粒度を落とす（62日超=週次ビン、400日超=月次ビン）。x ラベルは最大16本まで間引く。バーの分離線幅は px 換算（`2 * 72 / dpi`）し、実効バー幅が 8px 未満なら 0 にする（`linewidth` はポイント単位なので、細いバーでは枠線が塗りを食い潰して「棒が消える」）。
- 全期間 $0 の系列（単価未収載モデル等）は色面が1画素も描かれないため凡例から外し、代わりにチャート下に「$0 のため非表示」と明記する。
- リポジトリが9件以上なら上位8 + `その他` に畳む。

## 既知の制約
- セッション途中で cwd が変わっても帰属は**起動 cwd** で決める（行単位の帰属はしない）。root 外の cwd が現れたセッションは警告に出す。
- 同じ cwd で走った無関係セッションは区別できない（cost-manager `--scope global` と同型）。
- `encode_cwd` は非可逆のため、候補ディレクトリは広めに取って cwd で絞る方針に依存する。
- 期間窓を指定する以上、timestamp 欠落行は必ず落ちる（件数を警告に出す）。
- uuid グローバル dedup（resume 二重計上防止）は cost_lib 由来。**実データで発火することを確認済み**（Lav/git 2026-07 で同一 uuid の重複 817 件 = fork 元 45ee03c7 と fork 先 883b73f1）。合計は不変だが、per-session の帰属は「作成が古い transcript 側に寄せる」規則に依存する（警告に出す）。
- fork でコピーされた親の活動イベント（timestamp ごとコピー）は dedup されないため、子セッションの `active_time` に親の活動時間が含まれうる（列の合計は TOTAL 行の union と一致しない）。全体値は union なので影響しない。並行実行分の重なりも同様なので、`sessions.csv` の注記行と summary.md の合計表直下に「列を SUM しても TOTAL とは一致しない（常に TOTAL 以上）」と明記する。
- `sessions.csv` の `cost_usd` / `cost_jpy` は行ごとに丸めるため、明細を SUM すると TOTAL 行と端数分ずれる（CSV の注記行に明記）。
- `sessions.csv` の行順は「明細行 → `# 注記` 行（1〜3本）→ `TOTAL`」。`TOTAL` を文字どおり最終行に置くことで `tail -1` で合計が取れる（注記行を最後に置くと、機械的に合計を拾う経路が壊れる）。
- レポート生成コマンド自身の消費は集計スナップショット確定後に発生するため含まれない。
- `message.usage.iterations` を見ていない（トップレベル `usage` のみで集計する cost_lib の方針に合わせている）。7月分 Lav/git で `iterations` を持つ行は窓内に 22,964 行あるが、うち `iterations` の合計がトップレベル `usage` を上回る（＝過小計上になる）のは **1 行・約 $0.53** だけで、全体の 0.02%（多イテレーションの行は 0 件）。将来モデルが多イテレーション化すると乖離が広がりうる。
- `ai-title` は**期間スコープを持たない**（transcript の最終行の `aiTitle` を採る）。長期セッションを期間で切って集計すると、その期間の作業とは別テーマのタイトルが出ることがある。作業内容サマリ（`digests.json` / `summary` 列 / `--summarize`）は窓内のレコードだけから作るため、この制約を受けない。
- `--summarize` は人間の発話（各250字）と Bash コマンド例を**外部モデル（`claude -p`）へ無加工で送る**。秘匿情報を含む発話・コマンドがあればそのまま送信され、`digests.json` と `var/summaries/` にも平文で残る。レポートを他人に渡す前に中身を確認する。
- LLM 要約は**初回生成時だけ非決定的**（キャッシュヒット時はバイト一致）。要約の内容は `claude -p` のモデル出力そのもので、検証していない主張が混じりうる。数値・事実は CSV / `digests.json` を正とする。
- 要約はメイン jsonl のみを材料にするため、サブエージェント側でしか行われていない作業（メイン側に tool_use の痕跡が残らないもの）は要約に現れない。
- 定期実行（cron）セッションのタイトルは `(定期実行) <タスク名>` になるため、同一タスクの複数回実行は一覧上で同じタイトルに見える（`session_id` と開始日時で区別する）。
- `var/summaries/` は LLM 要約のキャッシュ専用（`--summarize` 時のみ書き込む）。第1段だけの実行では書込先は `reports/` か `--out-dir` だけ。

## 実測（2026-08-14）
- `--root ~/Documents/medirom/projects/Lav/git --month 2026-07`: 対象 1,733 ファイル / 431 MB、21 セッション、dedup 後 19,920 行、合計 $2,282.76（¥365,241）、実処理時間 41時間15分。単価未収載モデルは 0 件（`claude-opus-5` 追記後）。
- 所要時間（同一マシン・warm キャッシュ、`--format` を揃えて比較）: `--format csv,png,md` が 7.1〜8.6 秒、同条件 `--no-active` が 3.3〜3.6 秒。`--format csv,md` は 5.9〜6.3 秒、同条件 `--no-active` が 2.7〜2.8 秒。**`--no-active` の短縮率はどちらの条件でも約1/2**（初回の cold キャッシュでは 9.8 秒 / 4.2 秒）。旧記載の「9.0 秒 → 2.9 秒（約1/3）」は `--format` を揃えずに比較した値で、再現しない。
- main / subagent の内訳（同じ 2026-07 を transcript の別で集計）: サブエージェント側がコスト $721.28（31.6%）・トークン 857.7M（44.0%）・dedup 後 API 呼び出し 16,367 件（82.2%）。2026-W33 では 32.7% / 44.1% / 72.2%。コストの大半はメインセッション側（68.4%）にあり、サブエージェント側が7割前後を占めるのは**呼び出し件数**のみ。
  - モデル別: claude-fable-5 $1,244.74 / claude-opus-4-8 $616.33 / claude-sonnet-5 $234.06 / claude-opus-5 $187.62。
- `--root ~/Documents/medirom/projects/Lav/git --week this`（2026-W33）: 7 セッション、$460.38（¥73,660）、実処理時間 9時間40分。
- `sessions_by_model.csv` のモデル別合計は summary.md のモデル別表と一致（自己整合を確認）。
- 作業内容サマリ（2026-08-14 実測、Lav/git 2026-07 の21セッション）: 第1段のみの実行は 9.6 秒（サマリ追加前 7.1〜8.6 秒に対し +1.5 秒程度）。`--summarize` 初回は 1分52秒（うち `claude -p` haiku が約100秒）、キャッシュヒット時は 11.6 秒で `claude` を呼ばない。`digests.json` は 21 セッションで人間の発話 216 件・ハーネス注入メッセージの混入 0 件。第1段のみ・`--summarize` キャッシュヒットのいずれでも、2回実行で CSV/PNG/digests.json がバイト一致することを確認済み。
- タイトル判定の広域確認: `--root /Users/isogai --month 2026-08`（71 セッション）で、タイトルが生のハーネス注入タグになるケース 0 件・`(タイトルなし)` 0 件・サブエージェント向け指示文（`[SYSTEM NOTIFICATION …]` / `The coordinator sent a message …` / `[structured-output-enforce] …`）が漏れたケース 0 件。
