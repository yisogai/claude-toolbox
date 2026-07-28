# Changelog

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
