#!/usr/bin/env bash
# handoff_compact_hook.sh — SessionStart hook（matcher: compact）。
# compact 直後の新しいコンテキストへ、直前に保存された handoff（引き継ぎ）を自動注入する。
# 要約器が /compact 引数のファイルパスを要約から落としても、引き継ぎが確実に復元される保険レイヤー。
#
# 仕組み:
#   - handoff_save.sh が保存成功時にマーカー ~/.claude/handoffs/.pending-<セッションID>
#     （ID 不明時は .pending）へ保存先パスを書く。
#   - 本 hook は自セッションのマーカーを最優先で読み、無ければ無印 .pending を
#     「新鮮な場合のみ」採用（他セッション由来の可能性があるため 60 分に制限）。
#   - 注入したらマーカーを消す（consume-once。再 compact や別セッションへの再注入を防ぐ）。
#     handoff 本体ファイルは控えとして残す。
# 安全策: 何があっても exit 0（セッション開始をブロックしない）。
set -u

INPUT="$(cat 2>/dev/null || true)"

home="${HOME:-}"
[ -z "$home" ] && exit 0
dir="$home/.claude/handoffs"
[ -d "$dir" ] || exit 0

# セッション ID を stdin JSON から拾う（jq 優先、無ければ sed で近似）
SID=""
if command -v jq >/dev/null 2>&1; then
  SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
fi
if [ -z "$SID" ]; then
  SID="$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
fi

# マーカー選択: 自セッション優先（12時間まで）、次に無印（60分以内のみ）
marker=""
max_age_min=0
if [ -n "$SID" ] && [ -f "$dir/.pending-$SID" ]; then
  marker="$dir/.pending-$SID"
  max_age_min=720
elif [ -f "$dir/.pending" ]; then
  marker="$dir/.pending"
  max_age_min=60
fi
[ -z "$marker" ] && exit 0

# 新鮮さ判定（mtime）。古いマーカーは掃除だけして終了
if ! find "$marker" -mmin "-$max_age_min" 2>/dev/null | grep -q .; then
  rm -f "$marker" 2>/dev/null
  exit 0
fi

hf="$(head -1 "$marker" 2>/dev/null)"
rm -f "$marker" 2>/dev/null
[ -n "$hf" ] && [ -f "$hf" ] || exit 0

# 全文注入のサイズ上限（バイト）。超える場合はパス参照の指示のみ注入
size="$(wc -c < "$hf" 2>/dev/null | tr -d ' ')"
case "$size" in ''|*[!0-9]*) size=0;; esac

# 保存時刻を注入文に明記する。handoff 保存後に compact せず作業が進んでから
# auto-compact に落ちた場合、要約の方が新しい情報を持つ——無条件に「引き継ぎを優先」と
# 指示すると新しい決定を古い引き継ぎで巻き戻してしまうため、優先関係を時刻で条件付ける。
saved_at="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$hf" 2>/dev/null || stat -c '%y' "$hf" 2>/dev/null | cut -c1-16)"
[ -z "$saved_at" ] && saved_at="時刻不明"

# 注意: 変数の直後にマルチバイト文字を続けると bash が変数名を誤解釈するため必ず ${} で囲む
if [ "$size" -gt 0 ] && [ "$size" -le 24000 ]; then
  CTX="【handoff 自動復元】${saved_at} に保存された引き継ぎ（${hf}）を以下に注入する。保存時点までの状態はこの内容を正として作業を再開すること。ただし保存より後の決定・進捗が会話要約にある場合は、そちらが新しいので優先する。ファイルを改めて Read する必要はない。

$(cat "$hf" 2>/dev/null)"
else
  CTX="【handoff 自動復元】${saved_at} に保存された引き継ぎが ${hf} にある（サイズが大きいため全文注入は省略）。作業を再開する前に必ずこのファイルを Read すること。保存時点までの状態はその内容を正とし、保存より後の決定・進捗が会話要約にある場合はそちらを優先する。"
fi

# SessionStart hook は stdout がそのままコンテキストへ追加される（公式ドキュメント推奨の方式）
printf '%s\n' "$CTX"
exit 0
