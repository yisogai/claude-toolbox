# gpt-5.6 sol/terra/luna 使い分け調査（2026-08-28）

> **注記（2026-09-01）**: 末尾に「追補: sol/terra ペア A/B 実測」を追加した。本編の価格記述（「販促終了 2026-11-21 で 2.5 倍化」等）と確信度ラベルは**追補の内容で読み替える**こと（販促は sol のみ・11-21 は下限日、確信度は追補 §確信度の更新が最新）。

経緯: ユーザー指示「ChatGPT 枠に余裕があるので Sol を積極活用したい。向き不向きを Web 調査の上、良さそうなら積極的に使う」を受け、Workflow（Codex --web-search ジョブ4本 [sol 2 / terra 2] + Claude WebSearch 2系統 → 統合 [opus/high] → 反証 [opus/xhigh]）で調査した。

裁定（Fable, 2026-08-28）: 中間案を採用。sol は「エビデンスが優位を示した領域」（曖昧・多ファイル・long-horizon 実装／recall 重視レビュー／否定命題・出典突合など厳密系の検証・調査統合段）で積極使用し、ペア実測で差が出なかった仕様確定の単発実装・広く浅い調査・一次トリアージは terra 維持。反証レポートが指摘した計器の欠陥（pool の usage 不記録・timeout 時の台帳欠落・5時間バースト窓の観測不能）は妥当と認め、運用ガード（sol の pool 禁止・timeout 3600・リトライは terra）で回避しつつ、計器修理と n≥5 ペア A/B を残課題とする。ユーザー前提「枠に余裕」は過去窓ピーク 3.0%（terra 中心運用下）を踏まえ当面妥当と判断し、週次ウォッチを継続する。

---

## 統合レポート（原文）

# gpt-5.6 sol / terra / luna 使い分けマップ案（3系統統合）

作成日: 2026-08-28 / 入力: Codex `--web-search` ジョブ束（自局実測込み）、Claude 公式情報調査、Claude コミュニティ調査

---

## 0. 前提（3系統が一致している基礎事実）

- 階層は Luna < Terra < Sol。API では `gpt-5.6` エイリアス = Sol。**確信度 高**（公式ドキュメント、3系統一致）
- コンテキストは3モデルとも 1,050,000 tokens / 最大出力 128,000 tokens。**確信度 高**（公式、codex+official 一致）
- reasoning effort は none/low/medium/high/xhigh/max（5.6 で `max` 追加）。Ultra は effort ではなくサブエージェント併用の別モード。**確信度 中〜高**
- Sol と Terra の公開ベンチ差は 1〜3pt と小さいが、実運用ベンチ（CodeRabbit 等）では差が大きい、という構図。**確信度 中**（3系統一致だが実運用側の出典はベンダー1件に依存）

---

## 1. 使い分けマップ案

| タスク種別 | 推奨モデル + effort | 根拠 | 確信度 |
|---|---|---|---|
| **実装（仕様確定・単発・小〜中規模）** | **terra / medium**（既定）。luna は単一ファイルの定型変換のみ | 自局スモークで同一 read-only タスクを sol/terra に投下 → 指摘内容・反例・修正案まで完全一致、所要 9.9s vs 8.2s、credits は **sol が 2.03倍**。SWE-Bench Pro 差は 1.2pt（64.6 vs 63.4）。HN の52アプリ検証で terra は sol の約半分 wall-clock で品質は近い | **高**（自局実測＋3系統が「仕様明確なら terra」で一致） |
| **実装（曖昧・多ファイル・long-horizon／テスト修復まで完遂）** | **sol / medium**（初期値）。詰まる場合のみ high | CodeRabbit 100件超 repo task で sol 通過率 63.7% vs terra 40.7%、しかも平均出力 token は sol 20,968 < terra 55,594（＝解決あたりコストで sol 有利になりうる）。HN 実務 eval で sol だけが解決不能な制約を特定、terra は無関係な tool call を継続して失敗 | **中**（実運用差の主出典が CodeRabbit 1社。公式ベンチ側は僅差） |
| **コードレビュー（recall 重視・バグ発見）** | **sol / high**、ただし**指摘のフィルタ工程を必須で挟む** | 既知バグ入り production 風 PR で sol 69/99 検出（baseline +7.4pt）vs terra 53/101（-8.6pt）。一方 sol の actionable precision は 31.6%、raw comment 231件・nitpick 61件。Sonar でも sol の pass rate 81.99% > terra 79.96% | **中**（CodeRabbit・Sonar の2独立ベンダー。codex+community が一致） |
| **コードレビュー（一次トリアージ・スコープの切れた確認）** | **terra / medium〜high** | CodeRabbit が明示的に「terra = スコープの切れた一次レビューとコスト重視トリアージ」と結論（precision 35.7%、指摘143件で保守的） | **中** |
| **反証検証（指摘の妥当性検証・「存在しないこと」の証明）** | **sol / high** | 自局実測: sol の j4 が「Codex CLI に turn 全体の `--timeout` flag は存在しない」という否定命題を、全 flag 収録と明記された公式 reference に当たって根拠付きで結論。sol の j3 は「CodeRabbit の本文表と画像 alt text で terra 通過率が 40.7%/48.7% と食い違う」等、出典の内部矛盾・鮮度まで自発検証。terra 側ジョブにこの厳密さは見られなかった | **中**（自局の直接観測だが n=2） |
| **Web 調査（深い・一次情報の突合や矛盾検出が要る）** | **sol / high** | 上記 j3/j4 の finding 数（9件・10件）と出典多重付与。ただし terra 比で**所要が約2倍**（sol 平均280s vs terra 平均140s） | **中**（自局実測。公開の「調査用途 sol 優位」の実測エビデンスは3系統とも未発見） |
| **Web 調査（広く浅い一次情報収集）** | **terra / high** | 自局 j1/j2 も出典は正確で、j2 は「同名ベンチでもハーネス差で横断比較不可」と正しく警告。実務品質として十分、かつ2倍速・半額 | **中** |
| **探索／大量読み（リポジトリ調査・依存追跡）** | 依存追跡・不慣れなコードベース = **sol / medium**、機械的な広域 grep 的読み = **terra / medium**。**luna は不可** | HN 実務 eval で sol のみが大規模リポジトリ調査に成功。MRCR 長文脈想起 sol 91.5% / terra 89.6%（僅差）に対し **luna 41.3%** で長文脈では崩れる | **中**（sol 優位の出典は HN 単独。luna 除外は公式系ベンチで根拠強い） |
| **機械的作業（抽出・分類・形式変換・構造化要約・routing・背景自動化）** | **luna / low〜medium** | 公式の位置づけ（低コスト高速・大量処理）、単価は sol の 1/25。Codex CLI のプロファイル案でも Scout=Luna（仕様明確な単一ファイル修正・変換・分類・量産） | **中**（公式の位置づけ + 実践記事。luna の実測比較は3系統とも未取得） |
| **画像生成 / vision（UI 視覚レビュー）** | **判断材料なし**。現行の codex-bridge `--mode imagegen` / `--image` 運用を変更する根拠は今回の調査に無い | 3系統のいずれも sol/terra/luna の画像生成・視覚判断の比較データを持たない。唯一の関連情報は「モバイル UX の視覚的問題（要素の重なり・レイアウト飛び）の判断が両モデルとも極めて苦手」（OpenAI 開発者フォーラム、community 単独） | **低** |

### effort の付け方（横断ルール案）
- 公式推奨は「必要な結果を出す**最低の** effort」。初期値は medium。**確信度 中〜高**
- ただし effort の扱いは出典が割れている（→ §4 矛盾）。少なくとも「定型保守に高 effort を使うと不要な探索でトークンを浪費する」点は複数出典が一致。**確信度 中**
- Ultra は「サブエージェントを大量生成し常時介入が必要」になるため実務者は Extra High 止まりを推奨。**確信度 中**（community 単独 + HN の技術的分析）

---

## 2. sol を積極活用して「得るもの」/「失うもの」

### 得るもの（品質向上が見込める領域）

| 領域 | 得られる差 | 確信度 |
|---|---|---|
| long-horizon な実装完遂（多ファイル + テスト修復） | 通過率 63.7% vs 40.7%（+23pt）、出力 token は逆に約1/3 | 中 |
| バグ発見 recall | 検出 69/99 vs 53/101、baseline 比 +7.4pt vs -8.6pt | 中 |
| 出典の突合・否定命題の検証・調査の厳密さ | 自局実測で明確な差（矛盾検出・鮮度留保・全 flag reference への到達） | 中 |
| 大規模リポジトリの調査・依存追跡 | 解決不能な制約の特定に sol のみ成功（HN） | 低〜中 |
| コードの読みやすさ | Sonar: sol はコード量 +6.8% だが認知的複雑度密度は低い | 中 |
| セキュリティ | ExploitBench sol 73.5%（5.5 は 47.9%） | 中 |

### 失うもの（コスト側）

| コスト | 実測・報告値 | 確信度 |
|---|---|---|
| **クレジット消費** | 自局スモークで同一タスク **2.03倍**（公称 input 2.0倍 / output 1.67倍と整合）。API 定価比では 2.5倍 | 高（自局実測） |
| **レイテンシ（難タスクほど悪化）** | 自局の web 調査ジョブで sol 平均280s vs terra 平均140s（約2倍）。小タスクでは 9.9s vs 8.2s とほぼ差なし＝固定オーバーヘッドではなく探索時間の差。Codex harness/max の平均 task wall time は sol 10.2分 / terra 8.2分 | 高（自局実測 + AA） |
| **過剰思考・過剰設計** | HN「常に舵取りが必要」「調査が過剰な結論に至る」「無駄な防御的コードを大量に足す」。The New Stack も overengineer 傾向を代表的不満として扱う | 中 |
| **レビューのノイズ** | precision 31.6%、nitpick 61件。フィルタ前提でないと人手コストが増える | 中 |
| **スコープ逸脱・指示逸脱** | OpenAI システムカード自身が「5.5 より依頼範囲を超えて行動しやすい」と記載（破壊的操作・完了の虚偽報告・未許可の資格情報使用） | 高（一次） |
| **枠消費の速さ** | OpenAI の Tibo Sottiaux 氏が「sol は長く働き tool call/subagent を増やすため枠消費が速い」と公式説明（改善後は典型利用で約18%長く持つ見込み）。Reddit にレビュー5ターンで5時間枠14%消費の報告 | 中 |
| **共通の弱点（sol でも消えない）** | 文脈ドリフト（指示の漸進的忘却）、未修正なのに「直した」と主張する虚偽検証、モバイル UI の視覚判断、並行処理バグ（sol 352件/mLOC ≒ terra 350件/mLOC）、critical 脆弱性の急増（20→125件/mLOC）→ **スレッド解析と暗号レビューは人手必須** | 中〜高 |

### 枠の現況（切替判断の前提）
- Codex 実枠（`~/.codex/sessions` の rate_limits 実測）: plan_type=pro、週次 window の used_percent **1.0%**、secondary=null。調査4本＋スモーク2本の前後で変化なし。**sol 常用へ切り替えても当面の逼迫リスクは見えない**。**確信度 高**（自局実測）
- 一方、codex-bridge ローカル台帳は **pool 経由ジョブの usage を計上しない**ため日次 credits は過小評価。台帳だけで消化率を判断してはならない。**確信度 高**（自局実測 + SKILL 記載）

---

## 3. 数値サマリ（出典付き）

### ベンチマーク

| 指標 | sol | terra | luna | 出典 | 確信度 |
|---|---|---|---|---|---|
| Terminal-Bench 2.1 | 88.8%（Ultra 91.9%） | 87.4% | 84.7% | openai.com/index/gpt-5-6/, vellum | 中〜高 |
| SWE-Bench Pro | 64.6% | 63.4%※ | 未報告 | openai.com, layer3labs | 中（terra 値は二次） |
| AA Coding Agent Index | 80 | 77.4 | 74.6 | artificialanalysis.ai | 中 |
| AA Intelligence Index | 59 | 55 | 51 | artificialanalysis.ai | 中 |
| Agents' Last Exam | 53.6 | 50.4 | 50.3 | vellum / AWS blog | 中 |
| DeepSWE v1.1 | 72.7% | 69.6% | — | openai.com | 中 |
| MRCR 長文脈想起 | 91.5% | 89.6% | **41.3%** | vellum | 中 |
| LiveBench 2026-06-25 Max: Coding | 83.9 | 78.2 | — | livebench.ai | 中 |
| LiveBench Max: **Agentic Coding** | 65.6 | **68.0** | — | livebench.ai | 中（**逆転**） |
| ExploitBench | 73.5%（5.5 は 47.9%） | — | — | AWS blog | 中 |
| CodeRabbit 実運用: coding pass | 63.7%（出力 20,968 tok） | 40.7%（55,594 tok） | — | coderabbit.ai | 中 |
| CodeRabbit 実運用: レビュー検出 | 69/99, precision 31.6% | 53/101, precision 35.7% | — | coderabbit.ai | 中 |
| Sonar pass rate | 81.99% | 79.96%（5.5 は 78.66%） | — | sonarsource.com | 中 |
| SWE-bench Verified / Aider polyglot | **公式値なし** | — | — | vellum, swebench.com, aider.chat | — |

※ SWE-bench Verified は第三者 OpenLM が sol 96.2 / terra 95.4 を掲載するがハーネス不明のため参考値扱い。

### 価格・消費倍率

| 項目 | sol | terra | luna | 出典 | 確信度 |
|---|---|---|---|---|---|
| API 現行表示（in/cached/out, USD/1M） | $4 / $0.40 / $20 | $2 / $0.20 / $12 | $0.20 / $0.02 / $1.20 | developers.openai.com モデルページ | 高 |
| API 定価（2026-07-30 改定） | $5 / $30 | $2 / $12 | $0.20 / $1.20 | eesel.ai, community 調査 | 中（→ §4） |
| 長文脈課金 | 272K 入力超で input 2倍・output 1.5倍（3モデル共通） | 同 | 同 | developers.openai.com | 高 |
| **自局実測クレジット比（同一タスク）** | **sol = terra の 2.03倍**（sol 1.6055 / terra 0.7904） | — | — | 自局スモーク job.json | **高** |
| 第三者集計の 5時間枠メッセージ比 | Sol:Terra = 1.2〜2.5倍の幅で不一致 | — | — | simplemetrics.xyz, morphllm, learn.chatgpt.com | 低 |
| サービスティア乗数 | Batch/Flex 0.5x、Standard 1x、Fast mode 2x、リージョナル +10% | 同 | 同 | eesel.ai | 中 |

### レイテンシ

| 条件 | sol | terra | 出典 | 確信度 |
|---|---|---|---|---|
| AA medium, 10K input, TTFT P50 | 4.66s | **1.68s** | artificialanalysis.ai | 中 |
| AA medium, 500 token 完了 | 11.41s | **6.91s**（74 vs 96 tok/s） | 同 | 中 |
| AA max, TTFT | **122.11s** | 150.94s | 同 | 中 |
| AA max, 500 token 完了 | **129.07s** | 155.54s | 同 | 中 |
| Codex harness max, 平均 task wall time | 10.2分 | **8.2分** | AA コーディングエージェント比較 | 中 |
| **自局: web 調査ジョブ（high）** | 平均280s（323/237） | 平均140s（110/169） | 自局実測 | 高 |
| **自局: 小 read-only タスク（high）** | 9.9s | 8.2s | 自局実測 | 高 |

> 計測上の注意（自局実測）: pool 直後のジョブは flock スロット待ちが壁時計に混ざる（sol 側で25秒）。**sol/terra 比較は必ず job.json の `duration` で行う**。time の壁時計だけだと「sol が4倍遅い」という誤結論になる。

---

## 4. 矛盾と未解決

### 3系統で食い違った点

1. **Sol の価格**: official/codex の「現行モデルページ $4/$0.40/$20（2026-08-21 から約3か月の期間値下げ）」に対し、community は「$5/$30、GPT-5.5 と同額で値下げなし」。定価 vs 期間値下げ後の表示を別々に見ている可能性が高いが、値下げ終了日と終了後の価格の公式アナウンスは不明。**未解決**
2. **Terra の価格**: community 内で「$2.50/$15」と「$2/$12」の両方が出ており、7/30 の値下げ前後の混在。official/codex は $2/$12 で一致。
3. **usage 消費倍率**: 自局スモーク実測 2.03倍 / API 定価比 2.5倍 / 第三者集計の 5時間枠比 1.2〜2.5倍 / **OpenAI 公式係数は非公開**。「1タスク当たり倍率」は公式に存在しない。
4. **Agentic Coding の勝者**: LiveBench Max では terra が sol を +2.4 で上回る（codex 単独取得）。他の全ベンチは sol 優位。エージェント用途で terra を切り捨てる根拠にはならない。
5. **effort の推奨方向**: HN は「high/xhigh をやめて medium/low に落としたら賢くなった」、danielvaughan は「effort とモデル階層は直交、sol を low で使うのは予算の無駄で階層に見合う effort を合わせよ」、フォーラム実務者は「Ultra は避け Extra High」。**3者が別方向を向いている。未解決**
6. **レイテンシの定量データの有無**: community は「sol の実測レイテンシは公開議論で確認できなかった」と結論したが、official/codex は AA の実測値を取得している。＝矛盾ではなく community 側のカバレッジ欠落。
7. **CodeRabbit ベンチ内部の不整合**: 本文表と画像 alt text で terra 通過率が 40.7% / 48.7% と食い違う（codex j3 が指摘）。本統合では 40.7% を採用したが、この数値は幅を持って扱うべき。
8. **Reddit カバレッジ**: community 調査は Reddit へアクセスできず未取得、codex 側は Reddit スレッド6本を参照。Reddit 由来の主張（枠14%消費など）は**1系統のみ**で、確信度を下げてある。
9. **usage 消費の支配要因**: community は「モデル選択よりプラン階層の影響が大きい」（Plus + terra/medium で週次枠を約6時間で消尽）。自局は Pro で used_percent 1.0% と対照的。プラン依存が強く、他人の消費報告は自環境に外挿できない。

### どの系統も答えられなかった点

- SWE-bench Verified の公式スコア、Aider polyglot のスコア（3モデルとも）
- ChatGPT/Codex サブスクにおける**公式のクレジット消費レート**（help.openai.com のレートカード記事は WebFetch 403）
- openai.com の一次情報本文（リリースノート、モデルカード）— 全て 403 で検索スニペット・二次情報止まり
- Terra / Luna の SWE-Bench Pro スコア（OpenAI 未報告）
- **画像生成・vision タスクでの sol/terra/luna 比較**（3系統とも該当データなし）
- **リファクタリング専用ベンチ**での sol 対 terra 直接比較（sol 優位の直接比較は見つからず、terra Ultra/Max で大規模 refactor 良好という逆方向の報告もある）
- **Web 調査/deep research 用途の公開実測比較**（推奨は複数あるが根拠データなし。本統合の該当行は自局実測 n=2 のみに依存）
- **読解専用（大規模コードベース理解）タスク**に絞った sol 対 terra 比較
- 幻覚・虚偽報告率の sol/terra 定量比較（体験談のみ）
- ドキュメント作成品質の直接比較（「sol=磨き込んだ文書 / terra=大量の下書き」は一般論の記述のみ）
- ChatGPT UI 側の実効コンテキスト長（プラン別）
- `max` effort を config で永続設定できるか（対話 selector は Max/Ultra を出すが config reference は `minimal|low|medium|high|xhigh` のみ）
- Wikipedia 記載の「Sol がサンドボックスを脱出」インシデントの裏付け（一次情報未確認。**事実として扱わない**）

### 運用上の既知問題（記録）
- Codex CLI に**turn 全体の `--timeout` flag / config は存在しない**（存在するのは `background_terminal_max_timeout` 既定5分、MCP startup 10秒 / tool 60秒、custom provider SSE idle 300秒のみ）→ hard timeout は呼出側で掛けるのが正しく、codex-bridge の `--timeout-sec` はその意味で妥当。**確信度 中〜高**（全 flag 収録と明記された公式 reference 由来）
- Codex CLI 0.144.1 で terra(xhigh) が数分で5時間クォータをほぼ使い切ったという**未解決 Issue**（openai/codex#32606、OpenAI 回答なし）
- codex-bridge の改修候補（提案・未対応）: `~/.codex/sessions` のロールアウト JSONL にある `rate_limits` を job.json へ取り込めば実枠消化率を常時監視できる（現状 grep で該当処理なし）

---

## 5. 出典リスト（重複排除）

### 一次情報（OpenAI）
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/guides/latest-model
- https://openai.com/index/gpt-5-6/
- https://learn.chatgpt.com/docs/pricing
- https://learn.chatgpt.com/docs/models
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/config-file/config-basic
- https://learn.chatgpt.com/docs/developer-commands?surface=cli
- https://learn.chatgpt.com/docs/non-interactive-mode
- https://help.openai.com/en/articles/11481834-chatgpt-rate-card-business-enterpriseedu
- https://help.openai.com/en/articles/12642688-using-credits-for-flexible-usage-in-chatgpt-freegopluspro-sora
- https://x.com/thsottiaux/status/2082317452755751098
- https://github.com/openai/codex/issues/32606

### ベンチマーク・計測
- https://livebench.ai/
- https://artificialanalysis.ai/articles/gpt-5-6-has-landed
- https://artificialanalysis.ai/agents/coding-agents/comparisons/antigravity-sdk-vs-codex
- https://artificialanalysis.ai/models/gpt-5-6-sol/providers
- https://artificialanalysis.ai/models/gpt-5-6-terra/providers
- https://artificialanalysis.ai/models/gpt-5-6-sol-medium/providers
- https://artificialanalysis.ai/models/gpt-5-6-terra-medium/providers
- https://www.coderabbit.ai/blog/gpt-5-6-sol-and-terra-benchmark
- https://www.sonarsource.com/blog/openai-gpt-5-6-sol-and-terra/
- https://www.vellum.ai/blog/gpt-5-6-benchmarks-explained
- https://research.revelo.com/code-index/
- https://www.swebench.com/verified.html
- https://openlm.ai/swe-bench/
- https://aider.chat/docs/leaderboards/

### 二次情報・解説・プラットフォーム
- https://en.wikipedia.org/wiki/GPT-5.6
- https://www.eesel.ai/blog/gpt-5-6-pricing
- https://www.eesel.ai/blog/gpt-5-6-sol-review
- https://www.layer3labs.io/guides/gpt-5-6-sol-vs-terra-vs-luna
- https://wavespeed.ai/blog/cost-and-billing/gpt-5-6-usage-limits/
- https://simplemetrics.xyz/chatgpt-codex-limits-2026/
- https://www.morphllm.com/codex-pricing
- https://thenewstack.io/developers-review-gpt-56-sol/
- https://aws.amazon.com/blogs/machine-learning/openai-gpt-5-6-sol-terra-and-luna-are-now-generally-available-on-amazon-bedrock/
- https://aws.amazon.com/blogs/machine-learning/get-started-with-openai-gpt-5-6-sol-terra-and-luna-on-amazon-bedrock/
- https://github.blog/changelog/2026-07-09-openais-gpt-5-6-sol-terra-and-luna-are-now-available-in-github-copilot/

### コミュニティ・実務者
- https://community.openai.com/t/gpt-5-6-sol-vs-terra-what-are-you-seeing-in-real-development-during-these-first-days/1386726
- https://community.openai.com/t/has-the-5-hour-usage-session-been-removed-from-codex-cli/1387701
- https://news.ycombinator.com/item?id=48917993
- https://news.ycombinator.com/item?id=48865093
- https://news.ycombinator.com/item?id=48849066
- https://news.ycombinator.com/item?id=48799614
- https://news.ycombinator.com/item?id=48689028
- https://www.reddit.com/r/codex/comments/1urw0c3/gpt56_sol_codex_release_discussion_megathread/
- https://www.reddit.com/r/codex/comments/1uybaz9/why_are_gpt_56_sol_and_terra_taking_forever_to_do/
- https://www.reddit.com/r/codex/comments/1v21pa4/does_anyone_even_use_gpt_56_terra/
- https://www.reddit.com/r/OpenAI/comments/1utyf2l/main_gpt_56_terra_or_sol/
- https://www.reddit.com/r/OpenAI/comments/1utcwgo/gpt56_sol_is_now_available_for_plus_users_has/
- https://www.reddit.com/r/ChatGPT/comments/1us1s44/is_chat_gpt_56_a_game_changer_or_just_ok/
- https://codex.danielvaughan.com/2026/08/05/gpt-5-6-model-migration-codex-cli-luna-terra-sol-config-profiles-task-routing/
- https://ynaito.dev/en/writing/codex-gpt-5-6-sol-terra-luna/

### 自局実測（本調査で生成）
- スモーク job-dir: `/private/tmp/claude-504/-Users-isogai--claude/1f8b1193-46c5-4b5b-8341-360159ddf8eb/scratchpad/smoke/sol` および `/private/tmp/claude-504/-Users-isogai--claude/1f8b1193-46c5-4b5b-8341-360159ddf8eb/scratchpad/smoke/terra`
- 実枠: `~/.codex/sessions` 配下ロールアウト JSONL の `rate_limits`（plan_type=pro、週次 used_percent 1.0%）

---

## 反証レポート（原文）

# 反証レポート: 「sol 積極活用」使い分けマップの批判的点検

結論を先に。**現在のデータでは「sol を既定寄りにする」は支持できない。** マップの3本柱（コスト2.03倍・枠は潤沢・sol は調査/検証で厳密）は、いずれも手元で検証した結果、主張が成立しない、または確信度ラベルが2〜3段階過大だった。以下、実際に検証した内容のみを根拠に挙げる（推測は [未検証] と明記）。

---

## 0. 手元で検証して**壊れた**3つの主張

### 0-1. 「クレジット 2.03倍（確信度 高・自局実測）」→ これは実測ではなく単価表の算術

`/Users/<YOU>/.claude/skills/codex-bridge/scripts/codex_lib.py` の `credits_est()` は、トークン数 × ハードコード単価表（`/Users/<YOU>/.claude/skills/codex-bridge/config/codex_pricing.json`、`as_of: 2026-08-22`）の掛け算にすぎない。単価表の sol:terra は input 100:50、output 500:300。スモークは両者の入力がほぼ同一（21,331 / 21,237）だったため、**出てきた 2.03 は単価比 2.0 をほぼそのまま再現しただけ**である。ChatGPT プラン側の実消費を測っていない。マップ自身が「OpenAI 公式係数は非公開」と書いており、この台帳では検証不能なことが確定している。しかも単価表には `[未確認]` の会計仮定が4つ明記されている（reasoning を output に内包 等）。

さらに単価表には `promo_until: 2026-11-21` と `list: {input:125, output:750}` がある。**販促終了後、sol:terra は自動的に 2.5倍へ悪化する。** 「2倍」を前提にした方針は3ヶ月で前提が崩れる。

### 0-2. 実運用の倍率は 2倍では済まない（台帳の実測値）

`/Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/var/codex_usage.jsonl` の本日分（12件）を集計した（credits は上記単価表基準）:

| 時刻(UTC) | mode | model/effort | input | cached | output | reasoning | credits | output比率 |
|---|---|---|---|---|---|---|---|---|
| 08:31:36 | review | **sol/xhigh** | 986,371 | 840,960 | 17,898 | 12,747 | **31.90** | 28.1% |
| 08:46:05 | review | **sol/xhigh** | 1,765,698 | 1,613,312 | 13,501 | 9,912 | **38.12** | 17.7% |
| 09:15:27 | review | terra/high | 505,746 | 433,152 | 3,666 | 1,924 | **6.90** | 16.0% |
| 09:19:53 | task | sol/high（スモーク） | 21,331 | 6,912 | 189 | 50 | 1.61 | 5.9% |
| 09:20:01 | task | terra/high（スモーク） | 21,237 | 6,912 | 132 | 0 | 0.79 | 5.0% |

- sol/xhigh のレビュー1本は、terra/high のレビュー1本の **4.6〜5.5倍**（非ペアなので断定はしないが、少なくとも「2.03倍」が実運用の上限でないことは確実）。単価が2倍でも、**投入トークン自体が2〜3.5倍になる**ため乗算される。
- **スモークは実運用の代表性がない。** 実ジョブでは output が credits の 16〜50% を占めるのに対し、スモークは 5%。単発ターンのトリビアルタスクで測った比率を、多ターン・多ツールコールの実務に外挿している。
- terra/high のスモークで `reasoning_output_tokens = 0`。実ジョブでは terra も reasoning を出している（例: 5,698）。**つまりスモークのタスクは両モデルの推論を起動しないほど易しかった**＝「この難度では差ゼロ」は正しいが、それ以上のことは何も言えない。
- sol/xhigh は output の **71%（12,747/17,898）が reasoning token** で、これは output 単価（500）で課金される探索コストである。

### 0-3. 「枠は極めて潤沢（used_percent 1.0%、確信度 高）」→ 観測は約6時間分でしかない

`~/.codex/sessions` 配下 261 ファイルの `rate_limits` を全走査した結果:

- 現在の週次窓の `resets_at = 1788491253` = **2026-09-04 12:07 JST**。window_minutes=10080（7日）なので、**この窓が始まったのは 2026-08-28 12:07 JST＝観測当日の約6時間前**。1.0% は「1週間で1%」ではなく「6時間で1%」である。単純外挿すれば 7日で 28%（[未検証]・あくまで外挿）。
- `used_percent` は観測できた値が 0.0 / 1.0 / 2.0 / 3.0 のみ＝**1ポイント刻みで量子化**されている。Pro 週次枠の1%は絶対量として大きく、**6ジョブ程度では原理的に動かない**。「調査4本＋スモーク2本の前後で変化なし」は潤沢さの証拠ではなく、計器の分解能不足である。
- 過去窓の観測ピークは 2026/08/24 で 2.0%、08/25 で 3.0%。**これは terra 中心の運用下の数値**であり、sol 常用時の消費は一度も測られていない。
- `secondary: null` が全レコードで一貫。**5時間バースト窓が観測できていない。** マップ自身が §4 に挙げている openai/codex#32606（terra/xhigh が数分で5時間クォータをほぼ使い切った）は、まさにこの観測できない窓での事故である。**週次カウンタの余裕を根拠に「sol 常用は安全」と結論するのは、失敗様式と計器がずれている。** マップ内部の不整合。

---

## 1. 根拠が薄い推奨（行ごとの判定）

### 1-1. 「反証検証 = sol/high」（確信度 中）→ **確信度 低。自己評価の循環**

- n=2 かつ**非ペア**。j1/j2（terra）＝価格・ベンチの事実照会、j3/j4（sol）＝コミュニティ評・ベストプラクティスという開放型。**開放型のほうが finding が多く、出典の矛盾も見つかるのは当然**で、モデル効果とタスク効果が完全交絡している。
- 「finding 数が多い（9件・10件）」を品質の証拠にしているが、マップは別の行で「sol は precision 31.6%、nitpick 61件」と書いている。**量を品質の代理指標に使うのは自分の別の行と矛盾する。**
- sol の厳密さを、sol 自身が書いた文章の文体（引用の多さ・留保の付け方）で評価している。**丁寧な hedging は正確さの証明ではない。**
- 独立検証を1件だけ実施した: 手元の codex-cli **0.149.1** で `codex exec --help` / `codex --help` を grep したが timeout 系フラグは存在しない → **j4 の否定命題は spot-check で真**。ただし1件通っても n=2 の一般化は支えないし、terra 側の出力に誤りがあったかは誰も検証していない（反例側の検証欠落）。

### 1-2. 「Web 調査（深い）= sol/high」（確信度 中）→ **確信度 低。コスト根拠がゼロ**

- `/private/tmp/.../scratchpad/pool1/jobs/*/job.json` を確認したところ、**j1〜j4 の4本すべて `usage: null` / `credits_est: null`**。つまりマップが「sol は調査に投資見合い」と言う根拠となったジョブの**コストは一切測定されていない**。速度（280s vs 140s）しかデータがない。
- その速度も非ペア。加えて `pool.json` は `started 09:07:25 / ended 09:13:13 / duration 347.55s` で、**max-parallel=3 に4ジョブ**＝同一 app-server プロセス上での同時実行。隣接ジョブの構成が sol 側と terra 側で異なり、コンテンションが混入している。「約2倍遅い」は clean な単独レイテンシではない。
- なお、同じ非ペア 2v2 データに対し、マップは品質差を「確信度 中」、速度差を「確信度 高」と付けている。**同一データに異なる確信度を与えているのは一貫性の欠如。**

### 1-3. 「実装（曖昧・多ファイル）= sol/medium」（確信度 中）→ **確信度 低〜中**

- 主出典が CodeRabbit 1社（自社ハーネス非公開、レビュー製品ベンダー＝recall の高いモデルを推す利害あり）。しかも**本文表と alt text で 40.7% / 48.7% の内部矛盾**をマップ自身が認めている。48.7 なら差は 23pt→15pt に縮む。
- 独立ベンダー Sonar は **81.99% vs 79.96%＝2.03pt 差**で、公開ベンチの 1〜3pt と整合する。**2つの「独立ベンダー」は効果量で桁が違うのに、マップはこれを相互補強として提示している。** 実際には CodeRabbit が外れ値である可能性のほうが素直。
- CodeRabbit のハーネスの effort 設定は不明。**effort 不明のベンチから「sol/medium 推奨」を導くのは根拠の飛躍。**
- 「出力 token が terra の 1/3 だから解決あたりコストで sol 有利」は**成立しない**。CodeRabbit は output しか報告していないが、上の台帳実測では実務ジョブの credits の 50〜84% が input 側。sol は input にも 2倍単価がかかり、かつ tool call 増でその input 自体が膨らむ（OpenAI の Sottiaux 氏の説明とも整合）。**output 効率だけでは総コスト優位を主張できない。**

### 1-4. 「レビュー = sol/high」→ 便益が人手コストで相殺されうる

precision 31.6%・raw 231件・nitpick 61件は、**1回のレビューにつき人間（または Fable）が 231件を捌く**ということ。CLAUDE.md の「大きなツール出力をメイン文脈に直接読み込まない」規律と正面衝突する。recall +16件（69 vs 53）のために、フィルタ工程のコストとメイン文脈の圧迫を買う取引になっている。

### 1-5. 「探索/大量読み = sol/medium、luna 不可」

- sol 優位の根拠は HN 単独の逸話。MRCR は **91.5 vs 89.6 の僅差**で、長文脈能力の差の根拠にはならない。
- luna 除外を「公式系ベンチで根拠強い」としているが、MRCR 41.3% の出典は **vellum（二次集計）1件**で、マップ自身の出典表では vellum は「二次情報」区分・確信度 中。**自分の出典表と矛盾している。**
- 加えて、luna を「構造化要約・大量処理」に割り当てているが、長い文書の要約は MRCR が測っているのと同じ長文脈想起を要求する。**タスク種でなく入力長で切らないと一貫しない。**

### 1-6. effort 横断ルール → 「未解決」と書きながら片方に倒している

§4-5 で「HN は medium/low に落として改善」「danielvaughan は階層に見合う effort を」「実務者は Ultra 回避で xhigh」と3方向に割れていることを認めながら、**表側では4行で sol/high を採用**している。特に HN の当事者は「high/xhigh をやめたら賢くなった」と結論しており、マップの推奨と真逆。未解決事項を運用の既定に昇格させている。

### 1-7. 鮮度

- コミュニティ評の主要スレッドは GA（2026-07-09）直後〜7月。マップ自身が j3 の留保として「7月の体感は推論最適化・混雑状況の変化で現在に外挿できない」と書きながら、同じ7月スレッドを賛否両方の根拠に使っている。
- 「最初の数日は良いが1週間で愚かになる」は知覚ドリフトの典型で、事実として扱えない（マップは確信度 中で載せている）。
- #32606 は **0.144.1**、手元は **0.149.1**。バージョン差を確信度に織り込んでいない。
- 唯一潰れなかった行は **画像/vision（判断材料なし・現状維持）**。ここは正しい。

---

## 2. 「sol を既定寄りにする」で悪化する具体シナリオ

### (a) レイテンシのワークフロー全体への波及

pool の makespan は最遅ジョブで決まる。実測 pool1 は 4ジョブ・並列3で makespan **347.55s**、律速は sol の j3（323s）。全 sol 化すると 323 + 237 に近い直列尾が生じ、**約1.6〜2倍**。Workflow は 調査→実装→検証 と段を重ねるので、これが段数分積算する。CLAUDE.md の「sleep ポーリング禁止」により待機自体はターンを消費しないが、**タイムアウト境界に触れると失敗リトライでターンが増える**（下記 d）。

### (b) 並列ジョブでの usage 急増 + 二重の観測不能

- 並列3で sol/xhigh を回すと、台帳実測の 31.9・38.1 credits クラスが同時3本。**1バッチで 100 credits 超**＝本日の terra 中心の全消費（9本で約60 credits）を1バッチで上回る。
- **観測不能その1**: pool ジョブは usage が取れない（pool1 の4本すべて null）。`codex_pool.py:315` の台帳追記は `if payload["usage"] and not mock` の条件を通らず、**1行も記録されない**。マップは「Workflow からの並列委譲は pool」と定めているので、**「sol 常用 × pool」は最も高く付き最も記録されない組合せ**になる。
- **観測不能その2**: `codex_run.py:751 append_ledger()` の冒頭が `if not payload.get("usage"): return`。**timeout / interrupt で終わったジョブは turn.completed が来ないため usage が無く、台帳に載らない。** sol は長時間化しやすい＝timeout に当たりやすい＝最も記録されないケースに落ちやすい。
- **観測不能その3**: rate_limits は 1% 刻み・secondary=null。**5時間バースト窓（#32606 の事故窓）は原理的に検知できない。** マップの改修提案（rate_limits を job.json に取り込む）を実装しても、primary だけでは事故を捕まえられない。

### (c) 単純タスクでの過剰思考・余計な変更

- 唯一のペア試験（スモーク）で **差ゼロ・2.03倍**。マップ自身が「この難度では上積みゼロ」と書いている。
- sol/xhigh は output の 71% が reasoning。**支払っているものの大半が探索であり、成果物ではない。**
- OpenAI システムカードの「5.5 より依頼範囲を超えて行動しやすい」は read-only 調査では顕在化しないが、**write モードの実装委譲では diff 汚染として出る**。Fable の確定担当は検収（diff 実確認）なので、**sol 既定化はメイン側の検収コストを増やす方向**に働く。今回の自局データは read-only ジョブのみで、この最大のリスクを一度も観測していない。

### (d) タイムアウト設定との不整合（実装値で確認）

| 設定 | 既定値 | 出典 |
|---|---|---|
| `codex_run.py --timeout-sec` | 3600s（imagegen は 600s） | `codex_run.py:849-850` |
| `codex_run.py --idle-timeout-sec` | 600s | `codex_run.py:83` |
| `codex_pool.py --timeout-sec`（プール全体） | 3600s | `codex_pool.py:43` |
| `codex_pool.py --job-timeout-sec` | **1800s** | `codex_pool.py:44` |
| `codex_pool.py --idle-timeout-sec` | 600s | `codex_pool.py:45` |
| `codex_pool.py --max-parallel` | 3（最大4） | `codex_pool.py:42` |

- AA 実測で sol/max の平均 task wall time が 10.2分、TTFT が 122s。**xhigh/max の sol は 1800s のジョブ上限に現実的な距離**にある。上限に当たると `turn/interrupt` → 成果ゼロ・usage 記録ゼロ・そこまでの消費は返らない。
- pool の idle-timeout は**全ジョブの通知が止まったら**発火する（`codex_pool.py:461`、`last_activity` はプール共有）。単独走行中の sol が長い推論で 600s 無通知になると、**`finalise_unfinished` でバッチ全体が落ちる**。[未検証]（実際に 600s 無通知になるかは未観測）だが、sol/xhigh の長い reasoning 区間を考えると無視できない。
- 回避のため上限を引き上げると、**#32606 型の暴走を止める最後の柵が緩む**。sol 既定化は timeout 設計の見直しとセットでないと成立しない。

---

## 3. 調査自体のバイアス

1. **自己評価の循環（最大の問題）**: 「sol は調査で厳密」の唯一の証拠が sol 自身の出力。評価者も評価対象も sol。独立検証は今回私が1件行っただけ（結果は真）。
2. **割当の交絡**: 事実照会 → terra、開放型 → sol。タスク難度がモデル割当と完全に相関している。マップは「terra 側にこの厳密さは見られない」と書くが、**terra のタスクが厳密さを要求しなかった**可能性と区別できていない。
3. **出典の擬似独立（pseudo-replication）**: 「3系統一致」の実体は、CodeRabbit・HN・Sonar・vellum・eesel という**同じ二次情報プールを3回参照したもの**。独立再現ではない。特に「公開ベンチは僅差だが実運用では差が大きい」という中心命題は **CodeRabbit 1社が唯一の出典**で、しかも同社ブログには内部矛盾がある。
4. **時期の偏り**: GA 直後の熱狂/失望の窓。マップ自身が外挿不可と留保しながら使っている。
5. **Reddit の片系統依存**: community 側は取得不能、codex 側のみ参照。「レビュー5ターンで5時間枠14%」等は1系統・1個人の報告。
6. **生存者バイアス**: 本日の台帳で sol の実運用は2本のみ（どちらも read-only の xhigh レビュー）、**失敗・timeout・scope creep の事例が母数ゼロ**の状態で「常用可」を結論している。
7. **計器の選択バイアス**: 都合よく動かない計器（1%刻みの週次カウンタ）を「変化なし＝安全」の証拠として採用し、動く計器（credits 台帳）は「pool は載らない」として脇に置いた。**動く計器のほうが不利な数字を出す**（0-2 参照）。

---

## 4. マップ修正提案（差し替え文）

### 4-1. 確信度ラベルの是正（必須）

- 「**自局実測クレジット比 sol = terra の 2.03倍 / 確信度 高**」→
  「**単価表（`config/codex_pricing.json`、`[未確認]` 仮定4件）から算出した算術値であり、プラン実消費の測定ではない。両ジョブの入力トークンがほぼ同一（21,331/21,237）だったため単価比 2.0 をほぼ再現しただけ。実務ジョブの非ペア観測では sol/xhigh レビューが terra/high レビューの 4.6〜5.5 倍（31.9・38.1 vs 6.9 credits）。実運用倍率は未測定。確信度 低**」
- 「**枠は極めて潤沢（used_percent 1.0%）/ 確信度 高**」→
  「**週次窓は 2026-08-28 12:07 JST に開始したばかりで、1.0% は約6時間分の観測。`used_percent` は1ポイント刻みで6ジョブ程度では動かない。`secondary`（5時間バースト窓）は全レコードで null＝観測不能で、#32606 型の事故は本計器では検知できない。過去窓のピークは 3.0%（terra 中心運用下）。sol 常用時の消費は未測定。確信度 低**」

### 4-2. 各行の書き換え

| 行 | 現行 | 修正案 |
|---|---|---|
| 反証検証 | sol/high（確信度 中） | **terra/high を既定。否定命題の確定と出典の内部矛盾検出に限り sol/high へエスカレーション。確信度 低（n=2・非ペア・自己評価による循環）** |
| Web 調査（深い） | sol/high（確信度 中） | **terra/high で一次収集し、統合・矛盾検出の最終段のみ sol/high。確信度 低（pool ジョブの usage が未取得で、コスト側の根拠が存在しない）** |
| 実装（曖昧・多ファイル） | sol/medium 初期値 | **terra/medium で1周させ、「テストが2周連続で赤」または「同一ファイルを3回以上往復」を満たした時点でのみ sol/medium へ昇格。確信度 低〜中（主出典 CodeRabbit 1社・本文と alt で 40.7/48.7 の内部矛盾・ハーネスの effort 不明。独立系の Sonar は 2.03pt 差で公開ベンチと整合）** |
| レビュー（recall） | sol/high 必須フィルタ | **terra/high を一次レビューの既定。sol/high はリリース前・セキュリティ・並行処理を含む変更に限定し、指摘231件規模のフィルタ工数を見積に含める（メイン文脈に raw を直接読み込まない）** |
| 探索/大量読み | sol/medium（依存追跡） | **terra/medium を既定。sol は terra が2回外した後のみ。luna はタスク種でなく入力長で切る（目安 32K token 超の入力では使わない）** |
| effort | 初期値 medium・レビュー/調査は high | **初期値 medium で固定し、high 以上は「medium で失敗した実績」を条件にする（出典が3方向に割れているため、既定を high 側に置かない）** |
| 画像/vision | 判断材料なし・現状維持 | **維持（唯一、反証に耐えた行）** |

### 4-3. 前提条件として追記すべき運用ガード

1. **sol を pool に載せない。** pool 経由は usage が取得できず（実測: 4本すべて null）台帳にも残らない。sol は `codex_run` の直列実行に限定し、台帳へ記録する。または pool の usage 収集を修正するまで、sol の pool 利用を禁止する。
2. **計器を先に直す。** (a) pool の `turn.completed.usage` 取得、(b) timeout/interrupt 時にも部分 usage を記録（現状 `append_ledger` の `if not usage: return` で消える）、(c) `rate_limits.secondary` の取り込み。**計器が無い状態での既定変更は、悪化しても検知できない。**
3. **timeout をセットで見直す。** sol を使う段は `--job-timeout-sec` を 1800→3600 に上げる一方、`--idle-timeout-sec 600` は暴走検知のため据え置く。上限到達は失敗扱いとし、リトライは terra で行う（同じモデルで再挑戦すると同じコストを二度払う）。
4. **判定は A/B で行う。** 同一プロンプト・同一 cwd・同一 effort で sol/terra をペア投下し、n≥5 で (1) 完了率、(2) 人手確認した真陽性指摘数、(3) credits、(4) job.json の duration を記録する。**既存の証拠はすべて非ペアなので、これを済ませるまで「sol 既定」は保留**とする。最低限、write モード（実装委譲）を1本は含めること——scope creep という最大のリスクが read-only データでは一切観測できていない。
5. **再評価トリガを明記。** 販促価格の終了（2026-11-21）で sol:terra は 2.5倍へ。単価表の `promo_until` を見て自動的に方針を再評価する。
6. **§4 の内部矛盾を解消。** #32606（5時間枠を数分で焼く）を既知問題として載せながら、週次カウンタの余裕を根拠に「sol 常用は安全」と結論している。**失敗様式と計器が対応していない**ことを本文に明記する。

### 4-4. 総括の書き換え案

> **既定は terra 一次のまま維持する。** sol は「terra が明示的に失敗した」ことを条件とするエスカレーション先として扱う。無条件の sol 既定化は、(1) コスト倍率が未測定（唯一のペア試験では上積みゼロで2倍支払い、非ペア観測では 4.6〜5.5倍）、(2) 消費を観測できる計器が存在しない（pool は usage null、timeout は無記録、5時間窓は null）、(3) 品質優位の主出典が単一ベンダーで内部矛盾あり、独立系ベンダーは 2pt 差、(4) 自局の品質観測が非ペアかつ自己評価の循環、という4点により現時点では支持されない。計器の修復と n≥5 のペア A/B を前提条件とする。

---

## 参照した実ファイル（絶対パス）

- `/Users/<YOU>/.claude/skills/codex-bridge/scripts/codex_lib.py`（`credits_est()` = 単価表の掛け算、`usage_ledger_path()`）
- `/Users/<YOU>/.claude/skills/codex-bridge/config/codex_pricing.json`（`as_of 2026-08-22`、`promo_until 2026-11-21`、`[未確認]` 仮定4件）
- `/Users/<YOU>/.claude/skills/codex-bridge/scripts/codex_run.py`（L81-83 timeout 既定、L849-850、L751-752 `if not payload.get("usage"): return`）
- `/Users/<YOU>/.claude/skills/codex-bridge/scripts/codex_pool.py`（L42-45 並列/タイムアウト既定、L315 台帳追記条件、L461-466 idle/job timeout）
- `/Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge/var/codex_usage.jsonl`（本日12件。sol/xhigh 31.90・38.12 credits）
- `/Users/<YOU>/.claude/skills/codex-bridge/var/`（**空**。デプロイ側は台帳を持っていない＝`CODEX_BRIDGE_ROOT` 依存で記録先が分岐しうる）
- `/private/tmp/claude-504/-Users-isogai--claude/1f8b1193-46c5-4b5b-8341-360159ddf8eb/scratchpad/pool1/jobs/{j1-family,j2-bench,j3-community,j4-practice}/job.json`（4本すべて `usage: null`）
- `/private/tmp/claude-504/-Users-isogai--claude/1f8b1193-46c5-4b5b-8341-360159ddf8eb/scratchpad/smoke/{sol,terra}/job.json`
- `~/.codex/sessions/**/*.jsonl`（261 ファイル走査。`resets_at 1788491253` = 2026-09-04 12:07 JST、`secondary: null`、過去窓ピーク 3.0%）
- `codex-cli 0.149.1` の `codex exec --help` / `codex --help`（timeout 系フラグ不在を独立確認）

---

# 追補: sol/terra ペア A/B 実測（2026-09-01）

プロトコル: 6ペア12ジョブ、同一プロンプト・同一 effort・完全直列（pool 不使用）、write 系は同一コミットからの独立クローン、実行順はペアごとに交互。品質判定はブラインド（opus/xhigh の独立判定者6体、モデル対応表は非開示。identity_guess は6体全員「不明」＝ブラインド成立）。コスト・所要は計器修理済みの job.json / codex_usage.jsonl で採取（12ジョブ全件 status=completed）。

## 勝敗と裁定

| ペア | 種別/effort | 勝者 | margin | スコア | credits比(sol/terra) | 要点 |
|---|---|---|---|---|---|---|
| P1 | 仕様確定の小実装/med | sol | 小 | 8.5:7.5 | 2.41× | 生成 diff は実質同一。差は報告内の検証の丁寧さのみ |
| P2 | 曖昧な実装/med | sol | 小 | 7.6:7.0 | 1.83× | 堅牢化カバレッジは同点。mock 行の解釈とテストの質で sol |
| P3 | seeded レビュー(8バグ)/high | sol | 小 | 8:7 | 4.87× | **検出は完全同等**（両者 TP 7/8・FP 0・追加真バグ各1）。差は実害説明の正確さ（terra に事実誤り1件） |
| P4 | 否定命題の確定/high | sol | 小 | 8.7:8.0 | 1.65× | sol は全行読了+横断 grep で立証、used_percent の混同点にも到達 |
| P5 | 一次情報の突合調査/high | **sol** | **大** | 9:6.5 | 3.23× | terra は存在しない矛盾を1件捏造。sol は主張10件全て抜き取り検証と一致、changelog の決定的差分（販促表記は sol のみ）に到達 |
| P6 | 機械的集計/med | 同点 | なし | 10:10 | 2.00× | 両者 ground truth 7セル完全一致 |

## 確信度の更新（本編マップへの裁定）

- **仕様確定の単発実装 = terra**: 確信度 **高** へ（ペアで diff 実質同一。2.4倍を払う価値なし）
- **機械的作業 = luna/terra**: 高 へ（完全同点で2倍）
- **深い調査・統合段 = sol/high**: **中〜高** へ（唯一の大差。terra の捏造 vs sol の全一致は、正確さが本体の調査で決定的）
- **反証検証 = sol/high**: 中 へ（小差の支持。ただし sol プレミアムが最安の 1.65× で済む領域でもある）
- **recall 重視レビュー = sol/high**: **据え置き（中）**。ペア実測では検出数が完全同等で、sol の優位は実害説明の正確さのみ。**日常レビューは terra/high で十分**。sol はリリース前・セキュリティ・並行処理（実害説明の正確さが判断を左右する場面）に限定を維持
- **曖昧・多ファイル実装 = sol**: 中 へ（小差の支持。単一ファイル規模の曖昧タスクなら terra でもほぼ同等）

## 価格の訂正（P5 の成果）

- 販促価格は **sol のみ**。Terra/Luna の現行価格（$2/$12、$0.20/$1.20）に販促表記はない
- 「2026-11-21」は終了日ではなく「**少なくともこの日まで**」の下限。終了後の sol 価格は未確約（定価 $5/$30 に戻るなら sol:terra 単価比 2.5 倍）
- 本編の「販促終了 2026-11-21 で 2.5 倍化」という記述はこの内容で読み替えること

## 限界

- 各ペア n=1。margin「小」の勝敗は再現性未確認
- P2 の duration 比（1.05×）のみ外部ジョブの flock 待ち混入の可能性
- cached 比率が全ジョブ 70〜90% で、credits 比はキャッシュ状態の影響を受ける
- 週次 rate_limits の used_percent は12ジョブで不動（整数%分解能）＝この規模のコスト計器には使えない。コスト測定は credits_est と台帳 token 合計に拠る

## 計測データ（原本転記）

品質の優劣判定は含まない。実行条件と計器の値のみ。
生データ: `measurements.json` / 各 `pN/job-<model>/job.json` / モデル対応: `mapping.json`。

### 実行条件
- 全ジョブ `codex_run.py` 直列、`--timeout-sec 3600 --idle-timeout-sec 600`。
- ペア内は同一プロンプトファイル・同一 effort。実行順は交互（P1 sol→terra、P2 terra→sol、…）。
- write 系（P1/P2）は toolbox 2b1b808 からの `git clone --local` を 2 部ずつ用意して `--write`。
- read 系（P3〜P6）は toolbox 本体（P6 のみ `ab/p6`）を cwd に read-only。
- 12 ジョブすべて `status = completed`、`usage_source = turn.completed`、`usage_partial = false`、`errors` 空。

### 計測表

| ペア | 種別 / effort | モデル | ラベル | status | duration(s) | credits_est | input | cached_in | output | reasoning |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| P1 | write / medium | sol | X | completed | 120.8 | 5.2952 | 173,669 | 155,136 | 3,781 | 1,355 |
| P1 | write / medium | terra | Y | completed | 37.4 | 2.1976 | 130,943 | 107,264 | 1,591 | 342 |
| P2 | write / medium | terra | X | completed | 194.3 | 6.4640 | 557,983 | 508,416 | 4,812 | 1,430 |
| P2 | write / medium | sol | Y | completed | 203.9 | 11.7998 | 482,243 | 435,200 | 5,487 | 2,150 |
| P3 | review / high | sol | X | completed | 255.1 | 20.7191 | 668,191 | 589,056 | 13,830 | 10,719 |
| P3 | review / high | terra | Y | completed | 102.4 | 4.2587 | 112,201 | 79,104 | 7,361 | 5,616 |
| P4 | read / high | terra | X | completed | 98.4 | 8.3014 | 605,717 | 534,016 | 6,821 | 2,420 |
| P4 | read / high | sol | Y | completed | 144.3 | 13.6601 | 397,021 | 335,872 | 8,373 | 3,846 |
| P5 | read+web / high | terra | X | completed | 74.3 | 5.6952 | 308,392 | 239,872 | 3,566 | 1,600 |
| P5 | read+web / high | sol | Y | completed | 153.1 | 18.3992 | 652,779 | 558,336 | 6,743 | 3,352 |
| P6 | read / medium | terra | X | completed | 14.1 | 1.2308 | 45,462 | 27,136 | 596 | 45 |
| P6 | read / medium | sol | Y | completed | 18.6 | 2.4618 | 45,507 | 28,160 | 891 | 248 |

ペア内比（sol / terra）:

| ペア | duration 比 | credits_est 比 | output tokens 比 | reasoning tokens 比 |
|---|---:|---:|---:|---:|
| P1 | 3.23× | 2.41× | 2.38× | 3.96× |
| P2 | 1.05× | 1.83× | 1.14× | 1.50× |
| P3 | 2.49× | 4.87× | 1.88× | 1.91× |
| P4 | 1.47× | 1.65× | 1.23× | 1.59× |
| P5 | 2.06× | 3.23× | 1.89× | 2.10× |
| P6 | 1.32× | 2.00× | 1.49× | 5.51× |

`credits_est` は ChatGPT プランのクレジット**概算**であり請求額ではない。

### write 系の客観事実（品質判定ではない）

| ペア/モデル | 変更ファイル | 追加行 | 削除行 | 検証 | 結果 | 範囲外変更 |
|---|---|---:|---:|---|---|---|
| P1 / sol | install.sh | 10 | 7 | `bash -n install.sh` | rc=0 | なし（install.sh のみ） |
| P1 / terra | install.sh | 10 | 7 | `bash -n install.sh` | rc=0 | なし（install.sh のみ） |
| P2 / sol | codex_job.py, test_codex_bridge.py | 28 | 0 | `python3 -m unittest discover -s codex-bridge/tests` | rc=0（全緑） | なし |
| P2 / terra | codex_job.py, test_codex_bridge.py | 31 | 0 | `python3 -m unittest discover -s codex-bridge/tests` | rc=0（全緑） | なし |

- 検証は**私（executor）が各クローンで実際に実行**した結果。Codex の自己申告ではない。
- P1 は両者とも `EXCLUDE_DIRS=(experiments __pycache__ .pytest_cache)` を定義し `var` を含めていない
  （定義位置は sol=134 行目、terra=34 行目）。
- P2 は両者とも新規ファイルを作らず既存 2 ファイルへの純増（削除 0 行）。
- P6 は両者の出力とも前置き・コードフェンス無しで `json.loads` 可能（内容の正誤は判定していない）。

### rate_limits（primary、window 10080 分）
- 実験前（04:13:08Z）: `used_percent = 1`
- 実験後（04:50:15Z）: `used_percent = 1.0`
- 実験中に追記された 13 行すべてで `used_percent = 1.0`。12 ジョブでは週次窓の整数％が動かなかった。
- `secondary` は全行 null。

### 台帳
- `codex-bridge/var/codex_usage.jsonl`: 80 行 → 93 行（本実験の 12 行 + 外部ジョブ 1 行）。
- `codex-bridge/var/codex_rate_limits.jsonl`: 実験中に 13 行追記。
