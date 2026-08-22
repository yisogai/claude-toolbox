#!/usr/bin/env bash
# model_policy_reminder_hook.sh — UserPromptSubmit hook（2本目。handoff_threshold_hook と共存）。層4。
#
# 目的:
#   次の4条件のいずれかのときだけ、毎プロンプトの冒頭に additionalContext を注入する:
#   (1) 緩和中（relaxed）: 残り時間・戻し方（従来動作）
#   (2) settings.json の恒久 model が fable: メインモデル・ドリフト警告（opus 運用への計器）
#   (3) fable 例外の TTL（fable_exempt_until、任意設定・既定無効）の失効48時間前: 失効予告
#   (4) 週次枠のペース（cost-manager の pace キャッシュ）が逼迫／余らせ気味、または Fable が
#       上限ペース超過: effort・並列度の助言。**セッションごとに注入文が変わったときだけ 1 回**。
#   注入の有無（(4) について）:
#     - pace が relaxed_below〜tight_above の中間、または不明（cache 無し/古い/壊れ/未算出）→ 無出力
#     - 余らせ気味／逼迫／Fable 超過 → セッションごとに 1 回注入（同じ文面なら再注入しない）
#   (1)〜(3) が該当せず (4) も上記の無出力条件なら、出力は完全にゼロ（トークンゼロ）。
#   戻し忘れ/ドリフト/失効事故を構造的に防ぐための可視化。
#
# 設計上の厳守事項:
#   - UserPromptSubmit で exit 2 はプロンプトをブロックするため厳禁。何があっても exit 0。
#   - ハートビートは書かない（毎プロンプト発火するため last-agent-hook と混ざらないように。
#     もし将来書くなら last-reminder-hook を使うこと）。
#   - additionalContext は各 hook 独立に加算注入されるため、handoff の閾値通知と共存できる。

INPUT="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0

# --- ポリシー読み取り（インライン展開。層1 と同一ロジック）----------------------
MODE="enforce"; DEFMODEL="opus"; ALLOWED="opus sonnet haiku"; ON_FABLE="deny"; DENY_FORK="true"; RUNTIL="0"; EXEMPT=""; EXUNTIL="0"

# stdin から要る値は 1 回の jq でまとめて取り出す（毎プロンプト走るので呼び出し回数を削る）。
# 行単位で読むので、値に含まれる改行・タブ・NUL は潰しておく（1 フィールドが
# 複数行になると後続フィールドがずれる）。
IN_PARSED="$(printf '%s' "$INPUT" | jq -r '
  def one: tostring | split("\u0000") | join("") | gsub("[\n\r\t]"; " ");
  (.cwd // "" | one),
  (.session_id // "" | one),
  (.transcript_path // "" | one)' 2>/dev/null)"
{
  IFS= read -r CWD; IFS= read -r IN_SID; IFS= read -r IN_TP
} <<EOF
$IN_PARSED
EOF
POLICY_FILE=""
if [ -n "$CWD" ] && [ -f "${CWD}/.claude/model-policy.json" ]; then
  POLICY_FILE="${CWD}/.claude/model-policy.json"
elif [ -f "$HOME/.claude/model-policy/policy.json" ]; then
  POLICY_FILE="$HOME/.claude/model-policy/policy.json"
fi

if [ -n "$POLICY_FILE" ]; then
  # 1 行 1 フィールド抽出（@tsv の空フィールド畳み込み回避。層1 と同一イディオム）
  PARSED="$(jq -r '
    (.mode // "enforce"),
    (.default_model // "opus"),
    ((.allowed // ["opus","sonnet","haiku"]) | join(" ")),
    (.on_fable // "deny"),
    (if .deny_fork == null then true else .deny_fork end | tostring),
    (.relaxed_until // 0),
    ((.fable_exempt_subagent_types // []) | join(" ")),
    (.fable_exempt_until // 0)' "$POLICY_FILE" 2>/dev/null)"
  if [ -n "$PARSED" ]; then
    {
      IFS= read -r p_mode; IFS= read -r p_defmodel; IFS= read -r p_allowed
      IFS= read -r p_onfable; IFS= read -r p_denyfork; IFS= read -r p_runtil
      IFS= read -r p_exempt; IFS= read -r p_exuntil
    } <<EOF
$PARSED
EOF
    case "$p_mode"    in enforce|off) MODE="$p_mode";; esac
    [ -n "$p_defmodel" ] && DEFMODEL="$p_defmodel"
    [ -n "$p_allowed"  ] && ALLOWED="$p_allowed"
    case "$p_onfable" in deny|rewrite) ON_FABLE="$p_onfable";; esac
    case "$p_denyfork" in true|false)  DENY_FORK="$p_denyfork";; esac
    case "$p_runtil"  in ''|*[!0-9]*) RUNTIL=0;; *) RUNTIL="$p_runtil";; esac
    EXEMPT="$(printf '%s' "$p_exempt" | tr '[:upper:]' '[:lower:]')"
    case "$p_exuntil" in ''|*[!0-9]*) EXUNTIL=0;; *) EXUNTIL="$p_exuntil";; esac
  fi
fi

# --- 状態判定 ------------------------------------------------------------------
NOW="$(date +%s)"
if [ "$MODE" = "off" ]; then
  STATE="off"
elif [ "$RUNTIL" -gt "$NOW" ] 2>/dev/null; then
  STATE="relaxed"
else
  STATE="enforce"
fi

# --- 注入メッセージの組み立て（該当なしなら無出力 exit 0 = 平常時トークンゼロ）---
MSGS=""

# (1) 緩和中リマインダー（従来動作）
if [ "$STATE" = "relaxed" ]; then
  REMAIN=$(( (RUNTIL - NOW + 59) / 60 ))          # 残り分（切り上げ）
  UNTIL_H="$(date -r "$RUNTIL" '+%H:%M' 2>/dev/null)"  # 復帰時刻（BSD date）
  MSGS="【モデルポリシー緩和中】サブエージェントのモデル強制が緩和されています（残り約 ${REMAIN} 分、${UNTIL_H} まで）。緩和が不要になったら /model-policy reset で即時 enforce に戻すこと。"
fi

# (2) メインモデル・ドリフト計器: 恒久設定（settings.json の model）が想定外なら警告。
#     Fable メイン運用（2026-07-31〜）では fable（週次枠50%まで）が正、opus（枠消化後の代替）も正。
#     それ以外（sonnet/haiku 等）が恒久化されていたら毎プロンプトで気づかせる。
#     未設定（空）はエイリアス既定に任せる扱いとして警告しない。
SETTINGS_MODEL="$(jq -r '.model // ""' "$HOME/.claude/settings.json" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
# 表示する値はモデル名の形（小文字英数と . -、30 文字以内）に限る。
# settings.json も編集されうるファイルなので、任意テキストを注入文へ verbatim で通さない。
SETTINGS_MODEL_DISP="$SETTINGS_MODEL"
case "$SETTINGS_MODEL" in
  *[!a-z0-9.-]*) SETTINGS_MODEL_DISP="（表示できない値）";;
esac
[ "${#SETTINGS_MODEL_DISP}" -gt 30 ] && SETTINGS_MODEL_DISP="（表示できない値）"
case "$SETTINGS_MODEL" in
  *fable*|*opus*|"") : ;;
  *)
    MSGS="${MSGS:+$MSGS
}【メインモデル注意】settings.json の恒久 model が ${SETTINGS_MODEL_DISP} です。運用方針はメイン=fable（週次枠50%まで。枠消化後は opus）。意図的な設定でなければ fable へ戻すこと。"
    ;;
esac

# (3) fable 例外のまもなく失効警告（残り48時間未満のときだけ。平常時は無出力を保つ）
if [ -n "$EXEMPT" ] && [ "$EXUNTIL" -gt "$NOW" ] 2>/dev/null; then
  EX_REMAIN_H=$(( (EXUNTIL - NOW) / 3600 ))
  if [ "$EX_REMAIN_H" -lt 48 ]; then
    MSGS="${MSGS:+$MSGS
}【fable例外まもなく失効】fable 例外（${EXEMPT}）が残り約 ${EX_REMAIN_H} 時間で失効し、以降 fable-advisor は deny されます。Fable の課金条件（サブスク内か従量か）を確認のうえ、継続するなら model_policy.sh exempt 14 で延長すること。"
  fi
fi

# --- (4) 週次ペース通知（tuning の review_by_pace に連動）------------------------
#   cost-manager の pace キャッシュ（seven_day.pace / fable.pace）を読み、
#   逼迫／余らせ気味／Fable 超過のときだけ助言を注入する。
#   同じ状態では再注入しない（セッションごとに「最後に通知した状態」を保存）。
#   unknown（cache 無し・古い・壊れ・pace 未算出）は通知せず、状態も保存しない。
USER_TUNING="$HOME/.claude/model-policy/tuning.json"
PROJ_TUNING=""
[ -n "$CWD" ] && [ -f "${CWD}/.claude/model-policy-tuning.json" ] \
  && PROJ_TUNING="${CWD}/.claude/model-policy-tuning.json"

# 内蔵既定は CLI と共有の scripts/tuning_defaults.json を読む（二重管理を避ける）。
# 読めない場合だけ、ここのハードコード値にフォールバックする。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
DEFAULTS_FILE="${SELF_DIR}/tuning_defaults.json"
RBP_ENABLED="true"
RBP_SOURCE="$HOME/Documents/personal/tools/claude-toolbox/cost-manager/var/pace/cache.json"
RBP_RELAXED="1.0"; RBP_TIGHT="1.1"; RBP_WHEN_TIGHT="high"; RBP_MAXAGE="1800"
STATIC_REVIEW="xhigh"

# jq --argjson に渡してよい数値か（"1.2.3" や "." を弾く）
is_num() { case "$1" in ''|.|*[!0-9.]*|*.*.*) return 1;; *) return 0;; esac; }

# effort 語彙の強さ（比較用。語彙外は 0）。CLI の effort_rank と同一。
effort_rank() {
  case "$1" in
    minimal) printf '1';; low) printf '2';; medium) printf '3';;
    high)    printf '4';; xhigh) printf '5';; *) printf '0';;
  esac
}

# 先頭の ~/ を展開（tuning_defaults.json は実ホーム名を持たない）
expand_tilde() {
  case "$1" in
    "~/"*) printf '%s' "$HOME/${1#\~/}";;
    *)     printf '%s' "$1";;
  esac
}

# tuning JSON（$1 = ファイルパス）を 1 キーずつ 7 行に落として読み、
# **妥当な値だけ**を採用する。1 キーの型違いで全体を落とさないよう try/catch で包み、
# 改行・タブは空白へ潰す（行単位で読む前提と、注入文への任意文字列混入を防ぐため）。
read_tuning_file() { # $1=ファイル $2=source を採用するか（yes/no）
  local f="$1" take_src="$2" out
  [ -f "$f" ] || return 1
  out="$(jq -r '
    def s(p): (try (getpath(p)) catch null) as $v
      | if $v == null or ($v|type) == "object" or ($v|type) == "array" then ""
        else ($v | tostring | split("\u0000") | join("") | gsub("[\n\r\t]"; " ")) end;
    s(["review_by_pace","enabled"]),
    s(["review_by_pace","source"]),
    s(["review_by_pace","relaxed_below"]),
    s(["review_by_pace","tight_above"]),
    s(["review_by_pace","effort_when_tight"]),
    s(["review_by_pace","max_age_sec"]),
    s(["effort","review"])' "$f" 2>/dev/null)"
  [ -n "$out" ] || return 1
  local t_en t_src t_rb t_tb t_wt t_ma t_rev
  {
    IFS= read -r t_en; IFS= read -r t_src; IFS= read -r t_rb; IFS= read -r t_tb
    IFS= read -r t_wt; IFS= read -r t_ma; IFS= read -r t_rev
  } <<EOF
$out
EOF
  case "$t_en" in true|false) RBP_ENABLED="$t_en";; esac
  if [ "$take_src" = "yes" ] && [ -n "$t_src" ]; then RBP_SOURCE="$(expand_tilde "$t_src")"; fi
  is_num "$t_rb" && RBP_RELAXED="$t_rb"
  is_num "$t_tb" && RBP_TIGHT="$t_tb"
  # effort 値は語彙で検証する。非語彙（＝任意テキスト）は内蔵既定のまま使い、注入文に入れない。
  case "$t_wt"  in minimal|low|medium|high|xhigh) RBP_WHEN_TIGHT="$t_wt";; esac
  is_num "$t_ma" && RBP_MAXAGE="$t_ma"
  case "$t_rev" in minimal|low|medium|high|xhigh) STATIC_REVIEW="$t_rev";; esac
  return 0
}

# 1. 内蔵既定（共有ファイル）
if [ -f "$DEFAULTS_FILE" ]; then
  read_tuning_file "$DEFAULTS_FILE" yes || true
fi
# 2. ユーザー側 tuning.json。ここまでが「基準」。
if [ -f "$USER_TUNING" ]; then
  read_tuning_file "$USER_TUNING" yes || true
fi
# 3. プロジェクト側ファイル。クローンしたリポジトリにも置けるので、
#    **安全な方向の変更だけ**を採用する（CLI の read_tuning と同じ規則）:
#      - source は常に無視（細工した cache で逼迫/余裕の通知を操作されないため）
#      - effort は基準と同等以上のみ（引き下げは無視）
#      - review_by_pace.enabled は true のみ（無効化は無視）
#      - review_by_pace.tight_above は基準以上のみ（引き下げると常に「逼迫」になり、
#        実効 effort が effort_when_tight へ落ちる＝effort 引き下げの抜け道になる）
if [ -n "$PROJ_TUNING" ]; then
  B_EN="$RBP_ENABLED"; B_WT="$RBP_WHEN_TIGHT"; B_REV="$STATIC_REVIEW"; B_SRC="$RBP_SOURCE"
  B_TB="$RBP_TIGHT"
  read_tuning_file "$PROJ_TUNING" no || true
  RBP_SOURCE="$B_SRC"
  if is_num "$RBP_TIGHT" && is_num "$B_TB" \
     && LC_ALL=C awk -v a="$RBP_TIGHT" -v b="$B_TB" 'BEGIN{exit !(a<b)}'; then
    RBP_TIGHT="$B_TB"
  fi
  [ "$(effort_rank "$RBP_WHEN_TIGHT")" -lt "$(effort_rank "$B_WT")" ] && RBP_WHEN_TIGHT="$B_WT"
  [ "$(effort_rank "$STATIC_REVIEW")"  -lt "$(effort_rank "$B_REV")" ] && STATIC_REVIEW="$B_REV"
  [ "$RBP_ENABLED" = "false" ] && [ "$B_EN" = "true" ] && RBP_ENABLED="true"
fi
# 3. 環境変数による明示上書き（テスト・別ホストからの利用）
[ -n "${TUNING_PACE_SOURCE:-}" ] && RBP_SOURCE="$TUNING_PACE_SOURCE"

PACE_BAND="unknown"; FABLE_BAND="unknown"
PACE_VAL="-"; PACE_PROJ="-"; FABLE_VAL="-"; FABLE_CAP="-"
if [ "$RBP_ENABLED" = "true" ] && [ -n "$RBP_SOURCE" ] && [ -f "$RBP_SOURCE" ]; then
  P_OUT="$(jq -r \
      --argjson rb "$RBP_RELAXED" --argjson tb "$RBP_TIGHT" \
      --argjson ma "$RBP_MAXAGE" --argjson now "$NOW" '
    def fin(p): (p|type) == "number"
                and ((p|isnan)|not) and ((p|isinfinite)|not)
                and ((p|fabs) < 1e15);
    def bandof(p): if (fin(p)|not) then "unknown"
                   elif p >= $tb then "tight"
                   elif p < $rb then "relaxed"
                   else "mid" end;
    def numstr(p): if fin(p) then (p|tostring) else "-" end;
    (.computed_at? // null) as $ca
    | (if fin($ca) then ($now - $ca) else null end) as $age
    | (if (fin($age)|not) or $age > $ma then "old" else "ok" end),
      bandof(.seven_day.pace? // null),
      numstr(.seven_day.pace? // null),
      numstr(.seven_day.projected_end_pct? // null),
      bandof(.fable.pace? // null),
      numstr(.fable.pace? // null),
      numstr(.fable.cap_pct? // null)
  ' "$RBP_SOURCE" 2>/dev/null)"
  if [ -n "$P_OUT" ]; then
    {
      IFS= read -r p_fresh; IFS= read -r PACE_BAND; IFS= read -r PACE_VAL; IFS= read -r PACE_PROJ
      IFS= read -r FABLE_BAND; IFS= read -r FABLE_VAL; IFS= read -r FABLE_CAP
    } <<EOF
$P_OUT
EOF
    if [ "$p_fresh" != "ok" ]; then PACE_BAND="unknown"; FABLE_BAND="unknown"; fi
  fi
fi

# 数値整形（不正値は素通し。printf の失敗で hook を落とさない）。
# 小数点はロケール依存（de_DE 等では "1.25" が不正な数として弾かれる）ため LC_ALL=C で固定。
fmt_num() {
  case "$2" in
    ''|-|*[!0-9.eE+-]*) printf '%s' "${2:--}";;
    *) LC_ALL=C printf "$1" "$2" 2>/dev/null || printf '%s' "$2";;
  esac
}

PACE_MSGS=""
case "$PACE_BAND" in
  tight)
    PACE_MSGS="【ペース】週次 $(fmt_num '%.2f' "$PACE_VAL")x（逼迫）。レビュー/検証の effort は ${RBP_WHEN_TIGHT}（自動）。並列 fan-out は控えめに。"
    ;;
  relaxed)
    PACE_MSGS="【ペース】週次 $(fmt_num '%.2f' "$PACE_VAL")x（余らせ気味、週末見込み $(fmt_num '%.0f' "$PACE_PROJ")%）。Workflow の並列度・effort を上げる余地あり（レビューは ${STATIC_REVIEW}）。"
    ;;
esac
if [ "$FABLE_BAND" = "tight" ]; then
  PACE_MSGS="${PACE_MSGS:+$PACE_MSGS
}【Fable ペース】上限 $(fmt_num '%.0f' "$FABLE_CAP")% に対し $(fmt_num '%.2f' "$FABLE_VAL")x。委譲を増やす／メインの effort を下げる。"
fi

if [ -n "$PACE_MSGS" ]; then
  # セッション識別（session_id → transcript_path の basename → unknown）。パス片は無害化する。
  SID="${IN_SID:-}"
  if [ -z "$SID" ] && [ -n "${IN_TP:-}" ]; then
    SID="${IN_TP##*/}"
  fi
  [ -z "$SID" ] && SID="unknown"
  SID="$(printf '%s' "$SID" | sed 's/[^A-Za-z0-9._-]/_/g' | cut -c1-128)"

  STATE_DIR="$HOME/.claude/model-policy/reminder-state"
  STATE_FILE="${STATE_DIR}/${SID}.json"
  # 状態キーは「実際に注入する文そのもの」のハッシュ。バンドの組だと、出力に影響しない差
  # （例: fable 0.99↔1.01）で状態が変わり、同じ文を再注入してしまうため。
  CUR_STATE="$(printf '%s' "$PACE_MSGS" | cksum 2>/dev/null | tr -s ' ' '-' | tr -d '\n')"
  # cksum が無い環境ではバンドの組だけになるが、それだと文面が変わっても（例:
  # effort_when_tight の変更）状態が同じに見えて再注入されない。文長を混ぜて識別性を上げる。
  [ -z "$CUR_STATE" ] && CUR_STATE="${PACE_BAND}|${FABLE_BAND}|${#PACE_MSGS}"
  PREV_STATE="$(jq -r '.state // empty' "$STATE_FILE" 2>/dev/null)"
  if [ "$CUR_STATE" != "$PREV_STATE" ]; then
    # state を保存できたときだけ注入する。書けないまま注入すると毎プロンプト再注入になる
    # （トークンゼロの原則が壊れる）ので、書込に失敗したら今回は黙って見送る（次回持ち越し）。
    STATE_SAVED=no
    mkdir -p "$STATE_DIR" 2>/dev/null
    if [ -d "$STATE_DIR" ]; then
      STATE_TMP="$(mktemp "${TMPDIR:-/tmp}/model-policy-reminder.XXXXXX" 2>/dev/null)"
      if [ -n "$STATE_TMP" ]; then
        if jq -n --arg s "$CUR_STATE" --argjson at "$NOW" '{state:$s, at:$at}' > "$STATE_TMP" 2>/dev/null \
           && mv "$STATE_TMP" "$STATE_FILE" 2>/dev/null; then
          STATE_SAVED=yes
        else
          rm -f "$STATE_TMP" 2>/dev/null
        fi
      fi
    fi
    if [ "$STATE_SAVED" = "yes" ]; then
      MSGS="${MSGS:+$MSGS
}${PACE_MSGS}"
      # 古い state ファイルの掃除（7 日超）。state を書いたときだけでよい（毎プロンプト
      # find を走らせない）。失敗しても無視する。
      find "$STATE_DIR" -type f -mtime +7 -delete >/dev/null 2>&1 || true
    fi
  fi
fi

[ -n "$MSGS" ] && \
  jq -n --arg msg "$MSGS" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$msg}}' 2>/dev/null

exit 0
