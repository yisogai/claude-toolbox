#!/usr/bin/env bash
# license_set.sh — Claude Code 用ライセンス secret（setup-token / API キー）を macOS Keychain へ登録する。
#
# 使い方:
#   license_set.sh <name>            # 対話プロンプトで secret を入力して登録（推奨）
#   echo "$TOKEN" | license_set.sh <name>   # パイプ入力で登録（自動化用。argv 経由のため ps に一瞬露出する）
#   license_set.sh <name> --delete   # 登録済みエントリを削除
#
# 設計:
#   - service 名は claude-license-<name>、account は $USER。login Keychain に保存する。
#   - -U 付き add で既存エントリは上書き更新。
#   - secret は Keychain にのみ置き、ファイル・シェル履歴には残さない（対話モードでは argv にも渡らない）。
#   - macOS 専用（security コマンド前提）。

set -euo pipefail
USER="${USER:-$(id -un)}"

usage() {
  echo "使い方: license_set.sh <name> [--delete]" >&2
  echo "  <name> は英数字・ハイフン・アンダースコア（先頭は英数字）。例: work, partner-x" >&2
  exit 1
}

NAME="${1:-}"
[ -n "$NAME" ] || usage
case "$NAME" in
  -*) echo "エラー: name は - で始められません（オプションの書き忘れ？）: $NAME" >&2; usage ;;
  *[!a-zA-Z0-9_-]*) echo "エラー: name に使える文字は英数字・ハイフン・アンダースコアのみです: $NAME" >&2; exit 1 ;;
esac
SERVICE="claude-license-${NAME}"

if [ "${2:-}" = "--delete" ]; then
  if security delete-generic-password -a "$USER" -s "$SERVICE" >/dev/null 2>&1; then
    echo "削除しました: Keychain エントリ ${SERVICE}"
  else
    echo "エントリが見つかりません（何もしていません）: ${SERVICE}" >&2
  fi
  exit 0
fi
[ -z "${2:-}" ] || usage

if [ -t 0 ]; then
  # 対話モード: security 自身のプロンプトに入力させる（argv・履歴に一切残らない）
  echo "secret（setup-token のトークン or API キー）を入力してください。入力は表示されません。"
  security add-generic-password -U -a "$USER" -s "$SERVICE" -w
else
  # パイプモード: stdin から1行読む（-w の引数として渡るため、登録の一瞬だけ ps に露出しうる）
  # || true: 末尾改行なしの入力（printf '%s' / pbpaste 等）では read が EOF で非ゼロを返すが、
  # 変数には値が入っている。set -e で落とさず、空チェックに委ねる。
  IFS= read -r SECRET || true
  [ -n "$SECRET" ] || { echo "エラー: stdin から secret を読めませんでした" >&2; exit 1; }
  security add-generic-password -U -a "$USER" -s "$SERVICE" -w "$SECRET"
fi

echo "登録しました: Keychain エントリ ${SERVICE}（account=${USER}）"
echo "次: 案件ディレクトリで license_envrc.sh ${NAME} oauth|apikey を実行して .envrc を生成してください。"
