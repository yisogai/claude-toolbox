<!--
生成: 2026-08-25、Claude Code セッション（メイン=Fable 5）。
Workflow（opus 8体: 4系統リサーチ → 敵対的検証、579k tokens・15分）＋ローカル観察で構成。
リサーチは openai/codex の Rust ソース（rust-v0.149.1 タグの codex-rs/login/src/auth/）を実読して裏取り済み。
[確認済] = 一次情報（ソース・issue・公式 docs）で検証、[中確度] / [未検証] を区別。
姉妹編: 2026-08-24-chatgpt-multimodal-usage.md、2026-08-22-claude-codex-collab.md。
-->

# Codex CLI のローカル並列実行 調査（2026-08-25 時点、codex-cli 0.149.1）

## 1. 結論

- **「複数の codex exec を無条件に同時実行してよい」とは今も言えない。** 認証（ChatGPT アカウント）の
  トークンリフレッシュに**プロセス間の排他が存在しない**ことをソースで確認。公式 CI/CD ドキュメントも
  「auth.json は runner ごと、または直列化されたワークフローごとに1つ。並行ジョブでの共有禁止」と明示 [確認済]。
- **構造的に安全な本命は「1プロセス多重化」**: `codex exec` は常に自プロセス内 app-server を起動する
  （共有デーモンに接続できない）[確認済] が、**app-server の JSON-RPC 直叩き、または Python SDK は
  1プロセスで複数 conversation を並行駆動できる** [中確度]。1プロセス = AuthManager 1個 =
  リフレッシュはプロセス内 Semaphore で直列化されるため、認証競合が構造的に消える。
- 複数セッション問題への現状回答: codex-bridge 経由なら `var/locks/` の flock が**マシン全体で共有**
  されるため、既に直列化されている（多重実行にならず待ち行列になる）。

## 2. 認証機構の事実（rust-v0.149.1 ソース実読、検証エージェントが独立照合済み）

- **refresh token はローテーション型・実質単回使用**。リフレッシュ応答の新 refresh_token で上書きされ、
  旧トークンの再利用はサーバが `refresh_token_reused` 等で恒久拒否する [確認済]。
- リフレッシュ発火: access token JWT の exp が「現在+5分」以内、または last_refresh が 8 日超 [確認済]。
  **加えて 401 起因の UnauthorizedRecovery という独立経路がある**ため、「5分窓を避ければ無害」は不成立
  （検証エージェントが反証）[確認済]。
- 排他はプロセス内 `Semaphore(permits=1)` のみ。**プロセス間の file lock は無い**。緩和策は
  「guarded reload」（refレッシュ前に auth.json を再読込し、他プロセスが更新済みならスキップ＋
  失敗後もディスクの新トークンを拾って自己修復）のみで、read→write 間の TOCTOU 窓は残る [確認済]。
- auth.json の書込は **temp+rename でない非原子的 truncate+write**（部分書込・破損の窓あり）[確認済]。
- MCP OAuth 側には跨プロセス lock（store_lock.rs、0.144.0）が実装済みだが ChatGPT auth には未展開 [確認済]。
- issue **#26303**（連続/並列 exec で token_invalidated、0.136.0）は 2026-08-25 時点 **open・コメント0**。
  0.136→0.149.1 のリリースノートに本件の修正記載なし [確認済]。**逐次実行ですら発生した報告**であり、
  リスクモデルは競合窓だけでは説明しきれない。
- issue **#27773**: 呼び出し量が多いとサーバ側がセッショントークンを無効化する報告（Pro、0.147.0 でも）
  [中確度]。→ 401 をアグレッシブにリトライしない。
- セッション取り違え（#11435）は exec の app-server 再実装で解消済み。`--ephemeral` もある [確認済]。

## 3. 選択肢の評価

| 方式 | 安全性 | 評価 |
|---|---|---|
| 現行（flock 直列＋spawn＋cloud） | ◎ | **当面の既定を維持**。複数セッションも共有 flock で直列化済み |
| app-server / Python SDK の1プロセス多重 | ◎（構造的） | **本命**。codex-bridge に「ワーカープール」モードを作る PoC の価値あり。app-server は公式に experimental、SDK は CI/自動化向けの公式推奨（8/22 調査） |
| 共有 auth.json のまま 2〜3 並列（--ephemeral・stagger・プリウォーム） | △ | 動く見込みは高いが保証なし（#26303 が逐次でも発生）。やるなら実測カナリアで確認後、401 検出→直列フォールバック付きで |
| CODEX_HOME 分離 + auth.json **コピー** | ✕ | **禁止**。ローテーション1回でコピーと原本が連鎖失効 [確認済] |
| CODEX_HOME 分離 + auth.json **symlink** | ○ | 実行時状態（sessions/SQLite）の隔離が目的なら有効（コミュニティ allowlist レシピ）。認証は共有と同じ扱いのまま |
| API キー従量課金 | ◎ | 原理的に安全だが別課金。サブスク枠温存の方針と背反、不採用 |

## 4. 2〜3並列を試す場合の設計（カナリア前提）

1. 事前に auth.json をバックアップ（診断用。ローテーション後は復旧には使えない）
2. fan-out 直前に単発 exec を1回（プリウォーム。ただし万能でない——§2）
3. ワーカーは `--ephemeral`、起動を数百 ms〜数秒スタガー
4. 401 / token_invalidated 検出時: 数秒待って**1回だけ**再試行（guarded reload の自己修復を拾う）、
   再失敗なら fan-out 全体を停止して直列へフォールバック、再ログインをユーザーに報告
5. 失敗時の復旧コスト: `codex login`（ブラウザ）1回

## 5. 推奨アクション

1. 既定は現状維持（直列＋spawn＋cloud）。SKILL の並列度1の根拠がソースレベルで確定した。
2. 次の一手として **Python SDK / app-server の1プロセス多重の PoC**（codex-bridge ワーカープール化）。
   成功すれば Workflow から `agent()` 感覚で codex ワーカーを N 並列にできる。
3. 上流 watch: #26303（並列 exec 認証）と #27773（量ベース無効化）。ChatGPT auth に store_lock 相当の
   跨プロセスロックが入ったら並列度引き上げを再検討。
4. 全セッションで「codex 直呼びせず codex-bridge 経由」を守る（共有 flock に乗せるため）。

## 6. PoC 実測結果（同日追記。opus 実装、codex-bridge/experiments/parallel-poc/）

**1プロセス多重化は成立した。** `codex app-server` 1個に 3 thread を作り `turn/start` を連続送信:

- 3本の `sleep 20` シェル実行区間が **19 秒重複**（3本同時）。直列なら 60 秒のところ**全体 34 秒**
- `~/.codex/auth.json` の mtime は実行前後で**不変**（リフレッシュ競合なし）
- フレーミングは素の JSONL。承認拒否ゼロ（approvalPolicy "never" + sandbox "read-only" で date/sleep 素通り）
- 副次的発見: **ChatGPT.app が常駐の codex app-server を持っており**、同じ auth.json を共有した状態でも
  競合は観測されなかった（= この環境では従来から2プロセス共有が常態だった）
- [未検証] 並列度4以上／トークンリフレッシュ契機を跨ぐ長時間 turn／`initialized` 通知の要否

→ §5 の推奨どおり、次段は codex-bridge への「ワーカープール」モード実装（app-server 1プロセスを
起動し、Workflow から N ジョブを thread として並行投入する配管）。

## 7. 未解決の問い

- 0.149.1 実機で N=2〜4 同時起動の再現率（意図的に未実施＝認証破壊リスク。やるならカナリア設計 §4 で）
- #27773 の量ベース無効化の閾値（公開情報なし）
- Python SDK の並行 conversation の実挙動（公式サンプル・型定義の実確認は PoC で）
