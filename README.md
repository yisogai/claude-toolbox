# claude-skills — 共有用 Claude Code スキル集

個人の `~/.claude` から**汎用スキルだけ**を切り出した共有リポジトリ。`~/.claude` 本体は
private（`plans/` や社内向けスキルを含むため公開しない方針）で、そのうち誰でも使える
汎用スキルだけをこのリポジトリに置いて同僚と共有する。

各スキルは自己完結したディレクトリで、`~/.claude/skills/<skill-name>/` に置き、各スキルの
ドキュメント（README.md / SKILL.md）に従って `settings.json` を配線すれば動く。

---

## スキル一覧

| スキル | 概要 | 同梱物 |
|---|---|---|
| [`model-policy`](model-policy/) | サブエージェント（Agent ツール / Workflow の `agent()`）への **fable 割り当てをハーネスレベルで禁止**し、作業を安価な opus サブエージェントへ委譲させてコストを制御する。fable=統括専任・opus=作業という運用を PreToolUse hook で強制する。 | PreToolUse / UserPromptSubmit hook 3本＋運用 CLI＋導入ドキュメント |
| [`handoff`](handoff/) | `/compact` の**直前に引き継ぎファイルを生成**し、compact 後に無劣化で復元する。要約器が読めないファイルへ全文を保存し、`/compact` 引数にはパス参照だけを渡す方式。SessionStart(compact) hook による自動注入と statusline / 閾値通知を同梱。 | hook（compact 自動注入・閾値通知）・statusline・保存 CLI |

各スキルの詳細・仕組み・導入手順は、それぞれのディレクトリの **README.md / SKILL.md** を参照。

---

## インストール

```bash
git clone <このリポジトリの URL> claude-skills
cd claude-skills

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
このリポジトリへコピーして反映する。**このリポジトリを直接編集しない**（原本と乖離させない
ため）。コピー時は個人・社内情報が混入しないよう、絶対パスは `/Users/<YOU>` プレースホルダに
戻すなどのスクラブを行う。

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
