# vision/lib.sh — render.sh / check.sh 共通のヘルパ関数群。
#
# source して使う（実行可能ではない）。呼び出し側は `set -euo pipefail` 済みの
# bash スクリプトであることを前提とする。
#
# 提供する機能:
#   - vision_resolve_chrome   : $CHROME_BIN の解決（既定パスへフォールバック）
#   - vision_make_profile_dir : 隔離ユーザープロファイルディレクトリの作成
#                                （EXIT クリーンアップ登録は呼び出し側の責務。
#                                関数自身のコメント参照 — サブシェルスコープの
#                                落とし穴があるため）
#   - vision_add_cleanup      : EXIT 時に rm -rf するパスを登録
#   - vision_wait_for         : PID を監視しつつ、条件コールバックが真になるか
#                                プロセスが自然終了するまで待つ poll-and-kill
#                                watchdog（60秒上限）。自己マッチする pgrep -f は
#                                一切使わず、起動時に取得した PID を直接監視する。
#
# 環境事実（このマシンで実証済み、2026-07-12）:
#   headless Chrome (`--headless=new`) は --screenshot / --dump-dom の処理を
#   完了させた後もプロセスを自発的に終了しない（ハングする）。よって
#   「出力が揃うのを待って自分で kill する」以外に完了検知の方法が無い。
#   GNU timeout は本機に無いため、待機とタイムアウト検知を自前で行う
#   poll-and-kill パターンを採用する。
#
#   プロファイルディレクトリのリーク（実機で毎回1件、約150〜230ファイルを
#   確認済み）には根本原因が2つあった:
#   (1) 【主因・修正済み】vision_make_profile_dir を
#       `PROFILE_DIR="$(vision_make_profile_dir)"` の形（コマンド置換）で
#       呼ぶと、置換はサブシェルで実行されるため、関数内部で
#       vision_add_cleanup を呼んでも「サブシェル内のコピー」の
#       VISION_CLEANUP_PATHS が変更されるだけで親シェルには一切反映されず、
#       EXIT ハンドラの rm -rf 対象に一度も登録されないまま毎回リークしていた
#       （プロセスの kill 漏れではなく、純粋なシェルのスコープの取り違え）。
#       vision_make_profile_dir 自身のコメント参照。
#   (2) 【副次的要因・防御的に対策済み】起動した Chrome（ランチャ）プロセス
#       自身は headless の実処理を担う別プロセス群（GPU/network/storage/
#       renderer 等、実機で1回のヘッドレス実行につき7〜9個のヘルパー
#       プロセスを確認）を fork する。ランチャの PID だけを TERM/KILL しても
#       これらの子プロセスが生き残って user-data-dir への書き込みを続け
#       うる。
#
#   実機調査で分かったこと: 子孫を `pgrep -P`（実PPIDベース）で辿って個別に
#   kill するだけでは不十分（早期kill時に再現する）。「kill しようとしている
#   まさにその瞬間にブラウザ本体が新しいヘルパーを fork している」レースが
#   存在し、そのヘルパーは（a）まだ `pgrep -P` のスナップショットに写らない
#   ことがあり、かつ（b）親が死んで launchd に再親化されると ppid ベースの
#   追跡ではその後二度と見つけられなくなる。一方 macOS では「死んだ親の子が
#   launchd に再親化される」際、pgid（プロセスグループID）はリペアレント
#   では変わらない（setpgid 呼び出しでしか変わらない）。そこで呼び出し元
#   スクリプト（render.sh/check.sh）で `set -m`（ジョブ制御）を有効にして
#   Chrome をバックグラウンド起動し、そのジョブ専用の新しいプロセスグループ
#   （pgid = ランチャ自身の pid）で走らせる。fork 直後の子は fork() の時点で
#   親の pgid をそのまま継承するため、グループ全体へ1回のシグナルで kill
#   すれば「まだ pgrep のスナップショットに写っていない直後の fork」も
#   「既に再親化された孫プロセス」も両方カバーできる。念のため pgrep -P に
#   よる子孫追跡（vision_kill_tree）も後追いで併用する（`set -m` が効いて
#   いない呼び出し環境や、意図的に pgid を離脱したプロセスへの保険）。
#   コマンドライン文字列に基づく `pgrep -f` は自己マッチのリスクがあるため
#   使わない。

: "${CHROME_BIN:=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

VISION_CLEANUP_PATHS=()

vision_cleanup() {
  # 重要: この関数は EXIT ハンドラとして呼ばれる。呼び出し元スクリプトは
  # `set -e` 下で `exit N` を呼んでおり、この関数内の最後のコマンドの終了
  # ステータスがそのまま「シェル全体の最終終了コード」を上書きしてしまう
  # （bash の既知の落とし穴）。実際に踏んだ実例: VISION_CLEANUP_PATHS が空配列の
  # とき `[[ -n "$p" ]] && rm -rf "$p"` が偽（$p=""）で終わり、exit 2 で
  # 抜けたはずが exit 1 にすり替わっていた。個々のアサーション失敗と
  # ツール自身の異常を exit code で区別する設計の根幹に関わるため、
  # 必ず `return 0` で終えて呼び出し元の exit code を汚染しない。
  local p
  for p in "${VISION_CLEANUP_PATHS[@]:-}"; do
    if [[ -n "$p" ]]; then
      rm -rf "$p" || true
    fi
  done
  return 0
}
trap vision_cleanup EXIT

vision_add_cleanup() {
  VISION_CLEANUP_PATHS+=("$1")
}

vision_resolve_chrome() {
  # 実行可能性まではここでは断定しない（例: CHROME_BIN=/bin/false は
  # 実行可能ビットが立っているが起動しても何も生成しない）。存在確認のみ行い、
  # 実際の成否は vision_wait_for の完了判定に委ねる（黙って成功にしないため）。
  if [[ ! -x "$CHROME_BIN" ]]; then
    echo "error: CHROME_BIN が実行可能ではありません: $CHROME_BIN" >&2
    return 1
  fi
  return 0
}

#   重要（呼び出し方の注意）: この関数自身は EXIT クリーンアップの登録を
#   行わない。`PROFILE_DIR="$(vision_make_profile_dir)"` のようにコマンド
#   置換で呼ぶと、置換はサブシェルで実行されるため、関数内で
#   vision_add_cleanup を呼んでも「サブシェル内のコピー」の
#   VISION_CLEANUP_PATHS が変更されるだけで親シェルには一切反映されない
#   （実機で確認済みの実バグ: mktemp -d で作った1プロファイルディレクトリ
#   [約150〜230ファイル] が check.sh/render.sh の呼び出し毎に必ず1つ
#   リークしていた。原因はプロセスの kill 漏れではなく、この
#   サブシェル・スコープの取り違えだった）。そのため呼び出し側で
#   明示的に次のように2行に分けて呼ぶこと（WORK_DIR/LOG_FILE と同じ
#   パターン）:
#     PROFILE_DIR="$(vision_make_profile_dir)"
#     vision_add_cleanup "$PROFILE_DIR"
vision_make_profile_dir() {
  mktemp -d "${TMPDIR:-/tmp}/fablize-vision-profile.XXXXXX"
}

# vision_collect_descendants <pid>
#   <pid> の子孫プロセスの PID を（<pid> 自身は含めず）1行1個で標準出力へ
#   列挙する。`pgrep -P` は実際のOSの親子関係で追跡するため、コマンドライン
#   文字列に基づく `pgrep -f` と違って自己マッチのリスクが無い。
vision_collect_descendants() {
  local pid="$1"
  local children
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  local c
  for c in $children; do
    vision_collect_descendants "$c"
    printf '%s\n' "$c"
  done
}

# vision_kill_tree <pid> <signal>
#   <pid> とその子孫プロセス全員へ <signal> を送る。子孫の収集は kill する
#   「前」に一括で行う（kill しながら辿ると、途中で死んだプロセスの子が
#   launchd/init に再親化されて追跡できなくなる恐れがあるため）。
#   vision_drain_group の保険用フォールバック（`set -m` が効いていない
#   呼び出し環境向け）。
vision_kill_tree() {
  local root="$1" sig="$2"
  local descendant
  while IFS= read -r descendant; do
    [[ -n "$descendant" ]] && kill -"$sig" "$descendant" 2>/dev/null || true
  done < <(vision_collect_descendants "$root")
  kill -"$sig" "$root" 2>/dev/null || true
}

# vision_drain_group <pgid>
#   <pgid> に属する全プロセス（pgrep -g による実際のプロセスグループ所属
#   判定。再親化の影響を受けない — pgid は setpgid でしか変わらないため、
#   親が死んで launchd に再親化されたプロセスも引き続き同じ pgid のまま
#   捕捉できる）が消えるまで、最大6ラウンド（前半 TERM・後半 KILL、各
#   ラウンド間 0.15秒）にわたってシグナルを送り続ける。
#
#   実機で確認した事実（2026-07-12）: headless Chrome を早期 kill すると、
#   ブラウザ本体プロセスがシグナル受信後の終了処理中に「GPU プロセスの
#   再起動」など新しいヘルパーを fork することがあり、そのヘルパーは fork
#   直後に親（ブラウザ本体）が死んで即座に launchd へ再親化される
#   （観測時点で既に ppid=1）。1回きりの `kill -TERM -- -pgid` では、この
#   ヘルパーが fork される「前」に signal が発行されていた場合に取りこぼす
#   （シグナルは送出時点で存在するプロセスにしか届かない）。pgid は
#   fork 時に親から継承され、かつ再親化されても変わらないため、複数
#   ラウンドに分けて `pgrep -g` で再列挙・再 kill することで、この手の
#   「shutdown 中の遅延 fork」も捕捉できる。
vision_drain_group() {
  local pgid="$1"
  local round sig members m
  for round in 1 2 3 4 5 6; do
    members="$(pgrep -g "$pgid" 2>/dev/null || true)"
    if [[ -z "$members" ]]; then
      return 0
    fi
    if [[ "$round" -le 3 ]]; then sig=TERM; else sig=KILL; fi
    for m in $members; do
      kill -"$sig" "$m" 2>/dev/null || true
    done
    sleep 0.15
  done
  members="$(pgrep -g "$pgid" 2>/dev/null || true)"
  [[ -z "$members" ]]
}

# vision_wait_for <pid> <timeout_seconds> <condition_fn>
#   condition_fn は引数なしで呼ばれ、成功したら 0 を返す関数名を渡す。
#   戻り値（このシェル関数自体の exit code）:
#     0 = condition_fn が真になった（成功）
#     1 = condition_fn が真にならないまま Chrome プロセスが自然終了した
#     2 = timeout_seconds 経過してもどちらにもならなかった（ハング）
#   いずれの場合も、対象プロセスとその子孫（headless Chrome が fork する
#   ブラウザ本体等）がまだ生きていれば確実に止めてから返る（ハングしたまま
#   呼び出し元へ制御を返すことは絶対にしない。子孫を放置するとプロファイル
#   ディレクトリのリークにつながる — ファイル冒頭の環境事実コメント参照）。
vision_wait_for() {
  local pid="$1" timeout_s="$2" cond_fn="$3"
  local waited_ms=0
  local interval_ms=100
  local outcome=2

  while :; do
    if "$cond_fn"; then
      outcome=0
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      outcome=1
      break
    fi
    if [[ "$waited_ms" -ge $((timeout_s * 1000)) ]]; then
      outcome=2
      break
    fi
    sleep 0.1
    waited_ms=$((waited_ms + interval_ms))
  done

  if kill -0 "$pid" 2>/dev/null; then
    # 呼び出し元が `set -m` 済みなら $pid はそれ自身の pgid を持つジョブ
    # グループのリーダーなので pgid==$pid。vision_drain_group がグループ
    # 全体を複数ラウンドかけて確実に空にする。
    vision_drain_group "$pid"
    # 保険: `set -m` が効いていない呼び出し環境や pgid を離脱した子への
    # フォールバック（ppid ベースの子孫追跡）。
    if kill -0 "$pid" 2>/dev/null; then
      vision_kill_tree "$pid" KILL
    fi
  fi
  wait "$pid" 2>/dev/null || true

  return "$outcome"
}
