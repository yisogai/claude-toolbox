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

**呼び出し元セッションの無条件除外（flush 競合対策）**: マーカー検出には flush 競合の弱点がある。
レポートスクリプト自身を起動した Bash tool_use レコード（＝マーカー）は、スクリプトが transcript を
読む時点でまだファイルに書かれていないことがあり、その場合マーカー検出による除外が失敗して終端が
レポート依頼ターンの冒頭まで伸びてしまう。そこで `cost_report.py` は
`scan_activity(..., invoking_session_id=lib.current_session_id())` として呼び出し元セッション ID
（env `CLAUDE_CODE_SESSION_ID`）を渡し、`scan_activity()` はそのセッションのメインファイル
（TFile なら `session_id`、生 Path ならファイル名 stem との一致で判定）についてはマーカーの有無に
関係なく上記の最終ターン除外（`L` 決定 → 連続ターン境界列の遡り → `ts >= L` 除外 → `report_cutoff`
登録）を無条件で適用する。これが正当なのは、スクリプトが計測対象セッションの内側から実行されている
場合、そのメインファイルの開いている最終ターンは構造上必ずレポート実行ターン自身だからである。
マーカー検出は、別セッションのファイルや `invoking_session_id=None`（env 未設定・手動実行等）の
場合のフォールバックとしてそのまま残る。

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

## pace 実装（フェーズ2 その1・週次枠ペーシング）

サブスクの週次枠（`rate_limits.seven_day`）・5時間枠（`five_hour`）・Fable の 50% サブ枠を
「使い切れているか」を statusline に常時表示し、調整（並列度・effort）の判断材料となるサンプルを
蓄積する。理想は「リセット時点で 100% 近くまで使い切る」こと（余らせるのも早期枯渇も NG）。

### データ源

Claude Code は statusline コマンドの stdin JSON に
`rate_limits.five_hour.{used_percentage,resets_at}` と `rate_limits.seven_day.{...}` を渡す
（Pro/Max、セッション初回 API 応答後のみ。各窓は独立に欠落しうる。`resets_at` は Unix epoch 秒。
公式 docs: https://code.claude.com/docs/en/statusline ）。**Fable 専用のサブ枠フィールドは無い**ため、
Fable の消費は transcript から推定する。

### 仮定（いずれも [未検証]。実装ではパラメータ化してある）

- **[未検証] A1**: 週次枠の消費は各モデルの「USD 換算コスト」に比例する（`pricing.json` の単価で
  重み付け）。したがって Fable の枠内シェア = `fable_usd / total_usd`（窓内・全プロジェクト・dedup 済）、
  Fable 推定使用率 = `seven_day.used × share`。実際の枠消費がトークン数基準・リクエスト数基準・
  モデル別重み基準のいずれであるかは公開されておらず、検証できていない。
- **[未検証] A2**: Fable の 50% 上限は同じ `seven_day` 窓に対する比率である
  （`pricing.json` の `_billing_note` 記載の「週次リミットの50%まで」の解釈）。`fable_cap_pct` で変更可。
- **[未検証] A3**: 窓の開始 = `resets_at − 7日`（5時間枠は `− 5時間`）。ローリングウィンドウであれば
  この仮定は成り立たないが、`resets_at` 以外に窓の起点を知る手段が無い。
- 較正（calibration）: 上記 A1 を実測で裏付ける・置き換えるための材料として、`samples.jsonl` の
  隣接ペア（Δused ≥ 1%）について同区間の USD 増分 / Δused の中央値（= 週次枠 1% あたりの USD）を
  算出して `cache.json` に載せる。ペアが 3 未満なら `null`。この値が安定すれば
  「USD → 枠%」の直接換算に切り替えられる（今回は表示のみで、推定には使っていない）。

### 別ライセンスのセッション除外（仕様）

`license-switch`（同リポジトリ `license-switch/`）で案件ディレクトリごとに別アカウントへ
切り替えている場合、そのセッションの消費は**別の週次枠**に載る。`pace_refresh.py` は
セッション単位で最初の `cwd`（メイン jsonl の先頭の `"cwd"` フィールド。無ければ当該ファイル自身の
先頭 `"cwd"`）を取り、次のいずれかに該当するセッションを集計から除外する。

1. その `cwd` か祖先ディレクトリに `.envrc` があり、内容に
   `generated-by: claude-toolbox/license-switch` を含む（direnv と同様に**最も近い** `.envrc` だけを見る）。
2. `config.json` の `budget.pace.exclude_cwd_prefixes`（手動指定）のいずれかで始まる。

除外したセッション数は `cache.json` の `notes` に載る。`cwd` が読めないセッションは除外しない
（保守的に計上する）。`--no-exclude-license` で 1. を無効化できる。
`exclude_cwd_prefixes` は絶対パス（`~` 可）で書く。`~` は展開し、末尾スラッシュは無視して
ディレクトリ境界（`cwd == prefix` または `cwd` が `prefix/` で始まる）で一致させる。

**[前提]** この除外が意味を持つのは、**direnv によるライセンス切替が実際に効いている**場合に限る。
`license-switch` には「無効な OAuth トークンが設定されていると Claude Code が無言でメイン
アカウントへフォールバックする」既知挙動がある（`license-switch` の実測知見）。その場合、
実際にはメイン枠を消費しているセッションを `.envrc` の存在だけを見て除外してしまい、
**週次枠の集計が過小になる**。`cache.json` の除外件数が想定より多いときは、対象ディレクトリで
実際に別ライセンスが効いているかを確認すること。

### 未収載モデルがあるときの扱い（Fable 推定不能）

`pricing.json` に単価が無いモデルはトークン数だけ計上され USD が 0 になる。これを放置すると
(a) 未収載の Fable（例: `claude-fable-6`）はシェア 0 → `F≈0%` を薄色で無警告表示し、
「fable のまま使う余地あり」という**逆の推奨**を出す、(b) 未収載の非 Fable は USD の分母を
縮めて Fable シェアを過大にする。したがって未収載モデルが 1 つでもあれば
`cache.json` に `unknown_models` / `unknown_tokens` を載せたうえで
`fable.share` / `fable.est_pct` / `fable.pace` を `null` にし、statusline は `F?!`（警告色）、
`pace_report.py` は推奨の代わりに「未収載モデルがあるため Fable 推定不能: …」を出す。
対処は `pricing.json` に単価を追加すること。

### 集計失敗のネガティブキャッシュ

statusline は `cache.json` の mtime だけで refresh の要否を判定する。そのため refresh が
キャッシュを書かずに落ちると、statusline が呼ばれるたびに refresh を起動し続ける
（無音の再起動ループ）。`pace_refresh.py` の `main()` は全例外を捕まえ、
`{"computed_at", "error", "notes"}` の最小キャッシュを atomic に書いてから exit 1 する。
mtime が進むので TTL のバックオフが効き、statusline は `error` キーを見て `F!`（警告色）を出す。
併せて、読めない `*.jsonl` 1 つで全体が落ちないよう `cost_lib.iter_usage()` の `open()` は
`OSError` を捕まえて 1 件 skip する（dedup ロジックには手を触れていない）。

### 構成

- `scripts/pace_statusline.sh`: 合成 wrapper。同期処理は jq + ファイル読みだけで、python を同期で
  呼ばない。`license_statusline.sh`（→ `handoff_statusline.sh`）を無改変で呼んで BASE を得てから
  pace セグメントを ` | ` で連結する。BASE が空でも pace セグメントは出す。
- `scripts/pace_refresh.py`: 集計・キャッシュ更新。`cost_report.py --scope global` と同じ経路
  （`iter_transcripts(glob_all=True)` → `collect_dedup_rows()` → `aggregate()`）を再利用しており、
  dedup ロジックは複製していない。
- `scripts/pace_report.py`: 人間向け CLI（スキルの「ペース確認」から呼ぶ）。
- 状態は `var/pace/`（`samples.jsonl` / `cache.json` / `refresh.lock` / `sample.lock`）。

### サンプル記録と single-flight refresh

statusline は `rate_limits` があるときだけ `var/pace/samples.jsonl` へ 1 行追記する
（`sample_min_interval_sec`（既定60秒）未満の連続入力はスロットル。直近行は `tail -1` で読み、
壊れた行は無視する）。キャッシュが無い／`refresh_ttl_sec`（既定300秒）より古いときは
`pace_refresh.py` をバックグラウンド起動する。

記録される窓は `used_percentage` と `resets_at` が**両方揃っているもの**だけで、揃わない窓は
`null` として記録する（片方だけの窓を記録すると、後段が「有効サンプル無し」と誤判定する）。
`resets_at` は Unix epoch 秒として妥当な範囲（`0 < x < 2**31`）のものだけを受け付ける
（ミリ秒値が来ると `datetime.fromtimestamp` が `year out of range` で落ちるため）。

「直近行を読む → 追記する」は check-then-act であり、statusline が並行起動されると 60 秒
スロットルが破れる（10 並列で 8 行入ることを実測）。そのため記録は `var/pace/sample.lock`
（refresh 用とは別の mkdir ロック。stale 判定は 60 秒）で直列化する。**ロックが取れなければ
記録は諦める（best-effort）**。サンプルは統計材料であり、1 サンプルの欠落より statusline を
待たせないことを優先する。

`pace_statusline.sh` の `SCRIPT_DIR` は bash だけで symlink を解決する（`readlink` のループ +
`cd -P`）。macOS の `readlink` に `-f` が無く、`python3 -c 'os.path.realpath(...)'` は同期 python
呼び出しになるため使えない。symlink 経由で配置する場合は `FABLE_COST_MANAGER_ROOT` の明示も推奨。

ロックは **`mkdir` による single-flight**。ミニ仕様では `flock -n` を第一候補としていたが、
macOS には `flock` コマンドが無く（実機で確認）、`mkdir` ロックは Linux/macOS の双方で正しく
動作するため一本化した（二重実装は検証面積が増えるだけと判断）。ロックディレクトリの mtime が
10 分を超えたものは stale として奪う。子プロセスは `nohup … &` で stdin/stdout/stderr を切り離し、
終了時に自分で `rmdir` する。

### 表示と色

`📅W <used>%/<elapsed>% ·<pace>` / `F≈<est>%/<cap>% ·<pace_f>` / `⏱5h <used>%/<elapsed>%`。
`pace = used / elapsed`、`elapsed < 1%` のときは `·—`。キャッシュが無ければ `F?`、
`refresh_ttl_sec × 3` より古ければ薄色 + 末尾 `?`。

色は `on_pace_band`（既定 `[0.8, 1.1]`）を基準に、薄色（余らせ気味）/ 緑（band 内）/
黄（band 超過）/ 赤（band 超過かつ枯渇が早い）の4段。

**仕様からの読み替え**: ミニ仕様の赤条件 `exhaust_before_reset`（`pace > 1` かつ
`100/used × elapsed < 100`）は、展開すると `elapsed/used < 1` すなわち `pace > 1` と数学的に同値で、
band 判定（`pace ≤ 1.1` は緑）を飲み込んで黄が到達不能になる。そこで赤は
「枠を使い切る時点（`elapsed × 100 / used`）が窓の `exhaust_margin_pct`%（既定80）経過より前」
と読み替えた。4段すべてが到達可能であることは実行確認済み
（pace 0.35 薄色 / 0.96 緑 / 1.23 黄 / 1.66 赤）。

### Fable の判別

`billing_class()` は使えない。`pricing.json` 上 Fable は `billing: "included"`（Max/Team Premium に
恒久包含）であり payg バケツに入らないためである。pace では
`cost_lib.is_fable_model()`（モデル名に `fable` を含むか）で判別する。

### 性能

7日窓の全プロジェクト走査は実データ（dedup 後 13,036 行）で **6.6 秒**。`duration_sec` を
`cache.json` に記録する。TTL 300 秒で十分なため増分 offset パースは実装していない。

### statusLine への組み込み

`~/.claude/settings.json` の `statusLine.command` を `pace_statusline.sh` に向ける
（本実装は settings.json を書き換えない。README の手順を参照）。

## Codex 使用量レーン（フェーズ2 その2）

Claude 側（transcript 集計）と同じ画面で Codex 側の消費（クレジット）を見られるようにするレーン。
入力は codex-bridge が書く使用量台帳 `codex-bridge/var/codex_usage.jsonl` で、**読み取り専用**
（cost-manager からは一切書かない）。Claude 側の dedup 集計（`iter_usage` / `Accumulator` /
`collect_dedup_rows` / `aggregate`）には手を触れていない。台帳は 1 ジョブ 1 行で、そもそも
requestId 重複のような二重計上の構造が無いため dedup も不要である。

### データ源と解決順

- パス: env `FCM_CODEX_LEDGER` > `config.json` の `budget.pace.codex_ledger` > 既定
  `../codex-bridge/var/codex_usage.jsonl`。相対パスは `cost_lib.code_root()`（スクリプトの実位置）
  基準で解決するため、`FABLE_COST_MANAGER_ROOT` を差し替えるテストでも既定値は移動しない。
- 単価: `codex-bridge/config/codex_pricing.json`（credits per MTok）。台帳パスとは独立に
  `code_root()` 基準で解決する（台帳だけスクラッチへ向けても単価表は本物を読めるように）。

### 行の扱い

- `ts` は **ISO8601 文字列と epoch 数値の両対応**。codex-bridge の現行実装
  （`codex_run.py` の `append_ledger` → `codex_lib.iso()`）は `2026-08-22T09:15:03Z` 形式の
  ISO 文字列を書く（実装を読んで確認済み）。epoch は `0 < x < 2**31` の範囲だけ受ける
  （ミリ秒値を秒として誤解釈しないため）。
- 無視する行（`ignored_rows` として件数を注記に出す）: `mock` が非 null ／ `usage` が無い or dict
  でない ／ JSON として壊れている ／ `ts` が欠落・不正 ／ クレジットが NaN・inf。窓外の行は
  「無視」ではなく `out_of_window` として別に数える（`cost_report.py` は「範囲外 N 件」と注記する）。
  NaN・inf を合算すると合計が NaN になり、`json.dumps` が JSON として不正な `NaN` リテラルを
  書いて statusline の jq がキャッシュ全体を読めなくなる（🅒 も F も消える）ため、
  `aggregate_codex()` が `math.isfinite()` で落として `ignored` に数える。
- 台帳が**存在するのに開けない**ときは `stats["unreadable"]=1` を立て、「台帳を読めませんでした:
  <path>」と注記する（「台帳がまだ無い」= 正常、と区別するため）。
  ※ codex-bridge の現行台帳には `mock` フィールドが**常に書かれる**（実行時は `null`）。
  モック実行の行はそもそも書き手側で台帳から除外されているため、読み手側の
  「`mock` が非 null なら無視」は二重防御である（書き手が仕様を変えても効く）。
- クレジットは行の `credits_est` を優先し、無ければ単価表から再計算する（`codex_credits_for()`）。
  計算式は codex-bridge の `codex_lib.credits_est()` と同一（いずれも [未確認] の解釈:
  `input_tokens` は cached を内包 / `cache_write_input_tokens` は input 単価 /
  `reasoning_output_tokens` は output に内包）。単価も `credits_est` も無いモデルは 0 として扱い、
  `unknown_models` と注記に出す。

### 窓と % の扱い（[未検証]）

- Codex の枠は「5時間窓 + 週次」で**絶対値が非公開**。したがって既定では % を出さず、消費
  クレジットと件数だけを出す。`budget.pace.codex_weekly_credits` を設定したときだけ
  `used_pct` / `pace` / `projected_end_pct` を計算する（未設定なら `weekly_cap: null`）。
- 窓は Claude の `seven_day` 窓（`resets_at − 7日 〜 now`）をそのまま流用する。**Codex 側の週次窓は
  リセット時刻が別**なので近似であり、`codex.notes` に毎回明示する。
- 5時間窓は「窓終端から遡った 5 時間」。Codex 側の 5 時間窓のリセット時刻は取得手段が無い。
- `cost_report.py` の Codex 窓の終端は **`until`（Claude 行と同じ）** であり、`end_display` では
  ない。`end_display` は「最終 Claude アクティビティ」へ手前に補正されるため、それを終端にすると
  「Claude は待っているだけで、その間に完了した Codex ジョブ」が無注記で落ちる。

## Codex 公式 used_percent の自動サンプリング（フェーズ2 その3 / codex-official）

Codex 側の消費 % を**一次情報**として取り、`🅒` を `📅W` と同格の表示に昇格させるレーン。
台帳（クレジット）は自前の推定値でしかないため、公式 % が取れるならそちらを窓の基準にする。

### 一次情報（2026-08-24 実機確認・この形を正とする）

`GET https://chatgpt.com/backend-api/wham/usage`（`Authorization: Bearer <access_token>` +
`chatgpt-account-id: <account_id>`）。認証情報は `$CODEX_HOME/auth.json` の `tokens.*`。
応答の `rate_limit.primary_window` に `used_percent` / `limit_window_seconds` / `reset_at`。

### 設計上の判断

- **数値だけを保存する**。応答には `email` / `user_id` / `account_id` が含まれるので、
  `codex_official._shape()` で `{plan_type, primary{used_percent, limit_window_seconds,
  reset_at}, secondary}` に削ぎ落としてから `var/pace/codex_official_samples.jsonl` に書く。
  `plan_type` も `^[A-Za-z0-9_.-]{1,32}$` に一致するときだけ通す（想定外文字列＝識別子混入の保険）。
- **例外に中身を載せない**。fetcher の例外は型名だけ、HTTP エラーはステータスだけを
  `OfficialError` の自前メッセージに載せる（本文は読み捨てる）。トークンを持つローカル変数は
  `finally` で落とし、traceback のフレームに残さない。テストは合成トークン・合成メールを
  仕込んだうえで、隔離ルート配下の全ファイルと stdout/stderr を**文字列走査**して検証している。
- **auth.json は読むだけ**。トークンのリフレッシュは codex CLI の仕事で、401 は注記に
  「codex CLI を一度実行すると回復することがある」と書くだけにする（勝手に書き戻さない）。
- **サンプリングは refresh からのみ**。statusline から同期ネットワークを呼ぶと statusline が
  ブロックする。スロットル（`min_interval_sec`、既定 900）は
  「**後方読みで最初に見つかった有効行**の ts」で判定する（`read_official_samples()` は末尾
  64KB だけを後方に読み、壊れた行・`ts` や窓が範囲外の行を飛ばして有効な最終行 1 件だけを返す。
  全行パースはしない）。不正行を飛ばすのは、悪いサンプルが最終行に居座るとスロットルの間ずっと
  それが再利用されるため。サンプル jsonl は 5,000 行を超えたら先頭半分を切り詰める
  （`atomic_write_text`。最新 1 件しか使わないので履歴は捨ててよい）。
- **タイムアウトは二段**。`timeout_sec`（既定 10）は urllib の**1 操作あたり**の上限で、DNS 解決や
  複数アドレスへの接続で実時間は積み上がる。そこで fetch を別スレッド（daemon）で回し、
  `timeout_sec * 3` を**全体の壁時計期限**（`time.monotonic()`）として `join()` で見張り、
  超過したら諦めて注記にする。取り残したスレッドは refresh の終了とともに消える。
- **enabled は真偽値のみ**。`budget.pace.codex_official.enabled` は JSON の `true` のときだけ真。
  `"false"` / `"true"` のような文字列は（truthy でも）偽として扱い、型警告を注記に出す。
  `codex_official.py` の CLI も同じ判定に従い、無効なら `--force` でも取得せず exit 1。
- **fetcher 注入**（`fetch_official(..., fetcher=)`）でテストはネットワークを使わない。
  サブプロセス経路（`pace_refresh.py`）はテスト専用 env `FCM_CODEX_OFFICIAL_FIXTURE` で応答を
  差し替える（`{"status","body"}` / `{"error"}` / `{"sleep": 秒}`）。テストの `setUp` は
  `codex_official.enabled=false` と存在しない `CODEX_HOME` の二重で隔離してあり、既存テストが
  誤ってネットワークへ出ることはない。フィクスチャ由来のサンプルには `"fixture": true` が付き、
  pace の注記に「[テスト] フィクスチャ応答を使用」が必ず出る（本番で無警告に効かせない）。
- **窓の値は範囲検証してから使う**。`limit_window_seconds` は `0 < span <= 7日 × 8`、
  `reset_at` / `ts` / `window_start` は `0 < x < 2**31` の Unix 秒。NaN / inf / 1e12 /
  ミリ秒・マイクロ秒・ナノ秒単位の値をそのまま `datetime.fromtimestamp()` に渡すと
  ValueError / OverflowError で refresh 全体が落ち、**Claude 側の集計まで巻き添え**で
  エラーキャッシュになるため。外れた場合は「窓が不正なため無視しました」の注記付きで
  公式レーンだけを `None` にする。
- **窓**: 公式サンプルがあるとき Codex 節の窓は公式窓（`reset_at − limit_window_seconds` 〜 now）。
  無いときは従来どおり Claude の `seven_day` 窓の近似（注記も従来どおり）。公式窓は Claude の
  サンプルに依存しないので、**Claude 側のサンプルがまだ無くても Codex 節は出る**
  （`pace_report.py` も「Claude 側サンプルなし」の断り付きで Codex 節だけを出して exit 0）。
  台帳側の経過率・ペースの分母も公式窓の実幅（`reset_at − window_start`）にする。7 日固定に
  すると、公式窓が 7 日以外のときに公式行と台帳行のペースが食い違う。
- **自動較正**: `weekly_cap_est = 公式窓内の台帳クレジット ÷ (used_pct / 100)`。
  `used_pct < 1`（整数丸めで 0 になりやすい）や窓内クレジット 0 では算出しない。
  % とペースの分母は「手動 `codex_weekly_credits` > 自動 `weekly_cap_est`」の優先で選び、
  どちらを使ったかを `cap_source`（`"manual"` / `"estimated"` / `null`）に残す。

### [未検証]

- `used_percent` の丸め粒度（整数丸めと推定。週内に約 46 クレジット消費済みでも 0 が返る実測）。
  したがって `weekly_cap_est` の誤差は大きい。
- 窓アンカー（`reset_at`）の安定性。
- 非公開 API であり予告なく変わりうる。壊れた場合は取得失敗として注記に出て、表示は従来の
  クレジット表示へフォールバックする（refresh 自体は落ちない）。

### 出力先

- `pace_refresh.py`: `cache.json` に `codex`（台帳も公式サンプルも無ければ `null`）。
  公式レーンは `codex.official`（`used_pct` / `window_start` / `reset_at` / `elapsed_ratio` /
  `pace` / `projected_end_pct` / `plan_type` / `sampled_at` / `stale` / `secondary`）と
  `codex.weekly_cap_est` / `codex.cap_source`。
- `pace_statusline.sh`: `codex` があるときだけ末尾に `🅒` セグメントを足す。`codex.official` が
  あればそれを優先して `🅒 12%/34% ·0.35`（色は `📅W` と同じ band 判定・`stale` は薄色 + `?`）、
  無ければ従来のクレジット表示へフォールバックする。cache.json を読む既存の jq 呼び出しに
  フィールドを足しただけで **jq の起動回数は増やしていない**。台帳が無い場合・公式サンプルが
  無い場合の出力は変更前と**バイト単位で同一**（テストで担保）。
- `pace_report.py`: 「Codex」節（先頭に公式行・較正推定・使用中の上限、続いて窓内クレジット・
  件数・モデル別・5時間窓・上限の有無・無視行数・注記）と `--json` の `codex` キー。
  値は `cache.json` の丸ごと引き渡し（再計算しない）。
- `cost_report.py`: レポート Markdown の「## Codex（参考）」表と `var/log/reports.jsonl` の
  `codex` キー。範囲は表示用の開始〜終了と揃える。`--scope session`（既定）は
  `claude_session_id` が対象セッション ID と一致する行だけ、`--scope global` は時間窓のみ。
  クレジットは USD 合計には**含めない**（別通貨・別枠のため）。PNG カードは変更していない。

## 既知の制約

- Codex レーンの `claude_session_id` は codex-bridge が env `CLAUDE_CODE_SESSION_ID` から拾うため、
  env 無しで実行された Codex ジョブは `null` になり `--scope session` では拾えない
  （件数を注記に出し、`--scope global` へ誘導する）。
- `--scope global` は全プロジェクトの時間窓走査になるため、並行して動いている無関係セッションの
  usage を拾う可能性がある（レポートに注記を出す）。
- レポート生成コマンド自体のトークン消費は「until=now のスナップショット確定後」に発生するため
  集計に含まれない（軽微・許容）。
- 本実装の `uuid` グローバル dedup による resume 二重計上防止は、検証に使った実データ（単一の
  非 resume セッション）では発火するケースが無かった。ロジックは requestId dedup と独立した
  安全網として実装済みだが、resume を伴う実データでの追加検証は今後の課題。
