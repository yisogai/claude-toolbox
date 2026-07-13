# 設計メモ

フェーズ1実装の背景・根拠をまとめる。実装プラン（承認済み・作者のローカル環境にのみ存在）を
正本とし、ここではその要約と実データ検証の結果を記録する。

## データ源

- メイン transcript: `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`
- Agent（サブエージェント）: `<projectDir>/<sessionId>/subagents/agent-*.jsonl`
- Workflow: `<projectDir>/<sessionId>/subagents/workflows/wf_*/agent-*.jsonl`

subagents を含めないとサブエージェント分のコストが大幅に漏れる（実データ検証: 本セッションの
subagents ファイルだけで dedup 後 105 行中 86 行を占めた）。

### cwd エンコードのルール（実データで確定）

`~/.claude/projects` 配下のディレクトリ名は、cwd の絶対パスに対し
**英数字とハイフン以外の文字（`/` `_` `.` 空白 等）をすべて `-` に置換**したもの。
プラン記載の「`/`→`-`」だけでは不十分で、`_` や `.` も置換対象であることを実ディレクトリ名
（例: `my_project` → `-...-my-project`、`sample.app` → `-...-sample-app`）との突合で確認した。
非可逆変換のため、cwd の実値は常に JSONL 行内の `"cwd"` フィールドから読む
（`cost_lib.encode_cwd()` は探索用のディレクトリ名生成にのみ使う）。

## dedup（requestId 単位の重複排除）— 根拠

Claude Code の transcript は、同一 API 応答の content block（thinking / text / tool_use 等）ごとに
複数の `type=="assistant"` 行として保存され、各行の `message.usage` には**同一の最終 usage 値**が
繰り返し記録される。requestId は同一だが `uuid` は行ごとに異なる。

実データ検証（本タスクの本セッション transcript を凍結コピーして集計）:

- assistant + usage 行の単純合計: 262 行
- requestId（欠落時は uuid）dedup 後: 105 行
- output_tokens の naive 合算 339,495 に対し dedup 後 159,628（過大計上係数 約2.13倍）

dedup ロジック:

1. `requestId` をキーに、同一 key 内では `output_tokens` が最大の行を採用する。
2. `requestId` が欠落する行は `uuid` をキーとして同じ map に載せる。
3. 別途グローバルな `uuid` 集合を保持し、既に取り込み済みの `uuid` が別ファイル（session resume に
   よる再シリアライズ等）で再度出現した場合は、`requestId` が振り直されていても無条件で skip する。
   これは「同一メッセージが resume で requestId だけ変わって再出現する」ケースに対する保険であり、
   通常の content-block 分割（`requestId` 同一・`uuid` 相違）とは異なる経路の二重計上を防ぐ。

### 独立検証（jq によるクロスチェック）

凍結データに対し `jq` で `group_by(.requestId) | map(max_by(.message.usage.output_tokens))` した
モデル別集計（input_tokens / cache_read_input_tokens / cache_creation の5m・1h別 / output_tokens）が
`cost_lib.aggregate()` の結果と全モデル・全フィールドで完全一致することを確認済み（実行コマンドは
本タスクの自己検証ログ参照）。

### 手計算検証

1 requestId 分の usage（`claude-sonnet-5`, input=2, cache_write_5m=7519, cache_read=8232, output=8）
について、pricing.json の intro 単価（input=$2, output=$10, 5m倍率1.25, read倍率0.1）で電卓計算した
結果 `$0.0205279` と `aggregate()` の結果が浮動小数点誤差なく一致することを確認した。

## 実処理時間（active time）の算出

コスト集計（`iter_usage` / `Accumulator` / `collect_dedup_rows` / `aggregate`）とは完全に独立した
transcript 走査を `cost_lib.scan_activity()` で行い、「経過（elapsed = end_display - start_display の
壁時計時間）」のうち Claude が実際に処理していた区間の合計（実処理時間）を求める。dedup ロジックには
一切手を入れない。

### イベント収集

`collect_dedup_rows` に渡すのと同一の tfiles（メイン transcript + `subagents/agent-*.jsonl` +
`subagents/workflows/wf_*/agent-*.jsonl`）を 1 行ずつ JSON パースし、`timestamp` を持つレコードだけを
対象にイベント列を作る（壊れた行は `iter_usage` と同様 JSONDecodeError を skip、パース不能な
timestamp も skip）。パスに `/subagents/` を含むファイルは「サブエージェントファイル」として扱い、
以降の人間プロンプト判定・レポートマーカー判定は行わず全レコードを活動イベントとする。

### 人間プロンプト判定式（メインファイルのみ）

実データで検証済みの判定式:

```
type == "user"
and isinstance(message, dict) and message.get("role") == "user"
and rec.get("isSidechain") is not True
and rec.get("isMeta") is not True
and "toolUseResult" not in rec
and content 条件:
    content が str の場合: lstrip() が "<command-name>" / "<local-command" で始まらない
    content が list の場合: type=="tool_result" のブロックを含まない、かつ最初の
        type=="text" ブロックの text.lstrip() が上記タグで始まらない
```

tool_result（`toolUseResult` キーあり）行・`<command-name>` 等のスラッシュコマンド行・`isMeta` 行は
人間の実入力ではないため除外する（=「人間プロンプト」ではないが、下記の通り「ターン境界」には別途含める）。

### ターン境界の判定（人間プロンプト or スラッシュコマンドレコード）

`scan_activity()` のセグメント分割・レポートターン除外の基準点（`L`、後述）は、上記の人間プロンプト
判定だけでなく「スラッシュコマンドレコード」も含めた `_is_turn_start()`
（`_is_human_prompt() or _is_command_record()`）で決まる。`/cost-manager` 等のスラッシュコマンド
起動時に連続出現する `<local-command-caveat>` / `<command-name>` / `<local-command-stdout>` の3
レコード（type=="user", message.role=="user"、isMeta の値は問わない）は `_is_human_prompt()` だけでは
人間プロンプト扱いされない。人間プロンプト判定のみを `L` の基準にすると、スラッシュコマンドでレポート
を生成した際に `L` が直前の実作業ターンまで後退し、実処理時間・`event_times` からその実作業が丸ごと
除外される過剰除外バグがあったため、`_is_command_record()` を追加してターン境界に含めた
（`_is_human_prompt()` 自体の判定式は変更していない）。

**既知の挙動（task-notification）**: バックグラウンドのサブエージェント（Workflow 等）が完了すると
メイン transcript に `<task-notification>` で始まる type=="user" レコードが記録される。これは
`_is_human_prompt()` の判定式上は人間プロンプトに該当し、ターン境界になる（メイン transcript 側では
通知直前の最終イベントで区切られ、通知までの待ち時間はセグメントに含まれない）。ただしその区間の
実処理はバックグラウンドで動いていたサブエージェント自身の transcript（`subagents/agent-*.jsonl` 等）
に記録されており、`active_seconds()` の interval union が拾うため、実処理時間から欠落することはない。

### レポートターン除外

レポート生成コマンド（`cost_report.py`）自体の実行はコスト計測対象外だが、実処理時間の計測対象に
含めてしまうと「レポートを作る操作」自体が実処理時間を水増ししてしまう。これを避けるため、assistant
レコードで `message.content` 内のいずれかのブロックが `type=="tool_use" and name=="Bash"` かつ
command 文字列が `"cost_report.py"` と `"python"` の両方を含むものを「レポートマーカー」として検出
する（`_is_report_marker()`）。スキル経由の起動は常に `python3 <path>/cost_report.py ...` 形式のため
検出できる一方、`cat scripts/cost_report.py` や `git log cost_report.py` のような閲覧系コマンドは
`"python"` を含まないため誤検出しない。制約として、シェバン直接実行（`./scripts/cost_report.py ...`）
は `"python"` を含まないため検出対象外（このツールの通常の起動経路ではないため許容している）。

ファイルごとに最後のターン境界時刻を `L` とし、`L` 以降（`ts >= L`）にレポートマーカーが存在する
場合（＝現在進行中のレポート生成ターン）、そのファイルの `ts >= L` のイベントを全て捨てる。ただし
`L` の直前のイベントもターン境界である間は `L` をそこまで遡らせる（連続するターン境界レコード列 =
コマンド展開の caveat / command-name / local-command-stdout の3連続等は run の先頭まで遡ってまとめて
除外する。遡らないとコマンド起動時のレコードが終端計算に残り、放置時間の除外が不完全になるため）。
捨てた `L` の最小値を `ActivityScan.report_cutoff` として保持する。これにより `scan.event_times`
（レポートターン除外済み）の最大値がそのまま「コスト計測を除いた最終アクティビティ時刻」になり、
`cost_report.py` はこれを `end_display` の補正に使う（`--until` 明示時は補正しない）。

### interval 生成とギャップ閾値

ファイルごとにイベントを ts 順にソートし、以下の規則でセグメント（interval）に分割する。

- メインファイル: ターン境界イベント（人間プロンプト or スラッシュコマンドレコード）で必ず新しい
  セグメントを開始する（直前とのギャップの大小に関係なく分割）。これにより「人間の入力待ち」がそもそも
  同一セグメントに混ざらない。さらにセグメント内で連続イベント間のギャップが
  `active_gap_max_sec`（既定900秒。`config/config.json`）を超えたら
  分割する。
- サブエージェントファイル: ギャップ > `active_gap_max_sec` でのみ分割する（人間プロンプトが存在
  しないため）。
- 各セグメントは `(最初のイベント ts, 最後のイベント ts)` の interval になる。イベント1個のセグメント
  は長さ0の interval として保持する。

### interval union（サブエージェント並行の扱い）

`active_seconds(intervals, clip_start, clip_end)` は全ファイル分の interval をマージ（union）してから
`[clip_start, clip_end]` でクリップし合計秒を返す。メインターンの実行中にサブエージェントが並行して
動いている時間帯は union によって二重計上されない。サブエージェントがメインの最終イベントより後まで
動いていた場合は、その分だけ `end_display`（＝event_times の最大値）も後ろに伸びる。

### meta["active_text"] の表示規則

- 計測不能（イベント0件等）: `"—"`。
- `duration_sec > 0`: `f"{lib.fmt_duration(active_sec)}（経過の{pct}%）"`
  （`pct = min(100, round(active_sec / duration_sec * 100))`）。
- `duration_sec == 0`: `lib.fmt_duration(active_sec)` のみ（%表示なし）。

### 不採用案: `turn_duration` システムレコード

transcript には Claude Code 自身が書き込む `turn_duration` 系のシステムレコードが存在するが、
どのイベントを「ターンの開始/終了」とみなして計上しているかの除外条件（人間の待ち時間・権限
プロンプト待ちを含むか否か等）が非公開でブラックボックスであり、本ツールの dedup 検証と同水準の
実データ突合ができない。そのため採用せず、上記の自前の transcript 走査（イベント列 → 人間プロンプト
分割 → ギャップ閾値分割 → union）を実装した。

### 既知の制約

- `active_gap_max_sec`（既定900秒）以下の権限プロンプト待ち・その他の無操作区間は実処理時間に
  含まれてしまう（同一セグメント内のギャップが閾値を超えない限り分割されないため）。
- バックグラウンド実行（長時間のビルド・テスト等）中にイベントが閾値を超えて発生しない区間は、
  実際には Claude が処理を継続していても実処理時間から除外される。

## 単価（pricing.json）出典

- 出典: https://platform.claude.com/docs/en/about-claude/pricing（2026-07-13 時点）
- `claude-sonnet-5` は 2026-08-31 まで導入価格（$2/$10 per MTok）が適用される。基準日はレポート
  生成日（JST）。基準日が `until` を超えると標準価格（$3/$15）に自動的に切り替わる
  （`cost_lib.rate_for()` は `at: date` を引数に取るため、任意の基準日を差し込んでテスト可能）。
- キャッシュ倍率（5m write ×1.25 / 1h write ×2.0 / read ×0.1）は導出値。モデル別に公式単価が判明
  次第、pricing.json の該当モデルエントリに `cache_write_5m` / `cache_write_1h` / `cache_read`
  （$/MTok 直接値）を追加すれば倍率より優先される（`rate_for()` 側で対応済み）。
- 2026-07-13 に公式ページで裏取り済み。base/キャッシュとも倍率導出値と完全一致。`claude-sonnet-5`
  の導入期間中はキャッシュ単価も導入価格（$2）基準の倍率適用であることを公式で確認済み
  （モデル別の明示キャッシュキー追加は不要）。
- `as_of` から `stale_after_days`（既定90日）を超えるとレポートに古さ警告が出る。

## config / var のルート分離

- `config/` `var/` `reports/` は `FABLE_COST_MANAGER_ROOT` で差し替え可能なデータルート
  （`cost_lib.repo_root()`）。テスト時はスクラッチルートに `config/` だけコピーすれば良い。
- `templates/` `scripts/` はコード資産として `cost_lib.code_root()`
  （`cost_lib.py` 自身の実位置から `parent.parent` で解決）を使う。これによりテスト用スクラッチ
  ルートを使う際に `templates/` までコピーする必要がない。

## フェーズ2向けの先行投資（今回実装したのはこの4点のみ）

1. `iter_usage(path, start_offset=0)` の `offset` 引数（増分パース用の席。フェーズ1は常に0固定）。
2. `cost_status.py --json`（statusline wrapper がそのまま読める形式）。
3. `var/` の予約名（`agg_state.json` / `monitor_cache.json` / `*.lock` 相当の置き場は未実装だが
   `var/` 配下に置く前提でディレクトリ構成を確保）。
4. `config/config.json` の `budget` キー（`default_thresholds` / `monitor_cache_ttl_sec` /
   `desktop_notify`）の席（フェーズ1では未参照）。

statusline wrapper・hook・増分キャッシュそのものは実装しない（やり過ぎ回避）。

## フェーズ2計画メモ（実装は次フェーズ）

> 注: 以下は作者環境（handoff スキル併用）を前提にした将来計画メモであり、本リポジトリ単体では未実装。

statusline は既存 `handoff_statusline.sh` を**無改変**のまま合成 wrapper（stdin を変数退避 →
BASE 出力保証 → 予算セグメント連結）で拡張する。表示はキャッシュ描画のみとし、集計は stale 時に
`flock` single-flight のバックグラウンド refresh（`iter_usage` の増分 offset パースを使う）で行う。
閾値 50/80/100% は `UserPromptSubmit` hook（`handoff_threshold_hook.sh` と同型）で会話に注入し、
`thresholds_fired`（`active_task.json` に既に席を確保済み）で重複通知を抑止する。hook は必ず
`exit 0` で終わる。

## 既知の制約

- `--scope global` は全プロジェクトの時間窓走査になるため、並行して動いている無関係セッションの
  usage を拾う可能性がある（レポートに注記を出す）。
- レポート生成コマンド自体のトークン消費は「until=now のスナップショット確定後」に発生するため
  集計に含まれない（軽微・許容）。
- 本実装の `uuid` グローバル dedup による resume 二重計上防止は、検証に使った実データ（単一の
  非 resume セッション）では発火するケースが無かった。ロジックは requestId dedup と独立した
  安全網として実装済みだが、resume を伴う実データでの追加検証は今後の課題。
