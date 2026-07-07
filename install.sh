#!/usr/bin/env bash
# install.sh — このリポジトリの汎用スキルを ~/.claude/skills/ へ導入する。
#
# 使い方:
#   ./install.sh <skill-name> [--force]
#
#   <skill-name>   このリポジトリ直下のスキルディレクトリ名（例: model-policy / handoff）。
#   --force        導入先に同名スキルが既にある場合、退避（.bak-<日時>）してから上書きする。
#
# 挙動:
#   1. <repo>/<skill-name>/ が無ければ、利用可能スキル一覧を出して exit 1。
#   2. ~/.claude/skills/<skill-name> が既にあり --force 無し → 案内して exit 1。
#      --force あり → <skill-name>.bak-<日時> へ退避してからコピー。
#   3. コピー後: scripts/*.sh に chmod +x、コピー先ドキュメント（*.md）内の
#      プレースホルダ /Users/<YOU> を実際の $HOME に sed 置換。
#   4. 次の手動ステップ（settings.json 配線ほか）を表示。
#
# 移植性: macOS の BSD sed（sed -i ''）を前提にしつつ GNU sed（sed -i）へ自動フォールバック。
set -u

# --- このスクリプトが置かれたディレクトリ = リポジトリルート ---------------------
REPO_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
if [ -z "$REPO_DIR" ]; then
  echo "エラー: リポジトリのパスを特定できませんでした。" >&2
  exit 1
fi

SKILLS_DEST="$HOME/.claude/skills"

# --- 利用可能スキル（直下で SKILL.md を持つディレクトリ）を列挙 ------------------
list_available() {
  local d found=0
  echo "利用可能なスキル:"
  for d in "$REPO_DIR"/*/; do
    [ -d "$d" ] || continue
    if [ -f "${d}SKILL.md" ]; then
      echo "  - $(basename "$d")"
      found=1
    fi
  done
  [ "$found" -eq 0 ] && echo "  （見つかりませんでした）"
}

usage() {
  echo "使い方: ./install.sh <skill-name> [--force]" >&2
  echo >&2
  list_available >&2
}

# --- 引数解析（--force はどこに来てもよい。最初の非フラグを skill 名に）----------
SKILL=""
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -*)
      echo "エラー: 不明なオプション: $arg" >&2
      usage
      exit 1
      ;;
    *)
      if [ -z "$SKILL" ]; then
        SKILL="$arg"
      else
        echo "エラー: 引数が多すぎます（skill 名は1つだけ）: $arg" >&2
        usage
        exit 1
      fi
      ;;
  esac
done

if [ -z "$SKILL" ]; then
  echo "エラー: スキル名を指定してください。" >&2
  usage
  exit 1
fi

# skill 名にパス区切り等が混じるのを弾く（../ などによる誤コピー防止）
case "$SKILL" in
  */*|.|..|*"\\"*)
    echo "エラー: 不正なスキル名です: $SKILL" >&2
    exit 1
    ;;
esac

SRC="$REPO_DIR/$SKILL"
if [ ! -d "$SRC" ] || [ ! -f "$SRC/SKILL.md" ]; then
  echo "エラー: スキル '$SKILL' はこのリポジトリに存在しません。" >&2
  echo >&2
  list_available >&2
  exit 1
fi

DEST="$SKILLS_DEST/$SKILL"

# --- 既存チェック ---------------------------------------------------------------
if [ -e "$DEST" ]; then
  if [ "$FORCE" -ne 1 ]; then
    echo "既存あり: $DEST" >&2
    echo "--force を付けると上書きします（上書き前に ${SKILL}.bak-<日時> へ退避します）。" >&2
    echo "例: ./install.sh $SKILL --force" >&2
    exit 1
  fi
  BAK="$DEST.bak-$(date +%Y%m%d-%H%M%S)"
  if ! mv "$DEST" "$BAK"; then
    echo "エラー: 既存スキルの退避に失敗しました: $DEST -> $BAK" >&2
    exit 1
  fi
  echo "既存スキルを退避しました: $BAK"
fi

# --- コピー ---------------------------------------------------------------------
if ! mkdir -p "$SKILLS_DEST"; then
  echo "エラー: 導入先ディレクトリを作成できませんでした: $SKILLS_DEST" >&2
  exit 1
fi
if ! cp -R "$SRC" "$DEST"; then
  echo "エラー: コピーに失敗しました: $SRC -> $DEST" >&2
  exit 1
fi
echo "コピーしました: $SRC -> $DEST"

# --- scripts/*.sh に実行権限を付与 ----------------------------------------------
if [ -d "$DEST/scripts" ]; then
  find "$DEST/scripts" -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null
  echo "実行権限を付与しました: $DEST/scripts/*.sh"
fi

# --- sed のインプレース編集を BSD/GNU 両対応で用意 ------------------------------
if sed --version >/dev/null 2>&1; then
  sed_inplace() { sed -i "$@"; }        # GNU sed
else
  sed_inplace() { sed -i '' "$@"; }     # BSD sed（macOS）
fi

# 置換文字列（$HOME）を sed の置換側で安全にするためエスケープ（& | \ を保護）
HOME_ESC="$(printf '%s' "$HOME" | sed -e 's/[&|\\]/\\&/g')"

# --- コピー先ドキュメント内のプレースホルダ /Users/<YOU> を $HOME に置換 --------
replaced=0
while IFS= read -r mdfile; do
  [ -f "$mdfile" ] || continue
  if grep -q '/Users/<YOU>' "$mdfile" 2>/dev/null; then
    sed_inplace -e "s|/Users/<YOU>|$HOME_ESC|g" "$mdfile"
    replaced=$((replaced + 1))
  fi
done <<EOF
$(find "$DEST" -type f -name '*.md' 2>/dev/null)
EOF
if [ "$replaced" -gt 0 ]; then
  echo "ドキュメント内のプレースホルダ /Users/<YOU> を $HOME に置換しました（${replaced} ファイル）。"
fi

# --- 次の手動ステップ -----------------------------------------------------------
if [ -f "$DEST/README.md" ]; then
  DOC="$DEST/README.md"
else
  DOC="$DEST/SKILL.md"
fi

echo
echo "=== 次の手動ステップ ==="
echo "1. $DOC を参照して、~/.claude/settings.json への hooks / permissions / statusLine 配線を行ってください。"
echo "   （このスクリプトはファイルのコピーのみ。settings.json は自動では変更しません。）"
if [ "$SKILL" = "model-policy" ]; then
  echo "2. model-policy は導入後に README の Stage 0 検証（tool_input のキー名確認）と"
  echo "   カナリアテスト（fable 指定サブエージェントが deny されるか）を必ず実施してください。"
fi
echo
echo "導入完了: $SKILL"
