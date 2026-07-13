# fable-cost-manager リポジトリ規約

Claude Code のタスク単位コスト集計・レポートツール。このリポジトリで作業する際は以下を厳守する。

## 実装規約

- スクリプトは **python3 標準ライブラリ + Pillow のみ**。他の外部パッケージを追加しない。
- 各スクリプトは `#!/usr/bin/env python3` + `argparse` + docstring（用途と実行例）を持つ。
- 共通処理は `scripts/cost_lib.py` に集約する。各スクリプトは
  `sys.path.insert(0, str(Path(__file__).resolve().parent))` してから `import cost_lib as lib` する。
- ファイル書込は必ず `mktemp → os.replace` のアトミック書込にする（`cost_lib.atomic_write_text` /
  `atomic_write_json` を使う）。書込先は本リポ配下の `var/` `reports/` のみ。
- `~/.claude/projects` は**読み取り専用**。書き込み・削除は絶対に行わない。
- テストは `FCM_PROJECTS_DIR`（transcript 探索元）と `FABLE_COST_MANAGER_ROOT`（`config/` `var/`
  `reports/` の親ルート）の環境変数で実データ・実 var/ から分離する。本物の `var/active_task.json`
  を実行検証のために作らない。
- `templates/` は `cost_lib.code_root()`（スクリプト自身の実位置基準）で解決する。
  `FABLE_COST_MANAGER_ROOT` を差し替えるテストでも `templates/` のコピーは不要。

## 集計ロジック（変更時は要注意）

- 課金対象行: `type=="assistant"` かつ `message.usage != null`。実使用モデルは `message.model` のみ
  参照し、`"<synthetic>"` は除外する。
- **requestId dedup は必須**（同一応答が content block ごとに複数行へ分割され usage が重複計上され
  る。実データで naive 合算がおよそ2倍以上過大になることを確認済み）。`requestId` 単位で
  `output_tokens` 最大の行を採用。`requestId` 欠落行は `uuid` をキーに同じ map へ。さらにグローバル
  `uuid` 集合で resume 再シリアライズによる二重計上を防ぐ（詳細は `docs/design.md`）。
- `subagents/agent-*.jsonl` と `subagents/workflows/wf_*/agent-*.jsonl` を必ず含める（含めないと
  サブエージェント分のコストが大幅に漏れる）。
- キャッシュ: `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` を別単価で
  計算。ネスト欠落時はトップレベル `cache_creation_input_tokens` を 5m 扱いにフォールバックする。
- 時刻は内部 UTC 保持、表示は JST 固定 `timezone(timedelta(hours=9))`（`zoneinfo` は使わない）。

## ドキュメント・コミュニケーション

- 出力文言・コメント・コミットメッセージは日本語。
- 単価・為替を更新した場合は `config/pricing.json` の `as_of` を裏取り日に更新すること。
