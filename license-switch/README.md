# license-switch — 案件ディレクトリごとの Claude Code ライセンス自動切替

Max 20x などのメインアカウントを `/login` のまま維持しつつ、**特定の案件ディレクトリ配下で
起動した claude だけ別のライセンス**（提携先の Team/Enterprise シート・仕事用サブスク・
提携先発行の API キー）で動かすためのツール。direnv + macOS Keychain の組み合わせで、
ディレクトリに入るだけで自動的に切り替わる。

Claude Code には公式のマルチアカウント機能がない（切替は `/logout`→`/login` のみ）が、
**認証の優先順位**（[docs/authentication](https://code.claude.com/docs/en/authentication.md)）
で環境変数が保存済みログインに勝つことを利用する:

```
クラウド資格情報 > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY > apiKeyHelper
  > CLAUDE_CODE_OAUTH_TOKEN > /login 保存分
```

利用枠・課金は**その時点でアクティブな資格情報**に帰属する。つまり `.envrc` のある案件
ディレクトリでは提携先/仕事側の枠を消費し、それ以外ではメインの Max をそのまま使う。

## 前提

- macOS（`security` コマンドで login Keychain に secret を保存する）
- direnv 導入済み＋シェルに hook 設定済み（`eval "$(direnv hook zsh)"` 等）
- Claude Code v2.x

## なぜ CLAUDE_CONFIG_DIR 方式にしないか

macOS では認証トークンの実体が Keychain の**単一エントリ**（`Claude Code-credentials`）に
保存され、`CLAUDE_CONFIG_DIR` を分けてもログイン状態のポインタが分かれるだけでトークン
保存先は共有される（docs も credentials のディレクトリ分離を「Linux or Windows」限定と
記載）。複数サブスクを別プロファイルで `/login` すると上書き衝突のリスクがあるため、
Keychain に触れない環境変数方式を採る。副次的に、`~/.claude` の hooks / skills /
settings も全案件で共有されたままになる。

## 使い方

### 1. ライセンスの形態を決める（提携先と合意する）

| 形態 | 正規性 | このツールでの type |
|---|---|---|
| 提携先の Team/Enterprise に**シート招待**される | 公式に文書化された正規経路。複数組織への同時所属・個人 Pro/Max との併存も明文で可能 | `oauth` |
| 自分の**仕事用サブスク**を別途契約する | 正規（アカウントは自分名義） | `oauth` |
| 提携先が **Console の API キー**を発行する | 正規（従量課金は提携先の Console 組織持ち） | `apikey` |
| 他人のアカウント（ID/パスワード）を借りる | **Consumer Terms §2 で明文禁止**。使わない | — |

### 2. secret を取得して Keychain に登録する

サブスク系（シート招待・仕事用サブスク）の場合:

```bash
# 対象アカウントでブラウザログインした状態で
claude setup-token
# → OAuth 画面で該当プラン/組織を選んで Authorize → 表示されたトークンをコピー
#   （Pro/Max/Team/Enterprise で利用可・有効期限1年・現在の /login 状態には影響しない）

bash <このリポジトリ>/license-switch/scripts/license_set.sh work
# → プロンプトにトークンを貼り付け（表示されず、履歴にも残らない）
```

API キー系の場合も同様に `license_set.sh partner-x` で登録する。

### 3. 案件ディレクトリに .envrc を生成する

```bash
bash <このリポジトリ>/license-switch/scripts/license_envrc.sh work oauth ~/projects/work-proj
direnv allow ~/projects/work-proj
```

以後、そのディレクトリ配下で起動した claude はそのライセンスで動く。確認は起動後に
`/status`（organization / email が切り替わっていること）。ディレクトリを出れば direnv が
env を外し、メインの `/login` アカウントに戻る。

### .envrc の中身

生成される `.envrc` は secret を含まず、評価のたびに Keychain から取り出す:

```bash
# generated-by: claude-toolbox/license-switch
# license: work (oauth) → CLAUDE_CODE_OAUTH_TOKEN
# secret は Keychain（claude-license-work）から毎回取得。平文は置かない。
_claude_license_secret="$(security find-generic-password -a "$USER" -s "claude-license-work" -w 2>/dev/null)"
if [ -n "$_claude_license_secret" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$_claude_license_secret"
  echo "claude-license: work (oauth) 適用中 — 初回は claude の /status でアカウントを確認" >&2
else
  echo "claude-license: Keychain エントリ claude-license-work を取得できません。このシェルはメインの /login アカウントで動きます。" >&2
fi
unset _claude_license_secret
```

取り出しに失敗したときは**警告を出して export しない**（無言でメインの枠を消費する事故を
防ぐ）。既存の `.envrc` がある案件ではこのスニペットを手で追記してもよい。secret を
含まないため `.envrc` 自体はコミット可能だが、案件リポジトリの方針に従うこと。

## statusline にアカウント/ライセンスを常時表示する

`scripts/license_statusline.sh` は、既存の handoff statusline（`● Context 42% | モデル |
ディレクトリ名`）の末尾にアクティブなアカウント表示を追加する合成 wrapper
（handoff 側は無改変。cost-manager design.md の wrapper 合成方針と同じパターン）:

```
● Context 42% | Opus | myproj | ⚿ yuta.isogai     ← メイン（/login）のとき（薄色）
● Context 42% | Opus | work-proj | 🔑 work         ← license-switch 適用時（黄色強調）
● Context 42% | Opus | ci-proj | 🔑 env            ← license-switch 外の env 認証
```

導入は `~/.claude/settings.json` の `statusLine.command` を差し替えるだけ:

```json
"statusLine": {
  "type": "command",
  "command": "bash <このリポジトリ>/license-switch/scripts/license_statusline.sh"
}
```

仕組み: statusline スクリプトは claude プロセスの環境変数を継承するため、`.envrc` が
export する `CLAUDE_LICENSE_NAME`（secret ではない識別子）をそのまま表示する。メイン時の
メールは `~/.claude.json` の `oauthAccount.emailAddress` から取得し、mtime キーで
`$TMPDIR` にキャッシュする。

注意: この表示は「env 上でどのライセンスを**意図**しているか」であり、実際に課金された
アカウントのサーバー側検証ではない（oauth トークン失効時の無言フォールバックは検出でき
ない。Keychain からの取得失敗は `.envrc` 側で `CLAUDE_LICENSE_NAME` ごと未設定になるため
表示に反映される）。確実な確認は `/status`。

## 制約・注意

- `CLAUDE_CODE_OAUTH_TOKEN`（setup-token）はモデルリクエスト専用。**Remote Control と
  claude.ai コネクタは使えない**（ローカル設定の MCP サーバーは動く）。`--bare` モードでは
  読まれない。
- **無効・失効した oauth トークンはエラーにならず、無言でメインの `/login` アカウントに
  フォールバックする**（v2.1.218 実測: ダミートークンを設定しても正常応答してしまう。
  API キーの誤りが 401 で即顕在化するのと対照的）。このため oauth 型は、初回セットアップ時と
  トークン更新時に必ず claude 起動 → `/status` で organization / email を目視確認すること。
  `.envrc` 生成物が direnv ロード時に「適用中」の1行を出すのはこのリマインドのため。
- トークンの有効期限は**1年**。失効したら対象アカウントで `claude setup-token` を再実行し、
  `license_set.sh <name>` で上書き登録する（`.envrc` は変更不要）。**失効しても上記の通り
  エラーにならないので、更新日はカレンダー等で管理する**。
- **claude をラップ・計装する環境（cmux 等）では env による認証切替が効かないことがある**。
  ネストした Claude Code セッション内での実測では、cmux シム経由の `claude` はダミー
  `ANTHROPIC_API_KEY` を無視してメインのログインで応答した（実体バイナリ
  `~/.local/bin/claude` を直接叩くと docs 通り env が優先され 401 になる）。通常ターミナル
  での挙動は導入時に下の「動作検証」を一度実行して確認すること。
- Gmail 等の個人ドメインは組織 discovery の許可ドメインに追加できない。個人メールで
  シート参加する場合は**管理者からの直接招待（メール招待 / 招待リンク）**を使ってもらう。
- エントリ削除は `license_set.sh <name> --delete`。

## 動作検証（アカウントを増やす前にできる）

**apikey 型のダミー**で「env が `/login` より優先される」ことを確認できる（401 になれば成功。
oauth 型のダミーは上記の無言フォールバックにより陰性対照として使えない）:

```bash
echo "sk-ant-api03-DUMMY" | bash scripts/license_set.sh test-dummy
mkdir -p /tmp/lic-test && bash scripts/license_envrc.sh test-dummy apikey /tmp/lic-test
direnv allow /tmp/lic-test
cd /tmp/lic-test && claude -p "ok"
# → 「401 API key is invalid」ならこの環境で切替が機能している
# → 正常応答してしまう場合はラッパーが env を無効化している（上記「制約・注意」参照）
bash scripts/license_set.sh test-dummy --delete && rm -rf /tmp/lic-test
```

実ライセンス（oauth 型）の最終確認は、実トークン登録後にその案件ディレクトリで claude を
起動し `/status` の organization / email が切り替わっていることを見る。
