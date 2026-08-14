---
name: usage-report
description: ディレクトリ配下（子・孫を含む）で実行した全 Claude Code セッションを期間指定（月/週/任意）で集計し、CSV 2枚 + PNG 最大4枚 + summary.md を出力する。「7月分のサマリ」「今週のこのプロジェクトの使用量」「ディレクトリ集計」「リポジトリ別コスト」「先月いくら使った」「案件配下の使用量」などと言われたときに使う。全プロジェクトの任意のリポジトリから使える（スクリプトは絶対パスで呼ぶ）。
---

# usage-report — ディレクトリ×期間の使用量レポート

`/Users/<YOU>/Documents/personal/tools/claude-toolbox/usage-report/scripts/usage_report.py` を絶対パスで呼び、指定ディレクトリの**子孫ディレクトリで実行された全セッション**（サブエージェント transcript を含む）を期間集計する。cost-manager が「1タスク単位」の計測なのに対し、こちらは「ディレクトリ配下 × 期間」の棚卸しに使う。

## 手順
1. **発話から引数へ写像する**
   - 対象ディレクトリ: 明示が無ければ `--root` 省略（カレントディレクトリ）。「Lav 全体で」等なら `--root /Users/<YOU>/Documents/medirom/projects/Lav/git` のように案件ルートを渡す。
   - 期間: 「7月分」→ `--month 2026-07` ／「今週」→ `--week this` ／「先週」→ `--week last` ／「7/1〜7/14」→ `--from 2026-07-01 --to 2026-07-15`（`--to` は**含まない**ので翌日を渡す）。指定が無ければ当月（JST）。
2. **実行する**
   ```bash
   python3 /Users/<YOU>/Documents/personal/tools/claude-toolbox/usage-report/scripts/usage_report.py \
     [--root DIR] [--month YYYY-MM | --week this|last|YYYY-Www | --from ISO --to ISO] \
     [--label TEXT] [--format csv,png,md] [--no-active] [--out-dir DIR]
   ```
   - 大きい案件（数百 MB の transcript）でも 10 秒程度。急ぐときは `--no-active` で**約1/2**に短縮できる（同一 `--format` 条件での実測。実処理時間の列は空欄になる）。
   - PNG は**最大4枚**。リポジトリ別・モデル別は対象データが無いとスキップされ、2枚になることがある。
3. **報告する**（日本語）
   - 出力ディレクトリの絶対パス、生成ファイル名、**合計 USD / JPY**、セッション数、実処理時間、**警告一覧**を必ず伝える。警告（`## 警告` / stdout の `警告:`）と注記（`## 注記` / stdout の `注記:`）は別物で、伝える必要があるのは警告。注記（単価表 `as_of` より前の期間である旨、丸め誤差）は前提の説明なので、金額の解釈に効くときだけ添える。
   - 高コストのセッション上位（summary.md のセッション一覧）に触れて、どこに費用が寄っているかを一言添える。

## 原則
- コストは**全モデルを従量課金単価で仮計算した参考値**（Fable 等は実際にはサブスク込み）。請求額ではないことを添えて報告する。
- `pricing.json` に単価が無いモデルがあると $0 計上になり、stdout・警告に「うち N tok（X%）は単価未収載のため未計上」と出る。**その警告が出たときは、合計金額に必ずこの比率も添える**（合計はその分だけ過小）。警告が出ていなければ全モデル収載済みなので、比率に触れる必要はない。
- `~/.claude/projects` は読み取り専用。書込は `usage-report/reports/` のみ。
- 単価・為替は cost-manager の `config/pricing.json` / `config/config.json` を参照する（このツールは設定を持たない）。
