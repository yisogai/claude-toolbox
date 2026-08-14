# usage-report 設計（v1・実装済み）

本書は実装後の状態を記述する。仕様と実装が食い違ったら**実装が正**。

## 目的
指定ディレクトリ（既定 cwd）の**子孫ディレクトリで実行された全 Claude Code セッション**を期間指定（月/週/任意）で集計し、トークン使用量・従量課金換算コスト（USD/JPY）・作業内容サマリを **CSV 2枚 + PNG 最大4枚 + summary.md** に出力する。

## 非目標
- リアルタイム監視・予算アラート（cost-manager の領分）
- LLM によるセッション要約生成（タイトルは transcript の `ai-title` 等をそのまま使う）
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
│   └── charts.py       # matplotlib 描画（任意依存を隔離）
├── reports/.gitkeep
└── var/.gitkeep      # 枠のみ。現在の実装は var/ に一切書き込まない（未使用）
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

5. **出力**
   - CSV は `csv.writer`（CRLF）で組み立て、utf-8-sig にエンコードして `lib.atomic_write_bytes`。
   - PNG は `savefig` → BytesIO → ヘッダ検査（`png_size`）→ `lib.atomic_write_bytes`。
   - summary.md は `lib.atomic_write_text`。
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
- `ai-title` は**期間スコープを持たない**（transcript の最終行の `aiTitle` を採る）。長期セッションを期間で切って集計すると、その期間の作業とは別テーマのタイトルが出ることがある。
- 定期実行（cron）セッションのタイトルは `(定期実行) <タスク名>` になるため、同一タスクの複数回実行は一覧上で同じタイトルに見える（`session_id` と開始日時で区別する）。
- `var/` は現在の実装からは**未使用**（`.gitkeep` のみ。実行時状態を持たないため書込先は `reports/` か `--out-dir` だけ）。

## 実測（2026-08-14）
- `--root ~/Documents/medirom/projects/Lav/git --month 2026-07`: 対象 1,733 ファイル / 431 MB、21 セッション、dedup 後 19,920 行、合計 $2,282.76（¥365,241）、実処理時間 41時間15分。単価未収載モデルは 0 件（`claude-opus-5` 追記後）。
- 所要時間（同一マシン・warm キャッシュ、`--format` を揃えて比較）: `--format csv,png,md` が 7.1〜8.6 秒、同条件 `--no-active` が 3.3〜3.6 秒。`--format csv,md` は 5.9〜6.3 秒、同条件 `--no-active` が 2.7〜2.8 秒。**`--no-active` の短縮率はどちらの条件でも約1/2**（初回の cold キャッシュでは 9.8 秒 / 4.2 秒）。旧記載の「9.0 秒 → 2.9 秒（約1/3）」は `--format` を揃えずに比較した値で、再現しない。
- main / subagent の内訳（同じ 2026-07 を transcript の別で集計）: サブエージェント側がコスト $721.28（31.6%）・トークン 857.7M（44.0%）・dedup 後 API 呼び出し 16,367 件（82.2%）。2026-W33 では 32.7% / 44.1% / 72.2%。コストの大半はメインセッション側（68.4%）にあり、サブエージェント側が7割前後を占めるのは**呼び出し件数**のみ。
  - モデル別: claude-fable-5 $1,244.74 / claude-opus-4-8 $616.33 / claude-sonnet-5 $234.06 / claude-opus-5 $187.62。
- `--root ~/Documents/medirom/projects/Lav/git --week this`（2026-W33）: 7 セッション、$460.38（¥73,660）、実処理時間 9時間40分。
- `sessions_by_model.csv` のモデル別合計は summary.md のモデル別表と一致（自己整合を確認）。
- タイトル判定の広域確認: `--root /Users/isogai --month 2026-08`（71 セッション）で、タイトルが生のハーネス注入タグになるケース 0 件・`(タイトルなし)` 0 件・サブエージェント向け指示文（`[SYSTEM NOTIFICATION …]` / `The coordinator sent a message …` / `[structured-output-enforce] …`）が漏れたケース 0 件。
