# fable-cost-manager

Claude Code（fable / Opus / Sonnet 等）のタスク単位コストを、transcript（`~/.claude/projects`
配下の JSONL）から集計して可視化するツール。フェーズ1は「タスク完了時にコストレポート
（Markdown + PNG カード）を毎回同一形式で出力する」ところまでを実装している。

設計の詳細・dedup の根拠・単価出典・フェーズ2計画は [docs/design.md](docs/design.md) を参照。

## 配置方法（2通り）

1. `./install.sh cost-manager` で `~/.claude/skills/cost-manager` に自己完結配置する（`SKILL.md` の
   プレースホルダパス `/Users/<YOU>/.claude/skills/cost-manager/...` はこの配置を前提にしている）。
2. clone したこのリポジトリを直接参照する（`SKILL.md` のコマンドパスを clone 先に書き換える。作者は
   この方式を使用）。

`ROOT` はスクリプト自身の位置から動的に解決されるため、どちらの配置方法でも動作する。

## 前提

- python3（標準ライブラリ + Pillow のみ）。`pip install Pillow` 済みであること。
- `--via chrome` を使う場合は `/Applications/Google Chrome.app` が必要（無ければ Pillow に自動フォールバック）。

## 使い方

### Claude Code から使う（推奨）

Claude Code のチャットで下表の発火ワードを言うだけで、スキル `cost-manager` が対応するスクリプトを
呼び出す。`/cost-manager` と明示入力しても同様に発火する。

| 操作 | 発火ワードの例 | 対応スクリプト |
| --- | --- | --- |
| 計測開始 | 「コスト計測開始」「予算を設定」 | `cost_start.py` |
| 途中経過確認 | 「消化状況」「予算どれくらい使った」 | `cost_status.py` |
| コストレポート | 「コストレポート」「今回いくらかかった」「料金を出して」「コスト集計」 | `cost_report.py` |

### スクリプトを直接実行する

#### 1. タスク開始マーカーを作成する（任意）

```sh
python3 scripts/cost_start.py --task "設計レビューとdocs更新"
python3 scripts/cost_start.py --task "調査タスク" --budget-usd 20
```

- 実行中のセッション（env `CLAUDE_CODE_SESSION_ID`）と cwd を自動登録する。
- 既に進行中タスクがある場合はエラー（exit 2）。置き換えるには `--force`。
- マーカーを作らずに `cost_report.py` を実行した場合は「現在セッション全体」が対象になる。

#### 2. 途中経過を確認する（任意）

```sh
python3 scripts/cost_status.py
python3 scripts/cost_status.py --json   # フェーズ2 statusline 用
```

消化額（USD/JPY）・消化率・経過時間・平均 $/h・予算到達 ETA・モデル別内訳を表示する。
マーカーが無い場合は現在セッション全体を予算なしで表示する。

#### 3. コストレポートを生成する

```sh
python3 scripts/cost_report.py --desc "今回の作業内容を1〜2行で要約"
```

- 範囲は「マーカーの `started_at` 〜 現在」。マーカーが無ければ現在セッション全体。
  `--since` / `--until`（ISO8601）を指定すると最優先で上書きされる。
- 既定でレポート発行後にマーカーを close して `var/tasks/` へアーカイブする（`--keep-open` で継続）。
- 出力は `reports/YYYY/MM/<JSTタイムスタンプ>-<タスク名slug>.{md,png}`。
- `var/log/reports.jsonl` に発行履歴を1行追記する。
- 対象範囲にデータが無い場合は exit 3 で終了する。
- レポートの「経過」は開始〜終了の壁時計時間（旧称「実働」を改名したもの）。「実処理時間」は
  人間の入力待ち・放置時間を除いた Claude の処理時間合計（サブエージェント並行分は union で
  二重計上しない）。
- 期間終端はレポート生成ターン自体を除いた最終アクティビティ時刻に自動補正される
  （`--until` 明示時を除く）。

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--desc "<要約>"` | タスク内容の要約。Claude が渡すのが第一（省略時はマーカーの task_name、それも無ければ範囲内最初のユーザープロンプト冒頭）。 |
| `--scope session\|global` | `global` は全プロジェクト時間窓走査（無関係セッション混入の可能性あり、レポートに注記される）。既定 `session`。 |
| `--since` / `--until` | ISO8601 で範囲を明示指定（最優先）。 |
| `--no-image` | PNG カードを生成しない。 |
| `--via pillow\|chrome` | 画像レンダラを指定（省略時は `config/config.json` の `image.renderer`）。 |
| `--keep-open` | マーカーを close せず継続する。 |
| `--session <id>` | 対象セッションIDを明示指定（省略時はマーカー登録セッション or 現在のセッション）。 |

## 終了コード

各スクリプトは以下の終了コードで終わる（スクリプト共通。用途ごとに固有コードを割り当て、
それ以外のエラーは 1 に集約する）。

| コード | 意味 | 対象スクリプト |
| --- | --- | --- |
| `0` | 正常終了 | 全て |
| `1` | その他エラー（`config.json` / `pricing.json` の欠落・破損、対象セッション特定不能 等） | 全て |
| `2` | 既に進行中タスクがある（`--force` で置換可能） | `cost_start.py` |
| `3` | 対象範囲にコストデータが0件（範囲・スコープを確認） | `cost_report.py` |

## 単価・レートの更新手順

1. `config/pricing.json` を編集する。モデルごとの `input` / `output`（$/MTok）、`intro`（導入価格と
   `until` 日付）、`as_of`（裏取り日）を最新の公式情報
   （https://platform.claude.com/docs/en/about-claude/pricing ）に合わせて更新する。
2. キャッシュ倍率はモデル横断の既定値を `cache_multipliers` で持つ。特定モデルだけ倍率が異なる場合は
   そのモデルのエントリに `cache_write_5m` / `cache_write_1h` / `cache_read`（$/MTok 直接値）を追加すると
   倍率より優先される。
3. `as_of` から `stale_after_days`（既定90日）を超えるとレポートに「単価が古い可能性」の警告が出る。
4. 為替レートは `config/config.json` の `usd_jpy` を更新する。
5. `config/config.json` の `active_gap_max_sec`（既定900秒）は実処理時間の算出に使うギャップ閾値。
   ターン内でこれを超える無イベント区間は待ち時間とみなして実処理時間から除外する（長時間の権限
   プロンプト放置対策）。900秒以下の権限待ちは実処理時間に含まれる制約がある。

## 環境変数（テスト・複数環境向け）

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `FABLE_COST_MANAGER_ROOT` | スクリプト自身の親ディレクトリ（クローン/インストール先） | `config/` `var/` `reports/` の親ルート。テスト用スクラッチルートへの差し替えに使う。 |
| `FCM_PROJECTS_DIR` | `~/.claude/projects` | transcript 探索元。テストは凍結コピーへ向ける。 |
| `CLAUDE_CODE_SESSION_ID` | (Claude Code が設定) | 現在の実行セッションIDの取得元。 |

`templates/` `scripts/` はコード資産としてスクリプト自身の実位置から解決するため、
`FABLE_COST_MANAGER_ROOT` を差し替えるテストでも `templates/` のコピーは不要（`config/` のみコピーすれば良い）。

## ディレクトリ構成

```
config/     単価・為替・画像設定（config.json / pricing.json）
scripts/    実行スクリプト（cost_start / cost_report / cost_status / render_md / render_image / cost_lib）
templates/  Markdown / HTML カードのテンプレート
reports/    生成物（Markdown + PNG）。.gitignore 対象。
var/        実行時状態（active_task.json / tasks/ / log/reports.jsonl）。.gitignore 対象。
docs/       設計メモ
```

## フェーズ2（未実装）

statusline 常時表示・閾値通知は今回のスコープ外。`docs/design.md` にメモを残している。
