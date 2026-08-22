#!/usr/bin/env bash
# pace_statusline.sh — license_statusline.sh の出力の末尾に「週次枠ペーシング」セグメントを
# 追加する合成 wrapper（license_statusline.sh / handoff_statusline.sh は無改変のまま呼ぶ）。
#
#   📅W 41%/38% ·1.08   週次枠 used% / 窓経過% · ペース（used/elapsed）
#   F≈12%/50% ·0.63     Fable 推定使用率 / 上限% · 上限に対するペース（cache.json より）
#   ⏱5h 24%/61%         5時間枠 used% / 窓経過%（five_hour があるときのみ）
#   🅒 120cr            Codex の窓内消費クレジット（cache.json に codex があるときのみ・薄色）
#   🅒 34%/57% ·0.60    codex_weekly_credits を設定したときは % とペース（週次枠と同じ色）
#
# 設計:
#   - 同期処理は jq + ファイル読みだけ。python は同期で呼ばない（statusline をブロックしない）。
#   - 集計は pace_refresh.py をバックグラウンドで single-flight 起動して行い、表示は
#     var/pace/cache.json を読むだけ。
#   - エラーは表に出さない（失敗した要素は黙って省く）。BASE が空でも pace セグメントは出す。
#   - stdin に rate_limits が無いときはサンプル記録も refresh もしない（表示は `📅W ?`）。
#   - used と resets_at が両方揃った窓だけを有効とする（片方だけの窓は null 記録・`📅W ?` 表示）。
#
# F セグメントの状態:
#   F≈12%/50% ·0.63   通常（cache.json の fable.est_pct / fable.pace）
#   F?                キャッシュ未生成（薄色）／ ·0.63? はキャッシュが TTL×3 より古い（薄色）
#   F?!               pricing.json 未収載モデルがあり Fable 推定不能（警告色）
#   F!                直近の refresh が失敗（cache.json に error）（警告色）
#
# 色の割り当て（pace = used/elapsed。band は config の budget.pace.on_pace_band）:
#   薄色 pace < band[0]（余らせ気味） / 緑 band 内 / 黄 band 超過 /
#   赤 band 超過かつ「枠を使い切る時点が窓の exhaust_margin_pct% 経過より前」
#   ※ ミニ仕様の exhaust_before_reset 式（100/used × elapsed < 100）は pace>1 と数学的に
#     同値で band 判定を飲み込んでしまう（黄が到達不能になる）ため、赤は上記の
#     exhaust_margin_pct（既定80）による読み替えにしている。docs/design.md 参照。
#
# 環境変数（テスト・別配置向け）:
#   FABLE_COST_MANAGER_ROOT     config/ var/ の親ルート（既定: このスクリプトの親の親）
#   FCM_PACE_BASE_STATUSLINE    合成元の statusline スクリプト（既定: license_statusline.sh）
#   FCM_PACE_REFRESH_CMD        バックグラウンド集計コマンド（既定: python3 pace_refresh.py --quiet）
#   FCM_PACE_NOW                現在時刻（epoch 秒）。テスト用の固定時刻。

INPUT="$(cat)"

# symlink 経由で配置されても実体のあるディレクトリを指すよう、bash だけで解決する
# （macOS の readlink には -f が無い。python3 を同期で呼ぶと statusline がブロックする）。
resolve_script_dir() {
  local src="$1" dir
  while [ -L "$src" ]; do
    dir="$(cd -P "$(dirname "$src")" 2>/dev/null && pwd)" || return 1
    src="$(readlink "$src")"
    case "$src" in
      /*) ;;
      *) src="$dir/$src" ;;
    esac
  done
  cd -P "$(dirname "$src")" 2>/dev/null && pwd
}

SCRIPT_DIR="$(resolve_script_dir "${BASH_SOURCE[0]}")" || SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FABLE_COST_MANAGER_ROOT:-$(dirname "$SCRIPT_DIR")}"
PACE_DIR="$ROOT/var/pace"
CONFIG_FILE="$ROOT/config/config.json"
SAMPLES="$PACE_DIR/samples.jsonl"
CACHE="$PACE_DIR/cache.json"
LOCKDIR="$PACE_DIR/refresh.lock"
SAMPLE_LOCKDIR="$PACE_DIR/sample.lock"

RST=$'\033[0m'; DIM=$'\033[2m'

# --- 1. BASE（既存 statusline）を先に確保する -------------------------------
BASE=""
BASE_CMD="${FCM_PACE_BASE_STATUSLINE:-$SCRIPT_DIR/../../license-switch/scripts/license_statusline.sh}"
[ -f "$BASE_CMD" ] && BASE="$(printf '%s' "$INPUT" | bash "$BASE_CMD" 2>/dev/null)"

emit() {
  if [ -n "$BASE" ] && [ -n "$1" ]; then
    printf '%s %s %s' "$BASE" "${DIM}|${RST}" "$1"
  else
    printf '%s' "${BASE}${1}"
  fi
}

# jq が無ければ pace セグメントは諦めて BASE だけ出す
command -v jq >/dev/null 2>&1 || { emit ""; exit 0; }

NOW="${FCM_PACE_NOW:-$(date +%s)}"

# --- 2. 設定値（欠落は既定値） ----------------------------------------------
TTL=300; MIN_INTERVAL=60; CAP=50; BAND_LO=0.8; BAND_HI=1.1; MARGIN=80
# jq の `//` は null/false しか置換しないため、`refresh_ttl_sec: ""` のような空文字はそのまま
# 出てくる。@tsv の空フィールドは IFS=TAB の read で詰められ、以降の値が 1 つずつずれる
# （band が 1 つずれて色が化ける）。全フィールドを tostring して空なら "-" に落とす。
CFG="$(jq -r 'def s(v; d): (v // d) | tostring | if . == "" then "-" else . end;
      (.budget.pace // {}) as $p | [
        s($p.refresh_ttl_sec; 300), s($p.sample_min_interval_sec; 60),
        s($p.fable_cap_pct; 50), s($p.on_pace_band[0]; 0.8), s($p.on_pace_band[1]; 1.1),
        s($p.exhaust_margin_pct; 80) ] | @tsv' "$CONFIG_FILE" 2>/dev/null)"
[ -n "$CFG" ] && IFS=$'\t' read -r TTL MIN_INTERVAL CAP BAND_LO BAND_HI MARGIN <<< "$CFG"

# config に非数値が入っていても壊れない（`[: integer expression expected` を stderr へ漏らさない）
# 先頭が数字であることまで要求する: ".5" を通すと `${TTL%.*}` が空文字になって同じ事故になる。
num_or() {  # num_or <値> <既定値>
  case "$1" in
    ''|[!0-9]*|*[!0-9.]*|*.*.*) printf '%s' "$2" ;;
    *) printf '%s' "$1" ;;
  esac
}
TTL="$(num_or "$TTL" 300)"; MIN_INTERVAL="$(num_or "$MIN_INTERVAL" 60)"
CAP="$(num_or "$CAP" 50)"; BAND_LO="$(num_or "$BAND_LO" 0.8)"
BAND_HI="$(num_or "$BAND_HI" 1.1)"; MARGIN="$(num_or "$MARGIN" 80)"

# --- 3. stdin の rate_limits ------------------------------------------------
# used と resets_at が**両方揃った**窓だけを有効とする（片方だけの窓は無効＝"-"）。
# resets_at は Unix epoch 秒として妥当な範囲（0 < x < 2^31）のものだけ受け付ける
# （ミリ秒値が来ると窓の計算が壊れるため。pace_refresh.valid_resets_at と同じ条件）。
SD_USED="-"; SD_RESET="-"; FH_USED="-"; FH_RESET="-"
RL="$(printf '%s' "$INPUT" | jq -r '
      def w: if (type == "object") and ((.used_percentage | type) == "number")
                and ((.resets_at | type) == "number")
                and (.resets_at > 0) and (.resets_at < 2147483648)
             then [.used_percentage, .resets_at] else ["-", "-"] end;
      ((.rate_limits.seven_day // null) | w) + ((.rate_limits.five_hour // null) | w) | @tsv
      ' 2>/dev/null)"
[ -n "$RL" ] && IFS=$'\t' read -r SD_USED SD_RESET FH_USED FH_RESET <<< "$RL"

HAVE_RL=0
[ "$SD_USED" != "-" ] || [ "$FH_USED" != "-" ] && HAVE_RL=1
# 窓が 1 つも有効でなくても rate_limits 自体があればサンプルは記録する（後から窓が揃うため）
printf '%s' "$INPUT" | jq -e 'has("rate_limits") and (.rate_limits | type == "object")' >/dev/null 2>&1 && HAVE_RL=1

# --- 4. サンプル記録（スロットル付き） --------------------------------------
record_sample() {
  mkdir -p "$PACE_DIR" 2>/dev/null || return 0

  # 「直近行を読む → 追記する」は check-then-act なので、statusline が並行起動されると
  # スロットルが効かない（10 並列で 8 行入ることを実測）。mkdir ロックで直列化する。
  # ロックが取れなければ記録は諦める（best-effort。表示は止めない）。
  # refresh 用ロックとは別物（refresh は数十秒〜数分かかるため保持時間が桁違い）。
  if ! mkdir "$SAMPLE_LOCKDIR" 2>/dev/null; then
    local slm slage
    slm="$(stat -f %m "$SAMPLE_LOCKDIR" 2>/dev/null || stat -c %Y "$SAMPLE_LOCKDIR" 2>/dev/null)"
    [ -n "$slm" ] || return 0
    slage=$(( NOW - slm ))
    # stale ロック（60秒超）は奪う
    [ "$slage" -gt 60 ] || return 0
    rmdir "$SAMPLE_LOCKDIR" 2>/dev/null || return 0
    mkdir "$SAMPLE_LOCKDIR" 2>/dev/null || return 0
  fi
  local last_ts=""
  if [ -s "$SAMPLES" ]; then
    last_ts="$(tail -1 "$SAMPLES" 2>/dev/null | jq -r 'if type == "object" and (.ts | type) == "number" then .ts else empty end' 2>/dev/null)"
  fi
  if [ -n "$last_ts" ]; then
    # 直近サンプルから min_interval 未満なら記録しない（小数を含みうるので awk で比較する）
    if awk -v a="$NOW" -v b="$last_ts" -v m="$MIN_INTERVAL" 'BEGIN{exit !(a - b < m)}'; then
      rmdir "$SAMPLE_LOCKDIR" 2>/dev/null
      return 0
    fi
  fi
  local line
  line="$(printf '%s' "$INPUT" | jq -c --argjson ts "$NOW" '
      def w: if (type == "object") and ((.used_percentage | type) == "number")
                and ((.resets_at | type) == "number")
                and (.resets_at > 0) and (.resets_at < 2147483648)
             then {used: .used_percentage, resets_at: .resets_at} else null end;
      {ts: $ts,
       five_hour: (.rate_limits.five_hour | w),
       seven_day: (.rate_limits.seven_day | w),
       session_id: (.session_id // null),
       model: (.model.id // null)}' 2>/dev/null)"
  [ -n "$line" ] && printf '%s\n' "$line" >> "$SAMPLES" 2>/dev/null
  rmdir "$SAMPLE_LOCKDIR" 2>/dev/null
  return 0
}

# --- 5. バックグラウンド refresh（mkdir による single-flight） ----------------
# macOS には flock コマンドが無いため mkdir ロックに一本化している（両環境で動く）。
maybe_refresh() {
  local age=999999
  if [ -f "$CACHE" ]; then
    local m
    m="$(stat -f %m "$CACHE" 2>/dev/null || stat -c %Y "$CACHE" 2>/dev/null)"
    [ -n "$m" ] && age=$(( NOW - m ))
  fi
  [ "$age" -gt "${TTL%.*}" ] || return 0

  mkdir -p "$PACE_DIR" 2>/dev/null || return 0
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    # stale ロック（10分超）は奪う
    local lm lage
    lm="$(stat -f %m "$LOCKDIR" 2>/dev/null || stat -c %Y "$LOCKDIR" 2>/dev/null)"
    [ -n "$lm" ] || return 0
    lage=$(( NOW - lm ))
    [ "$lage" -gt 600 ] || return 0
    # ディレクトリなら rmdir。何かの拍子に通常ファイル（や symlink）として残った場合も
    # 回収する（放置すると refresh が永久に起動しなくなる）。
    if ! rmdir "$LOCKDIR" 2>/dev/null; then
      [ -d "$LOCKDIR" ] && return 0
      rm -f "$LOCKDIR" 2>/dev/null || return 0
    fi
    mkdir "$LOCKDIR" 2>/dev/null || return 0
  fi
  local cmd="${FCM_PACE_REFRESH_CMD:-python3 \"$SCRIPT_DIR/pace_refresh.py\" --quiet}"
  nohup bash -c "$cmd >/dev/null 2>&1; rmdir \"$LOCKDIR\" 2>/dev/null" </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
  return 0
}

if [ "$HAVE_RL" = "1" ]; then
  record_sample
  maybe_refresh
fi

# --- 6. 表示 ----------------------------------------------------------------
if [ "$SD_USED" = "-" ] && [ "$FH_USED" = "-" ]; then
  emit "${DIM}📅W ?${RST}"
  exit 0
fi

C_EST="-"; C_PACE="-"; C_CAP="$CAP"; C_AT=0; C_UNK=0; C_ERR=0
X_CR="-"; X_PCT="-"; X_PACE="-"
if [ -f "$CACHE" ]; then
  # Codex レーン（cache.json の .codex）も同じ jq 呼び出しで読む（jq の起動回数を増やさない）。
  # .codex が null（台帳なし）なら全て "-" になり、🅒 セグメントは出さない＝既存表示と完全一致。
  # CFG と同じ理由で全フィールドを tostring し、空文字は "-" に落とす（フィールドずれ防止）。
  CJ="$(jq -r 'def s(v; d): (v // d) | tostring | if . == "" then "-" else . end;
               [ s(.fable.est_pct; "-"), s(.fable.pace; "-"), s(.fable.cap_pct; 50),
                 s(.computed_at; 0), (((.unknown_models // []) | length) | tostring),
                 (if .error then "1" else "0" end),
                 s(.codex.window_credits; "-"), s(.codex.used_pct; "-"),
                 s(.codex.pace; "-") ] | @tsv' "$CACHE" 2>/dev/null)"
  [ -n "$CJ" ] && IFS=$'\t' read -r C_EST C_PACE C_CAP C_AT C_UNK C_ERR X_CR X_PCT X_PACE <<< "$CJ"
fi

SEG="$(awk -v sd_used="$SD_USED" -v sd_reset="$SD_RESET" \
           -v fh_used="$FH_USED" -v fh_reset="$FH_RESET" \
           -v now="$NOW" -v est="$C_EST" -v fpace="$C_PACE" -v cap="$C_CAP" -v cat="$C_AT" \
           -v unk="$C_UNK" -v cerr="$C_ERR" \
           -v xcr="$X_CR" -v xpct="$X_PCT" -v xpace="$X_PACE" \
           -v ttl="$TTL" -v blo="$BAND_LO" -v bhi="$BAND_HI" -v margin="$MARGIN" '
function color(u, e,   p) {
  if (e < 1) return DIM;
  p = u / e;
  if (p > bhi) { if (u > 0 && e * 100 / u < margin) return RED; return YEL }
  if (p >= blo) return GRN;
  return DIM;
}
function pacetxt(u, e) { return (e < 1) ? "—" : sprintf("%.2f", u / e) }
function crtxt(c) { return (c >= 100 || c <= -100) ? sprintf("%.0f", c) : sprintf("%.1f", c) }
BEGIN {
  RST = "\033[0m"; DIM = "\033[2m"; GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m";
  WEEK = 604800; FIVEH = 18000;
  out = "";
  ew = -1;
  if (sd_used != "-" && sd_reset != "-") {
    ew = (now - (sd_reset - WEEK)) / WEEK * 100;
    if (ew < 0) ew = 0; if (ew > 100) ew = 100;
    out = color(sd_used, ew) sprintf("📅W %.0f%%/%.0f%% ·%s", sd_used, ew, pacetxt(sd_used, ew)) RST;
  } else {
    # used と resets_at が揃わない窓は W を出せない
    out = DIM "📅W ?" RST;
  }
  if (ew >= 0) {
    if (cerr + 0 == 1) {
      # 直近の集計が失敗している（ネガティブキャッシュ）
      fseg = YEL "F!" RST;
    } else if (unk + 0 > 0) {
      # pricing.json 未収載モデルがあり Fable 推定が不能
      fseg = YEL "F?!" RST;
    } else if (est == "-") {
      fseg = DIM "F?" RST;
    } else {
      ufp = (cap > 0) ? est / cap * 100 : 0;
      fp = (fpace == "-") ? pacetxt(ufp, ew) : sprintf("%.2f", fpace);
      body = sprintf("F≈%.0f%%/%.0f%% ·%s", est, cap, fp);
      if (cat > 0 && now - cat > ttl * 3) fseg = DIM body "?" RST;
      else fseg = color(ufp, ew) body RST;
    }
    out = (out == "") ? fseg : out " " fseg;
  }
  if (fh_used != "-" && fh_reset != "-") {
    e5 = (now - (fh_reset - FIVEH)) / FIVEH * 100;
    if (e5 < 0) e5 = 0; if (e5 > 100) e5 = 100;
    fivseg = DIM sprintf("⏱5h %.0f%%/%.0f%%", fh_used, e5) RST;
    out = (out == "") ? fivseg : out " " fivseg;
  }
  # Codex レーン（cache.json に .codex があるときだけ）。
  #   上限未設定: 🅒 12cr（薄色。Codex の枠は絶対値非公開のため % は出せない）
  #   上限設定時: 🅒 34%/57% ·0.60（週次枠と同じ pace 色）
  if (xcr != "-") {
    if (xpct != "-" && ew >= 0) {
      xp = (xpace == "-") ? pacetxt(xpct, ew) : sprintf("%.2f", xpace);
      xseg = color(xpct, ew) sprintf("🅒 %.0f%%/%.0f%% ·%s", xpct, ew, xp) RST;
    } else {
      xseg = DIM sprintf("🅒 %scr", crtxt(xcr)) RST;
    }
    out = (out == "") ? xseg : out " " xseg;
  }
  printf "%s", out;
}')"

emit "$SEG"
