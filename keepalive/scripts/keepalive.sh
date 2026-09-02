#!/usr/bin/env bash
# keepalive.sh — 長時間待ちの間、メインセッションのプロンプトキャッシュ（1h TTL）を温存する
# ための「眠って起こす」タイマー。Claude Code の Bash ツールから run_in_background: true で
# 1本だけ起動し、完了通知でメインを起こして no-op → 必要なら再武装する運用を支える。
#
# 使い方:
#   keepalive.sh tick   --key <KEY> --n <N> [--cap <CAP>=10] [--interval <SEC>=3000]
#   keepalive.sh stop   --key <KEY>
#   keepalive.sh status --key <KEY>
#
# 設計:
#   - 状態（何本目か）はセッション内で引数として持ち回る。ディスク上の状態は pid ファイルのみ。
#   - N > CAP なら sleep せず即座に停止メッセージを出す（モデルの記憶に依存しない暴走防止）。
#   - 同じ key で二重に起動されたら、古いプロセスを kill してから置き換える。
#   - sleep 完了時の出力は自己記述的（compact 後に起きても文脈なしで正しく動ける）。
#   - SIGTERM/SIGINT では自分の pid ファイルだけを消して静かに終了する（余計な出力を出さない）。
set -u

CMD_PATH="$HOME/.claude/skills/keepalive/scripts/keepalive.sh"

usage() {
  cat >&2 <<EOF
使い方:
  keepalive.sh tick   --key <KEY> --n <N> [--cap <CAP>=10] [--interval <SEC>=3000]
  keepalive.sh stop   --key <KEY>
  keepalive.sh status --key <KEY>

  --key       セッション識別子（英数・. _ - のみ、1〜64文字）
  --n         今回が何本目の tick か（1以上の整数）
  --cap       再武装の上限本数（既定 10）。N > CAP なら sleep せず停止メッセージを出す
  --interval  sleep する秒数（既定 3000 = 50分）
EOF
}

die() { echo "エラー: $*" >&2; usage; exit 2; }

pid_file_for() { echo "${TMPDIR:-/tmp}/claude-keepalive-$1.pid"; }

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

# --- 引数解析 -----------------------------------------------------------------
SUB="${1:-}"
[ -n "$SUB" ] || die "サブコマンドを指定してください（tick / stop / status）。"
shift || true

KEY=""; N=""; CAP=10; INTERVAL=3000
while [ $# -gt 0 ]; do
  case "$1" in
    --key)      [ $# -ge 2 ] || die "--key に値がありません。";      KEY="$2"; shift 2 ;;
    --n)        [ $# -ge 2 ] || die "--n に値がありません。";        N="$2"; shift 2 ;;
    --cap)      [ $# -ge 2 ] || die "--cap に値がありません。";      CAP="$2"; shift 2 ;;
    --interval) [ $# -ge 2 ] || die "--interval に値がありません。"; INTERVAL="$2"; shift 2 ;;
    *) die "不明な引数: $1" ;;
  esac
done

[ -n "$KEY" ] || die "--key は必須です。"
case "$KEY" in
  *[!A-Za-z0-9._-]*) die "--key に使える文字は英数と . _ - のみです: $KEY" ;;
esac
[ "${#KEY}" -le 64 ] || die "--key が長すぎます（64文字以内）: $KEY"

PIDFILE="$(pid_file_for "$KEY")"

# pid ファイルから PID / 開始 epoch を読む（成功時に PF_PID / PF_START を設定）
read_pidfile() {
  PF_PID=""; PF_START=""
  [ -f "$PIDFILE" ] || return 1
  read -r PF_PID PF_START < "$PIDFILE" 2>/dev/null || return 1
  is_uint "${PF_PID:-}" || return 1
  return 0
}

# 稼働中の先行プロセスがあれば止める（自分自身は対象外）
kill_existing() {
  read_pidfile || { rm -f "$PIDFILE"; return 0; }
  if [ "$PF_PID" = "$$" ]; then return 0; fi
  if kill -0 "$PF_PID" 2>/dev/null; then
    kill -TERM "$PF_PID" 2>/dev/null || true
    # 終了を最大 3 秒待つ（相手の trap が pid ファイルを消すのを待ってから置き換える）
    i=0
    while [ "$i" -lt 30 ] && kill -0 "$PF_PID" 2>/dev/null; do
      sleep 0.1
      i=$((i + 1))
    done
    KILLED_PID="$PF_PID"
  fi
  # 相手が消し損ねていても、自分のものでない pid ファイルは片付ける
  if read_pidfile && [ "$PF_PID" != "$$" ]; then rm -f "$PIDFILE"; fi
  return 0
}

# 自分が書いた pid ファイルだけを消す
cleanup_own() {
  if read_pidfile && [ "$PF_PID" = "$$" ]; then rm -f "$PIDFILE"; fi
}

case "$SUB" in
  tick)
    [ -n "$N" ] || die "--n は必須です。"
    is_uint "$N" && [ "$N" -ge 1 ] || die "--n は 1 以上の整数で指定してください: $N"
    is_uint "$CAP" && [ "$CAP" -ge 1 ] || die "--cap は 1 以上の整数で指定してください: $CAP"
    is_uint "$INTERVAL" && [ "$INTERVAL" -ge 1 ] || die "--interval は 1 以上の整数（秒）で指定してください: $INTERVAL"

    if [ "$N" -gt "$CAP" ]; then
      echo "KEEPALIVE: 上限 ${CAP} に到達（key=${KEY}）。再武装せず停止。ユーザーが戻ったら handoff + /compact を提案すること。"
      exit 0
    fi

    KILLED_PID=""
    kill_existing
    START_EPOCH="$(date +%s)"
    printf '%s %s\n' "$$" "$START_EPOCH" > "$PIDFILE"

    SLEEP_PID=""
    trap 'cleanup_own; [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null; exit 0' TERM INT

    if [ -n "$KILLED_PID" ]; then
      echo "KEEPALIVE: 先行プロセス(${KILLED_PID})を停止して置き換えました（key=${KEY}）。"
    fi

    sleep "$INTERVAL" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null || true
    cleanup_own

    NEXT=$((N + 1))
    echo "KEEPALIVE tick $N/$CAP 完了 ($(date +%H:%M:%S), interval ${INTERVAL}s, key=$KEY)"
    echo "行動: このターンは no-op の一言だけ返す。待ちが続くなら次を run_in_background で立てる:"
    echo "  bash $CMD_PATH tick --key $KEY --n $NEXT --cap $CAP --interval $INTERVAL"
    echo "完了通知が既に届いている／overage で TTL が5分に落ちた場合は再武装しない。"
    ;;

  stop)
    if read_pidfile && kill -0 "$PF_PID" 2>/dev/null; then
      kill -TERM "$PF_PID" 2>/dev/null || true
      i=0
      while [ "$i" -lt 30 ] && kill -0 "$PF_PID" 2>/dev/null; do
        sleep 0.1
        i=$((i + 1))
      done
      rm -f "$PIDFILE"
      echo "KEEPALIVE: 停止しました（key=$KEY, pid=${PF_PID}）。"
    else
      rm -f "$PIDFILE"
      echo "KEEPALIVE: 動いていません（key=${KEY}）。"
    fi
    ;;

  status)
    if read_pidfile && kill -0 "$PF_PID" 2>/dev/null; then
      NOW="$(date +%s)"
      if is_uint "${PF_START:-}"; then
        ELAPSED=$((NOW - PF_START))
        STARTED="$(date -r "$PF_START" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "epoch $PF_START")"
      else
        ELAPSED="?"; STARTED="不明"
      fi
      echo "KEEPALIVE: 稼働中（key=$KEY, pid=$PF_PID, 開始=${STARTED}, 経過=${ELAPSED}s）"
    else
      [ -f "$PIDFILE" ] && rm -f "$PIDFILE"
      echo "KEEPALIVE: 停止中（key=${KEY}）"
    fi
    ;;

  *)
    echo "エラー: 不明なサブコマンド: $SUB" >&2
    usage
    exit 2
    ;;
esac
