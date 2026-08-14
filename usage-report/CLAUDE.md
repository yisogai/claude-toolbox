# usage-report 実装規約

## 依存
- **python3 標準ライブラリ + `cost_lib`（import）+ matplotlib（任意依存）のみ**。Pillow は使わない。他の外部パッケージを追加しない。
- matplotlib は import 失敗時に PNG をスキップして CSV/md のみ生成する degrade を必ず維持する（exit 0）。チャート描画コードは `scripts/charts.py` に隔離し、`usage_lib.py` から import しない。

## cost-manager との関係
- 集計の中核（`iter_usage` / `Accumulator` / `aggregate` / `resolve_model` / `rate_for` / `scan_activity` / `active_seconds` / `atomic_write_*` / `parse_iso` / `to_jst` / `fmt_*`）は `cost-manager/scripts/cost_lib.py` を import して使う。**ロジックを複製しない**（窓フィルタなど、どうしても足りない小物のみ自前実装する）。
  - 例外: `usage_lib.first_real_user_text` は `cost_lib.find_first_user_text` に近い自前実装。cost_lib 側は候補を1件しか返さず、skip 述語（ハーネス注入メッセージを飛ばす条件）を渡す口が無いため、先頭が `<local-command-caveat>` 等だとタイトルが常に `(タイトルなし)` になる。cost-manager のコードは変更しない方針なので、こちらに複数候補走査を持つ。ただし抽出・時刻判定・人間プロンプト判定（`lib._extract_text` / `lib.parse_iso` / `lib._is_human_prompt`）は cost_lib のものを再利用し、判定ロジック自体は複製しない。
- **このツールのコードから cost-manager 配下のファイルを書き換えない**（読むだけ）。単価・為替も cost-manager の `config/pricing.json` / `config/config.json` を読む（このツールは独自の config を持たない）。
  - `config/pricing.json` への新モデル単価の追記は、**人手の運用判断として可**（実際 2026-08-14 に `claude-opus-5` を追記した）。その場合もコードは不変で、`as_of` の更新を伴う。

## 書込・読取
- `~/.claude/projects` は**読み取り専用**（書込・削除・touch 一切禁止）。
- 書込先は `usage-report/reports/`（`--out-dir` 指定時はその場所）のみ。`usage-report/var/` は枠として `.gitkeep` だけ置いてあり、**現在の実装は一切書き込まない**（未使用）。実行時状態を持たせる必要が出たときだけここを使う。書込は必ず `lib.atomic_write_text` / `lib.atomic_write_bytes` 経由（CSV は utf-8-sig でエンコードしてから `atomic_write_bytes`、PNG は `savefig` → BytesIO → `atomic_write_bytes`）。

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

## チャート
- ダークテーマ固定（surface `#1a1a19` / text `#ffffff` / secondary `#c3c2b7`）。`matplotlib.use("Agg")` を import 直後に呼ぶ。`font.family = "Hiragino Sans"`、`dpi=200`。
- **モデル色は固定割当**（ファミリー基調色 + モデル名から決まる固定の濃淡）。出現順・ランクで塗らない。
- 二軸グラフ禁止。全点数値ラベル禁止（選択的直接ラベルのみ）。積み上げ・隣接バーは surface 色 2px の隙間で分離する。
- 描画後に**バイト長 > 0 と PNG ヘッダの寸法**をコードで検査してから書き出す。

## テスト・検証
- 実データでの検証は `--out-dir` を scratchpad に向けて行い、`reports/` を汚さない。
- 合成 transcript での検証は `FCM_PROJECTS_DIR` を偽の projects ディレクトリに向ける（実データを触らない）。
