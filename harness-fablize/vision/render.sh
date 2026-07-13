#!/usr/bin/env bash
# vision/render.sh — headless Chrome ワンショットスクリーンショット
#
# usage: render.sh <input.(html|svg)> <out.png> [--size WxH] [--scale N] [--wait-ms MS]
#   既定: --size 1600x900 --scale 2 --wait-ms 2000
#
# 目的: Opus が図を生成した際、視覚結果を確認せずに完了宣言するのを防ぐための
# 「摩擦ゼロ」なレンダリングコマンド。1コマンドで PNG を得る。
#
# 実装メモ（2026-07-12 に本機で実証済みの環境事実）:
#   headless Chrome (--headless=new) は --screenshot の書き込みを終えた後も
#   プロセスを自発的に終了しない（ハングする）。よって「出力 PNG が安定した
#   ことを検知したら自分で kill する」poll-and-kill 方式を取る
#   （lib.sh の vision_wait_for、60秒 watchdog）。GNU timeout は本機に無い。
#
# 失敗時は必ず非0終了し、stderr に明確なエラーメッセージを出す
# （黙って成功したように見せない）。

set -euo pipefail
# ジョブ制御を有効化する: 以降 `cmd &` で起動するバックグラウンドジョブは
# それぞれ専用の新しいプロセスグループ（pgid = そのジョブの pid）を持つ。
# headless Chrome の子孫プロセスの kill 漏れ対策（lib.sh の環境事実コメント
# 参照）。
set -m

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  echo "usage: $0 <input.(html|svg)> <out.png> [--size WxH] [--scale N] [--wait-ms MS]" >&2
}

if [[ $# -lt 2 ]]; then
  usage
  exit 1
fi

INPUT="$1"
OUT="$2"
shift 2

WIDTH=1600
HEIGHT=900
SCALE=2
WAIT_MS=2000

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size)
      [[ $# -ge 2 ]] || { echo "error: --size には WxH 引数が必要です" >&2; exit 1; }
      if [[ ! "$2" =~ ^[0-9]+x[0-9]+$ ]]; then
        echo "error: --size の形式が不正です（例: 1600x900）: $2" >&2
        exit 1
      fi
      WIDTH="${2%%x*}"
      HEIGHT="${2##*x}"
      shift 2
      ;;
    --scale)
      [[ $# -ge 2 ]] || { echo "error: --scale には数値引数が必要です" >&2; exit 1; }
      if [[ ! "$2" =~ ^[0-9]+$ ]]; then
        echo "error: --scale は正の整数で指定してください: $2" >&2
        exit 1
      fi
      SCALE="$2"
      shift 2
      ;;
    --wait-ms)
      [[ $# -ge 2 ]] || { echo "error: --wait-ms には数値引数が必要です" >&2; exit 1; }
      if [[ ! "$2" =~ ^[0-9]+$ ]]; then
        echo "error: --wait-ms は正の整数で指定してください: $2" >&2
        exit 1
      fi
      WAIT_MS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: 不明な引数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f "$INPUT" ]]; then
  echo "error: 入力ファイルが見つかりません: $INPUT" >&2
  exit 1
fi

if ! vision_resolve_chrome; then
  exit 1
fi

# 絶対パス化（file:// URL と sips のため）
INPUT_ABS="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
case "$OUT" in
  /*) OUT_ABS="$OUT" ;;
  *) OUT_ABS="$(pwd)/$OUT" ;;
esac
mkdir -p "$(dirname "$OUT_ABS")"
rm -f "$OUT_ABS"

PROFILE_DIR="$(vision_make_profile_dir)"
vision_add_cleanup "$PROFILE_DIR"
LOG_FILE="$(mktemp "${TMPDIR:-/tmp}/fablize-vision-render-log.XXXXXX")"
vision_add_cleanup "$LOG_FILE"

"$CHROME_BIN" \
  --headless=new \
  --disable-gpu \
  --hide-scrollbars \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions \
  --user-data-dir="$PROFILE_DIR" \
  --force-device-scale-factor="$SCALE" \
  --window-size="${WIDTH},${HEIGHT}" \
  --virtual-time-budget="$WAIT_MS" \
  --screenshot="$OUT_ABS" \
  "file://$INPUT_ABS" \
  > "$LOG_FILE" 2>&1 &
CHROME_PID=$!

# out.png のサイズが2回連続で同じ（かつ0バイトより大きい）なら書き込み完了と
# みなす。前回サイズを外部変数 _prev_size で受け渡す（クロージャが無いため）。
_prev_size=-1
_stable_hits=0
render_condition() {
  if [[ ! -f "$OUT_ABS" ]]; then
    _prev_size=-1
    _stable_hits=0
    return 1
  fi
  local size
  size=$(sizeof_file "$OUT_ABS")
  if [[ "$size" -gt 0 && "$size" -eq "$_prev_size" ]]; then
    _stable_hits=$((_stable_hits + 1))
    if [[ "$_stable_hits" -ge 2 ]]; then
      return 0
    fi
    return 1
  fi
  _prev_size="$size"
  _stable_hits=0
  return 1
}

sizeof_file() {
  # macOS / BSD stat
  stat -f%z "$1" 2>/dev/null || wc -c < "$1"
}

OUTCOME=0
vision_wait_for "$CHROME_PID" 60 render_condition || OUTCOME=$?

if [[ "$OUTCOME" -eq 2 ]]; then
  echo "error: Chrome が60秒以内に応答せず watchdog により強制終了しました（入力: ${INPUT}）" >&2
  echo "--- Chrome ログ (末尾20行) ---" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  exit 1
fi

if [[ ! -f "$OUT_ABS" ]]; then
  echo "error: スクリーンショットが生成されませんでした: $OUT_ABS" >&2
  echo "--- Chrome ログ (末尾20行) ---" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  exit 1
fi

SIZE="$(sizeof_file "$OUT_ABS")"
if [[ "$SIZE" -eq 0 ]]; then
  echo "error: スクリーンショットが0バイトです: $OUT_ABS" >&2
  echo "--- Chrome ログ (末尾20行) ---" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  exit 1
fi

if [[ "$OUTCOME" -eq 1 ]]; then
  # プロセスは自然終了したが、それがスクリーンショット完了と同時だった可能性
  # もある（サイズ安定判定より先にプロセスが死んだ）。ファイルが有効なら
  # 成功扱いにするが、ログは念のため出しておく。
  echo "note: Chrome プロセスは自然終了しました（スクリーンショット自体は生成済み）" >&2
fi

set +e
DIMS="$(sips -g pixelWidth -g pixelHeight "$OUT_ABS" 2>/dev/null | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w"x"h}')"
set -e
if [[ -z "$DIMS" || "$DIMS" == "x" ]]; then
  echo "error: 生成された PNG の寸法を sips で取得できませんでした（破損の可能性）: $OUT_ABS" >&2
  exit 1
fi

echo "rendered: $OUT_ABS (${SIZE} bytes, ${DIMS}px)" >&2
exit 0
