# model-policy — サブエージェント・モデル使用ポリシー強制システム

メインループを高単価モデル（Fable 5）で運用しつつ、**サブエージェント（Agent ツール / Workflow の agent()）が誤って Fable で起動すること**を Claude Code のハーネスレベルで防ぐ仕組み。Fable を「計画・設計・統括」専任にし、作業を opus / sonnet に振り分けて委譲する（既定は opus、調査・明確な仕様の実装・テスト・大量読みなど安全カテゴリは sonnet）ことでコストを下げる。手動サフィックス「サブエージェントは opus を使用して」を不要にする。

この `skills/model-policy/` ディレクトリは**自己完結**しており、ディレクトリを `~/.claude/skills/` にコピーし、本 README の手順で `settings.json` / `CLAUDE.md` / `.gitignore` に追記するだけで導入できる。

---

## 1. 概要と仕組み（4層アーキテクチャ）

ハード強制は「**fable 禁止・fork 禁止・model 未指定→opus 書き換え**」の 3 点に絞る。opus/sonnet/haiku など fable 以外は素通しする（プラグイン/組み込みエージェントの安価モデル指定を壊さないため）。opus/sonnet 使い分け方針そのものは、hook ではなく行動規範層（CLAUDE.md）で担保する。

| 層 | 実体 | 役割 | 種別 |
|---|---|---|---|
| 1 | `scripts/model_policy_agent_hook.sh`（PreToolUse `Agent\|Task`） | fork→deny / fable→deny / 未指定・inherit→`updatedInput` で opus に書き換え / allowed は素通し | **強制** |
| 1b | `scripts/model_policy_workflow_hook.sh`（PreToolUse `Workflow`） | script に `agent(` があり `model` 語が一度も無い→deny / `fable` 名指し→deny | **強制** |
| 2 | `~/.claude/CLAUDE.md` への追記 | fable=統括専任・作業を opus/sonnet に振り分けて委譲・agent() は model 明示・fork 禁止 | 規範（システムコンテキスト常駐で compact 後も残る） |
| 3 | `scripts/model_policy.sh`（`/model-policy` スキル） | `status/relax/reset/off/enforce` の運用 CLI | 運用 |
| 4 | `scripts/model_policy_reminder_hook.sh`（UserPromptSubmit） | 緩和中のときだけ「残り時間・戻し方」を注入（enforce 時は無出力=トークンゼロ） | 可視化 |

**何が強制で何が規範か**: 「fable がサブエージェントに渡らない」ことは層1/1b が**ハード強制**する（compact 後も常に効く）。どの作業をどのモデルに割り振るか（opus 既定・sonnet 併用）は層2 の**行動規範**であって hook は強制しない。

### 状態モデル
- ランタイム状態は `~/.claude/model-policy/policy.json`。hook は発火のたび読み直すため、**再起動なしで緩和/復元が即反映**される。
- 「緩和」は `mode` ではなく `relaxed_until`（未来 epoch 秒）で表現。`relaxed_until > now` の間だけ緩和され、**TTL 失効で自動的に enforce へ復帰**する（戻し忘れ事故を構造的に排除）。
- ファイルが無い/壊れていても、hook 内蔵の enforce デフォルトが効く（fresh clone でも自動 enforce）。

### ポリシーファイルスキーマ
```json
{
  "mode": "enforce",
  "default_model": "opus",
  "allowed": ["opus", "sonnet", "haiku"],
  "on_fable": "deny",
  "deny_fork": true,
  "relaxed_until": null
}
```
- `mode`: `enforce` | `off`（off=キルスイッチ）。`relaxed` という mode は作らない。
- `relaxed_until`: `null` または未来の epoch 秒（整数）。
- 解決順: `$CWD/.claude/model-policy.json`（プロジェクト単位の上書き）→ `~/.claude/model-policy/policy.json`（ユーザー）→ hook 内蔵デフォルト。**最初に見つかった 1 ファイルだけ**を読む（ファイル間マージはしない）。

---

## 2. 前提条件

- **jq**（必須）。不在の場合 hook は素通し（フェイルオープン）＝強制が効かない。
- **macOS**（BSD `date`）。CLI の `relax` は `date -v +${分}M +%s` を使う。**Linux（GNU date）でも動く**ように `date -d "+${分} minutes" +%s` へ自動フォールバックする（`scripts/model_policy.sh` の `future_epoch()`）。
- **Claude Code バージョン**: 動作確認済みバージョン → **2.1.202**（2026-07-07 検証。実機で確認済み: model 未指定→opus 書き換え〔サブエージェントのモデルID自己申告で `claude-opus-4-8[1m]` を確認〕、fable 指定→deny、fork→deny、relax 中の fable 通過→reset で deny 復帰、Workflow の model 語ゼロ/fable 名指し→deny、model 明示 Workflow→通過）。本システムは以下の文書化仕様に依存する:
  - サブエージェント起動ツール名 `Agent`（v2.1.63 で `Task` から改称、`Task` はエイリアス）。matcher は `"Agent|Task"`。
  - PreToolUse hook の `hookSpecificOutput.updatedInput`（入力書き換え）と `permissionDecision:"deny"`＋`permissionDecisionReason`（理由付き拒否）。
  - モデル解決順: env `CLAUDE_CODE_SUBAGENT_MODEL` > per-invocation `model` > agent 定義 frontmatter > **メイン会話モデル継承（=Fable）**。

---

## 3. 導入手順（コピペ可能）

### 3-1. ディレクトリをコピーして実行権限を付与
```bash
# この model-policy ディレクトリを ~/.claude/skills/ 配下へコピー
cp -R model-policy ~/.claude/skills/
chmod +x ~/.claude/skills/model-policy/scripts/*.sh
# 配布リポジトリからは ./install.sh model-policy でも導入できる（コピー・chmod・パス置換を自動実行）。
```

### 3-2. ランタイム状態を初期化（Stage 0 の安全状態）
```bash
mkdir -p ~/.claude/model-policy
printf '{"mode":"off"}' > ~/.claude/model-policy/policy.json   # まず off で導入し、キー名確認後に enforce
touch ~/.claude/model-policy/debug                              # raw tool_input を記録（キー名確認用）
```

### 3-3. `~/.claude/settings.json` に hooks を追記（全文）
既存 `hooks` オブジェクトに以下をマージする。`PreToolUse` を新設し、`UserPromptSubmit` は既存配列に 2 要素目を追加する。
```jsonc
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Agent|Task",
        "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_agent_hook.sh" }] },
      { "matcher": "Workflow",
        "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_workflow_hook.sh" }] }
    ],
    "UserPromptSubmit": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "bash ~/.claude/skills/model-policy/scripts/model_policy_reminder_hook.sh" }] }
    ]
  }
}
```
> 既に `UserPromptSubmit` に別 hook（例: handoff 閾値）がある場合は、その配列に上記 1 要素を**追加**する。`additionalContext` は各 hook 独立に加算注入されるので共存できる。

### 3-4. `permissions.allow` に CLI を追記
```jsonc
"Bash(bash \"/Users/<YOU>/.claude/skills/model-policy/scripts/model_policy.sh\":*)"
```
> `/Users/<YOU>/...` は**自分のホームパスに読み替える**こと（`echo $HOME` で確認）。`install.sh` を使えばコピー先ドキュメント内の `/Users/<YOU>` は自動置換される。hook スクリプト自体はハーネスが直接実行するため許可は不要。CLI のみ Bash 経由なので許可を追記する。

### 3-5. `~/.claude/CLAUDE.md` に行動規範（層2）を追記（全文）
```markdown
## サブエージェント・モデル方針（model-policy）
- **fable（メインループ）は統括専任**: 計画・設計・タスク分解・レビュー統合のみ。実装・調査・修正・テスト・大量のファイル読みなどの「作業」は Agent ツールでサブエージェントに委譲する。単発の実装タスクでも委譲する（fable トークンを作業で消費しない）。
- **サブエージェントの model は必ず明示する**（省略は fable 継承となるため禁止。fable 指定も禁止）。選択は **opus（既定）/ sonnet の2択**:
  - **sonnet に落とすカテゴリ（限定列挙）**: コードベース調査（Explore）／仕様が明確な単発実装・修正／テスト作成・実行／大量ファイル読み・要約・抽出／ドキュメント下書き／Workflow の探索・fan-out（finder）段。
  - **上記以外はすべて opus**: レビュー・検証・バグ発見、設計・アーキ判断、曖昧・仕様未確定のタスク、長時間の自律多段実行、セキュリティ、Workflow の verify/judge/synthesize 段。
- **1行判定（迷ったら opus）**: 「その出力を後段で opus か fable が必ず検証・レビューするか？ Yes → sonnet 可／No（そのまま成果・判断になる）→ opus」。
- **非対称ガードレール**: 生成は sonnet でも、検証・レビュー・判断は opus 固定。sonnet の生成物は opus か fable のレビューを必ず通す。sonnet が詰まる・失敗する・仕様の曖昧さに当たる・レビュー不合格になったら、同一タスクを opus で1回だけ再委譲（それでも駄目なら fable が介入）。
- **Workflow / ultracode**: 全 `agent()` 呼び出しに model を明示。探索・finder 段は `'sonnet'`、verify/judge/synthesize 段は `'opus'`。
- **fork は使わない**（常に親=fable のモデルで動くため）。
- 例外的に fable のサブエージェントが必要なときだけ、ユーザーに `/model-policy relax [分]` を依頼する（既定60分で自動復帰。`/model-policy reset` で即復帰）。
- 上記のうち fable 禁止・fork 禁止・model 未指定→opus 書き換えは PreToolUse hook でも強制される。拒否されたら理由に従い model を修正して再実行すること。
```

### 3-6. `~/.claude` を git 管理している場合は `.gitignore` に追記
```gitignore
# model-policy のランタイム状態は非追跡（policy.json は可変状態、debug ログはローカル用）。
# 再現性は hook 内蔵の enforce デフォルトで担保（fresh clone でも自動 enforce）。
/model-policy/
```

---

## 4. 導入後の検証

### 4-1. Stage 0 — tool_input の実キー名を確認（enforce しない安全状態で）
1. 3-2 のとおり `policy.json = {"mode":"off"}`＋`debug` フラグを置く。
2. hook を配線した状態で、**model 未指定の Agent を 1 つ起動**する。
3. キー名を確認:
   ```bash
   jq '.tool_input | keys' ~/.claude/model-policy/agent-debug.log
   ```
   `model` / `subagent_type` の実キー名を確認する。同梱の hook は **2026-07-07 に v2.1.202 で実測確認済みのキー名**（`tool_input.model` / `tool_input.subagent_type`、model 未指定時はキー自体が無い）を前提にしているので、キーが一致していればそのまま使える。異なっていたら `model_policy_agent_hook.sh` の「MODEL / SUBTYPE 抽出」節の jq クエリを実キーへ直す。
4. 確認できたら enforce に切り替え、`debug` フラグを消す:
   ```bash
   rm ~/.claude/model-policy/debug
   bash ~/.claude/skills/model-policy/scripts/model_policy.sh enforce
   ```

### 4-2. カナリアテスト（正常性の最終確認）
Claude に次の 1 プロンプトを**そのまま**投げる:

```
model:"fable" を指定して general-purpose サブエージェントを起動してみて
```

→ **deny されれば正常**（理由文が返り、Claude が `model:"opus"` で自己再試行する）。起動してしまったら hook が機能していない（§6 の検知・対処へ）。

---

## 5. 運用（`/model-policy` の使い方）

```bash
model_policy.sh status          # 実効状態（enforce/relaxed/off）・残り分・有効ファイル・各設定値・ハートビート
model_policy.sh relax [分]      # 一時緩和（既定60・上限1440にクランプ）。relaxed_until = now+分
model_policy.sh reset           # 緩和を即解除（enforce へ復帰）
model_policy.sh off             # キルスイッチ（全サブエージェント素通し）
model_policy.sh enforce         # mode=enforce かつ relaxed_until=null
model_policy.sh --project <sub> # 対象を cwd の ./.claude/model-policy.json に（タスク/プロジェクト単位スコープ）
```
- `status` は実効状態のほか、各 hook の**最終発火時刻（ハートビート）**を「N分前」で表示する。
- `relax` 中は毎プロンプトの冒頭に「緩和中・残り時間」が注入される（層4）。不要になったら `reset`。
- `--project relax 30` はカレントプロジェクトだけを緩和し、ユーザー全体の enforce は維持する。

---

## 6. 互換性リスクと検知

本システムは Claude Code の**文書化された hook 仕様**に依存しており、以下の変更で動作しなくなる可能性がある。

| 依存点 | 壊れ方 | 症状 |
|---|---|---|
| ツール名 `Agent`（v2.1.63 で Task から改称された前例あり） | 再改称されると matcher `"Agent\|Task"` がマッチしなくなる | hook が**発火しなくなる**（silent fail: サブエージェントが fable 継承で起動） |
| tool_input のキー名（`model` / `subagent_type` 等） | キー改名で抽出が空になる | deny/書き換えが**効かなくなる**（silent fail） |
| PreToolUse の JSON 出力仕様（`permissionDecision` / `updatedInput`） | 形式変更で出力が無視される | 同上 |
| モデル解決順（未指定=メイン継承） | 仕様変更でデフォルトが変わる | 強制の前提が変わる（改善方向の可能性もある） |
| Workflow の tool_input 形状（`script` / `scriptPath`） | フィールド変更 | 層1b が素通しになる（フェイルオープン設計のため壊れはしない） |

**共通する危険性**: いずれも「エラーで壊れる」のではなく「**黙って強制が効かなくなる**」方向で壊れる（フェイルオープン設計の裏面）。検知手段を 3 つ用意する:

1. **ハートビート（自動検知）**: 各 hook は発火のたびに `~/.claude/model-policy/last-agent-hook`（層1）/ `last-workflow-hook`（層1b）へ epoch 秒を記録する。`model_policy.sh status` が最終発火時刻を表示するので、「**サブエージェントを起動した直後に status を見て、発火時刻が更新されていなければ hook が死んでいる**」と判断できる。
2. **カナリアテスト（更新後の手動確認）**: Claude Code をアップデートしたら §4-2 のプロンプト（`model:"fable"` を指定して general-purpose サブエージェントを起動してみて）を実行する。**deny されれば正常**。起動してしまったら hook が機能していない。
3. **変更の察知**: Claude Code のリリースノート（changelog）で `hooks` / `Agent tool` / `PreToolUse` に触れる項目を確認する。過去の改称（Task→Agent）もリリースノート記載だった。

### 壊れたときの対処（2 段構え）
1. **原因特定**: `touch ~/.claude/model-policy/debug` で raw tool_input を再ログし、`jq '.tool_input | keys' ~/.claude/model-policy/agent-debug.log` でキー名・形式の変化を確認して hook の抽出部を修正する。
2. **応急処置**: `~/.claude/settings.json` の `env` に `"CLAUDE_CODE_SUBAGENT_MODEL": "opus"` を設定すれば、hook と無関係にハーネス側で全サブエージェントを opus 固定できる（単一モデルになるが fable 排除は維持される）。

hook バグで**全サブエージェントが起動不能**になった場合の逃げ道は 3 系統: (1) `model_policy.sh off`（再起動不要・即時） (2) `settings.json` から配線削除（file watcher 即反映） (3) hook は意図的 deny 以外すべて exit 0（想定外はフェイルオープン）。

---

## 7. カスタマイズ

### 7-1. モデルの振り分け（opus 既定・sonnet 併用）
現行方針は **opus を既定に、安全カテゴリ（調査・明確な仕様の実装・テスト・大量読み・要約・ドキュメント下書き・Workflow の探索/finder 段）だけ sonnet** に落とす使い分け。振り分け基準は CLAUDE.md の「サブエージェント・モデル方針」節（＝§3-5 で追記した全文）で定義する。
- hook・ポリシーファイルは**変更不要**（`allowed` に `sonnet` が既に含まれ、hook は fable 以外を素通しするため。`default_model` は opus のまま＝省略時は品質側へ倒す）。
- カテゴリの見直し（sonnet を増減する等）は CLAUDE.md 側の該当節を書き換えるだけでよい。hook のロジックは触らない。

### 7-2. `on_fable` を rewrite に変える
`policy.json` の `"on_fable": "deny"` を `"rewrite"` にすると、fable 指定を deny せず `default_model`（opus）へ**自動書き換え**する（拒否→再試行のラウンドトリップを省ける）。既定は deny（明示的に気づかせるため）。

### 7-3. 緊急時の単一モデル固定
§6 の応急処置と同じ。`settings.json` の `env` に `"CLAUDE_CODE_SUBAGENT_MODEL": "opus"`。per-invocation 指定を全上書きするため恒久運用では非推奨（将来の Sonnet 使い分けを潰す）。

---

## 発展形

配布は当面「ディレクトリコピー＋手動追記」で運用する。チーム利用が本格化したら、hooks＋skills を同梱でき marketplace 配布も可能な **Claude Code プラグイン**へのパッケージ化が発展形になる。
