#!/usr/bin/env bash
# vision/tests/canary.sh — render.sh / check.sh / geometry.js の
# 決定論カナリア（実モデル呼び出しなし）。
#
# tests/canary.sh の PASS/FAIL 出力の流儀を踏襲する。
# 使い方: bash vision/tests/canary.sh
# 全項目 green なら exit 0、1件でも FAIL があれば exit 1。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VISION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"

RENDER="$VISION_DIR/render.sh"
CHECK="$VISION_DIR/check.sh"
FIXED_SVG="$FIXTURES_DIR/fixed.svg"
BROKEN_SVG="$FIXTURES_DIR/broken.svg"
ASSERTIONS="$FIXTURES_DIR/canary-assertions.json"
FORGED_SVG="$FIXTURES_DIR/forged-results.svg"
FORGED_ASSERTIONS="$FIXTURES_DIR/forged-results-assertions.json"
ARROW_HEAD_SVG="$FIXTURES_DIR/arrow-head-mismatch.svg"
ARROW_HEAD_ASSERTIONS="$FIXTURES_DIR/arrow-head-mismatch-assertions.json"
MIRROR_OCCLUSION_SVG="$FIXTURES_DIR/mirror-and-occlusion.svg"
MIRROR_OCCLUSION_ASSERTIONS="$FIXTURES_DIR/mirror-and-occlusion-assertions.json"
ARROW_AUTO_SVG="$FIXTURES_DIR/arrow-auto-head.svg"
ARROW_AUTO_ASSERTIONS="$FIXTURES_DIR/arrow-auto-head-assertions.json"
NO_MIRROR_HTML="$FIXTURES_DIR/no-mirror-html-unsupported.html"
NO_MIRROR_HTML_ASSERTIONS="$FIXTURES_DIR/no-mirror-html-unsupported-assertions.json"
FORALL_PASS_HTML="$FIXTURES_DIR/forall-pass.html"
FORALL_FAIL_HTML="$FIXTURES_DIR/forall-fail.html"
FORALL_ASSERTIONS="$FIXTURES_DIR/forall-assertions.json"
FORALL_NUANCE_HTML="$FIXTURES_DIR/forall-nuance.html"
FORALL_NUANCE_ASSERTIONS="$FIXTURES_DIR/forall-nuance-assertions.json"
SIZE_PROBE_HTML="$FIXTURES_DIR/size-probe.html"
SIZE_PROBE_500_ASSERTIONS="$FIXTURES_DIR/size-probe-500x400-assertions.json"
SIZE_PROBE_900_ASSERTIONS="$FIXTURES_DIR/size-probe-900x700-assertions.json"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fablize-vision-canary.XXXXXX")"
# FAIL が出た場合は WORK_DIR を消さずに残す（flake/failure 時のログを事後解析
# できるようにするため）。全 green のときだけ通常どおり掃除する。
cleanup_work_dir() {
  if [[ "${FAIL:-1}" -eq 0 ]]; then
    rm -rf "$WORK_DIR"
  else
    echo "note: FAIL があったため WORK_DIR を残しています: $WORK_DIR" >&2
  fi
}
trap cleanup_work_dir EXIT

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); printf '  PASS: %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL: %s\n' "$1"; }

echo "== canary: vision (render.sh / check.sh / geometry.js) =="

# ------------------------------------------------------------------------
# a. check.sh fixed.svg → exit 0、全 assertion pass
# ------------------------------------------------------------------------
{
  OUT_A="$WORK_DIR/fixed-results.json"
  bash "$CHECK" "$FIXED_SVG" "$ASSERTIONS" --out "$OUT_A" >"$WORK_DIR/a.stderr.log" 2>&1
  RC_A=$?

  if [[ "$RC_A" -ne 0 ]]; then
    fail "a: check.sh fixed.svg の exit code = $RC_A (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/a.stderr.log")"
  else
    pass "a: check.sh fixed.svg の exit code = 0"
  fi

  if [[ -f "$OUT_A" ]]; then
    FAIL_COUNT_A="$(jq -r '.fail_count' "$OUT_A" 2>/dev/null)"
    ALL_PASS_A="$(jq -r '[.results[] | select(.pass != true)] | length' "$OUT_A" 2>/dev/null)"
    if [[ "$FAIL_COUNT_A" == "0" && "$ALL_PASS_A" == "0" ]]; then
      pass "a: fixed.svg の全 assertion が pass（fail_count=0, per-assertion 検証も0件fail）"
    else
      fail "a: fixed.svg で pass しないアサーションがある (fail_count=$FAIL_COUNT_A, non-pass件数=$ALL_PASS_A)"
    fi
  else
    fail "a: results.json が生成されなかった: $OUT_A"
  fi
}

# ------------------------------------------------------------------------
# b. check.sh broken.svg → exit 1、fail する assertion の id 集合が
#    期待とちょうど一致（偽陽性・偽陰性の同時検出）
# ------------------------------------------------------------------------
{
  OUT_B="$WORK_DIR/broken-results.json"
  bash "$CHECK" "$BROKEN_SVG" "$ASSERTIONS" --out "$OUT_B" >"$WORK_DIR/b.stderr.log" 2>&1
  RC_B=$?

  if [[ "$RC_B" -ne 1 ]]; then
    fail "b: check.sh broken.svg の exit code = $RC_B (期待値 1)。ログ: $(tail -n 5 "$WORK_DIR/b.stderr.log")"
  else
    pass "b: check.sh broken.svg の exit code = 1"
  fi

  EXPECTED_FAIL_IDS="conn-ab-end arrow-bc-direction arrow-bc-start arrow-bc-end"
  if [[ -f "$OUT_B" ]]; then
    ACTUAL_FAIL_IDS="$(jq -r '[.results[] | select(.pass != true) | .id] | sort | join(" ")' "$OUT_B" 2>/dev/null)"
    EXPECTED_SORTED="$(printf '%s\n' $EXPECTED_FAIL_IDS | sort | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$ACTUAL_FAIL_IDS" == "$EXPECTED_SORTED" ]]; then
      pass "b: broken.svg で fail した assertion id 集合が期待と一致: [$ACTUAL_FAIL_IDS]"
    else
      fail "b: broken.svg の fail id 集合が不一致。期待=[$EXPECTED_SORTED] 実際=[$ACTUAL_FAIL_IDS]"
    fi
  else
    fail "b: results.json が生成されなかった: $OUT_B"
  fi
}

# ------------------------------------------------------------------------
# c. render.sh fixed.svg → PNG が存在し 0 バイトでなく、寸法が 3200x1800
# ------------------------------------------------------------------------
{
  PNG_C="$WORK_DIR/fixed.png"
  bash "$RENDER" "$FIXED_SVG" "$PNG_C" >"$WORK_DIR/c.stderr.log" 2>&1
  RC_C=$?

  if [[ "$RC_C" -ne 0 ]]; then
    fail "c: render.sh fixed.svg の exit code = $RC_C (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/c.stderr.log")"
  elif [[ ! -s "$PNG_C" ]]; then
    fail "c: PNG が生成されなかった、または 0 バイト: $PNG_C"
  else
    DIMS_C="$(sips -g pixelWidth -g pixelHeight "$PNG_C" 2>/dev/null | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w"x"h}')"
    if [[ "$DIMS_C" == "3200x1800" ]]; then
      pass "c: render.sh fixed.svg → 3200x1800 の PNG が生成された"
    else
      fail "c: render.sh fixed.svg の寸法が期待と不一致 (got=$DIMS_C, expected=3200x1800)"
    fi
  fi
}

# ------------------------------------------------------------------------
# d. render.sh broken.svg → 同上（壊れた図もレンダリング自体は成功する）
# ------------------------------------------------------------------------
{
  PNG_D="$WORK_DIR/broken.png"
  bash "$RENDER" "$BROKEN_SVG" "$PNG_D" >"$WORK_DIR/d.stderr.log" 2>&1
  RC_D=$?

  if [[ "$RC_D" -ne 0 ]]; then
    fail "d: render.sh broken.svg の exit code = $RC_D (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/d.stderr.log")"
  elif [[ ! -s "$PNG_D" ]]; then
    fail "d: PNG が生成されなかった、または 0 バイト: $PNG_D"
  else
    DIMS_D="$(sips -g pixelWidth -g pixelHeight "$PNG_D" 2>/dev/null | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w"x"h}')"
    if [[ "$DIMS_D" == "3200x1800" ]]; then
      pass "d: render.sh broken.svg → 3200x1800 の PNG が生成された（壊れた図もレンダリングは成功する）"
    else
      fail "d: render.sh broken.svg の寸法が期待と不一致 (got=$DIMS_D, expected=3200x1800)"
    fi
  fi
}

# ------------------------------------------------------------------------
# e. 存在しないセレクタを含む spec → exit 1（graceful fail、クラッシュしない）
# ------------------------------------------------------------------------
{
  BAD_SELECTOR_SPEC="$WORK_DIR/bad-selector.json"
  cat > "$BAD_SELECTOR_SPEC" <<'JSON'
{"assertions":[{"id":"nope","type":"exists","selector":"#this-selector-does-not-exist"}]}
JSON
  OUT_E="$WORK_DIR/bad-selector-results.json"
  bash "$CHECK" "$FIXED_SVG" "$BAD_SELECTOR_SPEC" --out "$OUT_E" >"$WORK_DIR/e.stderr.log" 2>&1
  RC_E=$?

  if [[ "$RC_E" -eq 1 ]]; then
    MSG_E="$(jq -r '.results[0].message' "$OUT_E" 2>/dev/null)"
    if [[ "$MSG_E" == *"selector not found"* ]]; then
      pass "e: 存在しないセレクタ → exit 1、graceful fail（message: ${MSG_E}）"
    else
      fail "e: exit 1 だったが message の中身が想定外: $MSG_E"
    fi
  else
    fail "e: 存在しないセレクタなのに exit code = $RC_E (期待値 1)。クラッシュ or 黙って成功した可能性。ログ: $(tail -n 5 "$WORK_DIR/e.stderr.log")"
  fi
}

# ------------------------------------------------------------------------
# f. assertions が空配列の spec → exit 2
# ------------------------------------------------------------------------
{
  EMPTY_SPEC="$WORK_DIR/empty.json"
  printf '%s' '{"assertions":[]}' > "$EMPTY_SPEC"
  bash "$CHECK" "$FIXED_SVG" "$EMPTY_SPEC" >"$WORK_DIR/f.stdout.log" 2>"$WORK_DIR/f.stderr.log"
  RC_F=$?

  if [[ "$RC_F" -eq 2 ]]; then
    pass "f: assertions が空配列 → exit 2（黙って合格にしない）"
  else
    fail "f: assertions が空配列なのに exit code = $RC_F (期待値 2)。ログ: $(tail -n 5 "$WORK_DIR/f.stderr.log")"
  fi
}

# ------------------------------------------------------------------------
# g. CHROME_BIN=/bin/false で check.sh → exit 2（Chrome 故障を黙って
#    成功にしない）
# ------------------------------------------------------------------------
{
  CHROME_BIN=/bin/false bash "$CHECK" "$FIXED_SVG" "$ASSERTIONS" >"$WORK_DIR/g.stdout.log" 2>"$WORK_DIR/g.stderr.log"
  RC_G=$?

  if [[ "$RC_G" -eq 2 ]]; then
    pass "g: CHROME_BIN=/bin/false → exit 2（Chrome 故障を黙って成功にしない）"
  else
    fail "g: CHROME_BIN=/bin/false なのに exit code = $RC_G (期待値 2)。ログ: $(tail -n 5 "$WORK_DIR/g.stderr.log")"
  fi
}

# ------------------------------------------------------------------------
# h. 入力ファイル不在 → render.sh / check.sh とも非0で明確なエラー
# ------------------------------------------------------------------------
{
  MISSING="$WORK_DIR/does-not-exist.svg"

  bash "$RENDER" "$MISSING" "$WORK_DIR/h-render-out.png" >"$WORK_DIR/h-render.stdout.log" 2>"$WORK_DIR/h-render.stderr.log"
  RC_H_RENDER=$?
  if [[ "$RC_H_RENDER" -ne 0 ]] && grep -qi "見つかりません\|not found\|no such file" "$WORK_DIR/h-render.stderr.log"; then
    pass "h: render.sh 入力ファイル不在 → 非0終了、明確なエラーメッセージ"
  else
    fail "h: render.sh 入力ファイル不在の挙動が想定外 (rc=$RC_H_RENDER)。ログ: $(tail -n 5 "$WORK_DIR/h-render.stderr.log")"
  fi

  bash "$CHECK" "$MISSING" "$ASSERTIONS" >"$WORK_DIR/h-check.stdout.log" 2>"$WORK_DIR/h-check.stderr.log"
  RC_H_CHECK=$?
  if [[ "$RC_H_CHECK" -eq 2 ]] && grep -qi "見つかりません\|not found\|no such file" "$WORK_DIR/h-check.stderr.log"; then
    pass "h: check.sh 入力ファイル不在 → exit 2、明確なエラーメッセージ"
  else
    fail "h: check.sh 入力ファイル不在の挙動が想定外 (rc=$RC_H_CHECK)。ログ: $(tail -n 5 "$WORK_DIR/h-check.stderr.log")"
  fi
}

# ------------------------------------------------------------------------
# i. 偽の __fablize_results 要素を仕込んだ入力 → 偽合格せず、実ジオメトリに
#    基づいて素直に FAIL する（exit 1）。結果チャネルの入力からの分離の回帰
#    テスト。
# ------------------------------------------------------------------------
{
  OUT_I="$WORK_DIR/forged-results.json"
  bash "$CHECK" "$FORGED_SVG" "$FORGED_ASSERTIONS" --out "$OUT_I" >"$WORK_DIR/i.stderr.log" 2>&1
  RC_I=$?

  if [[ "$RC_I" -ne 1 ]]; then
    fail "i: 偽の結果要素を仕込んだ入力の exit code = $RC_I (期待値 1 = 偽合格せず実ジオメトリでFAIL)。ログ: $(tail -n 5 "$WORK_DIR/i.stderr.log")"
  else
    pass "i: 偽の結果要素を仕込んだ入力 → exit 1（偽合格しない）"
  fi

  if [[ -f "$OUT_I" ]]; then
    MSG_I="$(jq -r '.results[0].message' "$OUT_I" 2>/dev/null)"
    PASS_COUNT_I="$(jq -r '.pass_count' "$OUT_I" 2>/dev/null)"
    if [[ "$MSG_I" == *"forged"* ]]; then
      fail "i: 結果 JSON の message が偽要素由来の文言 'forged' を含んでいる（偽物が採用された可能性）: $MSG_I"
    elif [[ "$PASS_COUNT_I" == "1" ]]; then
      fail "i: pass_count=1（偽要素の pass_count がそのまま採用された可能性）"
    else
      pass "i: 結果 JSON は偽要素由来ではなく実ジオメトリの評価結果（message=${MSG_I}, pass_count=${PASS_COUNT_I}）"
    fi
  else
    fail "i: results.json が生成されなかった: $OUT_I"
  fi
}

# ------------------------------------------------------------------------
# j. --out の出力先ディレクトリが存在しない → 自動で mkdir -p され、
#    全 assertion pass なら exit 0（従来は偽 fail=exit 1 になっていた）
# ------------------------------------------------------------------------
{
  OUT_DIR_J="$WORK_DIR/nested/does/not/exist/yet"
  OUT_J="$OUT_DIR_J/r.json"
  bash "$CHECK" "$FIXED_SVG" "$ASSERTIONS" --out "$OUT_J" >"$WORK_DIR/j.stderr.log" 2>&1
  RC_J=$?

  if [[ "$RC_J" -ne 0 ]]; then
    fail "j: --out の親ディレクトリ未作成時の exit code = $RC_J (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/j.stderr.log")"
  elif [[ ! -f "$OUT_J" ]]; then
    fail "j: --out 先へ結果ファイルが作成されなかった: $OUT_J"
  else
    pass "j: --out の親ディレクトリが無くても自動作成され exit 0（偽 fail にならない）"
  fi
}

# ------------------------------------------------------------------------
# k. arrow_direction の head パラメータ／endpoints_touch: line の座標記述順
#    と矢頭の実位置が食い違う reasonable な描き方でも、head/endpoints_touch
#    を使えば視覚どおりに正しく判定できる（座標記述順ベースの既定挙動との
#    後方互換性も同時に確認する）
# ------------------------------------------------------------------------
{
  OUT_K="$WORK_DIR/arrow-head-mismatch-results.json"
  bash "$CHECK" "$ARROW_HEAD_SVG" "$ARROW_HEAD_ASSERTIONS" --out "$OUT_K" >"$WORK_DIR/k.stderr.log" 2>&1
  RC_K=$?

  if [[ "$RC_K" -ne 0 ]]; then
    fail "k: arrow-head-mismatch.svg の exit code = $RC_K (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/k.stderr.log")"
  else
    pass "k: arrow-head-mismatch.svg の exit code = 0"
  fi

  if [[ -f "$OUT_K" ]]; then
    ALL_PASS_K="$(jq -r '[.results[] | select(.pass != true)] | length' "$OUT_K" 2>/dev/null)"
    if [[ "$ALL_PASS_K" == "0" ]]; then
      pass "k: head/endpoints_touch を含む全 assertion が pass（座標記述順と矢頭実位置の食い違いを正しく扱えている）"
    else
      fail "k: arrow-head-mismatch.svg で pass しないアサーションがある (non-pass件数=$ALL_PASS_K)"
    fi
  else
    fail "k: results.json が生成されなかった: $OUT_K"
  fi
}

# ------------------------------------------------------------------------
# l. no_mirror / visible_at_center: 鏡映テキスト・180度回転・不透明矩形に
#    よる遮蔽の偽陽性・偽陰性を同時に検証する（fail id 集合の完全一致）。
# ------------------------------------------------------------------------
{
  OUT_L="$WORK_DIR/mirror-occlusion-results.json"
  bash "$CHECK" "$MIRROR_OCCLUSION_SVG" "$MIRROR_OCCLUSION_ASSERTIONS" --out "$OUT_L" >"$WORK_DIR/l.stderr.log" 2>&1
  RC_L=$?

  if [[ "$RC_L" -ne 1 ]]; then
    fail "l: check.sh mirror-and-occlusion.svg の exit code = $RC_L (期待値 1)。ログ: $(tail -n 5 "$WORK_DIR/l.stderr.log")"
  else
    pass "l: check.sh mirror-and-occlusion.svg の exit code = 1"
  fi

  EXPECTED_FAIL_IDS_L="mirror-mirrored-fail visible-node-hidden-fail"
  if [[ -f "$OUT_L" ]]; then
    ACTUAL_FAIL_IDS_L="$(jq -r '[.results[] | select(.pass != true) | .id] | sort | join(" ")' "$OUT_L" 2>/dev/null)"
    EXPECTED_SORTED_L="$(printf '%s\n' $EXPECTED_FAIL_IDS_L | sort | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$ACTUAL_FAIL_IDS_L" == "$EXPECTED_SORTED_L" ]]; then
      pass "l: mirror-and-occlusion.svg で fail した assertion id 集合が期待と一致（鏡映テキストと遮蔽要素のみ fail、180度回転と非遮蔽要素は pass）: [$ACTUAL_FAIL_IDS_L]"
    else
      fail "l: mirror-and-occlusion.svg の fail id 集合が不一致。期待=[$EXPECTED_SORTED_L] 実際=[$ACTUAL_FAIL_IDS_L]"
    fi
  else
    fail "l: results.json が生成されなかった: $OUT_L"
  fi
}

# ------------------------------------------------------------------------
# m. arrow_direction の head:"auto": marker-end 矢印・marker-start 矢印
#    （orient="auto-start-reverse" 慣用形／orient="auto" 非慣用形）・
#    marker なし矢印のいずれも視覚どおりの向きを正しく pass/fail すること
#    （常に pass する偽陽性ではないことも wrongfail ケースで確認する）。
#    加えて marker-start の orient が auto/auto-start-reverse のどちらでも
#    ない（固定角度・省略時既定値）場合は、座標だけから視覚上の向きを
#    判定できないため黙って推測せず明確な理由で fail することも確認する。
# ------------------------------------------------------------------------
{
  OUT_M="$WORK_DIR/arrow-auto-head-results.json"
  bash "$CHECK" "$ARROW_AUTO_SVG" "$ARROW_AUTO_ASSERTIONS" --out "$OUT_M" >"$WORK_DIR/m.stderr.log" 2>&1
  RC_M=$?

  if [[ "$RC_M" -ne 1 ]]; then
    fail "m: check.sh arrow-auto-head.svg の exit code = $RC_M (期待値 1)。ログ: $(tail -n 5 "$WORK_DIR/m.stderr.log")"
  else
    pass "m: check.sh arrow-auto-head.svg の exit code = 1"
  fi

  EXPECTED_FAIL_IDS_M="arrow-a-auto-up-wrongfail arrow-d-auto-up-wrongfail arrow-e-auto-undetectable-fail"
  if [[ -f "$OUT_M" ]]; then
    ACTUAL_FAIL_IDS_M="$(jq -r '[.results[] | select(.pass != true) | .id] | sort | join(" ")' "$OUT_M" 2>/dev/null)"
    EXPECTED_SORTED_M="$(printf '%s\n' $EXPECTED_FAIL_IDS_M | sort | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$ACTUAL_FAIL_IDS_M" == "$EXPECTED_SORTED_M" ]]; then
      pass "m: arrow-auto-head.svg で fail した assertion id 集合が期待と一致（marker-end/marker-start(auto-start-reverse)/marker-start(auto非慣用)/markerなしの4種とも head:\"auto\" が視覚どおりに判定できている）: [$ACTUAL_FAIL_IDS_M]"
    else
      fail "m: arrow-auto-head.svg の fail id 集合が不一致。期待=[$EXPECTED_SORTED_M] 実際=[$ACTUAL_FAIL_IDS_M]"
    fi

    MSG_M_E="$(jq -r '.results[] | select(.id == "arrow-e-auto-undetectable-fail") | .message' "$OUT_M" 2>/dev/null)"
    if [[ "$MSG_M_E" == *"auto-start-reverse"* ]]; then
      pass "m: orient が auto/auto-start-reverse のいずれでもない marker-start の fail message に理由が明記されている: ${MSG_M_E}"
    else
      fail "m: arrow-e-auto-undetectable-fail の message が想定外（理由が不明瞭）: ${MSG_M_E}"
    fi
  else
    fail "m: results.json が生成されなかった: $OUT_M"
  fi
}

# ------------------------------------------------------------------------
# n. no_mirror を HTML 要素（getScreenCTM 非対応）に使うと、黙って pass に
#    せず明確な fail メッセージ（unsupported element type）を返すこと。
# ------------------------------------------------------------------------
{
  OUT_N="$WORK_DIR/no-mirror-html-unsupported-results.json"
  bash "$CHECK" "$NO_MIRROR_HTML" "$NO_MIRROR_HTML_ASSERTIONS" --out "$OUT_N" >"$WORK_DIR/n.stderr.log" 2>&1
  RC_N=$?

  if [[ "$RC_N" -ne 1 ]]; then
    fail "n: check.sh no-mirror-html-unsupported.html の exit code = $RC_N (期待値 1 = 黙って pass にしない)。ログ: $(tail -n 5 "$WORK_DIR/n.stderr.log")"
  else
    pass "n: check.sh no-mirror-html-unsupported.html の exit code = 1（HTML要素への no_mirror は黙って pass にならない）"
  fi

  if [[ -f "$OUT_N" ]]; then
    MSG_N="$(jq -r '.results[0].message' "$OUT_N" 2>/dev/null)"
    if [[ "$MSG_N" == *"unsupported element type"* ]]; then
      pass "n: message に 'unsupported element type' が明記されている（黙って pass にしていない）: ${MSG_N}"
    else
      fail "n: exit 1 だったが message の中身が想定外: $MSG_N"
    fi
  else
    fail "n: results.json が生成されなかった: $OUT_N"
  fi
}

# ------------------------------------------------------------------------
# o. forall 型アサーション（geometry.js 拡張B）: text_present /
#    no_overlap_text_leaves / no_horizontal_overflow / max_page_height /
#    all_text_visible の5種類すべてが、forall-pass.html では全 pass
#    することを確認する（DOM構造が同一の forall-fail.html との対比は次の p）。
# ------------------------------------------------------------------------
{
  OUT_O="$WORK_DIR/forall-pass-results.json"
  bash "$CHECK" "$FORALL_PASS_HTML" "$FORALL_ASSERTIONS" --out "$OUT_O" >"$WORK_DIR/o.stderr.log" 2>&1
  RC_O=$?

  if [[ "$RC_O" -ne 0 ]]; then
    fail "o: check.sh forall-pass.html の exit code = $RC_O (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/o.stderr.log")"
  else
    pass "o: check.sh forall-pass.html の exit code = 0"
  fi

  if [[ -f "$OUT_O" ]]; then
    ALL_PASS_O="$(jq -r '[.results[] | select(.pass != true)] | length' "$OUT_O" 2>/dev/null)"
    if [[ "$ALL_PASS_O" == "0" ]]; then
      pass "o: forall-pass.html で text_present/no_overlap_text_leaves/no_horizontal_overflow/max_page_height/all_text_visible の全 assertion が pass"
    else
      fail "o: forall-pass.html で pass しないアサーションがある (non-pass件数=$ALL_PASS_O)"
    fi
  else
    fail "o: results.json が生成されなかった: $OUT_O"
  fi
}

# ------------------------------------------------------------------------
# p. forall-fail.html は forall-pass.html と DOM 構造が同一で、5欠陥
#    （テキスト重なり・横はみ出し・ページ高さ超過・要素遮蔽・対象テキスト
#    不在）に対応する CSS 値だけを変えてある。5種すべてが期待どおり fail
#    すること（偽陽性・偽陰性の同時検出）。
# ------------------------------------------------------------------------
{
  OUT_P="$WORK_DIR/forall-fail-results.json"
  bash "$CHECK" "$FORALL_FAIL_HTML" "$FORALL_ASSERTIONS" --out "$OUT_P" >"$WORK_DIR/p.stderr.log" 2>&1
  RC_P=$?

  if [[ "$RC_P" -ne 1 ]]; then
    fail "p: check.sh forall-fail.html の exit code = $RC_P (期待値 1)。ログ: $(tail -n 5 "$WORK_DIR/p.stderr.log")"
  else
    pass "p: check.sh forall-fail.html の exit code = 1"
  fi

  EXPECTED_FAIL_IDS_P="text-present-marker no-overlap-siblings no-horizontal-overflow max-height-under-limit all-text-visible-ok"
  if [[ -f "$OUT_P" ]]; then
    ACTUAL_FAIL_IDS_P="$(jq -r '[.results[] | select(.pass != true) | .id] | sort | join(" ")' "$OUT_P" 2>/dev/null)"
    EXPECTED_SORTED_P="$(printf '%s\n' $EXPECTED_FAIL_IDS_P | sort | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$ACTUAL_FAIL_IDS_P" == "$EXPECTED_SORTED_P" ]]; then
      pass "p: forall-fail.html で fail した assertion id 集合が期待と一致（5種の forall アサーションすべてが対応する欠陥を検出）: [$ACTUAL_FAIL_IDS_P]"
    else
      fail "p: forall-fail.html の fail id 集合が不一致。期待=[$EXPECTED_SORTED_P] 実際=[$ACTUAL_FAIL_IDS_P]"
    fi
  else
    fail "p: results.json が生成されなかった: $OUT_P"
  fi
}

# ------------------------------------------------------------------------
# q. no_overlap_text_leaves の exclude_selector（decoy要素を除外すると
#    偽陽性が消える）と、all_text_visible の max_elements 打ち切り
#    （超過分を黙って全数検査したふりをせず message に明記する）を検証する。
# ------------------------------------------------------------------------
{
  OUT_Q="$WORK_DIR/forall-nuance-results.json"
  bash "$CHECK" "$FORALL_NUANCE_HTML" "$FORALL_NUANCE_ASSERTIONS" --out "$OUT_Q" >"$WORK_DIR/q.stderr.log" 2>&1
  RC_Q=$?

  if [[ "$RC_Q" -ne 1 ]]; then
    fail "q: check.sh forall-nuance.html の exit code = $RC_Q (期待値 1)。ログ: $(tail -n 5 "$WORK_DIR/q.stderr.log")"
  else
    pass "q: check.sh forall-nuance.html の exit code = 1"
  fi

  EXPECTED_FAIL_IDS_Q="without-exclude-detects"
  if [[ -f "$OUT_Q" ]]; then
    ACTUAL_FAIL_IDS_Q="$(jq -r '[.results[] | select(.pass != true) | .id] | sort | join(" ")' "$OUT_Q" 2>/dev/null)"
    if [[ "$ACTUAL_FAIL_IDS_Q" == "$EXPECTED_FAIL_IDS_Q" ]]; then
      pass "q: forall-nuance.html で fail した assertion id 集合が期待と一致（exclude_selector 指定時は偽陽性が消え、無指定時は実際の重なりを検出）: [$ACTUAL_FAIL_IDS_Q]"
    else
      fail "q: forall-nuance.html の fail id 集合が不一致。期待=[$EXPECTED_FAIL_IDS_Q] 実際=[$ACTUAL_FAIL_IDS_Q]"
    fi

    MSG_Q="$(jq -r '.results[] | select(.id == "max-elements-truncated") | .message' "$OUT_Q" 2>/dev/null)"
    if [[ "$MSG_Q" == *"未検査"* ]]; then
      pass "q: all_text_visible の max_elements 打ち切りが message に明記されている（黙って全数検査したふりをしない）: ${MSG_Q}"
    else
      fail "q: max-elements-truncated の message が想定外（打ち切りの明記が無い）: ${MSG_Q}"
    fi
  else
    fail "q: results.json が生成されなかった: $OUT_Q"
  fi
}

# ------------------------------------------------------------------------
# r. check.sh --size WxH が headless Chrome の --window-size へ正しく反映
#    されること。size-probe.html の #probe（100vw/100vh）の bbox 幅が
#    指定した --size の幅と一致し（オフセット無し）、高さは要求した
#    --size 間の差分がそのまま反映される（環境依存の固定オフセットの絶対値
#    はアサーションに埋め込まず、2つの --size 実行間の差分のみを検証する）。
# ------------------------------------------------------------------------
{
  OUT_R1="$WORK_DIR/size-probe-500x400-results.json"
  OUT_R2="$WORK_DIR/size-probe-900x700-results.json"
  bash "$CHECK" "$SIZE_PROBE_HTML" "$SIZE_PROBE_500_ASSERTIONS" --size 500x400 --out "$OUT_R1" >"$WORK_DIR/r1.stderr.log" 2>&1
  RC_R1=$?
  bash "$CHECK" "$SIZE_PROBE_HTML" "$SIZE_PROBE_900_ASSERTIONS" --size 900x700 --out "$OUT_R2" >"$WORK_DIR/r2.stderr.log" 2>&1
  RC_R2=$?

  if [[ "$RC_R1" -ne 0 ]]; then
    fail "r: check.sh --size 500x400 の exit code = $RC_R1 (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/r1.stderr.log")"
  else
    pass "r: check.sh --size 500x400 → #probe の幅が500pxに一致（exit 0）"
  fi

  if [[ "$RC_R2" -ne 0 ]]; then
    fail "r: check.sh --size 900x700 の exit code = $RC_R2 (期待値 0)。ログ: $(tail -n 5 "$WORK_DIR/r2.stderr.log")"
  else
    pass "r: check.sh --size 900x700 → #probe の幅が900pxに一致（exit 0）"
  fi

  if [[ -f "$OUT_R1" && -f "$OUT_R2" ]]; then
    H1="$(jq -r '.results[] | select(.id == "height-probe-500") | .actual' "$OUT_R1" 2>/dev/null)"
    H2="$(jq -r '.results[] | select(.id == "height-probe-900") | .actual' "$OUT_R2" 2>/dev/null)"
    if [[ -n "$H1" && -n "$H2" && "$H1" != "null" && "$H2" != "null" ]]; then
      DELTA="$(python3 -c "print(abs(($H2 - $H1) - (700 - 400)))" 2>/dev/null)"
      if [[ -n "$DELTA" ]] && python3 -c "import sys; sys.exit(0 if float('$DELTA') <= 2 else 1)" 2>/dev/null; then
        pass "r: --size の高さ指定が反映されている（500x400→h=${H1}px, 900x700→h=${H2}px, 差分=$((700-400))pxに一致）"
      else
        fail "r: --size の高さ差分が想定と不一致（500x400→h=${H1}px, 900x700→h=${H2}px, 期待差分300px, 実差分ズレ=${DELTA}）"
      fi
    else
      fail "r: --size 高さプローブの actual 値を取得できなかった (H1=$H1, H2=$H2)"
    fi
  else
    fail "r: results.json が生成されなかった: $OUT_R1 または $OUT_R2"
  fi
}

echo ""
echo "== 結果: PASS=$PASS FAIL=$FAIL =="

if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
exit 0
