# Opus 5 レビュー effort の A/B（medium / high / xhigh）— 2026-08-22

model-policy の「レビュー・検証 = xhigh 固定」を事実で決めるための実験。Workflow（opus 52 体、約 311 万トークン、36 分）で実施。

## 方法
- 個人リポジトリの Python 主体コミット 4 本（cost-manager `0b8b4da` / fukusenzu-trainer `9ee79c7` / logic-puzzles `6bbbfde` / machinokakera `ba5d93c`）の差分を「PR」とし、変更行の範囲内に既知バグ 5 件（logic / boundary / empty_none / state / api_contract 各 1）を opus(high) が注入。正解はパケット外に隔離。
- opus が effort medium / high / xhigh × 3 回ずつ独立にレビュー（パケット外の参照・編集禁止、重要度で絞らず全件報告）。
- fixture × effort ごとに opus(xhigh) の採点者が正解と突合（matched / real_other / nitpick / false_positive / duplicate）。採点者は effort を知らない。
- コストは各レビューエージェントの transcript を cost_lib（requestId dedup）で集計した従量単価換算。

## 結果（各 effort n=12）

| effort | 注入バグ捕捉率 | real_other（正解外の実在バグ）| nitpick | 偽陽性 | USD/レビュー | 出力トークン | キャッシュ読取 | 所要 | ツール呼出 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| medium | 96.7% | 0.75 | 4.42 | 0.00 | $0.544 | 8,911 | 206,520 | 147s | 7.1 |
| high | 100.0% | 1.42 | 3.42 | 0.08 | $0.794 | 12,996 | 394,918 | 230s | 9.2 |
| xhigh | 100.0% | 2.08 | 3.42 | 0.00 | $1.008 | 18,786 | 449,213 | 302s | 11.0 |

### fixture 別（USD / 出力トークン / 所要、3 回平均）

| fixture | medium | high | xhigh |
|---|---|---|---|
| ct-activity | $0.71 / 10,883 / 181s | $1.35 / 21,274 / 352s | $1.51 / 24,706 / 416s |
| fk-scoring | $0.53 / 7,479 / 129s | $0.77 / 11,722 / 229s | $1.09 / 20,325 / 323s |
| lp-m2 | $0.43 / 6,253 / 104s | $0.52 / 8,062 / 166s | $0.78 / 15,089 / 238s |
| mk-slice | $0.51 / 11,030 / 176s | $0.54 / 10,925 / 173s | $0.65 / 15,027 / 229s |

## 読み
- 注入バグの捕捉率は全 effort でほぼ 100%（medium の取りこぼし 2 件は ct-activity の同一バグ）。**注入バグは易しすぎて effort を弁別できない（天井効果）**。
- 弁別したのは **real_other（正解に無い実在バグ）**: xhigh 2.08 > high 1.42 > medium 0.75。xhigh は medium の 2.8 倍、high の 1.5 倍の深いバグを拾う。
- nitpick は xhigh で増えない（3.4、medium 4.4）。偽陽性は全 effort でほぼゼロ。CodeRabbit（2026-07-24）の「Opus 5 x-high は nitpick 4 倍・recall 55%」は本実験では再現しない（対象コード・採点基準・プロンプトが異なる）。
- コストは xhigh が medium の 1.85 倍・high の 1.27 倍。所要時間は 2.05 倍・1.31 倍。

## 判断
- 「レビュー・検証 = xhigh」は**品質面では正当**（余計な指摘を増やさず深いバグを余分に拾う）。コスト面では high が妥協点。
- 方針: **枠が余っているとき（週次ペース < 1.0）は xhigh、逼迫時（ペース > 1.1）は high に落とす**。medium はレビュー用途には使わない（深いバグを半分以上落とす）。この切替を調整ノブ（model-policy の effort 設定）に組み込む。

## 限界（[未検証] を含む）
- fixture は Python 4 本・各 100〜400 行。他言語・大規模差分では未検証。
- real_other の「実在」判定は opus 採点者の判断（files/ を読んで確認する指示あり）。人手での再確認はしていない。
- 採点者は effort ごとに別個体（同一 fixture の 3 本を 1 人が採点）。採点者間のばらつきは未測定。
- 注入バグが易しいため、recall の差は測れていない。難しいバグ（並行性・分散状態）での差は別実験が要る。

## 正解の見逃し（採点者メモ）

### ct-activity
- [medium] 3本とも見逃した bug_id は無し（b1〜b5 すべて少なくとも1本が検出）。

- b1 / b2 / b3 / b4 は 3 本とも high で検出。b1・b4 は複数レビューが具体的な数値例・実測（python 実行）まで添えており、注入バグの中でも発見容易だった。
- b5（_scan_file_events の open() に encoding="utf-8" が無い）は review 2 の finding 6 のみが検出。review 0・1 は見逃した。見つけにくさの理由: (a) 追加行の「無い属性」を指摘する必要があり、diff 上は新規追加コードなので「削られた」痕跡が見えない、(b) errors="replace" が付いているため一見防御済みに見える、(c) 同ファイル内の iter_usage / find_first_user_text（390 行）が encoding を明示していることに気づいて初めて不整合と分かる、という横断比較を要する。review 2 だけがこの比較を行っていた。

その他の共通傾向:
- 3 本とも「chrome 版カードの高さだけ +30px 増えて実処理時間行が描画されない」を挙げており、これは正解に無いが files/ を確認した限り PR 由来の実在する不整合（_build_card_ht
- [high] 3本とも見逃した bug は無し。正解 b1〜b5 はすべてのレビューが high 相当で検出した（b1 active_seconds の max 欠落、b2 duration_sec >= 0 のゼロ除算、b3 block["input"] 直接参照、b4 prev_ts 未更新、b5 encoding 未指定）。いずれも新規追加コード内の局所的な注入で、3本とも実際に import/再現実行して実測値を添えており（例: active_seconds([(t,t+600),(t+10,t+20)])=20.0、セグメント分割の実測）、検出難度は低かったと判断する。

検出順の差はあるが取りこぼしは無い: review 0 は b4→b1、review 1/2 は b1→b4 の順。b5（encoding）だけは3本とも severity nit 扱いで、実害（非 UTF-8 ロケールでのイベント欠落＝実処理時間の過少計上）まで踏み込んだのは review 2 finding 6 のみだった。

非正解 finding の傾向: 3本に共通して現れた本物の追加欠陥は (a) render_image._card_height の header_h +30 が chrome 版にも効くのに chrome は実処理時間行を描かない（3本とも指摘、real_other）、(
- [xhigh] 3本とも見逃した bug は無し。正解 b1〜b5 は 3 本すべてが high 相当で検出しており（b1 union の max 欠落、b2 duration_sec>=0 のゼロ除算、b3 block["input"] 直接参照、b4 prev_ts 未更新、b5 encoding 未指定）、いずれも再現条件・修正案まで具体的で、私の側でも実行検証して一致を確認した（active_seconds([(10:00,10:30),(10:05,10:10)])=600.0、input 欠落で KeyError・input:null で AttributeError、20分放置を挟む合成 transcript で intervals が長さ0に潰れ active=300秒）。b5 だけは正解の症状記述（モジバケ→JSONDecodeError で行ごと欠落）と各レビューの症状記述（U+FFFD 置換で無言破損）にずれがあるが、欠陥箇所と修正内容は同一なので high とした。

差がついたのは非正解 finding の質。review 1 だけが _is_report_marker の部分文字列マッチの緩さ（`python3 -m pytest tests/test_cost_report.py` / `grep -n "python" scripts/cost_re
### fk-scoring
- [medium] 3本とも見逃した bug は無し（b1〜b5 の 5 件すべてを 3 本全部が high 確度で指摘）。

- b1 (score.py:81 不等号逆) / b3 (score.py:58 sigma.get の既定値欠落) / b2 (model.py:115 SWITCH_3 の恒等欠落) は 3本とも blocking で、b2 については「3路2個の両方入替はネット分割として恒等と同じ no-op なので片側だけの 1-3/3-1 が列挙不能」という核心の推論まで 3本とも到達している（実コードで確認: _score_fixed は user 内の nid 一致だけを見るため、両方入替はグルーピング不変＝恒等と等価。指摘は正しい）。
- b4 (derive.py:122 seen のループ内初期化) も 3本とも指摘し、うち 2本（review 1, 2）は「Net.terminals が frozenset なので原理的に no-op」という追加根拠まで挙げている（model.py:155 で確認、正しい）。
- b5 (run_demo.py:38 引数取り違え) も 3本一致。

非正解 finding は全 8 件が nitpick 判定で、false_positive は 0 件。内訳は (a) 等価入替列挙の指数的コスト（3本すべて）、(b
- [high] 3本とも見逃した bug は無し（b1〜b5 の全5件を3本すべてが high 相当で検出、ファイル・行・修正案まで一致）。

補足:
- b1（score.py:81 の不等号反転）、b3（score.py:58 の sigma.get デフォルト欠落）、b2（model.py:115 の SWITCH_3 恒等欠落）は3本とも blocking で検出。b2 は「コメントは恒等を含むと書いているのに実装に無い」「product の直積なので全3路同時入替の1通りしか出ない」という機序まで3本とも正しく説明しており、特に精度が高い。
- b4（derive.py の seen 初期化位置）は3本とも「ネット横断で見ていない」に加え「Net.terminals が frozenset なのでネット内重複は原理的に起きず no-op」という二段目の理由まで到達しており、単なる行の指摘に留まっていない。
- b5（run_demo.py:38 の引数取り違え）も3本とも「デモ②が合格と表示される」という症状まで一致。

非正解 finding は3本合計6件で、すべて nitpick 判定（false_positive は無し）。内訳は「刻印欠けと構造欠陥の二重計上」3件（各レビュー1件ずつ）、「等価入替の指数的列挙」2件、「_relabel が polarity 集合に
- [xhigh] 3本とも見逃した bug は無し（b1〜b5 すべて 3/3 で high 一致）。

各 bug の検出状況:
- b1 (score.py:81 `>` 反転) — 3本とも blocking で指摘。いずれも docstring「欠陥最少」との矛盾を根拠にしており、`best.passed` の break が到達不能になる副作用まで正しく述べている。
- b2 (model.py:115 SWITCH_3 に恒等欠落) — 3本とも「product の直積では『全3路を必ず同時入替』しか出ず、片側のみ入替＝1-3/3-1 の交差が候補から落ちる」という核心まで到達。review1/review2 は「両方 swap は渡りネット集合を自分自身へ写す恒等的操作」と、なぜ救済されないかの機構まで説明しており質が高い。
- b3 (score.py:58 `sigma.get` の既定値欠落) — 3本とも high。review1/review2 は frozenset 内で `Core(None, gauge)` が重複除去されて心線本数まで変わる（刻印判定も狂う）という二次影響まで指摘。
- b4 (derive.py:121-122 `seen` をネット内で初期化) — 3本とも指摘。`Net.terminals` が frozenset propert
### lp-m2
- [medium] 3本とも見逃した bug は無し。b1〜b5 の 5 件すべてを 3 本とも high 相当の精度で検出し、行番号・症状（4の壁の欠落による解数過大、and による矛盾検出の恒偽化、median([]) の StatisticsError、progressed の while 外初期化による無限ループ、--boards の type=int 欠落による TypeError）まで正解記述と一致していた。3本の差は非正解 finding の並びだけで、共通して (a) make_graded_batch の無上限リトライループ（real_other 相当の実在する堅牢性欠陥）、(b) effort() のフルソルバ再実行という性能提案、(c) 一意盤前提が破れたときの method='lookahead' 誤報告という防御的指摘、(d) TIER_ORDER 未使用 import / classify_tier と tier_of の重複、(e) 分位点添字の境界偏り、を挙げた。(c)(e) は挙動の誤りとまでは言えず nitpick に分類した。false_positive は 3本とも 0 件で、事実誤認の主張は見当たらなかった（--boards の TypeError、median の例外、4の壁欠落と is_solution の不整合はいずれもコードで確認でき
- [high] 3本とも見逃した bug は無し。b1〜b5 の5件すべてを3本のレビューが blocking/major として明確に指摘し、全マッチが high confidence（solver.py:44 の `<4`、solver.py:72 の `and`、tactics.py:44 の progressed 初期化位置、grading.py:59 の median 空ガード欠落、grading.py:91 の type=int 欠落）。いずれも base との diff が短く、注入痕が unified diff 上で直接読める形だったため検出容易だったと見られる（b1/b2 は base 行が diff に残存、b4 は while と progressed の順序、b5 は隣接する --size/--seed との対比）。

非正解 finding の傾向: 3本に共通して (a) grading.calibrate_normal_max_steps の分位点境界が `<=` で Normal 側に寄り、steps 同値塊で Hard が空になる → b3 の例外を誘発する、(b) make_graded_batch の while に試行上限が無く生成失敗時にハングし得る、の2点が real_other として重なった。どちらも注入バグではないが、コードを読む限り
- [xhigh] 3本とも見逃した bug はゼロ（b1〜b5 すべて、3レビュー全部が confidence=high で検出）。5件とも injected 箇所が diff 上で目立つ形（`or`→`and`、`<=`→`<`、median のガード削除、type=int 抜け、`progressed` の位置移動）だったうえ、base/solver.py が同梱されていて b1/b2 は base と直接照合できたため検出容易だった。b3/b4/b5 は新規ファイルで base 照合ができないが、いずれも「空リスト→median」「argparse の type 不揃い（--size/--seed には type=int がある）」「ループ内フラグの初期化位置」という定型の欠陥パターンで、3本とも自力で再現確認まで行っていた。

非正解 finding の傾向も3本でほぼ一致（矛盾盤の Easy 誤分類、分位点のタイ問題、effort の再スキャン／余分な solve、make_graded_batch の上限なしループ・相関標本、tier_of と classify_tier の重複、未使用 TIER_ORDER）。false_positive は 1 件も無し。判断の分かれ目は performance 系の扱いで、二乗オーダーの再スキャン（r0#6 / r2#9）は実害があ
### mk-slice
- [medium] 3本とも見逃した bug は無い。b1〜b5 の全てを 3 本すべてが high confidence で検出した（レビュー0: f3/f0/f5/f2/f1、レビュー1: f3/f0/f5/f2/f1、レビュー2: f2/f0/f5/f3/f1）。b2（戻り値順序の入れ替え）は 3 本とも ValueError: Unknown format code 'f' の具体的な失敗経路まで、b5（used<1 の off-by-one）は 3 本とも docstring/PR 説明との食い違いと body 単体スライス事故の関係まで正しく説明しており、検出の質も高い。b3（and/or）も 3 本とも「片方だけ残ると素通り」という本質を捉え、修正案（or もしくは <item 単独判定）まで一致している。

非正解 finding の傾向: 3 本に共通して現れたのは (1) 147-153 行の assert による出力検証（-O で無効化、AssertionError が docstring の exit code 規約 0/1/2 から外れる）、(2) ZipFile を with なしで開くクローズ漏れ、(3) qa_drops の timeout/returncode 未確認、(4) 正規表現の書式決め打ち（<resources> / model_setti
- [high] 3本とも見逃した正解バグは無し（b1〜b5 すべてを 3本全部が high 一致で検出）。各バグの検出状況:
- b1(extract_gcode の namelist 空判定): 3本とも「GCODE_ENTRY not in z.namelist() にすべき」と修正案まで一致。
- b2(check_multicolor の戻り値順): 3本とも 217行の f'{w:.1f}g' で ValueError になることを再現確認済みと記述。GT は return 側、レビューは呼び出し側を直せと書いているが同一欠陥として high 判定。
- b3(and/or 取り違え): 3本とも検出。review_index=2 は「117行の resources 除去が成功すると object_1.model は必ず消えるのでガードは実質デッド」という一段深い分析まで到達しており最良。
- b4(rundir 使い回し): 3本とも run1 の残骸を run2 が成功と誤認する具体シナリオまで特定。
- b5(used < 1 の off-by-one): 3本とも docstring/PR説明との突き合わせで used < 2 が正しいと指摘。

非正解 finding の傾向: 3本に共通して挙がった実在の欠陥は「147-153行の受け入れ検査が assert（-
- [xhigh] 3本とも全5バグ（b1〜b5）を検出。3本とも見逃したバグは無し。

- b1（extract_gcode の `not z.namelist()`）: 3本とも line 70 で KeyError 経路まで含めて正確に指摘。
- b2（戻り値順 `used, ptime, weights`）: 3本とも blocking として指摘し、合格パス 217行での ValueError まで追跡。ground truth の symptom と完全一致。
- b3（119行 and→or）: 3本とも指摘（r0 f4 / r1 f8 / r2 f4）。
- b4（rundir 共有）: 3本とも `out3mf.exists()` 依存との組み合わせで前 run 成果物を掴む機序まで説明。
- b5（`used < 1`）: 3本とも docstring/PR 記述との不一致として指摘。

非正解 finding の傾向（3本に共通）:
1. 145-153行の検証を assert で書いている件（-O で無効化・AssertionError は exit 1 で docstring の exit code 契約 2 と衝突）。コード確認上、主張は事実として正しいので real_other に分類。
2. out_path へ直接書いてから検証するため、検証失敗時に不

## 材料
- パケット・正解・集計 JSON: セッション scratchpad（`scratchpad/ab/`, `scratchpad/gt_7f3a/`）。再現は Workflow スクリプト `opus-review-effort-ab`（セッションの workflows/scripts/ に保存）。

## 付録: 副次的に見つかった実在バグ（正解外・採点者が files/ を読んで実在と判定。出現回数 = 9 レビュー中）

注入前から存在するバグを含む。修正は未対応（提案）。行番号はパケット内 files/ 基準（注入分のずれがありうる）。同趣旨の指摘でも表現が違うと別行になる。


### ct-activity（claude-toolbox/cost-manager @0b8b4da）
- [1/9, minor] `render_image.py:68` header_h を 160 に増やしたが Chrome レンダラの HTML には実処理時間行が無く、空白 30px と表示不整合が生じる — 本 PR で _card_height の header_h を 130→160 に上げたが、_build_card_html の values に active_text が無く card.html.tmpl も未変更（テンプレートは packet にも含まれず diff にも無い）ため、chrome 版はカード高さだけ +30px 伸びて実処理時間行が描画されない。『実働』→『経過』の改名も 
- [1/9, major] `render_image.py:404` Chrome レンダラのカード高さだけ +30px 増え、実処理時間行は描画されない — review 0 の finding 4 と同種の実在不整合。共通関数 _card_height の header_h だけ +30px 増え、_build_card_html は active_text を渡さずテンプレートも未変更のため chrome 版に空白と機能欠落が生じる。
- [1/9, minor] `render_image.py:404` chrome 版カードは高さだけ 30px 増えるが実処理時間行が描画されない — review 0/1 と同じ chrome 版カードの高さ +30px と実処理時間行欠落。files/ で _build_card_html の values に active_text が無いことを確認済みの実在不整合。
- [1/9, major] `cost_report.py:151` end_display の補正が since_dt でフィルタしておらず、集計窓の外（マーカー開始前）のイベントを終端に採用し得る — cost_report.py:151 の activity_before_until が下限 (since_dt/start_display) でフィルタしていないのは実際にコードのとおり。scan_activity は since/until を受け取らずファイル全体を走査するため、窓外イベントが end_display 候補になり得る。b2 のゼロ除算の引き金でもあるが、reports.jso
- [1/9, minor] `render_image.py:66` chrome レンダラでは実処理時間行が描画されないのに、_card_height だけ +30px されて空白帯ができる — 実リポジトリの templates/card.html.tmpl を確認: active_text 行は無く、メタ行も『実働 ${duration}』のまま。一方 _card_height の header_h は 130→160 に上げられ chrome 版の --window-size 高さにも効くため、chrome 経路では約30px の空白帯＋旧ラベルという実際の出力不整合が発生する。表示
- [1/9, nit] `cost_lib.py:666` 作業とレポート生成が同一ターンだと、そのファイルの活動が丸ごと除外される — 合成 transcript で再現確認: 単一ターン内で作業＋レポート生成した場合、L がその唯一のターン開始になり filtered が空 → `if not filtered: continue` でそのファイルの intervals/event_times が丸ごと落ち、intervals=[]・event_times=0件・実処理 0 秒になった。`-p` 実行や『作業してレポートも出して
- [1/9, minor] `cost_lib.py:565` レポートマーカー判定が緩く、cost-manager 自体を開発するセッションで最終ターンを丸ごと誤除外する — 実行確認: _is_report_marker に `python3 -m pytest tests/test_cost_report.py` と `grep -n "python" scripts/cost_report.py` を渡すと両方 True。docstring の『閲覧系は python を含まないため誤検出しない』が成り立たず、誤検出時は最終ターン以降が全除外される。cost-ma
- [1/9, minor] `cost_report.py:151` end_display の候補が窓下限（start_display / since）で絞られていない — review 0 の finding 4 と同趣旨で、コードのとおり end_display 候補が下限フィルタされていない。実在の欠陥。
- [1/9, minor] `cost_lib.py:638` レポートターンの除外がサブエージェントファイルに適用されず、除外が漏れる — コードで確認: `if is_subagent: filtered = events` としており、メインファイルで決まったレポートターン cutoff がサブエージェントファイルに一切適用されない。レポート生成ターンがサブエージェントを起動した場合や --scope global で並行セッションがある場合、除外が漏れて end_display が伸びる。発火頻度は高くないが実在のギャップ。
- [1/9, nit] `render_image.py:66` header_h の +30 が chrome 版にも効くが、chrome 版は実処理時間行を描画しない — chrome 版の高さ +30px と実処理時間行の欠落。card.html.tmpl 実物で active_text 行が無いこと・メタ行が『実働』のままであることを確認済み。レビューは nit 扱いだが、出力 PNG に空白帯が出る実挙動の不整合なので real_other とした。
- [1/9, minor] `render_image.py:404` chrome フォールバック版のカードは高さだけ +30px 増えて実処理時間行は描画されない — chrome 経路の高さ +30px・実処理時間行なし。card.html.tmpl を実物で確認して裏取り済み（active_text プレースホルダ無し、メタ行は『実働』のまま）。docstring の『実処理時間行は常時描画される』が chrome では偽という指摘も正しい。
- [1/9, minor] `render_image.py:65` 共有関数 _card_height の header_h を +30 したため chrome レンダラのカードだけ余白が増える — 実在。_card_height の header_h 130→160 は render_pillow(250行)と _build_card_html(404行)の共有で、chrome 側の values に active_text は無く（418行に duration のみ）、templates/ に card.html.tmpl 自体も同梱されず未更新。chrome 経路のカードだけ描画行が増え
- [1/9, minor] `cost_lib.py:445` report_cutoff が呼び出し側で未使用で、サブエージェントのレポートターン活動が除外されない — grep で確認: report_cutoff の参照は cost_lib.py の定義・生成箇所のみで cost_report.py からは未使用（dead field）。またサブエージェントファイルは scan_activity の is_subagent 分岐で cutoff を一切適用されず（iter_transcripts は subagents/agent-*.jsonl も列挙する）
- [1/9, minor] `cost_lib.py:565` レポートマーカー判定が部分文字列一致のため、テスト実行や python 絡みの閲覧コマンドを誤検出する — 実在。_is_report_marker は command に "cost_report.py" と "python" が含まれるかの部分文字列 AND のみで、`python3 -m pytest tests/test_cost_report.py` や `grep -n python scripts/cost_report.py` は両方を満たす。docstring の『閲覧系は pytho
- [1/9, minor] `cost_report.py:151` report_cutoff が誰にも使われず、サブエージェントのイベントがレポートターン除外をすり抜ける — review 0 の finding 6 と同趣旨で、grep でも report_cutoff がリポジトリ内で未参照であることを確認。サブエージェント分岐が cutoff を適用しない点も実コード通り（is_subagent なら filtered=events）。軽微だが機構の指摘は正しい。
- [1/9, minor] `render_image.py:404` header_h の 160 への引き上げが Chrome 版にも効くが、Chrome 版は実処理時間行を描かないため 30px の空白が増える — 実在。_card_height の共有により chrome 版のカード高さだけ描画行なしで +30px、かつ active_text が values にも card.html.tmpl にも無いため実処理時間が chrome 経路で欠落する。
- [1/9, minor] `render_image.py:65` header_h の +30 が chrome レンダラにも効くが、chrome 側は実処理時間行を描画しない — 実在。_card_height を pillow/chrome で共有したまま header_h を +30 したため、chrome 経路（--via chrome / renderer=chrome / pillow 失敗時フォールバック）はカード高さだけ 30px 増え、values に active_text が無いので実処理時間行は描かれない。card.html.tmpl は本パケットの 
- [1/9, nit] `cost_lib.py:564` レポートマーカー判定が部分文字列 AND のため誤検出しうる — 実在（review 1 finding 4 と同趣旨）。部分文字列 AND のみの判定で `grep -n python scripts/cost_report.py` 等が誤検出し、docstring が明言する『閲覧系は誤検出しない』が破れる。誤検出時は最終ターンの活動が無言で除外され、数値だけが静かに狂う。

### fk-scoring（fukusenzu-trainer @9ee79c7）

### lp-m2（logic-puzzles @6bbbfde）
- [1/9, minor] `grading.py:75` make_graded_batch が終了保証を持たず、一意盤が出ない条件で無限ループ — grading.py:75 make_graded_batch の `while len(boards) < n_boards` は試行上限・進捗保証を持たず、smart_generate_one が None を返し続ける／carve 後に is_unique を通らない条件では永久に回る。注入前から新規コードに存在する本物の堅牢性欠陥（`if puz is None: continue` とい
- [1/9, minor] `grading.py:75` make_graded_batch に上限が無く、生成が失敗し続けると無限ループする — review_index=0 の finding 5 と同じ、make_graded_batch の無上限リトライループ。新規コードに実在する堅牢性欠陥。発現条件は packet 外の generator.py 依存で未検証。
- [1/9, minor] `grading.py:75` make_graded_batch に試行上限が無く、生成が失敗し続けると無限ループ — review_index=0 の finding 5 と同じ、make_graded_batch の無上限リトライループ。新規コードに実在する堅牢性欠陥。発現条件は packet 外の generator.py 依存で未検証。
- [1/9, minor] `grading.py:35` 分位点インデックスの off-by-one で Normal 側に偏り、Hard が空になり得る — grading.py:35-36/44 の分位点境界。idx=min(int(len*q), len-1) で得た steps[idx] を tier_of が `<=` で Normal 側に含めるため、境界値と同値の盤が全て Normal に落ちる。steps=[1,1,2,3] で Normal 3/4、全同値なら Hard 0 件になり docstring の「概ね半分ずつ」と乖離し、b3
- [1/9, minor] `grading.py:75` make_graded_batch は生成失敗時に無限ループし、また同一パズル由来の相関した盤を最大4件まとめて追加する — grading.py:75 の `while len(boards) < n_boards` に試行上限が無く、smart_generate_one が None を返し続ける／is_unique が通らない条件でハングするのは実在のリスク（--size がユーザー指定可）。内側 for が len 再チェックせず最大3件超過生成して boards[:n_boards] で末尾（carve回数の多
- [1/9, minor] `grading.py:35` 分位点の取り方が境界を Normal 側へ寄せ、同値分布では Hard が空になる — review 0 の finding 5 と同じ分位点境界の指摘。steps[idx] を `<=` で Normal 側に含めるため同値塊が全て Normal に倒れ、Hard が空になって b3 の例外を誘発する。記述はコードと一致しており実在の弱点。
- [1/9, minor] `grading.py:79` make_graded_batch が n_boards を超過して生成し、生成失敗時は無限ループしうる — grading.py:79 の内側 for が len(boards) を再チェックせず最大3件超過生成→末尾切り捨てで難易度分布にバイアス、捨てられる盤にも重い is_unique を回す、加えて while に試行上限が無くハングし得る。いずれもコードと一致する実在の欠陥（review 0 finding 7 と同趣旨）。
- [1/9, minor] `grading.py:75` make_graded_batch に試行上限が無く、生成が滞ると無限ループする — grading.py:75 の while に試行上限が無く、生成が滞ると無限ループする。--size をユーザーが任意指定できるため現実的なハングリスクで、注入バグとは別の実在の欠陥。
- [1/9, minor] `grading.py:35` 分位点境界が同値の塊を跨ぐと lookahead 盤が全て Normal に倒れる — grading.py:35 の分位点境界（review 0 finding 5 / review 1 finding 5 と同じ）。同値塊が全て Normal に倒れ Hard が空になり得るのはコードと一致する実在の弱点。
- [1/9, minor] `tactics.py:41` 矛盾盤（propagate が False）が method='propagation' のまま Easy に分類される — tactics.py:41-42 の挙動は記述どおり。初回 propagate が False なら `if ok and ...` を素通りして method='propagation'（=EASY）、59行目で ok=False になれば method='lookahead' のまま未解決で返る。解けたか否かを返さないため矛盾盤が最易ティアに紛れる。docstring の前提（一意盤）違反時の
- [1/9, minor] `tactics.py:61` 背理確定のたびに floors スキャンを先頭からやり直し、確定不能と判明済みのセルを再試行する — 61行目の break により確定のたびに floors 先頭から再スキャンするのは事実。1セル確定ごとに最大 |floors|×2 回 propagate を呼び直すため実質 O(K×|floors|) 回。挙動は正しいが、200盤×10x10 のバッチでは実害のある実アルゴリズム上の非効率。
- [1/9, minor] `grading.py:35` 分位点較正が同値（tie）に弱く、Normal/Hard の分割が極端に偏る — `idx=min(int(len*q), len-1)` + `steps <= nms` は同値塊を全て Normal 側へ寄せる。例示の steps=[1,1,1,1,2] → nms=1 → Normal4/Hard1 は計算どおりで、全同値なら Hard が 0 件になり b3 のクラッシュを誘発する。docstring の「概ね半分ずつ」が成立しない実在の設計欠陥。hard_quanti
- [1/9, minor] `grading.py:79` make_graded_batch のバッチが同一 puzzle 由来の相関盤で構成され、分位点較正が偏る — 内側 for は同一 puz から nc=0..3 の派生を無条件に追加し、len(boards) の再チェックが無いので最大3枚超過（高コストな is_unique 込み）してから [:n_boards] で捨てる。carve が 6 回呼ばれる点も記述どおり。相関標本により分位点較正・単調性の統計的裏付けが弱まるのも妥当な指摘（generator.py はパケットに無いため carve の非破
- [1/9, minor] `tactics.py:42` 解なし・矛盾盤が method='propagation'（=Easy）と誤分類される — review 0 の finding 5 と同趣旨で、tactics.py:42 の分岐挙動は記述どおり。矛盾盤・解なし盤が Easy／途中 lookahead として静かに集計される堅牢性欠陥。
- [1/9, minor] `grading.py:75` make_graded_batch に試行上限が無く、生成失敗が続くと無限ループ — make_graded_batch の `while len(boards) < n_boards` に試行上限が無く、smart_generate_one が None を返し続けるか is_unique が通らない場合に停止しないのはコード上明らか（None 分岐が存在する時点で失敗があり得る前提）。生成器本体はパケットに無いため実発生頻度は未確認だが、上限なし retry は妥当な指摘。
- [1/9, minor] `grading.py:35` 分位点の境界がタイに弱く、Normal/Hard の分割が意図どおりにならない — review 0 の finding 7 と同じ分位点タイ問題。`steps <= nms` が同値塊を Normal に寄せ、Hard 0 件 → b3 のクラッシュ誘発という因果も正しい。
- [1/9, minor] `tactics.py:42` propagate が矛盾を返した盤（解なし・非一意）が黙って Easy に分類される — 他2本と同じ、矛盾盤が Easy に落ちる件。『返り値に解けたかを示すフィールドが無い』という指摘も正確で、solved フラグ提案は妥当。
- [1/9, minor] `grading.py:35` `hard_quantile` は実際には Normal 側の割合であり、同値が多い分布では docstring どおりの分割にならない — 後半のタイ問題（同値集中で『概ね半分ずつ』が崩れ、Hard 0 件から b3 のクラッシュへ）は妥当。前半の『hard_quantile は実際には Normal 側の割合で名前と意味が逆』も計算どおり（q を上げるほど Hard が減る）だが、こちらは命名レベルの指摘。
- [1/9, minor] `grading.py:75` make_graded_batch に試行上限が無く、生成が失敗し続けると無限ループする — review 1 の finding 7 と同じ、make_graded_batch の上限なしループ。コード上は確認できるが、生成器がパケットに無いため実発生条件は未確認。
- [1/9, minor] `grading.py:79` 1 つの生成盤から carve 派生 4 枚を採るため、バッチのサンプルが相関し n_boards ほどの独立性が無い — review 0 の finding 8 と同じ、相関標本＋最大3枚の超過生成（超過分にも is_unique を走らせてから 85 行目で破棄）。コードのとおり。
- [1/9, minor] `tactics.py:46` 1手進むたびに floors 全体を先頭から再スキャンするため propagate 呼び出しが O(n^2) になる — review 0 の finding 6 と同じ再スキャンの二乗コスト。加えて 65 行目の solve が build_constraints を再実行する点も事実。挙動は正しいが実効性能上の欠陥。

### mk-slice（machinokakera @ba5d93c）
- [1/9, major] `slice-to-send.py:147` 出力3mfの検証を assert で行っており、-O で無効化され、失敗時は不正ファイルを残したまま exit 1 になる — 147-153行の受け入れ検査が全て assert。python3 -O / PYTHONOPTIMIZE=1 でバイトコードから除去されるのは事実で、その環境では『検証: …パス』と表示しつつ何も検証しない。加えて検証失敗時に既に書き終えた args.out が残る点も実コードどおり（133-143行で書込→146行以降で検証）。注入バグではないが本物の欠陥。
- [1/9, minor] `slice-to-send.py:147` 出力物の妥当性検証を assert に依存している（-O で全て無効化される） — assert による受け入れ検査は -O で無効化されるという指摘は正しく、147-153行の実装どおり。注入バグ以外の本物の欠陥。
- [1/9, minor] `slice-to-send.py:150` 検証で参照する必須エントリの欠落・検証失敗時に traceback／壊れた出力ファイルが残る — 検証失敗時に out_path の不完全な3mfが削除されず残る点は実コードどおり（133-143行で書込済み、146行以降で検証、失敗時 unlink 無し）で妥当。前半の MD5_ENTRY 欠落 KeyError は仮定的だが、後半の残留ファイル問題が実在の欠陥。
- [1/9, minor] `slice-to-send.py:147` 出力3mfの健全性検証を assert で行っており、-O 実行時に全て無効化されるうえ失敗時に不正ファイルが残る — assert の -O 無効化・検証失敗時に不正な out_path が残る点はいずれも実コードどおりで妥当。末尾の MD5_ENTRY KeyError は仮定的だが主張の中心は正しい。
- [1/9, major] `slice-to-send.py:147` 出力3mfの整合性検証を assert で行っており、-O 実行時に全ての検証が無効化される — 147-153行の出力3mf検証がすべて assert。python3 -O / PYTHONOPTIMIZE で全部消えるのは事実で、さらに失敗時は docstring が宣言する exit code 規約(0/1/2)ではなく AssertionError になる。ET.fromstring だけ assert 外という不揃いも指摘どおり。正解には無いが本物の欠陥。
- [1/9, minor] `slice-to-send.py:147` 出力の最終検証を assert で行っており、-O 実行時に全て無効化される・失敗時に壊れた出力が残る — 出力検証が全部 assert で -O 下に消える点に加え、検証失敗時に args.out へ書き終えた不完全な3mfが残る（tempfile ではなく本番パスへ直書き）という指摘も実コードどおりで妥当。正解外の本物の欠陥。
- [1/9, major] `slice-to-send.py:147` 出力3mfの最終検証を assert で行っており、python -O 実行時にすべて素通りする — 147-153行の assert による出力検証が -O で素通りする点、および失敗時に die(exit 2) ではなく AssertionError になり docstring の終了コード規約から外れる点はどちらも実コードどおり。正解外の本物の欠陥。
- [1/9, minor] `slice-to-send.py:147` 出力3mfの検証が assert 依存（python -O で無効化・終了コードが仕様と不一致・壊れた出力が残る） — 145-153行の検証が全て assert。python -O / PYTHONOPTIMIZE で丸ごと消え、md5不一致やG-code改変を無検証で「合格」と表示しうる。また AssertionError は未捕捉で exit 1 になり、docstring の exit code 契約（1=欠落0未達 / 2=前提・環境エラー）と衝突する。コードを確認して事実と一致。検証失敗時に out に
- [1/9, minor] `slice-to-send.py:147` 送信用3mfの安全性検証を assert で実装しており -O 実行時に丸ごと無効化される — review 0 finding 5 と同じ論点。147-153行の検証が assert のみで -O 下では完全に無効化され、しかも「検証: …」の行だけが印字される。実機送信用ファイルの完全性検査という用途を考えると妥当な欠陥。153行にメッセージが無いのも事実。
- [1/9, minor] `slice-to-send.py:133` 出力先へ直接書き込むため、検証失敗時に不完全な送信用3mfがユーザーのパスに残る — out_path へ直接書いてから 145-153行で検証する構造は事実で、検証失敗時に不正な（あるいは既存の正常ファイルを上書きした）3mf が --out に残る。tempfile+os.replace への改善提案も妥当。ただし「120行/127行の die() でも残る」という部分は誤り（この2つの die は 133行の書き込みより前）。本筋が正しいため real_other。
- [1/9, minor] `slice-to-send.py:147` 出力の健全性検証を assert と素の例外に依存している（-O で全て無効化、失敗時も exit code 契約に従わない） — review 0 finding 5 / review 1 finding 4 と同じ論点で、内容も正確。assert が -O で消える点、AssertionError が exit 1 になり docstring の exit code 2 契約と衝突する点、148/150行の zv.read() が KeyError になりうる点いずれもコードと一致。
- [1/9, minor] `slice-to-send.py:215` 最終出力先へ直接書いてから検証するため、検証失敗時に不正なファイルが --out に残る — out_path へ直接書いてから 147-153行で検証する構造は確認済み。検証失敗時にファイルが削除されず --out に残り、前回の正常な成果物を上書きしている可能性もある。tempfile+os.replace / 失敗時 unlink の提案は妥当。review 1 finding 6 と違い die() の位置についての誤りも無い。
