<!--
生成: 2026-08-24、Claude Code セッション（メイン=Fable 5）。
Workflow（opus 10体: 5観点調査 → 各系統の敵対的検証、598k tokens・15分）＋メイン側の実機プローブ5本で構成。
[実測] = このマシン（codex-cli 0.149.1 / macOS）で実行して確認、[確認済] = web 一次情報で検証、
[未検証] = 未検証。実測とドキュメントが食い違う箇所は実測を正とし、その旨を明記。
姉妹編: 2026-08-22-claude-codex-collab.md（コーディング委譲の接続方式）。未コミット（保存場所はユーザー判断で変更可）。
-->

# ChatGPT サブスクの開発活用調査 — 画像生成・UI 認識・その他（2026-08-24 時点）

## 1. 結論サマリ

- **画像生成: ChatGPT サブスク枠内で、CLI からプログラム的に呼べる。** Codex CLI の組み込みツール
  `image_gen`（`$imagegen` スキル、モデル **gpt-image-2**）が feature flag `image_generation` = stable
  で有効 [実測]。`codex exec -s workspace-write "$imagegen …を生成して ./x.png に保存"` だけで
  1024×1024 の高品質 PNG が約 43〜76 秒で保存された [実測]。API キー不要・**ChatGPT プランの
  Codex 一般利用枠に計上**（公式: "counts toward your general Codex usage limits"）[確認済]。
- **画像編集も高精度。** 実 UI スクリーンショットを `-i` で入力し「ヘッダー文字を白に、17件を赤に」
  と指示すると、他の要素（日本語テキスト・レイアウト）を忠実に保った修正版が生成された [実測]。
  「レビュー指摘の修正案を視覚モックで提示する」用途が成立する。
- **UI 認識レビュー: `codex exec -i <screenshot>` の素の vision 性能で実用水準。** 5 問題を意図的に
  仕込んだテストページを画像のみで渡し、**5/5 全件検出**（青背景×青文字、薄い数値、ボタン融合、
  極小フッター、金額の左寄せという subtle な慣習逸脱まで）＋妥当な追加指摘 5 件。位置・重要度
  （Must/Should/Nice）付きで返せた [実測]。
- **ループは Codex 内で完結もできる**（chrome-devtools MCP 経由で Codex 自身が撮影→閲覧→指摘、
  `--approve-for-me` が必要 [実測]）。ただし推奨構成は「**撮影 = Claude 側（chrome-devtools MCP）、
  視覚レビュー = `codex exec -i`、修正 = Claude / codex --write**」の分業（§4）。
- **規約面: `codex exec` 非対話呼び出しと `image_gen` は公式想定内で安全。** 一方 chatgpt.com の
  ブラウザ自動化による画像取得は ToU の "Automatically or programmatically extract data or Output"
  に正面から該当し**採用不可**。ChatGPT.app の AppleScript/AX 自動化もグレーで、`image_gen` という
  安全な代替がある以上リスクを取る理由がない（§5）。
- その他の即効性が高い活用: (a) **codex cloud**（`--attempts N` の best-of-N 並列実装）、
  (b) **GitHub @codex review**（PR 差分レビュー、security review も Pro 対象）、(c) **web_search を
  使った調査委譲**（Claude の WebSearch 枠温存）、(d) **Codex automations**（定期実行）。§6 参照。

## 2. 実機検証の記録（このマシン、codex-cli 0.149.1）

| # | プローブ | コマンド要点 | 結果 |
|---|---|---|---|
| 1 | 画像入力レビュー | `codex exec --skip-git-repo-check -s read-only -i shot.png -o out.md "<画像のみで UI レビュー>"` | 仕込み 5 問題を全件検出＋追加指摘 5 件。位置・重要度付き表で出力。約 40 秒 |
| 2 | ツール一覧の自己申告 | 同上（プロンプトで列挙指示） | `image_gen__imagegen` / `view_image` / `web__run` / collaboration 系（spawn_agent 等）/ goals 系を確認 |
| 3 | 画像生成 | `codex exec … -s workspace-write "image_gen__imagegen で生成して ./gen.png に保存"` | 指示どおりのアイコン（角丸・青グラデ・白稲妻・透過角）1024×1024 PNG。約 43 秒 |
| 4 | Codex 自身の撮影 | `codex exec … --approve-for-me "chrome-devtools MCP で撮影→閲覧→指摘"`（9222 に headless Chrome） | take_screenshot 成功、仕込み問題をほぼ全件指摘 |
| 5 | 画像編集 | `codex exec … -s workspace-write -i shot.png "編集して ./edited.png に保存"` | ヘッダー白・数値赤の修正版（1422×1106）。他要素は忠実に保存 |

実測で踏んだ罠（Workflow 側の独立検証でも再現）:

- **`-i` を先・プロンプトを後に素で置くと、プロンプトが画像リストに吸われて stdin 待ちで無言失敗**
  （`-i` は可変長引数）。`codex exec "<prompt>" -i img.png` か `codex exec -i img.png -- "<prompt>"` に
  固定する。間に `-o` 等の別オプションが入る場合も安全。
- 空ディレクトリ等 trusted 外の `--cd` では **`--skip-git-repo-check` が必須**（無いと即終了、exit 0 のまま）。
- 非 TTY では **`</dev/null` を付ける**か stdin 方式にする（codex-bridge は stdin 方式で回避済み）。
- **`--approve-for-me` と `-s/--sandbox` は 0.149.1 実機では排他**（clap エラー。当セッションと
  Workflow 内エージェントの 2 回独立再現）。一方、導入 PR #36373 や解説記事は併用前提で書かれて
  おり**ドキュメントと実装が不整合**。sandbox を指定したい場合は `-c sandbox_mode=…` に寄せる。
- 既定の approval_policy=never のままだと **MCP ツール呼び出しが「requires approval」で拒否**される
  （chrome-devtools の take_screenshot で実測）。`--approve-for-me` で自動承認レビュー経由になる。
- read-only サンドボックスでも画像生成自体は成功し、保存先が `~/.codex/generated_images/<thread-id>/`
  になる [確認済]。cwd に置きたければ workspace-write。ラッパーには generated_images から回収する
  フォールバックを入れると堅い。
- 成否は exit code でなくイベント/出力で判定（姉妹編 §4 と同じ注意）。

## 3. 画像生成の運用設計（通常タスクへの組み込み）

**実行形（実測済み）:**

```bash
codex exec --skip-git-repo-check -s workspace-write -C <出力先dir> \
  -o /tmp/imagegen-last.md \
  '$imagegen <日本語プロンプト>。<絶対パス>/name.png に保存して。' </dev/null
```

- `$imagegen` を明示するとスキル起動が確実（自然言語だけでも動いたが揺らぎ報告あり）。
- モデルは gpt-image-2（2026-04-21 発表、最大 2K、テキスト描画・レイアウト改善）[確認済]。
- `--output-schema` で `{saved_path, width, height}` を返させると成否をパースで機械判定できる。

**枠とコストの設計:**

- 消費先は **Codex 一般利用枠**。画像を伴うターンはテキストのみの 3〜5 倍速く枠を消費する [確認済]。
  Pro（$200 = Plus 比 20x）の枠は潤沢だが、無限ではない。
- 大量バッチ（目安 10 枚超）や反復リテイクは Images API（別課金）への切替が公式推奨。API 単価は
  トークン制（画像出力 $30/1M）で、**1枚あたりは品質階層別**: 1024² で low $0.006 / medium $0.012 /
  high $0.048、2048² high で $0.09 程度 [確認済・検証で訂正済みの値]。
- **OPENAI_API_KEY による「自動」API 課金フォールバックは無い**（検証で反証済み）。公式 SKILL.md は
  「既定は built-in image_gen。CLI フォールバック（要 API キー）はユーザーが明示的に求めたときのみ」
  と規定。とはいえ意図しない経路を塞ぐ安全策として、ラッパーで env からキーを外すのは妥当。

**運用パターン:**

1. **単発生成**（アイコン・OGP・図版・ダミー素材・README ヒーロー画像）: 上の実行形をそのまま。
   1枚 60〜80 秒、タイムアウトは 180 秒/枚が目安。
2. **生成→目視→修正ループ**: 生成 PNG を Claude が `Read` で視覚確認 → 不満点を
   `codex exec resume <thread_id> -i <生成画像> '$imagegen …を修正'` で同スレッド修正。
   `resume -i` は公式ドキュメントにも記載あり [確認済]。base64 が残留してコンテキストが肥大する
   既知問題があるため、画像スレッドは短く切る。
3. **実画像の編集**（スクリーンショットへの修正案適用、素材の色替え等）: `-i <入力画像>` ＋編集指示
   [実測]。忠実性が高く、UI 修正案のビジュアルモックに使える。

**制約:** 添付は PNG/JPEG/GIF/WebP（GIF は先頭フレームのみ）[中確度]。巨大画像は 413 やコンテキスト
肥大の報告があるため、添付前に長辺 2048px へ縮小・3〜4 枚上限の前処理を推奨。

## 4. UI 認識レビューのループ設計

**推奨アーキテクチャ（案A・分業型）:**

```
(1) Claude 側: chrome-devtools MCP / headless Chrome で 3 ビューポート
    （1440 / 768 / 375）のスクリーンショットをファイル保存
(2) codex exec "<レビュー指示>" -i shot-*.png で指摘リスト
    （位置・重要度トリアージ付き、--output-schema で JSON 化）
(3) Claude（メイン）が Must/Should/Nice を裁定
(4) Must のみ codex-bridge 実装委譲 or Claude が修正
(5) 再スクリーンショット → before/after を `-i before.png,after.png` で
    Codex に相対比較させ、退行チェック
```

- vision 消費が ChatGPT Pro 枠に乗り、Claude 週次枠を温存できる。
- Codex CLI の**内蔵 browser_use は使えない**（公式: "Browser isn't available in Codex CLI or the
  Codex IDE extension"。features list で stable/true と出ていても CLI サーフェス非対応）[確認済・実測]。
  撮影を Codex に任せる場合は chrome-devtools MCP 経由一択。
- 案B（Codex 内完結）も実測で成立: 前提は (a) Chrome を `--remote-debugging-port=9222` で起動、
  (b) `--approve-for-me` 付与（`-s` と排他、§2）。MCP 自動承認は権限昇格なので、**ブラウザ検証専用の
  モードに限定**し、通常の実装委譲は approval never のまま分離する。

**設計の要点（先行事例・研究の検証済み知見）:**

- **反復は上限 3 回**を既定に（実サイト級の複雑 UI のみ 5 回まで。UI2Code^N: 合成 UI は N=3 で飽和、
  実サイトは N=5 まで改善）[確認済]。無制限ループは枠と context の浪費。
- **検出は VLM、修正はテキスト**。修正フェーズには画像でなく「指摘リスト＋セレクタ＋computed style
  数値」を渡す（Coding with Eyes: 検出への視覚寄与 +2.85% に対し修正時添付は +0.65%）[確認済]。
- **絶対採点をさせない**。「このUIは何点か」ではなく before/after の相対比較（どちらが優れるか・
  退行はないか）を問う（UI2Code^N の RVPO と同じ発想）[確認済]。
- **座標・色値を VLM に答えさせない**。位置は a11y スナップショットの要素参照で、余白・色・
  コントラストは getComputedStyle / Lighthouse の数値で裏取り（GPT-5.6 系でも大解像度の bbox は
  不正確という報告）。視覚的な位置特定が要る場合は番号ラベルやグリッドをオーバーレイして渡す。
- **画像は 1 周 2 枚まで**（現状＋参照）。過去周回の画像は捨て、テキストの指摘履歴だけ残す。
- **機械で判る違反は先に静的検査**（design token 逸脱・生 hex・マジックナンバーの grep）で潰し、
  VLM には「トークン準拠だが視覚的に破綻」だけを見せる。
- ピクセル diff は合否判定でなく**変更領域の切り出し**に使う（クロップだけ VLM に見せる）。
- レビュー段の reasoning effort は高めを維持（低 effort で位置特定が劣化する報告）。コスト調整は
  画像枚数と反復回数で行う。
- 雛形として OneRedOak/claude-code-workflows の design-review エージェント（Phase 0–7、
  3 ビューポート、Blocker/High/Medium/Nit、「解決策でなく問題を書く」原則）が流用価値大 [確認済]。
  Figma 突き合わせが要る案件では Figma MCP のメタデータ＋書き出し画像の両方を渡す。

**画像の渡し方の使い分け:** 外部スクリーンショット少数は `-i` 添付。リポジトリ内画像・自撮りスクショは
パスを本文に書いて `view_image` に開かせる経路もある。添付画像の 2048×768 リサイズ問題（issue #14555）
は「原寸保持が既定化されて解決した可能性」があり現行の実挙動は未確定 [未検証]。細かい文字が潰れる
症状が出たら、対象領域の切り出しで対処するのが確実。

## 5. 規約・リスク（3分類）

| 手段 | 評価 | 根拠 |
|---|---|---|
| `codex exec` の非対話呼び出し（Claude からの委譲含む） | **安全** | 公式が Non-interactive mode としてスクリプト・CI 用途を案内。ChatGPT アカウント認証での CI/CD 手順も公式文書化 [確認済] |
| Codex 内蔵 `image_gen` での画像生成 | **安全** | 公式組み込みツール。"counts toward your general Codex usage limits" と公式明記 [確認済] |
| chrome-devtools MCP で**自分のアプリ**を撮影・操作 | **安全** | 対象が自分の開発物であり ChatGPT の Output 抽出に当たらない |
| chatgpt.com を Playwright/CDP 自動化して画像生成・取得 | **明確に違反・採用しない** | ToU "What you cannot do" の "Automatically or programmatically extract data or Output"（原文逐語確認済み）に該当。保護機構回避を伴えば "bypass any protective measures" にも重複抵触 [確認済] |
| ChatGPT.app（Atlas 系ブラウザベース）の AppleScript `execute javascript` / AX 自動化 | **グレー・採用しない** | 名指し条項は無いが実質は Output の programmatic 抽出。`image_gen` という安全な代替がある以上、リスクを取る理由が無い [確認済+実機で sdef 確認] |

- enforcement 実態: 公式ヘルプが "Circumventing security or access restrictions" を deactivation 理由に
  明記。非公式 API プロキシ利用者の一斉 BAN 事例あり [中確度]。**このアカウントは Codex 委譲の基盤**
  であり、停止時の被害は自動化画像生成の便益を大きく上回る。
- 条文は変わる（ToU は改定 30 日前通知、Usage Policies 最終更新 2025-10-29）。参照条文・取得日
  （2026-08-24）を本ドキュメントに固定し、半年後の再確認を推奨。openai.com は素の HTTP を 403 で
  弾くため、再取得は `r.jina.ai` 経由が有効だった。

## 6. その他の活用法（提案）

即効性・Claude Code フローとの親和性の順:

1. **codex cloud の best-of-N 実装委譲**（リッチ活用の本命）: `codex cloud exec --env <ENV_ID>
   --attempts 3` で同一仕様を 3 案並列生成 → `codex cloud list/diff` を Claude が読んで裁定 →
   採用案のみ `codex cloud apply`。Claude 枠を使わず複数案比較ができる。前提として ChatGPT 側で
   GitHub 連携とクラウド環境の作成が必要 [確認済・EXPERIMENTAL]。
2. **GitHub レビューの多重化**: PR コメント `@codex review`（差分レビュー）、自動レビュー有効化、
   `@codex fix` での修正委譲。**`@codex security review` は research preview で Pro 対象**（Plus 不可）
   [確認済]。レビュー規約は AGENTS.md の `## Code Review Rules` 節で共有。ローカル非対話は
   `codex exec review --base/--uncommitted/--commit`（`--output-schema` は無視される既知バグに注意、
   姉妹編 §4）。
3. **調査の委譲（web_search）**: `codex exec` に `-c web_search="live"` を渡し、「広く浅く一次情報を
   集める」段を Codex へ。実機ツール一覧にも `web__run`（検索・ページ閲覧・画像検索）を確認済み
   [実測]。Claude の WebSearch/枠を温存できる。`--search` フラグは手元 exec の help に無いため
   `-c web_search` 指定で検証してから使う。
4. **Codex automations（定期実行）**: 旧 Pulse は 2026-06-17 廃止 → Scheduled tasks に統合 [確認済]。
   デスクトップアプリからローカルディレクトリ/worktree 指定の定期タスク（例: 毎朝 main の依存更新
   差分レビュー）を設定し、出力を Claude が朝一で読む。CLI からは作成不可。
5. **モバイル遠隔操作**: ChatGPT モバイルアプリから Mac 上のローカル Codex セッションを QR ペア
   リングで遠隔操作（2026-05-14 提供、ホスト macOS のみ）[確認済]。長時間タスクの承認・追い指示を
   外出先から。`codex remote-control`（experimental）も同系。
6. **Codex 内マルチエージェント**: collaboration ツール（spawn_agent 等）が exec でも露出 [実測]。
   「Codex 1 呼び出しで内部 fan-out」の余地があるが、枠消費と制御性は未検証 [未検証]。
7. **skills 資産の共有**: `~/.codex/skills`（現在空）に `~/.claude/skills` のうち Codex にも効く規約系
   SKILL.md をリンク/複製し、委譲先でも同じ規約を効かせる。
8. **deep research（月 250 回/Pro）は UI 専用**と割り切る。結果のプログラム取得手段は無く [確認済]、
   自動化する調査は 3 の web_search 経路へ。Deep Research API は別課金で不採用。
9. 不採用・終了: Sora API は 2026-09-24 停止予定、Atlas は 2026-08-09 終了 [中確度]。ChatGPT への
   自作 MCP 接続（developer mode）は Pro 個人だと書き込み制限があり優先度低。codex-plugin-cc は
   姉妹編の裁定どおり自動ループ基盤にはしない（手動 slash 用途の導入余地はあり）。
   `codex mcp-server` は非推奨化済みかつ**画像パラメータ無し**（要望 issue は not planned）[確認済]
   — 画像が絡む委譲は必ず CLI 経路で。

## 7. codex-bridge 拡張の提案（→ 同日 2026-08-24 に全件実装済み）

**実装状況**: 下記 1〜6 はすべて同日中に実装した（1・2・3 の一部と 5 = codex_run.py 拡張、
3 = ui_screenshot.py + ui-review/ui-compare テンプレート、4 = codex_cloud.py、6 = AGENTS.md.tmpl）。
使い方は codex-bridge の SKILL.md / README.md「マルチモーダル・拡張」を参照。
以下は設計判断の記録として原文を残す。

1. **`--image` 対応**（codex_run.py、小）: `-i` を repeatable で受けて argv に足す。**引数順の罠**
   （§2）をラッパー側で固定し、回帰テストを 1 本追加。resume 時の `-i` も通す。添付前の自動縮小
   （長辺 2048px）と枚数上限 3〜4 を前処理に。
2. **`imagegen` モード**（新スクリプト or codex_run.py のモード追加、小〜中): workspace-write ＋
   `$imagegen` 明示＋保存先絶対パス指定。実行後に `file` で PNG 妥当性を検証し、失敗時は
   `~/.codex/generated_images/` から回収。`--output-schema {saved_path,width,height}` で機械判定。
   タイムアウト 180 秒/枚。env から OPENAI_API_KEY を除外。
3. **`verify-ui` モード**（中): §4 案Aのループを1コマンド化（撮影は chrome-devtools MCP / headless
   Chrome、レビューは `-i`、before/after 相対比較）。`--approve-for-me` を使う案Bはこのモード限定で
   許可し、通常委譲と権限を分離。レビュー用 `--output-schema`（severity/location/issue/suggestion）と
   render_prompt テンプレート `ui-review` を追加。
4. **`cloud` モード**（中): `codex cloud exec --attempts N` → list/diff 裁定 → apply の配管。
5. **調査モード**（小): `-c web_search="live"` 付き exec ＋ web_search アイテムの抽出。
6. **AGENTS.md テンプレ**に `## Code Review Rules` 節を追加し、CLAUDE.md から参照（二重管理回避）。

## 8. 情報源と検証結果

- Workflow: opus 10体（5系統リサーチ medium → 各系統の敵対的検証 xhigh）、598k tokens、15分、
  検証 25 クレーム中 confirmed 18 / refuted 3 / outdated 2 / plausible 2。
- 反証・訂正された主なもの:
  - 「OPENAI_API_KEY があると自動で API 課金にフォールバック」→ **誤り**。公式 SKILL.md は built-in
    既定・CLI フォールバックは明示要求時のみ。
  - 「gpt-image-2 の1枚単価は $0.03/$0.05/$0.08」→ **誤り**。品質階層別（1024² low $0.006〜high $0.048、
    2048² 〜$0.09）。
  - 「`--approve-for-me` と `-s` は併用可能（ドキュメント）」→ **実機では排他**（2 回独立再現）。
    実測を正とする。
  - 「Anthropic ベストプラクティスに『通常 2–3 回の反復』の記載」→ 現行ドキュメントに数値なし
    （ワークフロー自体の推奨は現存）。
  - 添付画像の 2048×768 リサイズ（issue #14555）→ 起票時は事実だが closed 後の現行挙動は不明。
- 主要一次情報: learn.chatgpt.com/docs（Codex 公式。developers.openai.com/codex/* から移設済み）の
  developer-commands / browser / non-interactive-mode / ci-cd-auth、openai.com/policies/terms-of-use
  （r.jina.ai 経由で逐語確認）、GPT-image-2 発表（community.openai.com、2026-04-21）、
  github.com/openai/codex の issues #9608 / #13508 / #14555 / #28316、PR #36373、
  OneRedOak/claude-code-workflows、arXiv: UI2Code^N・Coding with Eyes (VF-Coder)。
- 未解決の問い: 添付画像の公式サイズ・枚数上限／`image_detail_original` removed の意味（原寸が既定化
  されたか）／画像生成の枠消費レートの正確な値（実測 46,464 tokens/枚が一例）／codex cloud での
  image_gen 可否／Pro の画像生成レート上限の実数。
