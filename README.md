# claude-toolbox — 共有用 Claude Code ツール集

個人の `~/.claude` から**汎用スキル**を切り出した共有リポジトリ。`~/.claude` 本体は
private（`plans/` や社内向けスキルを含むため公開しない方針）で、そのうち誰でも使える
ものだけをこのリポジトリに置いて同僚と共有する。スキルを中心に、Claude Code の
ワークフローを支える周辺ツール（VSCode 拡張など、`~/.claude` に置かないもの）も含む。

各スキルは自己完結したディレクトリで、`~/.claude/skills/<skill-name>/` に置き、各スキルの
ドキュメント（README.md / SKILL.md）に従って `settings.json` を配線すれば動く。

---

## スキル一覧

| スキル | 概要 | 同梱物 |
|---|---|---|
| [`model-policy`](model-policy/) | サブエージェント（Agent ツール / Workflow の `agent()`）への **fable 割り当てをハーネスレベルで禁止**し、作業を opus（既定）へ振り分けてコストを制御する。コストの主制御は effort（探索=medium／検証=xhigh。下限 medium）で行い、sonnet は大量 fan-out 等の量的な段に限定して使う。fable/fork 禁止は PreToolUse hook で強制し、モデルの振り分けは CLAUDE.md 規範で担保する。**調整ノブ（`tune`）**で effort マトリクス・並列度・Codex 既定をファイル化し、cost-manager の週次ペースに連動してレビュー/検証の effort を自動切替する。特定サブエージェントの例外許可機構 `fable_exempt_subagent_types` はコードとして残るが現在未使用（旧 fable-advisor 運用は 2026-07-31 廃止）。 | PreToolUse / UserPromptSubmit hook 3本＋運用 CLI＋導入ドキュメント |
| [`handoff`](handoff/) | `/compact` の**直前に引き継ぎファイルを生成**し、compact 後に無劣化で復元する。要約器が読めないファイルへ全文を保存し、`/compact` 引数にはパス参照だけを渡す方式。SessionStart(compact) hook による自動注入と statusline / 閾値通知を同梱。 | hook（compact 自動注入・閾値通知）・statusline・保存 CLI |
| [`cost-manager`](cost-manager/) | Claude Code の transcript からタスク単位のコスト（USD + 参考JPY、モデル別内訳）を集計する。開始マーカーで計測範囲を区切り、完了時に Markdown + PNG カードのレポートを同一形式で出力する。予算マーカー・ETA・requestId dedup に対応。フェーズ2として**週次枠ペーシング（pace）**を同梱: statusline に週次枠・5時間枠・Fable 50% サブ枠・Codex クレジットの消化ペースを常時表示し、バックグラウンド集計でサンプルを蓄積する。 | 計測・レポート生成スクリプト（cost_start / cost_status / cost_report ほか）＋Markdown / PNG テンプレート＋設定（単価・為替） |
| [`codex-bridge`](codex-bridge/) | Claude Code から **OpenAI Codex CLI（`codex exec`）を非対話・構造化・タイムアウト付きで呼ぶ配管**。実装委譲（`--write`）とレビュー（read-only、JSON schema）を `job.json`・使用量台帳に落とし、Fable 向けの圧縮サマリを返す。codex-plugin-cc / MCP は使わない（調査結論は `docs/research/`）。Codex 未導入でもモックで配管全体をテスト可能。実機検証は README のチェックリスト。 | 実行ドライバ（codex_run / codex_job / render_prompt）＋プロンプト・AGENTS.md テンプレート＋Workflow 雛形＋モック付きテスト（63件） |
| [`usage-report`](usage-report/) | **指定ディレクトリの子・孫配下で実行された全セッション**を期間指定（月/週/任意、JST）で一括集計し、トークン使用量・従量課金換算コスト（USD/JPY）・作業内容サマリを CSV 2枚 + PNG 最大4枚 + summary.md に出力する。帰属判定はセッション起動時の `cwd`（メイン jsonl の最初の `cwd`）でセッション単位（エンコード名は非可逆のためヒントのみ）、dedup は全セッション共有の Accumulator、サブエージェント transcript 込み。単価・dedup 実装は cost-manager の `cost_lib.py` を import 再利用。 | 集計 CLI（usage_report / usage_lib / charts）＋設計ドキュメント。チャートは matplotlib（無い環境では CSV+md に自動縮退） |

各スキルの詳細・仕組み・導入手順は、それぞれのディレクトリの **README.md / SKILL.md** を参照。

---

## スキル以外のツール

| ツール | 概要 | 導入 |
|---|---|---|
| [`claude-file-paste`](claude-file-paste/) | VSCode 拡張。Mac のクリップボードにある画像やファイルを、Remote-SSH 接続中のターミナルへ**リモート側のファイルパスとして貼り付ける**（リモートの `/tmp/claude_paste/` へ自動転送）。Claude Code CLI に画像を渡す用途を想定。 | 下記 `install.sh` の対象外。**Mac ローカル側の VS Code に入れる**（リモート側に入れると動かない）。手順は [`claude-file-paste/README.md`](claude-file-paste/README.md) を参照。 |
| [`harness-fablize`](harness-fablize/) | **凍結（2026-08-01）: Opus メイン運用時代のアーカイブ**。Claude Opus をメインループで Fable ライクに運用するためのハーネス（①曖昧指示からの自律思考 ②マルチエージェント展開の自発性 ③未検証完了の防止、を hooks / CLAUDE.md 規範 / agents・workflows の3層で補う）。作者環境は Fable 5 メインへ移行し運用を停止したが、Opus メインの環境向け参考実装として残置。凍結の経緯と注意は README 冒頭を参照。 | 下記 `install.sh` の対象外。同梱の専用インストーラ `harness-fablize/install.sh`（`--dry-run` 既定 / `--apply`）で導入する。詳細は [`harness-fablize/README.md`](harness-fablize/README.md)、撤去は同梱の `UNINSTALL.md` を参照。 |
| [`license-switch`](license-switch/) | **案件ディレクトリごとに Claude Code のライセンスを自動切替**する（メインの `/login` は Max のまま、提携先の Team/Enterprise シートや仕事用サブスクの setup-token・提携先 API キーを direnv + macOS Keychain で配下だけに適用）。認証優先順位（env > `/login` 保存分）を利用し、利用枠・課金はアクティブな資格情報側に帰属。secret は Keychain のみに置き、`.envrc` には取り出しコマンドだけを生成する。アクティブなアカウント/ライセンスを statusline 末尾に常時表示する合成 wrapper（handoff statusline 無改変）も同梱。 | 下記 `install.sh` の対象外。リポジトリのスクリプトを直接叩く（macOS + direnv 前提）。手順は [`license-switch/README.md`](license-switch/README.md) を参照。 |

---

## インストール

```bash
git clone <このリポジトリの URL> claude-toolbox
cd claude-toolbox

# 例: model-policy を ~/.claude/skills/ に導入
./install.sh model-policy

# 既存の同名スキルを上書きする場合（退避してから上書き）
./install.sh model-policy --force
```

`install.sh` が行うのは **ファイルのコピー・実行権限付与・ドキュメント内プレースホルダ
（`/Users/<YOU>`）の自動置換**まで。`~/.claude/settings.json` への hooks / permissions /
statusLine の配線は各スキルのドキュメントに従って**手動**で行う（このスクリプトは
settings.json を変更しない）。

`model-policy` は導入後に、README の **Stage 0 検証**（`tool_input` のキー名確認）と
**カナリアテスト**（fable 指定サブエージェントが deny されるか）を必ず実施すること。

---

## 前提

- **macOS**（BSD `date` / `sed`）を主対象。`install.sh` と各スクリプトは Linux（GNU `date` /
  `sed`）へのフォールバックを備える。
- **jq**（必須）。model-policy の hook / CLI と handoff の一部処理が依存する。
- **Claude Code**。`model-policy` は **v2.1.202** で動作確認済み（hook 仕様に依存するため、
  バージョンアップで挙動が変わりうる。§免責を参照）。

---

## メンテナンス方針

各スキルの**原本は作者の `~/.claude/skills/`** にある。更新はまず原本で行い、そこから
このリポジトリへ**反映には `./sync.sh <skill-name>` を使う**。**このリポジトリを直接編集
しない**（原本と乖離させないため）。

`sync.sh` が行うこと（`install.sh` の逆方向・メンテナ専用）:

```bash
./sync.sh model-policy   # 原本 ~/.claude/skills/model-policy を取り込む
```

- リポジトリ側 `<skill-name>/` を削除し、原本からコピー。
- ドキュメント（`*.md`）内の**実ホームパス（実行者の `$HOME`）を自動でプレースホルダ
  `/Users/<YOU>` に置換**（＝コピー時のスクラブ。`install.sh` は導入時にこれを実 `$HOME`
  へ戻す）。
- 紛れ込んだ `*.log` / `.DS_Store` を削除。
- **漏えい検査**: 置換後に実ホームパス・ユーザー名が残っていないかを `grep` で確認し、
  残っていればエラーで停止（コミットさせない）。
- 最後に `git status` を表示するのみで、**コミットは自動では行わない**（メンテナが `diff`
  を確認してから手動でコミットする）。

`cost-manager` / `harness-fablize` はスキル本体にエンジン（スクリプト・処理系）を含み、
`claude-file-paste` は `~/.claude` に置かない VSCode 拡張のため、いずれも原本＝
このリポジトリ側で直接編集する。`sync.sh` の対象外。

---

## 免責

- ここに含まれる hook / statusline は Claude Code の**バージョンアップで黙って動作しなく
  なる可能性**がある（エラーで止まるのではなく、フェイルオープンで「強制が効かなくなる」
  方向に倒れる設計）。壊れていないかの検知方法は各スキルのドキュメントを参照すること。
  - `model-policy`: hook 発火のたびに記録する**ハートビート**（`/model-policy status` で確認）と、
    アップデート後に実行する**カナリアテスト**で検知する。
  - `handoff`: compact 後に引き継ぎが自動注入されない場合は、要約に残ったパスから手動で
    `Read` して復元できる（多層防御）。
- 引き継ぎファイル（`~/.claude/handoffs/`）は会話の要約であり、機微情報を含みうる。取り扱いは
  `handoff/SKILL.md` の「プライバシーと後始末」を参照。
