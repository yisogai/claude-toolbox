#!/usr/bin/env bash
# handoff_save.sh — 引き継ぎ本文をファイルへ保存し、実行用の /compact コマンド
# （ファイル参照つきの一行）を組み立ててシステムのクリップボードへ入れる。
#
# 本文は STDIN で受け取る（呼び出し側が Write tool で一時ファイルに書き、`< file`
# でリダイレクトする想定）。本文をシェルの引数やヒアドキュメントに一切通さないため、
# 多バイト文字・改行・バッククォート・$・``` コードフェンス等が壊れない。
#
# クリップボードに入れるもの（MODE で報告）:
#   MODE=cmd  : 保存に成功したとき。本文全文ではなく「/compact <パス参照+要約器への指示>」
#               の一行コマンドをコピーする。compact の要約器はツールを持たずファイルを
#               読めないため、要約には「パス」と「再開時にまず Read せよ」を残させ、
#               compact 後の Claude が Read で全文を復元する方式。
#   MODE=body : ファイルが無いとき（--no-save / 保存失敗）。従来どおり本文全文をコピー
#               し、ユーザーが `/compact ` の引数として貼り付ける。
#
# 使い方:
#   bash handoff_save.sh            < body.md   # 保存 + /compact コマンドをコピー
#   bash handoff_save.sh --no-save  < body.md   # ファイルを残さず本文全文をコピー
#
# 呼び出し側が解析できるよう、結果を機械可読で出す。MODE=cmd のときは最終行の前に
# `CMD: /compact ...` の一行（表示用のコマンド全文）を出す。最終行の例:
#   OK=1 SAVE=ok PATH=/Users/x/.claude/handoffs/handoff-20260622-101500.md CHARS=842 CLIPBOARD=ok OVERLONG=0 MODE=cmd
#   SAVE      : ok | skipped(--no-save) | failed(保存先に書けない/書き込みが途中失敗)
#   CLIPBOARD : ok | osc52 | none(コピー手段が無い/失敗)
#   PATH      : 保存先の絶対パス、または none
#   OVERLONG  : MODE=body で CHARS が 12000 を超えたら 1（貼り付け/引数の上限超過の恐れ）。MODE=cmd では常に 0
#   MODE      : cmd | body（クリップボードに入れた内容）
set -u

save=1
[ "${1:-}" = "--no-save" ] && save=0

# --- 本文の置き場所を決める -------------------------------------------------
final="none"
save_status="skipped"
target=""

if [ "$save" -eq 1 ]; then
  save_status="failed"
  home="${HOME:-}"
  [ -z "$home" ] && home="$(cd ~ 2>/dev/null && pwd || true)"
  if [ -n "$home" ]; then
    dir="$home/.claude/handoffs"
    if mkdir -p "$dir" 2>/dev/null && [ -w "$dir" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      cand="$dir/handoff-$ts.md"
      n=2
      while [ -e "$cand" ]; do cand="$dir/handoff-$ts-$n.md"; n=$((n + 1)); done
      target="$cand"
      final="$cand"
      save_status="ok"
    fi
  fi
fi

if [ -z "$target" ]; then
  target="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/handoff-$$.md")"
fi

# --- STDIN の本文を target へ（中身はシェル展開を一切通らない）-------------
# ディスクフル等で cat が途中失敗すると部分書き込みのファイルが残る。それを
# 「保存成功」として /compact コマンドに参照させると引き継ぎが黙って欠損するため、
# 失敗時は保存を取り消して残骸を消し、本文なし（CHARS=0・コピーなし）として報告する。
# 事前の [ -w "$dir" ] はパーミッションしか見ておらず、容量不足はここでしか検出できない。
if ! cat > "$target"; then
  rm -f "$target" 2>/dev/null
  target=""
  final="none"
  [ "$save_status" = "ok" ] && save_status="failed"
fi

chars=0
if [ -n "$target" ]; then
  chars="$(wc -m < "$target" 2>/dev/null | tr -d ' ')"
  [ -z "$chars" ] && chars=0
fi
overlong=0
{ [ "$chars" -gt 12000 ]; } 2>/dev/null && overlong=1

# --- compact 後の自動注入用マーカー -----------------------------------------
# SessionStart(compact) hook（handoff_compact_hook.sh）が読むマーカーへ保存先パスを書く。
# セッション ID（Bash ツール環境の CLAUDE_CODE_SESSION_ID）が分かればファイル名に付けて
# セッションへ紐付け、無関係なセッションの compact に注入されるのを防ぐ。
# 保存が無い経路（--no-save / 保存失敗）では、以前の保存で残った自セッションのマーカーを
# 必ず無効化する——残すと後続の compact で hook が「古い引き継ぎを最新」として誤注入する。
# あわせて 1 日より古いマーカーの残骸を掃除する（作ったが compact しなかった場合に残る）。
mhome="${HOME:-}"
[ -z "$mhome" ] && mhome="$(cd ~ 2>/dev/null && pwd || true)"
if [ -n "$mhome" ] && [ -d "$mhome/.claude/handoffs" ]; then
  mdir="$mhome/.claude/handoffs"
  find "$mdir" -name '.pending*' -mmin +1440 -delete 2>/dev/null
  sid="${CLAUDE_CODE_SESSION_ID:-}"
  case "$sid" in *[!A-Za-z0-9-]*) sid="";; esac  # パスに安全な形式のみ採用
  if [ "$save_status" = "ok" ]; then
    if [ -n "$sid" ]; then
      printf '%s\n' "$final" > "$mdir/.pending-$sid" 2>/dev/null
    else
      printf '%s\n' "$final" > "$mdir/.pending" 2>/dev/null
    fi
  else
    # 書き込み時と対称に、自分が書いた可能性のあるマーカーだけ消す
    # （sid 持ちが無印 .pending を消すと、ID の取れない別環境のマーカーを誤破壊するため）
    if [ -n "$sid" ]; then
      rm -f "$mdir/.pending-$sid" 2>/dev/null
    else
      rm -f "$mdir/.pending" 2>/dev/null
    fi
  fi
fi

# --- クリップボードへ入れる内容を決める ------------------------------------
# 保存に成功していれば「/compact コマンド一行（ファイル参照 + 要約器への指示）」を、
# ファイルが無ければ従来どおり本文全文をコピーする。コマンド末尾に改行を付けない
# （貼り付けと同時に送信されるのを防ぎ、ユーザーが確認してから Enter できるように）。
mode="body"
cmd=""
cmd_file=""
if [ "$save_status" = "ok" ]; then
  cmd="/compact 手動コンパクト。詳細な引き継ぎを $final に保存済み。要約の冒頭に「再開時は必ず最初に $final を Read してから作業を続行する」という指示をこの絶対パスごと逐語で含めること。加えて直近の作業状態・確定した決定・重要ファイルの絶対パスを優先して保持すること。"
  cmd_file="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/handoff-cmd-$$.txt")"
  if printf '%s' "$cmd" > "$cmd_file" 2>/dev/null; then
    mode="cmd"
    overlong=0  # 本文は貼り付けないので上限超過の恐れは無い
  else
    # コマンド用作業ファイルが書けない（/tmp もフル等）→ 本文コピーの旧方式に落とす
    rm -f "$cmd_file" 2>/dev/null
    cmd_file=""
    cmd=""
  fi
fi

# --- ファイルからクリップボードへコピー ------------------------------------
# 空本文のときはコピーしない（既存クリップボードを空で上書きして壊さないため）。
# 各ツールは「未インストールなら飛ばし、実行して失敗したら次を試す」よう、elif では
# なく独立した if で順に試す。iconv パイプは pipefail をサブシェルで効かせ、iconv 失敗時
# に clip.exe の成功終了コードで「空なのに ok」と誤判定するのを防ぐ。
clip="none"
if [ "$mode" = "cmd" ]; then
  src="$cmd_file"
else
  src="$target"
fi

if [ "${chars:-0}" -gt 0 ]; then
  # リモート運用（SSH + tmux）: tmux load-buffer -w が OSC 52 で「ユーザーの手元
  # ターミナル」のクリップボードへ転送する。リモートマシン自身のクリップボード
  # （clip.exe 等）に入れても手元では貼れないため、非 macOS では最優先で試す。
  # macOS ローカルは従来どおり pbcopy 優先（Darwin では試さない）。
  # 手元に実際に届くかは外側ターミナルの OSC 52 対応に依存するため ok でなく osc52 と報告する。
  if [ "$clip" = "none" ] && [ "$(uname 2>/dev/null)" != "Darwin" ] && [ -n "${TMUX:-}" ] \
     && command -v tmux >/dev/null 2>&1; then
    tmux load-buffer -w - < "$src" >/dev/null 2>&1 && clip="osc52"
  fi

  _is_wsl=0
  if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    _is_wsl=1
  fi

  # WSL を最初に判定（Linux にも見えるため）。clip.exe は UTF-8 を壊すので、
  # iconv で UTF-16LE に変換 → clip.exe を最優先。次点 powershell、最後に素の clip.exe。
  if [ "$_is_wsl" -eq 1 ]; then
    if [ "$clip" = "none" ] && command -v iconv >/dev/null 2>&1 && command -v clip.exe >/dev/null 2>&1; then
      ( set -o pipefail; iconv -f UTF-8 -t UTF-16LE "$src" 2>/dev/null | clip.exe >/dev/null 2>&1 ) && clip="ok"
    fi
    if [ "$clip" = "none" ] && command -v powershell.exe >/dev/null 2>&1; then
      powershell.exe -NoProfile -NonInteractive -Command \
        '$in=[Console]::In.ReadToEnd(); Set-Clipboard -Value $in' < "$src" >/dev/null 2>&1 && clip="ok"
    fi
    if [ "$clip" = "none" ] && command -v clip.exe >/dev/null 2>&1; then
      clip.exe < "$src" >/dev/null 2>&1 && clip="ok"  # 最終手段（多バイト化けの恐れ）
    fi
  fi

  # Wayland では XWayland 経由の xclip より wl-copy を優先したいので先に試す
  if [ "$clip" = "none" ] && [ -n "${WAYLAND_DISPLAY:-}" ] && command -v wl-copy >/dev/null 2>&1; then
    wl-copy < "$src" >/dev/null 2>&1 && clip="ok"
  fi

  # 汎用フォールバック（独立 if で、失敗時は次のツールへ落ちる）
  if [ "$clip" = "none" ] && command -v pbcopy >/dev/null 2>&1; then        # macOS
    pbcopy < "$src" >/dev/null 2>&1 && clip="ok"
  fi
  if [ "$clip" = "none" ] && command -v wl-copy >/dev/null 2>&1; then       # Wayland（汎用）
    wl-copy < "$src" >/dev/null 2>&1 && clip="ok"
  fi
  if [ "$clip" = "none" ] && command -v xclip >/dev/null 2>&1; then         # X11
    xclip -selection clipboard -in < "$src" >/dev/null 2>&1 && clip="ok"
  fi
  if [ "$clip" = "none" ] && command -v xsel >/dev/null 2>&1; then          # X11
    xsel --clipboard --input < "$src" >/dev/null 2>&1 && clip="ok"
  fi
  if [ "$clip" = "none" ] && command -v clip.exe >/dev/null 2>&1; then      # Git Bash / Cygwin
    if command -v iconv >/dev/null 2>&1; then
      ( set -o pipefail; iconv -f UTF-8 -t UTF-16LE "$src" 2>/dev/null | clip.exe >/dev/null 2>&1 ) && clip="ok"
    else
      clip.exe < "$src" >/dev/null 2>&1 && clip="ok"
    fi
  fi
fi

# 一時ファイルを掃除（保存しない場合の本文、コマンド用の作業ファイル）
if [ "$final" = "none" ] && [ -n "$target" ]; then
  rm -f "$target" 2>/dev/null
fi
[ -n "$cmd_file" ] && rm -f "$cmd_file" 2>/dev/null

# MODE=cmd のときは表示用にコマンド全文を1行で出す（呼び出し側はこれを逐語で提示する）
if [ "$mode" = "cmd" ]; then
  printf 'CMD: %s\n' "$cmd"
fi
echo "OK=1 SAVE=$save_status PATH=$final CHARS=$chars CLIPBOARD=$clip OVERLONG=$overlong MODE=$mode"
