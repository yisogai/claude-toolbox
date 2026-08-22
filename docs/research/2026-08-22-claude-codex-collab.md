<!--
生成: 2026-08-22、Claude Code セッション（メイン=Fable 5）。
Workflow（opus 16体: 7観点調査 → 反証検証 → 欠落チェック → 統合）の統合レポートに、
メイン側で直接確認した一次情報（codex-plugin-cc 内部コード、h5i MANUAL.md、learn.chatgpt.com docs）を突き合わせ済み。
[確認済] = 一次情報で検証、[未確認] = 未検証。未コミット（保存場所はユーザー判断で変更可）。
-->

# Fable 統括 × Codex 併用 技術調査レポート（2026-08-22 時点）

## 1. 結論サマリ

- **問いA（Fable統括→Codex実装→Opusレビュー→Fable検収）: 役割分担は妥当。ただし接続を codex-plugin-cc にする案は却下を推奨。**
- レビュー方向は正しい向き。arXiv 2607.21656 は Claude→Codex レビューで +18.1pt、逆向きは −8.6pt と非対称を報告 [確認済]（ただし旧世代モデル・単発・実行なし）。
- OpenAI 自身のシステムカードが「長尺の Codex 実装は監督必須」と明記しており、レビュー段は品質だけでなくベンダー公式要件でもある [確認済]。
- codex-plugin-cc は 2026-07-08 を最後に push なし・open issue 224 / PR 189 が滞留し、6コマンドが `disable-model-invocation: true` でサブエージェントから呼べない [確認済]。自動ループの基盤にできない。
- **接続層の推奨は `codex exec`（または Codex SDK）を Bash から非対話で叩く自作ラッパー。** CLI/SDK は 0.149.0 で日次更新中 [確認済]。
- **問いB（h5i 採用）: 現時点では見送り。ただし「自由度が落ちる」という懸念の立て方は誤り。**
- 想定していた h5i 像（worktree 競争・相互レビュー・i5h 会話）は 2026-08-05 の PR #385 で削除済み [確認済]。奪われる主導権はもう存在しない。
- 代わりの理由は成熟度。複数エージェント協調（forum）は v0.3.6（2026-08-21）で入ったばかりで、公開2日目 [確認済]。

## 2. 接続方式の比較

| 方式 | 仕組み | 非対話 | モデル指定 | Fable がメイン維持 | 導入コスト | 成熟度 | 向く用途 |
|---|---|---|---|---|---|---|---|
| codex-plugin-cc | slash → Node `codex-companion.mjs` → codex app-server (JSON-RPC) | 実質不可（6コマンドが model-invocation 不可 [確認済]）。Bash 直叩きなら可だが未文書 | `--model` は task/review 双方で受理、`gpt-5.6-*` は素通し [確認済]。effort は task 限定 [確認済] | 保てる | 低（プラグイン導入のみ） | **低**：2026-07-08 以降フリーズ [確認済] | 人間が手で叩く `/codex:adversarial-review` |
| `codex exec` を Bash ラッパー | CLI が in-process app-server を起動 | **可**（`--json` JSONL、`-o`、`--output-schema`）[確認済] | `-m` と `-c model_reasoning_effort=...` [確認済] | 保てる | 中（自作スクリプト） | **高**：0.149.0、日次リリース [確認済] | **本命**。実装委譲ループ |
| Codex SDK（TS/Python） | ローカル app-server を JSON-RPC 駆動 | 可 | `model=` 引数 [確認済] | 保てる | 中〜高 | 高（CLI と同一バージョンで追随）[確認済] | CI・厳密な制御が要る段 |
| `codex mcp-server` を MCP 直結 | `claude mcp add` でツール化 | 可 | `codex` ツールに `model`/`cwd`/`config` あり [確認済] | 保てる（メイン会話の呼び出しは2分で自動バックグラウンド化 [確認済]） | **最低**（1コマンド） | **将来性なし**：2026-08-20 に非推奨化 [確認済] | 短期の試用のみ |
| 逆方向（`claude mcp serve` / cc-plugin-codex） | Codex が外側ループ | 可 | — | **保てない**（主従逆転） | 低 | 中 | Codex 主導時の補助 |
| h5i | 使い捨て box + forum | box 単位で可 | box 内 CLI に依存 | 保てる（ホスト設定に触らない [確認済]） | 中 | **低**（forum は公開2日目） | 未知コードの隔離実行 |
| Git 経由（i5h） | — | — | — | — | — | **消滅**（`h5i msg` は削除済み [確認済]） | 採用不可 |
| 上位ハーネス（symphony 等） | 別ループが上に乗る | 可 | 可 | **保てない** | 高 | 中 | 今回は不採用 |

補足：`claude mcp serve` を Codex 側に繋ぐと `Workflow` `Skill` `Agent` `CronCreate` まで露出する（実測26ツール、`Grep`/`Glob` は非露出、`dispatch_agent` は現存せず）[確認済]。権限境界の観点でも逆方向は補助に留めるべき。

## 3. codex-plugin-cc の詳細

- 実体は Node CLI。公開サブコマンドは setup / review / adversarial-review / task / transfer / status / result / cancel の8種（内部用に task-worker / task-resume-candidate が実在）[確認済]。パスは `~/.claude/plugins/cache/openai-codex/codex/<version>/scripts/codex-companion.mjs`。README は `codex-companion` の語を一度も含まず、Bash 直叩きは未文書の内部実装 [確認済]。
- **worktree は作らない。** `task --write` は sandbox=`workspace-write` / approvalPolicy=`never` で Claude と同じチェックアウトを直接書き換える [確認済]（リポジトリ内の "worktree" 2件は「認識して除外する」テストのみ）。
- **構造化出力は adversarial-review だけ。** スキーマは `verdict: approve|needs-attention` / `findings[{severity, file, line_start, line_end, confidence, ...}]` / `next_steps[]` [確認済]。`/codex:review` は自由文。
- 主要な既知バグ（すべて open）：
  - #601 サブエージェントから6コマンドを呼べない [確認済]
  - #634 前景 Bash の120秒自動バックグラウンド化でジョブが `running` のまま凍結。**死んだタスクが apply_patch を4個適用済みのまま失敗シグナルを出さなかった** [確認済]
  - #542 600秒で harness がプロセスツリーを SIGTERM、session id も残らない [確認済]
  - #531 write モードで書き込み0件でも `completed` を返す [確認済]
  - #540/#612/#665 共有 broker がセッション終了で落ち、他セッションのジョブが黙って死ぬ [確認済]
  - #661/#664 SessionStart hook が `$CLAUDE_ENV_FILE` に無条件 append し、約8KB で全 Bash 呼び出しが壊れる（`/compact` 毎に発火。macOS での閾値は [未確認]）
  - #600 4コマンドが inline バッククォート記法のため Bash 権限マッチャを通せない [確認済]
- 同梱スキルは今も `gpt-5-4-prompting`、`spark` は `gpt-5.3-codex-spark` を指す [確認済]。GPT-5.6 対応 PR #638 は未マージ。

## 4. Codex CLI 非対話利用の要点

- 安定版 0.149.0（2026-08-20）[確認済]。
- **`--full-auto` は非推奨ではなく削除済み**（PR #36054、0.147.0 のリリースノートに明記）[確認済]。渡すと clap のパースエラーで即死する。代替は `--sandbox workspace-write`。公式 docs はこの点が古いまま。
- 主要フラグ：`--json`(=`--experimental-json`) / `-o|--output-last-message` / `--output-schema` / `-m` / `-c key=value`（TOML 解釈） / `-s` / `-C` / `-p` / `--ephemeral` / `--skip-git-repo-check` / `--approve-for-me`（0.147.0 追加）。`-s` `-C` `-p` `-i` はサブコマンドより前に置く必要がある [確認済]。
- **終了コードで成否判定してはいけない。** exit 1 は「致命エラー通知(will_retry でない) / turn が failed・interrupted / server request 失敗」のみ。テストが落ちたまま turn が正常完了すれば exit 0 [確認済]。
- `--json` のイベント型は `thread.started` / `turn.started` / `turn.completed` / `turn.failed` / `item.started` / `item.updated` / `item.completed` / `error` の8種。item 型は agent_message / reasoning / command_execution / **file_change**（複数形は誤り） / mcp_tool_call / collab_tool_call / web_search / **todo_list** / error の9種。usage は input / cached_input / cache_write_input / output / reasoning_output の5フィールド [確認済]。
- レビューは `codex exec review [--uncommitted|--base|--commit]` を使う。トップレベル `codex review` は `--json` `-o` `--output-schema` を受け付けない [確認済]。**`exec review` は `--output-schema` を受理するが無視して散文を返し exit 0 になる既知バグ**（#35596 / #38545、open）[確認済] ため、構造化レビュー判定が欲しければ通常の `codex exec --output-schema` にレビュー用プロンプトを渡す方が確実。
- config：`model_reasoning_effort` は `minimal|low|medium|high|xhigh`（公式リファレンス）。一方 codex 本体の enum には Max / Ultra があり、models_cache では sol/terra が ultra まで advertise、none/minimal は非対応 [確認済]。**公式ドキュメント間で不整合**。プロファイルは 0.134.0 以降 `$CODEX_HOME/<name>.config.toml` の別ファイル方式 [確認済]。
- `codex mcp-server` は 2026-08-20 に非推奨化（PR #39657）[確認済]。app-server は公式が experimental と明記し、「CI・自動化なら SDK を使え」としている [確認済]。
- AGENTS.md は起動ごとに1回読まれ、ルート→cwd で連結、上限 `project_doc_max_bytes` 既定 32KiB [確認済]。

## 5. ChatGPT Pro の利用枠

- Pro は $100（Plus比5x）と $200（Plus比20x）の2段階。想定の $200 は 20x [確認済]。
- 5時間窓のローカルメッセージ数（Pro 20x）：Sol 200〜2,000 / Terra 500〜4,000 / Luna 5,000〜40,000。公式は "estimates" と明記 [確認済]。
- **5時間窓は 2026-07-12 に一時撤廃 → 2026-07-30 に復帰。2026-08-22 現在は「5時間窓＋週次」の二層が有効** [確認済]。当初「情報が割れている」とした整理は誤り。
- **週次上限の絶対値は全プランで非公開**（"Additional weekly limits may apply." のみ）[確認済]。
- クレジット単価（per 1M in/cached/out）：Sol 100/10/500、Terra 50/5/300、Luna 5/0.5/30 [確認済]。**Sol は販促価格で、公式に「少なくとも 2026-11-21 まで」と期限付き**。定価換算では 125/12.5/750 相当のため、11月以降に約25%悪化しうる [確認済]。
- 非対話利用も ChatGPT 枠を消費する（プラグイン README「Usage will contribute to your Codex usage limits」）[確認済]。ただし **`OPENAI_API_KEY` が環境にあると予告なく API キー認証へ切り替わり従量課金に流れる既知バグ（openai/codex #20099、open）**[確認済]。固定するキーは二次記事が言う `preferred_auth_method` ではなく **`forced_login_method = "chatgpt"`** [確認済]。
- effort はトークン建てに直結する。実測報告では Sol Ultra を3〜4時間回しただけで週次枠の約30%を消費、サブエージェント39体構成で2日で枯渇 [確認済]（同スレッドの「2日で30億トークン」は別人の発言で、報告者本人の数値ではない）。
- GPT-5.4 / 5.4 mini は **2026-08-31 に Codex から提供終了** [確認済]。プラグイン README の `gpt-5.4-mini` 例をコピーすると直後に壊れる。

## 6. モデル適性

**実装：Sol か Terra か（評価が割れている）**

| 出典 | Sol | Terra |
|---|---|---|
| CodeRabbit 実装ベンチ（100+タスク、長時間）[確認済] | 63.7%（平均出力 20,968 tok） | 40.7%（55,594 tok、約2.65倍） |
| 公開ベンチ SWE-bench Pro [確認済] | 64.6% | 63.4% |
| OpenAI システムカード「破壊回避＋完遂」[確認済] | 0.44 | 0.37 |

長尺・多ファイル実装は Sol、仕様確定済みの単発修正は Terra、が実測に整合する。なお **SWE-bench Pro は 2026-07 の監査で公開タスクの約30%が壊れていると判明し OpenAI が推奨を撤回**しており、ベンチ差を根拠にするのは弱い [確認済]。

**レビュー：Opus か GPT-5.6 か**

- CodeRabbit 実測（2026-07-24）：Opus 5 x-high の既知バグ捕捉率 55.2%（ベースライン 61.1%、Sol 69.7%）、actionable precision 39.3%（同 35.2%）。弱点は論理エラー・レースコンディション・API 誤用、強みは設定ミス・コード品質 [確認済]。**full-stream precision は 28.6% でベースライン 32.8% を下回り、nitpick は4倍**。
- Anthropic 公式は Opus 5 を「高 precision かつ高 recall」とする [確認済]。これは指標定義の違い（per pass の実バグ率 vs 既知バグ集合の再現率）で正面衝突ではないが、nitpick 4倍は「few false positives」と整合しにくい。
- Endor Labs（2026-08-10）：セキュア生成 SecPass は Opus 5 32.4% > Fable 5 25.7% > Sol 20.1% [確認済]。
- 結論：**Opus 単独ゲートにしない**。Opus を精度レーン、Codex 側の adversarial-review / `codex exec` レビューを再現率レーンとして併走させる。
- 公式プロンプト指針：「高深刻度のみ報告」「保守的に」と書くと文字通り従って報告数が減るため、**全件報告させて別パスでフィルタ**する [確認済]。また Opus 5 に「最終検証ステップを入れよ」等の検証指示を書くと過剰検証になるので削除せよ、とある [確認済]。ただし禁じられているのは**自分の作業の自己再確認**であり、公式は writer-verifier パターン自体は推奨側に置いている [確認済]。つまり「Codex 実装 → Opus レビュー」は公式ガイドに反しない。

**クロスベンダーレビューのエビデンス**

- 定量研究は arXiv 2607.21656 の1本のみ（116問・単発・実行なし・Opus 4.7 / GPT-5.5）[確認済]。解説記事群（cross-model adversarial review、multi-model-review）はいずれも定量データなし [確認済]。誤りクラス分布の実測差（Opus は論理・並行性に弱く設定系に強い／Sol は高再現・低精度）が間接的裏付け。

**検収を Fable にやらせるか**

- CodeRabbit は Fable 5 をレビュー役として明確に非推奨（actionable precision 32.8%、コメント253件）[確認済]。**検収は「欠陥探索」ではなく「受入条件の適合判定＋`git diff --stat` とテスト実行結果の実確認」に限定**すべき。#634/#531 のように「実装した」という戻り値が嘘になる実例があるため、この実確認は必須。

## 7. h5i の評価

- **想定していた機能は削除済み。** 2026-08-05 の commit ede2e14（PR #385、+9,915/−110,060、185ファイル）で capture / recall / audit / msg(=i5h) / team / orchestra / mcp / hook 等 33 CLI モジュールを削除。続く M1b がさらに provenance ドメイン32モジュールと orchestra crate（約77k行）を削除 [確認済]。
- MANUAL.md が自ら「Not a provenance system. … multi-agent orchestra … That is gone.」と宣言 [確認済]。
- h5i-python は h5i-dev/senv にリネームされ、オーケストレーション SDK としては現存しない [確認済]。h5i.dev の i5h / orchestra / token reduction 記事8本はすべて「Article moved」スタブに差し替わっている（8URL 実測）[確認済]。「95% トークン削減」の主張は README・MANUAL のどこにも存在しない [確認済]。
- **現行の複数エージェント協調は forum。** 導入は PR #528（merged 2026-08-21）、v0.3.6 で初公開 [確認済]。役割は worker / reviewer / observer / human の4種で、Fable=human、Codex box=worker、Claude box=reviewer に素直に写像できる設計ではある。ただし本日時点で公開2日目、v0.3.7 も出ていない。
- **「自由度が落ちる」懸念の評価：方向としては半分正しく、理由が違う。** h5i は settings.json も hook もいじらず、`h5i skill install` 経由で Fable が CLI として呼ぶ設計＝部分採用が既定路線 [確認済]。奪われるものは無い。ただし box 内の Claude はホストの生きた `~/.claude` を使わず（process/supervised tier では per-box コピーへ bind-redirect、container tier ではホスト $HOME を非マウント）、**model-policy hook や cost-manager が効く保証がない** [確認済]。ここが実体的な「自由度のコスト」。
- macOS 固有の弱点：seccomp 相当なし、cgroups なしで mem/procs 上限が効かない、box は host の loopback を共有、配布バイナリは aarch64 のみ [確認済]。
- 1 box 1 ランタイム制約は資格情報レイヤで実際に強制されている（`agent-claude` は `~/.claude` のみ、`agent-codex` は `~/.codex` のみ、egress も分離）[確認済]。
- ROADMAP が「M4/M5/M7 の exit criteria は実エージェント・実人間で未実証」「control lock はエージェント側で未強制」と自認し、これは v0.3.6 当日時点でも維持されている記述 [確認済]。採用事例は自作2件のみ [確認済]。
- **判定：今回のループには採用しない。** 必要になるのは「モデル API トークンごと隔離したい」「実行 receipt を残したい」という具体要求が立ったときで、今回の構想には含まれない。Codex の作業ツリー衝突対策は、Claude Code 標準の `--worktree` と `codex exec -C <worktree>` で足りる。

## 8. 実践知見と落とし穴

- **収束条件を回数だけにしない。** Contrast Security の統制実験では R1 blocking 1件 → R2 で実装側の修正が既存呼出を壊して7件 → R3 は総37件のまま予算切れ、中核欠陥は未修正だった [確認済]。推奨停止条件は「直近2ラウンドで blocking が厳密に減っていなければ人間へ渡す」。AgentPatterns は独立に「自動修正は1パスに限れ」と主張 [確認済]。日本語ブログ由来の「3〜5ラウンドが相場」は収束の実測を示していないので、回数上限は予算ガードとしてのみ使う。
- **レビュアに編集権を与えない。** Opus サブエージェントから Edit/Write を外す。Codex 側は `--sandbox read-only`。Codex の `/review` は read-only が既定であって不変条件ではない [確認済]。
- **偽陽性フィルタを Fable に置く。** Opus 指摘をそのまま Codex に投げると往復が枠を食う。計画時に受入条件を明文化 → Opus 指摘を Fable が Must/Should/Nice に裁定 → Must のみ Codex へ、が定番。
- **タイムアウト設計。** Claude Code の Bash は最大600,000ms（現行 2.1.239 で確認）[確認済]。実装委譲は `run_in_background` かジョブID方式で投げ、ポーリング側に**自前の壁時計タイムアウト**を置く（死んだジョブが `running` に見え続けるため）。symphony の既定値（turn 1時間 / 無進捗5分 / 並列10）が出発点に使える [確認済]。
- **並列度は絞る。** ChatGPT 認証で `codex exec` を連続実行すると4回目で token_invalidated になる報告（#26303、open、0.136.0 時点）[確認済]。現行 0.149.0 での再現は [未確認]。並列実行のトークン競合ハングの報告もある。
- **非TTY で空出力になる報告**（#19945、open、0.124.0〜）。長い仕様は引数ではなく `cat prompt.txt | codex exec -` で stdin から渡すのが安全 [確認済]。
- **MCP は使わせない。** プラグイン経由の task 実行では MCP 承認要求に誰も応答せず全 MCP 呼び出しが拒否される（#640、open）[確認済]。Codex に委譲するタスクはリポジトリ内で完結するものに限る。
- **規約の伝達。** CLAUDE.md は連結型・公式推奨200行未満、AGENTS.md は closest-wins の上書き型・32KiB 上限と挙動が異なる [確認済]。symlink は Windows で権限問題（Git Bash ではコピーになり無言で古いまま）[確認済]。**階層を1段（リポジトリルートのみ）に留め、受入条件・コーディング規約・変更範囲の原則だけを AGENTS.md に抜き出す**のが安全。CLAUDE.md 全文コピーは不可。
- **Agent Teams は無効のまま維持。** 有効だと名前付き subagent が teammate 化し、結果を待つオーケストレーションが stall しうる（公式明記）[確認済]。`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0`、ただし project/local settings が `1` にすると user settings を上書きする点を確認すること。
- **cmux の codex シム。** PATH 先頭が cmux のシムディレクトリで、`codex` シムが `cmux-codex-wrapper` に exec しうる [確認済、ローカル実測]。オーケストレーション用の呼び出しは絶対パスで実バイナリを叩くか、シム経由の挙動を先に1回検証する。
- **effort と逸脱リスクのトレードオフ。** システムカードは「最高 effort 使用時の persistence 増大が意図逸脱を駆動している可能性」を明記 [確認済]。実装役 Sol は high 止まりにし、禁止事項を明示列挙する（「明示的に禁止されていなければ許可」と解釈する傾向があるため）。
- **effort 語彙の非互換。** Claude 側の `xhigh` をそのまま Codex へ持ち込むと環境によって設定エラーになる（プラグイン PR #99 で README の xhigh が high に差し替えられた）[確認済]。
- **cost-manager の穴。** Codex 分は計上できない。`turn.completed` の usage 5フィールドを拾って独自に記録する設計が要る [確認済]。

## 9. 未確認事項・矛盾

**検証で訂正されたもの（訂正後を上に記載済み）**
- `codex exec --full-auto` は「非推奨で警告」ではなく**削除済み**（公式 docs が古い）。
- 5時間窓は「情報が割れている」ではなく**復帰済みで確定**。
- h5i の「worktree grep 0件」は事実誤認（2件ヒット。ただし除外処理のみで結論は不変）。
- 「2日で30億トークン」は報告者本人の数値ではない。
- Scale 標準化版で「GPT-5.4 xHigh が首位」は誤り（首位は Muse Spark 1.1、しかも新世代モデルは未掲載）。
- `claude mcp serve` の露出ツールに `Grep`/`Glob` は無く `dispatch_agent` も現存しない。

**残る未確認**
- codex CLI 0.149.0 が `gpt-5.6-sol` を受けるか（#561 の HTTP 400 は 0.145.0 時点、#32520 は 0.144.1 時点でいずれも open）[未確認]。契約後に `codex exec review` 経路で Sol を1回通す実測が必要。
- #19945（非TTY 空出力）・#26303（token_invalidated）が現行版で解消しているか [未確認]。
- 前景 Bash の120秒自動バックグラウンド化が現行 Claude Code で起きるか（ツール仕様に明示なし）[未確認]。
- ChatGPT Pro 20x の週次上限の絶対値 [未確認・公式非公開]。
- 「Fable計画 → Codex実装 → Opusレビュー → Fable検収」という三者構成の実運用報告は7観点のどこにも1件も無い [未確認]。公開事例はすべて二者構成。
- Opus レビューの最適 effort（公式は「high から始めて eval で調整」、CodeRabbit 実測は x-high で recall 低下・nitpick 4倍、ユーザーの model-policy は xhigh 固定）。同一 PR を medium と xhigh で振る A/B が必要 [未確認]。
- 公式主張「Opus 5 は高 precision かつ高 recall」と CodeRabbit 実測の食い違いが effort 差なのかデータセット差なのか [未確認]。
- codex-plugin-cc #661 の Bash 破壊が macOS で何回の SessionStart で発症するか [未確認]。
- h5i の kernel tier で bind されたホスト `~/.claude` の下で model-policy hook が効くか [未確認]。

## 10. 出典一覧

**codex-plugin-cc**
- https://github.com/openai/codex-plugin-cc （README・releases・issues、2026-08-22 実測）
- Issue #601 / #600 / #634 / #542 / #531 / #540 / #612 / #665 / #661 / #664 / #640 / #653 / #639 / #620 / #561 / #651 / #654 / #648 / #638（すべて 2026-08-22 時点 open）
- https://github.com/openai/codex-plugin-cc/blob/main/plugins/codex/schemas/review-output.schema.json

**Codex CLI / SDK / 公式ドキュメント**
- https://learn.chatgpt.com/docs/non-interactive-mode （2026-08-22）
- https://learn.chatgpt.com/docs/config-file/config-reference （2026-08-22）
- https://learn.chatgpt.com/docs/pricing （2026-08-22）
- https://learn.chatgpt.com/docs/models / https://learn.chatgpt.com/docs/hooks / https://learn.chatgpt.com/docs/codex-sdk / https://learn.chatgpt.com/docs/code-review
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/exec/src/cli.rs, .../exec/src/lib.rs, .../exec/src/exec_events.rs（rust-v0.149.0 タグで照合）
- https://github.com/openai/codex/pull/36054（--full-auto 削除、merged 2026-07-30）
- https://github.com/openai/codex/pull/39657（mcp-server 非推奨、merged 2026-08-20）
- Issue #20099 / #26303 / #19945 / #32520 / #31869 / #35596 / #38545 / #32984 / #33898
- https://registry.npmjs.org/@openai/codex, https://registry.npmjs.org/@openai/codex-sdk （latest 0.149.0、2026-08-20）
- https://deploymentsafety.openai.com/gpt-5-6-preview/gpt-5-6-preview.pdf （文書日付 2026-06-25、改訂 2026-08-19）
- https://raw.githubusercontent.com/openai/symphony/main/SPEC.md （2026-08-19）

**Claude Code**
- https://code.claude.com/docs/en/mcp / agent-teams / cross-session-messaging / channels / memory （いずれも 2026-08-22 取得）
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5
- https://platform.claude.com/docs/en/about-claude/models/whats-new-opus-5
- https://claude.com/blog/a-field-guide-to-claude-fable-finding-your-unknowns （2026-07-06）

**h5i**
- https://github.com/h5i-dev/h5i/commit/ede2e14cb6 / pull/385（2026-08-05）、pull/528（merged 2026-08-21）
- https://raw.githubusercontent.com/h5i-dev/h5i/main/MANUAL.md / ROADMAP.md / SHOWCASE.md
- https://api.github.com/repos/h5i-dev/h5i （stars 538 / forks 51 / open issues 7、2026-08-22 実測）
- https://raw.githubusercontent.com/h5i-dev/h5i/v0.1.5/docs/i5h-protocol.md （旧 i5h 仕様）

**評価・実測**
- https://arxiv.org/abs/2607.21656 （2026-07-22、クロスモデルレビューの非対称性）
- https://www.coderabbit.ai/blog/opus-5-model-review （2026-07-24）
- https://www.coderabbit.ai/blog/gpt-5-6-sol-and-terra-benchmark （2026-07-09）
- https://www.coderabbit.ai/blog/fable-5-model-review （2026-06-09）
- https://www.contrastsecurity.com/security-influencers/when-ai-reviewers-cannot-agree （2026-07-20）
- https://www.endorlabs.com/learn/best-in-class-novel-in-method-opus-5-and-the-recall-then-diverge-pattern （2026-08-10）
- https://benchlm.ai/benchmarks/swe-bench-pro （2026-08-22 更新、公開タスク約30%破損の注記）
- https://agentpatterns.ai/code-review/review-then-implement-loop/ （2026-06-13）
- https://dev.classmethod.jp/articles/claude-code-codex-cross-review/ （2026-05-28）
- https://qiita.com/suzuki-navi/items/7c21c2a505772dde655a （2026-08-17、Opus 5 vs Sol 実務レビュー比較）
- https://community.openai.com/t/200-pro-exhausted-in-2-days-these-limits-are-unviable-for-higher-tiers/1388997 （2026-08-04〜07）