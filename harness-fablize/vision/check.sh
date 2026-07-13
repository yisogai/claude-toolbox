#!/usr/bin/env bash
# vision/check.sh — DOM 幾何アサーションの実行系
#
# usage: check.sh <input.(html|svg)> <assertions.json> [--out results.json]
#
# 視覚的な合否判断を数値の合否へ変換する。仕組み:
#   1. 入力(.html/.svg)を「ラッパー文書」に変換する（lib/build_wrapper.py）。
#      geometry.js を埋め込み、load 後に2ティック待ってから runAssertions(spec)
#      を実行して結果 JSON を
#      <script type="application/json" id="__fablize_results__<nonce>"> に
#      書き込むランナーを追記する（rAF ではなく setTimeout を使う理由は
#      lib/build_wrapper.py のコメント参照 — headless の --dump-dom +
#      --virtual-time-budget では rAF がレースして偽陰性になることを本機で
#      確認したため）。
#   2. headless Chrome --dump-dom でラッパー文書を開き、シリアライズされた
#      DOM をログファイルへ吐かせる。
#   3. ログから __fablize_results__<nonce> の中身を取り出す
#      （lib/extract_results.py）。
#
# セキュリティ上重要（検証チャネルの入力からの分離）:
#   結果要素の id には build_wrapper.py が毎回生成するランダムなノンス
#   （128bit）を付与する。採点対象の入力(.svg/.html)自身が
#   id="__fablize_results" を持つ要素を仕込んでいても（SVG は <script> 要素
#   を持てる／HTML は言うまでもない）、入力ファイルはそのノンスを事前に
#   知り得ないため、grep によるポーリング条件・extract_results.py の照合の
#   どちらにも一切マッチしない。よって偽の合否データが採用されることはない。
#
# 終了コード:
#   0 = 全アサーション pass
#   1 = 1件以上 fail（アサーション自体の失敗。ツールは正常動作）
#   2 = インフラ異常（入力不在・spec が不正 JSON・assertions が空配列・
#       Chrome 失敗・結果要素が回収できない・--out 書き込み失敗、等。
#       ツール自身の故障）
# 「アサーション失敗」と「ツールの故障」を必ず区別する。空の assertions を
# 黙って 0 で通すことはしない。
#
# 実装メモ: render.sh と同じ理由で headless Chrome はハングする
# （--dump-dom 完了後も自発終了しない）ため poll-and-kill watchdog を使う。
#
# 負荷耐性: 高負荷時に --virtual-time-budget の実時間側デッドラインが
# 結果要素の書き込みより先に発火し、watchdog タイムアウトや結果要素未回収が
# 間欠的に起きることを本機で観測した（負荷依存で決定論的な再現は不可）。
# 対策として (a) virtual-time-budget を余裕を持たせた値にし、(b) watchdog
# タイムアウト／結果要素未回収の場合に限り、入力ごと最大 MAX_ATTEMPTS 回まで
# 自動リトライする（アサーション自体の fail は絶対にリトライしない — それは
# ツール正常動作なので）。

set -euo pipefail
# ジョブ制御を有効化する: 以降 `cmd &` で起動するバックグラウンドジョブは
# それぞれ専用の新しいプロセスグループ（pgid = そのジョブの pid）を持つ。
# headless Chrome の子孫プロセスの kill 漏れ対策（lib.sh の環境事実コメント
# 参照）。
set -m

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

GEOMETRY_JS="$SCRIPT_DIR/geometry.js"
BUILD_WRAPPER="$SCRIPT_DIR/lib/build_wrapper.py"
EXTRACT_RESULTS="$SCRIPT_DIR/lib/extract_results.py"

usage() {
  echo "usage: $0 <input.(html|svg)> <assertions.json> [--out results.json] [--size WxH]" >&2
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

INPUT="$1"
SPEC="$2"
shift 2

OUT_FILE=""
SIZE_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      [[ $# -ge 2 ]] || { echo "error: --out にはファイルパス引数が必要です" >&2; exit 2; }
      OUT_FILE="$2"
      shift 2
      ;;
    --size)
      [[ $# -ge 2 ]] || { echo "error: --size には WxH 引数が必要です" >&2; exit 2; }
      if [[ ! "$2" =~ ^[0-9]+x[0-9]+$ ]]; then
        echo "error: --size の形式が不正です（例: 1600x900）: $2" >&2
        exit 2
      fi
      SIZE_ARG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: 不明な引数: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# --size 指定時のみ chrome へ --window-size を渡す（render.sh と同様の解釈）。
# 未指定時は従来どおり何も渡さず、headless Chrome の既定ウィンドウサイズを使う
# （挙動変更なし）。
CHROME_WINDOW_SIZE_FLAG=()
if [[ -n "$SIZE_ARG" ]]; then
  SIZE_WIDTH="${SIZE_ARG%%x*}"
  SIZE_HEIGHT="${SIZE_ARG##*x}"
  CHROME_WINDOW_SIZE_FLAG=(--window-size="${SIZE_WIDTH},${SIZE_HEIGHT}")
fi

if [[ ! -f "$INPUT" ]]; then
  echo "error: 入力ファイルが見つかりません: $INPUT" >&2
  exit 2
fi
if [[ ! -f "$SPEC" ]]; then
  echo "error: assertions ファイルが見つかりません: $SPEC" >&2
  exit 2
fi
if [[ ! -f "$GEOMETRY_JS" ]]; then
  echo "error: geometry.js が見つかりません（配置が壊れています）: $GEOMETRY_JS" >&2
  exit 2
fi

for bin in python3 jq; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "error: 必須コマンド '$bin' が見つかりません" >&2
    exit 2
  fi
done

if ! vision_resolve_chrome; then
  exit 2
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fablize-vision-check.XXXXXX")"
vision_add_cleanup "$WORK_DIR"

# --virtual-time-budget: 高負荷時の実時間デッドライン競合に備えて余裕を
# 持たせる（従来3000ms → 5000ms。lib/build_wrapper.py の setTimeout 2段
# ネストは仮想時間で確実に進むが、実時間側デッドラインが先に来ると
# --dump-dom がその時点のDOMで発火してしまうため）。
VIRTUAL_TIME_BUDGET_MS=5000
# watchdog タイムアウト／結果要素未回収（＝インフラ側のレース起因である
# 可能性がある2種）に限り、最大この回数まで入力ごと自動リトライする。
# アサーション自体の fail（FAIL_COUNT>0）はリトライ対象外（正常動作なので）。
MAX_ATTEMPTS=3

ATTEMPT=1
RESULT_JSON=""
LAST_ERROR=""
SUCCESS=0

while [[ "$ATTEMPT" -le "$MAX_ATTEMPTS" ]]; do
  WRAPPER_HTML="$WORK_DIR/wrapper-$ATTEMPT.html"
  DUMP_LOG="$WORK_DIR/dump-$ATTEMPT.log"

  set +e
  NONCE="$(python3 "$BUILD_WRAPPER" "$INPUT" "$GEOMETRY_JS" "$SPEC" "$WRAPPER_HTML")"
  BUILD_RC=$?
  set -e
  if [[ "$BUILD_RC" -ne 0 ]]; then
    # build_wrapper.py が既に理由を stderr へ出している（不正JSON/空
    # assertions等）。これは入力そのものの問題でリトライしても変わらない
    # ため即終了する。
    exit 2
  fi
  if [[ -z "$NONCE" ]]; then
    echo "error: build_wrapper.py が結果要素IDのノンスを返しませんでした（内部エラー）" >&2
    exit 2
  fi

  PROFILE_DIR="$(vision_make_profile_dir)"
  vision_add_cleanup "$PROFILE_DIR"

  "$CHROME_BIN" \
    --headless=new \
    --disable-gpu \
    --hide-scrollbars \
    --no-first-run \
    --no-default-browser-check \
    --disable-extensions \
    --user-data-dir="$PROFILE_DIR" \
    "${CHROME_WINDOW_SIZE_FLAG[@]}" \
    --virtual-time-budget="$VIRTUAL_TIME_BUDGET_MS" \
    --dump-dom \
    "file://$WRAPPER_HTML" \
    > "$DUMP_LOG" 2>&1 &
  CHROME_PID=$!

  # ノンス付きの結果要素タグの出現のみを見る。ノンスは16進数のみで構成される
  # ため grep -E に生のまま埋め込んでも安全。入力ファイル自身が
  # id="__fablize_results"（ノンス無し、または別のノンス）を持つ要素を
  # 仕込んでいても、このノンスを事前に知り得ないため一切マッチしない
  # （検証チャネルの入力からの分離。ファイル冒頭のコメント参照）。
  RESULT_TAG_RE='<script[^>]*id="__fablize_results__'"$NONCE"'"'
  check_condition() {
    # 注意: ここで毎回 python3 を起動すると（60秒 watchdog 中に最大数百回）、
    # 実機で確認済みの重大な副作用がある — python3 の頻繁な fork が Chrome
    # プロセス自身の CPU スケジューリングを圧迫し、--virtual-time-budget の
    # 実時間側デッドラインが rAF 2回待ちの完了より先に来てしまい、
    # __fablize_results 要素が書き込まれる前に --dump-dom が発火して
    # 「常にタイムアウトする」という偽陰性を引き起こすことを本機で再現した。
    # そのため軽量な grep だけをポーリング条件にし、正式な JSON 抽出・検証は
    # ループを抜けた後に一度だけ行う。
    grep -Eq "$RESULT_TAG_RE" "$DUMP_LOG" 2>/dev/null
  }

  OUTCOME=0
  vision_wait_for "$CHROME_PID" 60 check_condition || OUTCOME=$?

  if [[ "$OUTCOME" -eq 2 ]]; then
    LAST_ERROR="Chrome が60秒以内に応答せず watchdog により強制終了しました（試行 $ATTEMPT/$MAX_ATTEMPTS）"
    echo "warn: $LAST_ERROR" >&2
    tail -n 20 "$DUMP_LOG" >&2 || true
    ATTEMPT=$((ATTEMPT + 1))
    continue
  fi

  set +e
  RESULT_JSON="$(python3 "$EXTRACT_RESULTS" "$DUMP_LOG" "$NONCE" 2>&1)"
  EXTRACT_RC=$?
  set -e
  if [[ "$EXTRACT_RC" -ne 0 ]]; then
    LAST_ERROR="結果要素を回収できませんでした（試行 $ATTEMPT/$MAX_ATTEMPTS）: $RESULT_JSON"
    echo "warn: $LAST_ERROR" >&2
    tail -n 20 "$DUMP_LOG" >&2 || true
    ATTEMPT=$((ATTEMPT + 1))
    continue
  fi

  SUCCESS=1
  break
done

if [[ "$SUCCESS" -ne 1 ]]; then
  echo "error: $MAX_ATTEMPTS 回リトライしても結果を回収できませんでした。最後のエラー: $LAST_ERROR" >&2
  exit 2
fi

INFRA_ERROR="$(printf '%s' "$RESULT_JSON" | jq -r '.infra_error // false')"
if [[ "$INFRA_ERROR" == "true" ]]; then
  echo "error: アサーション実行ランナー自体が失敗しました（geometry.js 読み込み失敗等）" >&2
  printf '%s' "$RESULT_JSON" | jq -r '.results[] | "  " + .message' >&2 || true
  exit 2
fi

PASS_COUNT="$(printf '%s' "$RESULT_JSON" | jq -r '.pass_count')"
FAIL_COUNT="$(printf '%s' "$RESULT_JSON" | jq -r '.fail_count')"

if [[ -z "$PASS_COUNT" || -z "$FAIL_COUNT" || "$PASS_COUNT" == "null" || "$FAIL_COUNT" == "null" ]]; then
  echo "error: 結果 JSON の形が想定外です（pass_count/fail_count が無い）: $RESULT_JSON" >&2
  exit 2
fi

while IFS=$'\t' read -r ok id msg; do
  if [[ "$ok" == "true" ]]; then
    echo "PASS $id" >&2
  else
    echo "FAIL $id $msg" >&2
  fi
done < <(printf '%s' "$RESULT_JSON" | jq -r '.results[] | [(.pass|tostring), .id, .message] | @tsv')

echo "== 結果: PASS=$PASS_COUNT FAIL=$FAIL_COUNT ==" >&2

if [[ -n "$OUT_FILE" ]]; then
  OUT_DIR="$(dirname "$OUT_FILE")"
  if ! mkdir -p "$OUT_DIR" 2>/dev/null; then
    echo "error: --out の出力先ディレクトリを作成できません: $OUT_DIR" >&2
    exit 2
  fi
  if ! printf '%s\n' "$RESULT_JSON" > "$OUT_FILE" 2>/dev/null; then
    echo "error: --out へ結果を書き込めませんでした: $OUT_FILE" >&2
    exit 2
  fi
else
  printf '%s\n' "$RESULT_JSON"
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
