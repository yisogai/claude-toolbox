# harness-fablize — Opus を Fable ライクに運用するハーネス

Claude Opus をメインループに使うときの3つの体感ギャップを、hooks / CLAUDE.md 規範 /
エージェント・ワークフローの3層で埋めるハーネス。`fablize`（社内検証プロジェクト）の
ハーネス部分だけを切り出した配布物。

## 埋めようとしている3つのギャップ

Opus を素のまま使うと、Fable と比べて次の3点で体感差が出やすい。

1. **①曖昧・少ない指示からの自律思考** — 曖昧な依頼をそのまま実装に進めてしまい、
   ユーザーの意図とズレる。
2. **②敵対的レビュー含むマルチエージェント展開の自発性** — 指示されなければ
   レビュー役・検証役を自分から立てない。
3. **③うっかりミス・思い込みの少なさ（メタ認知）** — 未検証のまま「動くはずです」と
   完了を宣言してしまう。

## 構成要素

| 要素 | 実体 | 主に埋めるギャップ |
|---|---|---|
| 作業プロトコル | `claude-md/opus-fable-protocol.md`（`~/.claude/CLAUDE.md` に節として挿入） | ①③ |
| 検証台帳 hook | `hooks/verify_ledger_posttooluse.sh`（PostToolUse） | ③ |
| 完了ゲート hook | `hooks/completion_gate_stop.sh`（Stop）。コード編集後にテスト実行の記録が無いまま完了しようとした場合にだけブロックする | ③ |
| nudge hook | `hooks/workflow_nudge_prompt.sh`（UserPromptSubmit）。実装系プロンプトにだけワークフロー利用を1行だけ注入 | ② |
| verifier / implementer agent | `agents/verifier.md`（反証指向レビュー・opus）、`agents/implementer.md`（仕様確定済み実装・sonnet） | ② |
| fable-advisor agent | `agents/fable-advisor.md`（判断の要所だけ Fable 5 に相談する読み取り専用の判断オラクル・model: fable。稼働モデルの自己申告付き） | ①③ |
| workflows | `workflows/implement-verified.js`（仕様化→実装→反証検証→修正ループ）、`workflows/deep-review.js`（多視点並列レビュー→敵対的検証→統合） | ② |
| カナリアテスト | `tests/canary.sh`（hooks の単体テスト。実モデル呼び出しなし、実 `~/.claude` にも触れない） | — |
| 配備・撤去 | `install.sh`（`--dry-run` 既定 / `--apply` / `--switch-model`）、`UNINSTALL.md` | — |

**含まれないもの**: `model-policy`（サブエージェントのモデル振り分け規範）はユーザー
固有の運用ルールのため同梱しない。必要なら claude-toolbox の `model-policy/` スキルを
別途参照すること。

## 導入方法

```bash
# 差分プレビュー（既定・実変更なし）
harness-fablize/install.sh --dry-run

# 適用（適用前に ~/.claude 内の対象ファイルを自動バックアップ）
harness-fablize/install.sh --apply
```

`install.sh` が行うのは次の4点のみ。

1. `~/.claude/agents/{verifier,implementer,fable-advisor}.md` をコピー。
2. `~/.claude/workflows/{implement-verified,deep-review}.js` をコピー。
3. `~/.claude/settings.json` の `hooks` に4エントリ（PostToolUse×2 / Stop / UserPromptSubmit）を
   差分マージ（冪等。command はこのリポジトリ内の `hooks/*.sh` への絶対パス参照）。
4. `~/.claude/CLAUDE.md` に「## 作業プロトコル」節を追加（既存があれば置換）。

`headless`（`claude -p`）で hooks を注入したい場合は `settings.template.json` を使う。
`<REPO>` を実際の配置パスに置換してから `--settings` に渡すこと。

撤去はキルスイッチ（`~/.claude/completion-gate/state.json` の `mode` を `"off"` に）で
即時無効化するか、`UNINSTALL.md` の手動手順に従う。

## vision: 図・HTML の検品ツール

Opus が SVG/HTML の図を生成した際、レンダリング結果を見ずに完了宣言してしまう問題への
対策。`vision/render.sh` は1コマンドでスクリーンショット（PNG）を撮り、`vision/check.sh`
は視覚的な合否判断を幾何アサーションによる数値の合否に変換する。install.sh の配備対象
ではなく単体で使うツールで、詳細な使い方・アサーション種別は `vision/README.md` を参照。

動作要件: **macOS + Google Chrome**（既定パスから見つからない場合は `CHROME_BIN`
環境変数で実行ファイルのパスを上書き可）。カナリアテスト（実モデル呼び出しなし）は
`bash vision/tests/canary.sh`。

## 効果（要約）

- 盲検 LLM 判定（fable vs 素の opus のペア比較）: **13-9-2** で fable 優位 →
  同条件で **fable vs ハーネス付き opus** のペア比較では **12-11-1** とほぼ互角まで縮小。
- private ベンチでは、曖昧指示タスク・罠バグ修正タスクの決定論指標が上位モデルと
  同値になるケースが確認された。

（タスク内容・評価設計・具体的な指標の作り方はこのリポジトリには含まれない。上記は
要約数値のみ。）

## 注意

- **Claude Code v2.1.206 で検証**。hooks 仕様はバージョンアップで変わりうるため、
  導入後は `tests/canary.sh` を実行して回帰がないか確認すること。
- hooks は **`~/.claude/settings.json` への配線が無いと発火しない**。`install.sh` で
  自動配線されるが、手動で導入する場合は配線を忘れないこと。
- hooks はフェイルオープン設計（`jq` 不在等では常に素通り）。強制力の恒久保証はない。
- **fable-advisor は Fable 5 へのアクセスが前提**（プランに含まれない場合は従量課金になりうる。
  不要なら `~/.claude/agents/fable-advisor.md` を削除してよい — install.sh の他の配備物とは独立）。
  claude-toolbox の `model-policy/` スキルを併用している場合、fable サブエージェントは既定で
  deny されるため、`fable_exempt_subagent_types` への登録と TTL 設定が必要（model-policy README
  §7-4 参照）。登録が無ければ advisor は deny される（安全側）。
