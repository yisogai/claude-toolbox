# Changelog

## v2026.08.14 — usage-report 新設・cost-manager 単価表更新

### usage-report（新規スキル）

- ディレクトリ配下（子・孫）で実行された全 Claude Code セッションを期間指定
  （`--month` / `--week`（JST 月曜始まり）/ `--from`+`--to`、いずれも JST）で一括集計する
  CLI + グローバルスキルを追加。出力は sessions.csv（1行=1セッション）・
  sessions_by_model.csv（セッション×モデル明細）・PNG 最大4枚（サマリカード／日別
  積み上げ／リポジトリ別／モデル別、ダークテーマ・日本語 Hiragino Sans）・summary.md。
- 設計上の要点（実データ調査に基づく）: セッション帰属は**起動時の `cwd`**（メイン jsonl の
  最初の `cwd`）でセッション単位に判定（`~/.claude/projects` のエンコード名は `/` と `.` が
  同じ `-` になる非可逆変換のため絞り込みヒントとしてのみ使用）。dedup は全セッションで
  単一の `Accumulator` を共有（resume/fork のファイル横断重複 817 件を実測確認）。
  `<uuid>/subagents/**` を含めて走査（Lav/git 実測でサブエージェント側はコストの 31.6%・
  トークンの 44.0%・dedup 後 API 呼び出し件数の 72〜82%。件数では大半を占め、
  コスト・トークンでも無視できない）。期間境界は UTC→JST 変換後に判定。
- セッションタイトルの判定を改善: ハーネスが user ロールで注入する疑似メッセージを
  「**ハイフンまたはアンダースコアを含む**小文字始まりのタグで始まる」正規表現＋定型文
  プレフィックスで一括判定し（個別列挙を廃止。`<div>` のような素の HTML タグは巻き込まない）、
  **メイン jsonl のみ**を対象に窓内（`[since, until)`）のユーザーメッセージを古い順に
  最大60件見て最初の人間発話を採る。人間発話が無いセッションでは
  `<scheduled-task name="X">` から `(定期実行) X` をタイトルにし、スラッシュコマンド
  展開本文（`# /` 始まり）は見出し行だけを採る（本文ダンプの防止）。
- 警告と注記を分離: `## 警告` は異常・要注意のみ、前提の説明は `## 注記`（summary.md の
  新セクション／stdout の `注記:`）へ。単価表の `as_of` より前の期間である旨は注記に移し、
  判定も期間の**開始日**で行うようにした（従来は終端で見ていたため
  `--from 過去日 --to 今日` で落ちていた）。
- コストは全モデルを従量課金単価で仮計算した参考値（Fable 等のサブスク包含分も
  「従量だったら」の額で表示）。単価・dedup・実処理時間は cost-manager の
  `cost_lib.py` を import 再利用（複製なし）。
- 検証: 7月分 Lav/git 実データで独立実装による再計算と全モデル・全トークン系統の
  一致を確認。走査順シャッフル・合成 transcript（壊れ行/孤児 subagent/timestamp 欠落/
  JST 境界）・空期間・不存在 root・matplotlib 不在などの異常系を敵対的検証済み。

### cost-manager

- `config/pricing.json` に `claude-opus-5`（input $5 / output $25 per MTok、
  billing: included）を追加、`as_of` を 2026-08-14 に更新。従来は未収載のため
  $0 計上され、opus-5 使用分（7月の Lav/git 実測でトークンの約10%）が
  集計から漏れていた。cost-manager 本体・usage-report 双方のレポートに反映される。
  単価の確認日と根拠は同エントリの `_source` に残してある。

## v2026.08.01 — 運用転換: harness-fablize 凍結・model-policy の Fable メイン対応

作者環境がメインループを Opus から Fable 5 へ移行したことに伴う反映。

### harness-fablize（凍結）

- README 冒頭に凍結バナーを追加（Opus メイン運用時代のアーカイブ。Opus メイン環境向けの
  参考実装として残置）。凍結の根拠: (1) Claude 5 世代の公式プロンプトガイダンス
  「旧モデル向けの過剰な指示・検証強制は品質を下げる」 (2) 完了ゲートの実運用監査で
  4日間のブロック26件中88%が誤爆と確認（主因: scratchpad の一時スクリプトを拡張子だけで
  製品コードと誤判定／RSpec 系コマンドが検証パターン未対応）。真陽性は3件のみだった。
- 検証台帳に「テスト基盤を探すだけのコマンド（`find -name "pytest.ini"` 等）を部分文字列
  マッチで検証行為と誤記録する」偽陽性の欠陥があることを監査で確認（未修正のまま凍結。
  再有効化する場合はパターン照合を先頭トークン基準に改修すること）。
- `install.sh --switch-model` を方向引数化（`--switch-model[=opus|fable]`、値省略は
  従来どおり opus）。Fable メイン機で再インストールすると model が黙って opus に戻る
  事故の防止。
- `UNINSTALL.md`: agents 削除コマンドへの fable-advisor 追加・model 書き戻しコマンド例の
  追加・「別系統の配線（model-policy スキル等）は本手順の範囲外」節の追加・settings.json
  の hooks 削除 jq を「同居 hook を巻き添えにしない」安全版に差し替え。
- 作業プロトコル（claude-md）: 「応答の書き方」を 5→3 行に圧縮（Fable/Opus 混在運用に
  合わせたモデル非依存の最小規範化）。

### model-policy

- **追記（同日・改訂2）**: 数日の実運用で「メインの Fable が自己完結しすぎて週次50%枠に
  早期到達する」副作用を確認したため、規範を再調整。(1) 機械的に完結する作業（明確な
  単発実装・修正、テスト実行、大量ファイル読み・要約）は opus サブエージェントへ委譲し、
  Fable は判断・設計・レビューの要所に温存する (2) サブエージェント effort の下限を
  medium に引き上げ（low 廃止。opus 側の使用枠に余裕がある環境向けの設定）。
- fable-advisor 例外運用の廃止（2026-07-31）を README / SKILL.md / スクリプトの
  コメント・案内文に反映。`fable_exempt_subagent_types` 機構はコードとして残るが未使用。
- CLAUDE.md 貼付テンプレを新運用（メイン=Fable が実質作業も行う・サブは opus 既定 +
  effort でコスト制御・sonnet は大量 fan-out 限定。Sonnet 消費も共通週次枠に計上される
  実測 2026-08-01 に基づく）へ更新。
- reminder hook のメインモデル・ドリフト警告を反転（旧: model が fable なら警告 → 新:
  fable / opus 以外の恒久設定のみ警告）。

## v2026.07.28 — Linux（uutils coreutils）環境での completion_gate 修正

WSL2 + uutils coreutils の環境で completion_gate が「ブロックすべき場面でブロックしない」
（fail-open）状態になっていた不具合の修正。macOS では顕在化しない環境依存の問題。

### harness-fablize

**hooks/completion_gate_stop.sh**
- mtime 取得を `stat -f %m … || stat -c %Y …` の連結から、形式ごとに個別実行して数値に
  なったものだけを採用する方式へ変更。uutils の `stat` は BSD 形式 `-f` を「ファイルシステム
  情報の表示」と解釈し、その出力を **stdout** に出したうえで exit 1 するため、`A || B` では
  両方の stdout が同じコマンド置換に入り、mtime が複数行の非数値になって数値ガードで
  素通し（fail-open）になっていた。GNU coreutils は `-f` のエラーを stderr に出すため、
  この壊れ方は uutils 環境でのみ発生する

**vision/render.sh**
- `sizeof_file()` も同じ書き方だったため同方式（`-c %s` → `-f%z` → `wc -c`）に統一。
  サイズが非数値になると `[[ "$size" -gt 0 ]]` が算術エラーで失敗し、描画完了を検出できない
  まま watchdog が Chrome を強制終了する経路があった

検証: macOS（BSD stat）と WSL2（uutils stat）の双方で `tests/canary.sh` が PASS=20 FAIL=0
（修正前の uutils 環境は PASS=17 FAIL=3）。

## v2026.07.27 — effort 割当の明示化

前版で agent frontmatter に導入した effort 割当を、「検証は生成以上の effort」という原則で
統一し、Workflow の各段にも明示した。セッション既定を上げる運用（medium→xhigh）に合わせた
変更で、既定を上げるだけだと effort 未指定のサブエージェントが全部それを継承してしまうため、
「割当を明示する」変更とセットで入れている。

### harness-fablize

**agents**
- verifier: `effort: high` → `effort: xhigh`（implementer は `medium` のまま据え置き）

**workflows**
- deep-review / implement-verified の全 `agent()` 呼び出しに `opts.effort` を明示
  （fan-out・実装=medium / spec・synthesize=high / verify・judge=xhigh）。`meta.phases` の
  表示にも effort を併記し、進捗表示から実際の割当が読めるようにした

### effort の挙動メモ（実測、2026-07-27）

セッション既定を xhigh にした状態で各エージェントに `echo $CLAUDE_EFFORT` を実行させて確認
（transcript の `effort` フィールドとも一致）。ハーネスを使う側が踏みやすい点を残しておく。

- **frontmatter の effort はセッション既定に勝つ**（既定 xhigh でも implementer は medium のまま）
- **frontmatter を持たない組み込み agent（Explore / Plan / general-purpose 等）はセッション既定を
  継承する**。下げる手段がないため、セッション既定を上げるとそれらの探索コストがそのまま上がる
- **agent 定義はセッション起動時に読まれる**。frontmatter の変更が効くのは次のセッションから
  （`settings.json` の `effortLevel` も「新セッションの既定」で、現行セッションへは `/effort`）
- 環境変数 `CLAUDE_CODE_EFFORT_LEVEL` は frontmatter より強く、恒久設定すると割当が全て潰れる

注意: effort を上げることが品質向上につながるかは未確認。内部評価ではむしろ medium が同等以上
（品質同等・コスト約半分）という観測があり（n=3）、この版の xhigh 化は測定より先に適用している。

## v2026.07.26 — Opus 5 世代対応

Opus 5（2026-07-24 リリース）の公式移行ガイドと、Claude Code の lean system prompt 化
（v2.1.154〜、本体プロンプト約8割削減。Opus 5 版は応答形式の規定を持たない）への対応。

### harness-fablize

**作業プロトコル正本**
- 「verifier サブエージェントを完了前に必ず通す」ルールを撤去。Opus 5 は自己検証を内在化して
  おり、検証を指示する文は過剰検証（トークン浪費）を招くという公式ガイダンスに従った。
  代わりに公式推奨の完了規律（易しい部分だけで切り上げず、全部終わってから完了と言う）を追記
- 「### 応答の書き方」節を新設。Opus 5 の system prompt から消えた応答形式規定の空白を埋める:
  内容の区分が伝わる構造で書く／原因は「なぜ」を2層たどる／複数案は推奨+評価軸を付ける／
  ツール実行より先に発話へ返答する。指示予算を 40→50 行へ拡大（本体プロンプト8割減が根拠）

**model-policy 節（CLAUDE.md 挿入文）**
- 委譲促進の文言を反転し、明示的キャップへ（既定は委譲しない・同時3体まで・検証は委譲しない。
  Opus 5 は委譲過多に振れるという公式ガイダンスに対応）
- 複数段階の実質タスクで Workflow オーケストレーションを既定とする恒久許可の1行を追加

**hooks / agents**
- workflow_nudge: 注入文の先頭に「ツール実行より先に、この発話への返答を本文で書く」を追加
  （lean prompt の「まず動け」方針で返答を飛ばして作業に入る癖への対策。行動直前に毎回届く
  層が最も効くという実測報告に基づく）。verifier への誘導句は削除
- verifier: 自発起動（Use PROACTIVELY）を廃止して opt-in 化。`effort: high` を frontmatter に追加
- implementer: `effort: medium` を frontmatter に追加。effort の割当はすべて agent frontmatter で
  管理する（注意: 環境変数 `CLAUDE_CODE_EFFORT_LEVEL` を恒久設定すると frontmatter の割当が
  すべて上書きされるため、一時変更は `/effort` かエイリアスで行う）

**install.sh**
- `--switch-model` の設定先を固定モデル ID から `opus` エイリアスへ変更
  （Claude Code v2.1.219+ で Opus 5 に解決。Max プランでは 1M コンテキストが自動適用）

**効果の実測（非公開の評価基盤による）**
- 上記の削減は内部評価で無害を確認したうえで適用（品質同等のままコスト1〜4割減）
- Opus 5 素は旧世代で識別力のあった失敗クラスの大半を天井化。ただし網羅的なバグ探索の
  徹底では作業プロトコルの効果が残存しており、該当ルールは保持
- effort medium で同品質・コスト約半分の観測あり（n=3、追試中）
- 「応答の書き方」節と nudge の返答先行は対話品質の予防的調整であり、未測定（観察中）

### model-policy
- drift 警告（恒久 model が fable のときの注意喚起）の推奨文言を、固定モデル ID から
  `opus` エイリアスへ変更。モデル世代交代で文言が陳腐化しない形にした

### 必要環境
- Claude Code v2.1.219 以降（`opus` エイリアスが Opus 5 に解決されるバージョン）
