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
呼び出す。`/cost-manager` と明示入力しても同様に発火する。コストレポート生成時は、Claude が短い
タスク名（`--task`）と非エンジニア向けの平易な要約（`--desc`）を自動で組み立てて渡す。

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
python3 scripts/cost_report.py --task "短いタスク名" --desc "今回の作業内容を1〜2行で要約"
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
| `--task "<短いタスク名>"` | マーカー無し時に PNG カードのタイトルへ使う短いタスク名（15字程度を推奨）。マーカーがある場合はマーカーの task_name が優先される。 |
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
| `FCM_PACE_BASE_STATUSLINE` | `../../license-switch/scripts/license_statusline.sh` | `pace_statusline.sh` が合成元として呼ぶ statusline。 |
| `FCM_PACE_REFRESH_CMD` | `python3 <scripts>/pace_refresh.py --quiet` | バックグラウンド集計コマンド（テスト用の差し替え口）。 |
| `FCM_PACE_NOW` | (現在時刻) | `pace_statusline.sh` の現在時刻（epoch 秒）。テスト用。 |
| `FCM_CODEX_LEDGER` | `budget.pace.codex_ledger`（既定 `../codex-bridge/var/codex_usage.jsonl`） | Codex 使用量台帳のパス。テスト隔離・別配置用。 |
| `FCM_CODEX_PRICING` | `../codex-bridge/config/codex_pricing.json` | Codex の単価表（credits per MTok）。 |

`templates/` `scripts/` はコード資産としてスクリプト自身の実位置から解決するため、
`FABLE_COST_MANAGER_ROOT` を差し替えるテストでも `templates/` のコピーは不要（`config/` のみコピーすれば良い）。

## ディレクトリ構成

```
config/     単価・為替・画像・pace 設定（config.json / pricing.json）
scripts/    実行スクリプト（cost_start / cost_report / cost_status / render_md / render_image /
            pace_statusline.sh / pace_refresh / pace_report / cost_lib）
templates/  Markdown / HTML カードのテンプレート
tests/      unittest（合成 transcript + スクラッチルート隔離）
reports/    生成物（Markdown + PNG）。.gitignore 対象。
var/        実行時状態（active_task.json / tasks/ / log/reports.jsonl /
            pace/{samples.jsonl,cache.json,refresh.lock,sample.lock}）。.gitignore 対象。
docs/       設計メモ
```

## 週次枠ペーシング（pace / フェーズ2）

サブスクの**週次枠**（`rate_limits.seven_day`）・**5時間枠**（`five_hour`）・**Fable の 50% サブ枠**を
「使い切れているか」を statusline に常時表示する。理想は「リセット時点で 100% 近くまで使い切る」
（余らせるのも早期枯渇も NG）。

### 表示の読み方

```
📅W 55%/57% ·0.96   F≈26%/50% ·0.90   ⏱5h 24%/60%
```

| セグメント | 意味 |
| --- | --- |
| `📅W <used>%/<elapsed>%` | 週次枠の使用率 / 窓の経過率（`resets_at − 7日` を起点とする） |
| `·<pace>` | ペース = used / elapsed。`1.00` ちょうどがリセット時点で使い切るペース |
| `F≈<est>%/<cap>%` | Fable の推定使用率 / 上限（既定50%）。`est = 週次枠 used × fable_usd/total_usd` |
| `⏱5h <used>%/<elapsed>%` | 5時間枠（stdin に `five_hour` があるときだけ表示） |
| `🅒 <credits>cr` | Codex の窓内消費クレジット（台帳があるときだけ表示・薄色） |
| `🅒 <used>%/<elapsed>% ·<pace>` | `codex_weekly_credits` を設定したときの Codex の % とペース |

色は `on_pace_band`（既定 `[0.8, 1.1]`）基準で、**薄色**=余らせ気味 / **緑**=想定どおり /
**黄**=枠超過ペース / **赤**=枠超過かつ枯渇が早い（`exhaust_margin_pct`、既定80%）。
`F?` はキャッシュ未生成、`·0.90?`（薄色）はキャッシュが古い（TTL×3 超）。
`F?!`（黄）は `pricing.json` 未収載モデルがあって Fable 推定ができない状態（単価を追加すること）、
`F!`（黄）は直近の集計自体が失敗した状態（`python3 scripts/pace_report.py` か
`python3 scripts/pace_refresh.py` を手動実行すると理由が出る）。
stdin に `rate_limits` が無いセッション（初回 API 応答前・Pro/Max 以外）や、`used` と `resets_at` が
揃っていない窓は `📅W ?` になる。

### 仕組み

- `pace_statusline.sh` が既存の `license_statusline.sh`（→ `handoff_statusline.sh`）を**無改変**で
  呼び、その末尾に pace セグメントを連結する。同期処理は jq とファイル読みだけで python は呼ばない。
- `rate_limits` があるとき `var/pace/samples.jsonl` に 1 行記録する（60秒スロットル）。記録は
  `var/pace/sample.lock` の mkdir ロックで直列化するが、**ロックが取れなければ記録は諦める**
  （best-effort。statusline を待たせない方を優先する）。`used` と `resets_at` が揃わない窓は
  `null` として記録する。
- キャッシュ（`var/pace/cache.json`）が 300 秒より古ければ `pace_refresh.py` を
  `mkdir` ロックの single-flight でバックグラウンド起動する（表示はキャッシュを読むだけ）。
- `pace_refresh.py` は「`resets_at − 7日` 〜 now」の窓で全プロジェクトの transcript を
  `cost_report.py --scope global` と同じ経路で dedup 集計する（実データで約7秒）。
- **別ライセンスのセッションは除外する**。`license-switch` が生成した `.envrc`
  （`generated-by: claude-toolbox/license-switch` を含む）が効くディレクトリで起動した
  セッションは別の週次枠の消費のため。`config.json` の `budget.pace.exclude_cwd_prefixes` でも
  手動除外できる。除外件数は `cache.json` の `notes` に出る。

推定の前提（仮定 A1〜A3）は**いずれも未検証**。詳細と較正（calibration）の考え方は
[docs/design.md](docs/design.md) の「pace 実装」節を参照。

### ペース確認（CLI）

```sh
python3 scripts/pace_report.py            # テキスト
python3 scripts/pace_report.py --refresh  # 同期で集計してから表示
python3 scripts/pace_report.py --json
```

窓の開始・終了（JST）、週末到達見込み、Fable の 50% 到達見込み、モデル別 USD/tokens、較正、
日別のサンプル履歴、推奨行を出す。サンプル未取得のときは exit 3。

### 手動集計

```sh
python3 scripts/pace_refresh.py                                  # samples.jsonl の最新行から窓を決める
python3 scripts/pace_refresh.py --resets-at <epoch> --used <pct> # 手動検証用に窓を明示する
```

`--resets-at` / `--used` は **samples.jsonl がまだ無い状態で動作確認する**ための上書き引数。
`--no-exclude-license` で別ライセンス除外を無効化、`--now <epoch>` はテスト用の固定時刻。

### statusLine への組み込み

`~/.claude/settings.json` の `statusLine.command` を pace wrapper に向ける（本ツールは
settings.json を書き換えない。手動で設定すること）。

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /path/to/claude-toolbox/cost-manager/scripts/pace_statusline.sh"
  }
}
```

`pace_statusline.sh` は既定で `../../license-switch/scripts/license_statusline.sh` を BASE として
呼ぶ（見つからなければ pace セグメントだけを出す）。別の statusline を合成元にする場合は
環境変数 `FCM_PACE_BASE_STATUSLINE` で指定する。

**symlink 経由で置く場合**（`~/bin/statusline.sh` → 本スクリプト等）は、スクリプト側で symlink を
解決してリポジトリ位置を求めるが、`FABLE_COST_MANAGER_ROOT` を明示しておく方が確実
（`var/pace` の位置が shell 側と python 側で食い違わない）。

```json
{
  "statusLine": {
    "type": "command",
    "command": "FABLE_COST_MANAGER_ROOT=/path/to/claude-toolbox/cost-manager bash ~/bin/statusline.sh"
  }
}
```

### 設定（`config/config.json` の `budget.pace`）

| キー | 既定 | 意味 |
| --- | --- | --- |
| `fable_cap_pct` | 50 | Fable のサブ枠上限（週次枠に対する%） |
| `refresh_ttl_sec` | 300 | キャッシュの鮮度。超えるとバックグラウンド refresh |
| `sample_min_interval_sec` | 60 | samples.jsonl への最短記録間隔 |
| `on_pace_band` | `[0.8, 1.1]` | 「想定どおり」とみなすペースの帯 |
| `exhaust_margin_pct` | 80 | 赤（早期枯渇）判定: 使い切り時点が窓の何%経過より前か |
| `exclude_cwd_prefixes` | `[]` | 集計から除外する cwd の**絶対パス**リスト（`~` 可。末尾スラッシュは無視し、ディレクトリ境界で一致） |
| `codex_ledger` | `../codex-bridge/var/codex_usage.jsonl` | Codex 使用量台帳（相対パスはスクリプトの実位置基準で解決。`~` 可） |
| `codex_weekly_credits` | `null` | Codex の週次上限（クレジット）。`null` は「上限未設定」＝ % とペースを出さない |

## Codex 使用量レーン（codex-bridge の台帳を読む）

[codex-bridge](../codex-bridge/) が Codex ジョブごとに追記する使用量台帳
（`codex-bridge/var/codex_usage.jsonl`）を**読むだけ**のレーン。Claude 側（transcript 集計）と
同じ画面で Codex の消費（クレジット）を見られるようにする。cost-manager から台帳へ書き込むことは無い。

- 台帳が無ければ何も出さない（`cache.json` の `codex` は `null`、statusline も既存表示のまま）。
- 1 行 1 ジョブ。`ts` は ISO8601 文字列・epoch 数値の両方を受ける（codex-bridge の現行実装は ISO）。
- 無視する行: `mock` が非 null（モック実行）／`usage` が無い／JSON として壊れている／`ts` が不正。
  件数は `ignored_rows` として `pace_report` とレポートの注記に出る。
- クレジットは行の `credits_est` を優先し、無ければ `codex-bridge/config/codex_pricing.json`
  （credits per MTok）から再計算する。単価も `credits_est` も無いモデルは 0 として扱い注記に出す。
- **Codex の枠は「5時間窓 + 週次」で絶対値が非公開**のため、既定では % を出さない（消費クレジットと
  件数だけを出す）。実測で上限が分かったら `budget.pace.codex_weekly_credits` に設定すると
  % とペース（`🅒 34%/57% ·0.60`）が出る。
- 窓は Claude 側の `seven_day` 窓をそのまま使う。**Codex の週次窓はリセット時刻が別なので近似**
  （注記に明示）。5時間窓は窓終端から遡った 5 時間。
- `cost_report.py` は、タスク範囲に台帳行があれば「Codex（参考）」表をレポート Markdown と
  `var/log/reports.jsonl` の `codex` キーに載せる。`--scope session`（既定）は
  `claude_session_id` が対象セッションと一致する行だけ、`--scope global` は時間窓のみで拾う。
  PNG カードは変更していない（範囲外）。

## テスト

```sh
python3 -m unittest discover -s tests -v      # cost-manager ディレクトリで実行
```

`tests/` は `FABLE_COST_MANAGER_ROOT` と `FCM_PROJECTS_DIR` でスクラッチルート／合成 transcript に
隔離されており、実データ（`~/.claude/projects`）と実 `var/` には触れない。

## フェーズ2の残り（未実装）

`UserPromptSubmit` hook による閾値通知、増分 offset パース、desktop 通知は今回のスコープ外。
`docs/design.md` にメモを残している。
