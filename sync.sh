#!/usr/bin/env bash
# sync.sh — 作者の ~/.claude/skills/<skill-name>（原本）から、この配布リポジトリへ
#           スキルをコピーして反映する（メンテナ専用）。install.sh の「逆方向」。
#
# 使い方:
#   ./sync.sh <skill-name>
#
#   <skill-name>   原本 ~/.claude/skills/ 直下のスキルディレクトリ名（例: model-policy / handoff）。
#
# 挙動:
#   1. ~/.claude/skills/<skill-name>/ が無ければエラーで exit 1。
#   2. リポジトリ側 <skill-name>/ を削除し、原本からまるごとコピー。
#   3. コピー先の *.log / .DS_Store（紛れていれば）を削除。
#   4. コピー先ドキュメント（*.md）内の「実行者の実 $HOME 文字列」をすべて
#      プレースホルダ /Users/<YOU> に置換（install.sh はこの逆＝/Users/<YOU> → $HOME）。
#      これで原本が実パスを含んでいても、リポジトリには個人パスが残らない。
#   5. 漏えい検査: 置換後のコピー先に実 $HOME もユーザー名も残っていないことを grep で確認。
#      残っていたら警告して exit 1（コミットさせない）。
#   6. git status --short を表示し、「差分を確認してコミットせよ」と案内。
#
#   コミットは自動では行わない（メンテナが diff を確認してから手動でコミットする前提）。
#
# 移植性: macOS の BSD sed（sed -i ''）を前提にしつつ GNU sed（sed -i）へ自動フォールバック。
set -u

# --- このスクリプトが置かれたディレクトリ = リポジトリルート ---------------------
REPO_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
if [ -z "$REPO_DIR" ]; then
  echo "エラー: リポジトリのパスを特定できませんでした。" >&2
  exit 1
fi

SKILLS_SRC="$HOME/.claude/skills"

# --- 引数解析 -------------------------------------------------------------------
SKILL="${1:-}"
if [ -z "$SKILL" ]; then
  echo "エラー: スキル名を指定してください。" >&2
  echo "使い方: ./sync.sh <skill-name>" >&2
  echo >&2
  echo "原本 $SKILLS_SRC/ 直下のスキル:" >&2
  for d in "$SKILLS_SRC"/*/; do
    [ -d "$d" ] || continue
    echo "  - $(basename "$d")" >&2
  done
  exit 1
fi

# skill 名にパス区切り等が混じるのを弾く（../ などによる誤削除・誤コピー防止）
case "$SKILL" in
  */*|.|..|*'\'*)
    echo "エラー: 不正なスキル名です: $SKILL" >&2
    exit 1
    ;;
esac

SRC="$SKILLS_SRC/$SKILL"
DEST="$REPO_DIR/$SKILL"

# --- 原本の存在チェック ---------------------------------------------------------
if [ ! -d "$SRC" ]; then
  echo "エラー: 原本が存在しません: $SRC" >&2
  echo "（~/.claude/skills/<skill-name> を確認してください）" >&2
  exit 1
fi

# --- リポジトリ側を削除して原本からコピー --------------------------------------
if [ -e "$DEST" ]; then
  if ! rm -rf "$DEST"; then
    echo "エラー: リポジトリ側の削除に失敗しました: $DEST" >&2
    exit 1
  fi
  echo "リポジトリ側を削除しました: $DEST"
fi
if ! cp -R "$SRC" "$DEST"; then
  echo "エラー: コピーに失敗しました: $SRC -> $DEST" >&2
  exit 1
fi
echo "原本からコピーしました: $SRC -> $DEST"

# --- 紛れ込んだ *.log / .DS_Store を削除 ----------------------------------------
junk=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  rm -f "$f" && junk=$((junk + 1))
done <<EOF
$(find "$DEST" \( -name '*.log' -o -name '.DS_Store' \) -type f 2>/dev/null)
EOF
if [ "$junk" -gt 0 ]; then
  echo "不要ファイル（*.log / .DS_Store）を削除しました（${junk} 件）。"
fi

# --- sed のインプレース編集を BSD/GNU 両対応で用意 ------------------------------
if sed --version >/dev/null 2>&1; then
  sed_inplace() { sed -i "$@"; }        # GNU sed
else
  sed_inplace() { sed -i '' "$@"; }     # BSD sed（macOS）
fi

# --- 置換の準備: 検索側（$HOME）を BRE のメタ文字を含めてエスケープ ------------
#   区切り文字は '|'（パスに現れない前提）。$HOME 内の ] \ / $ * . ^ [ を保護する。
HOME_PAT="$(printf '%s' "$HOME" | sed -e 's/[]\/$*.^[]/\\&/g')"

# --- コピー先ドキュメント（*.md）内の実 $HOME を /Users/<YOU> に置換 -----------
replaced=0
while IFS= read -r mdfile; do
  [ -n "$mdfile" ] || continue
  [ -f "$mdfile" ] || continue
  if grep -qF -- "$HOME" "$mdfile" 2>/dev/null; then
    sed_inplace -e "s|${HOME_PAT}|/Users/<YOU>|g" "$mdfile"
    replaced=$((replaced + 1))
  fi
done <<EOF
$(find "$DEST" -type f -name '*.md' 2>/dev/null)
EOF
if [ "$replaced" -gt 0 ]; then
  echo "ドキュメント内の実ホームパスを /Users/<YOU> に置換しました（${replaced} ファイル）。"
else
  echo "ドキュメント内に実ホームパスは見つかりませんでした（置換なし）。"
fi

# --- 漏えい検査: 実 $HOME もユーザー名も残っていないこと ------------------------
#   USERNAME は $HOME の basename から導出（＝実行者のホームディレクトリ名）。install.sh が
#   置換できない別プレースホルダ等でホーム名が残っていないかを大小無視で洗う。
USERNAME="$(basename "$HOME")"

leak=0
if grep -rnF -- "$HOME" "$DEST" 2>/dev/null; then
  echo "警告: 上記に実ホームパス（${HOME}）が残っています。" >&2
  leak=1
fi
if [ -n "$USERNAME" ] && grep -rniF -- "$USERNAME" "$DEST" 2>/dev/null; then
  echo "警告: 上記にユーザー名（${USERNAME}）が残っています。" >&2
  leak=1
fi
if [ "$leak" -ne 0 ]; then
  echo "エラー: 漏えい検査に失敗しました。コミットしないでください（原本またはスクラブ範囲を見直す）。" >&2
  exit 1
fi
echo "漏えい検査: OK（実ホームパス・ユーザー名ともに 0 件）。"

# --- 差分の確認を案内（自動コミットはしない）-----------------------------------
echo
echo "=== git status（${REPO_DIR}）==="
git -C "$REPO_DIR" status --short
echo
echo "差分を確認してから、メンテナ自身でコミットしてください（このスクリプトはコミットしません）。"
echo "  例: git -C \"$REPO_DIR\" add $SKILL && git -C \"$REPO_DIR\" commit"
