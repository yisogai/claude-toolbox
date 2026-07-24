#!/usr/bin/env bash
# license_envrc.sh — 案件ディレクトリに direnv 用 .envrc を生成し、配下の claude だけ別ライセンスに切り替える。
#
# 使い方:
#   license_envrc.sh <name> oauth  [dir]   # サブスク系（claude setup-token のトークン）→ CLAUDE_CODE_OAUTH_TOKEN
#   license_envrc.sh <name> apikey [dir]   # API キー系（提携先の Console キー等）→ ANTHROPIC_API_KEY
#   dir 省略時はカレントディレクトリ。
#
# 設計:
#   - secret は Keychain（claude-license-<name>、license_set.sh で登録）から .envrc 評価時に毎回取り出す。
#     .envrc には取り出しコマンドだけを書き、平文 secret をファイルに置かない。
#   - Keychain エントリ未登録なら生成を中止する（空 export で無言のままメインのログインに
#     フォールバックする事故を防ぐ。.envrc 側にも同じガードを入れ、取り出し失敗時は stderr に警告）。
#   - 既存 .envrc は本スクリプトの生成マーカーが無い限り上書きしない。
#   - macOS 専用（security コマンド前提）。direnv hook 設定済みであること。

set -euo pipefail
USER="${USER:-$(id -un)}"

MARKER="# generated-by: claude-toolbox/license-switch"

usage() {
  echo "使い方: license_envrc.sh <name> oauth|apikey [dir]" >&2
  exit 1
}

NAME="${1:-}"
TYPE="${2:-}"
DIR="${3:-.}"
[ -n "$NAME" ] && [ -n "$TYPE" ] || usage
case "$NAME" in
  -*) echo "エラー: name は - で始められません: $NAME" >&2; exit 1 ;;
  *[!a-zA-Z0-9_-]*) echo "エラー: name に使える文字は英数字・ハイフン・アンダースコアのみです: $NAME" >&2; exit 1 ;;
esac
case "$TYPE" in
  oauth)  VAR="CLAUDE_CODE_OAUTH_TOKEN" ;;
  apikey) VAR="ANTHROPIC_API_KEY" ;;
  *) usage ;;
esac
[ -d "$DIR" ] || { echo "エラー: ディレクトリがありません: $DIR" >&2; exit 1; }
SERVICE="claude-license-${NAME}"
ENVRC="${DIR%/}/.envrc"

if ! security find-generic-password -a "$USER" -s "$SERVICE" >/dev/null 2>&1; then
  echo "エラー: Keychain エントリ ${SERVICE} を確認できません（未登録、または Keychain がロックされています）。" >&2
  echo "未登録の場合は先に license_set.sh ${NAME} で登録してください。" >&2
  exit 1
fi

if [ -L "$ENVRC" ]; then
  echo "エラー: $ENVRC はシンボリックリンクです。リンク先の書き換えを避けるため上書きしません。" >&2
  exit 1
fi
if [ -e "$ENVRC" ] && ! grep -qF "$MARKER" "$ENVRC"; then
  echo "エラー: $ENVRC は既に存在し、このツールの生成物ではありません。上書きしません。" >&2
  echo "既存の .envrc に手で組み込む場合は README の「.envrc の中身」を参照してください。" >&2
  exit 1
fi

cat > "$ENVRC" <<EOF
$MARKER
# license: ${NAME} (${TYPE}) → ${VAR}
# secret は Keychain（${SERVICE}）から毎回取得。平文は置かない。
_claude_license_secret="\$(security find-generic-password -a "\$USER" -s "${SERVICE}" -w 2>/dev/null)"
if [ -n "\$_claude_license_secret" ]; then
  export ${VAR}="\$_claude_license_secret"
  export CLAUDE_LICENSE_NAME="${NAME}"
  echo "claude-license: ${NAME} (${TYPE}) 適用中 — 初回は claude の /status でアカウントを確認" >&2
else
  echo "claude-license: Keychain エントリ ${SERVICE} を取得できません。このシェルはメインの /login アカウントで動きます。" >&2
fi
unset _claude_license_secret
EOF

echo "生成しました: ${ENVRC}（${NAME} / ${TYPE} → ${VAR}）"
echo "次の手順:"
echo "  1. direnv allow \"${DIR%/}\""
echo "  2. そのディレクトリで claude を起動し /status で organization / email を確認"
