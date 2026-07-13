# vision — 摩擦ゼロのレンダリング基盤 + 幾何アサーション（Phase 0 / Layer A）

Opus が SVG/HTML の図を生成する際、座標を推測で書いてレンダリング結果を見ずに
完了宣言してしまう問題への対策。(1) 1コマンドでスクリーンショットを撮る
`render.sh`、(2) 視覚判断を数値の合否に変換する `geometry.js` + `check.sh`。

## 依存・前提

- headless Chrome（既定: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。
  `CHROME_BIN` 環境変数で上書き可）
- `python3`, `jq`, `sips`（すべて標準の macOS 開発環境にある想定）
- 追加の npm 依存や node は不要（geometry.js はブラウザ内で完結する純粋 JS）

## render.sh — スクリーンショット

```
vision/render.sh <input.(html|svg)> <out.png> [--size WxH] [--scale N] [--wait-ms MS]
```

既定値: `--size 1600x900 --scale 2 --wait-ms 2000`（出力は 3200x1800px）。

入力ファイル不在・Chrome 失敗・PNG 未生成/0バイトのいずれでも非0終了し、
stderr に理由を出す（黙って成功しない）。成功時は stderr に出力パスと実測
ピクセル寸法（`sips` 実測）を1行で出す。60秒の poll-and-kill watchdog あり。

## check.sh — 幾何アサーション実行

```
vision/check.sh <input.(html|svg)> <assertions.json> [--out results.json] [--size WxH]
```

`--size WxH`（例: `--size 1600x900`）を指定すると、render.sh と同様に
headless Chrome の `--window-size` へそのまま渡す。未指定時は従来どおり
何も渡さず、headless Chrome の既定ウィンドウサイズ（環境依存、目安として
実測で約756x469px）のまま評価する（挙動変更なし）。`no_horizontal_overflow`
や `all_text_visible` のように viewport 幅・座標に依存するアサーションで、
採点意図のビューポート寸法を明示したい場合に使う。

入力を「ラッパー文書」に変換し（`.svg` は margin:0 の HTML に本体をインライン
展開、`.html` は自己完結 HTML の `</body>` 直前にランナーを注入）、
`geometry.js` を埋め込んで `window.load` 後2ティック待ってから
`window.__fablizeGeometry.runAssertions(spec)` を実行、結果 JSON を
`<script type="application/json" id="__fablize_results">` に書き込む。
headless Chrome `--dump-dom` でこれを回収し、パース・検証してから出力する。

**v0 の制約**: 自己完結文書のみサポート（外部 CSS/JS/画像/webfont 等の相対
参照は読み込まれない）。`.svg` は `width`/`height` 属性の明示を推奨（未指定
だと既定ビューポートいっぱいに間延びして解釈されることがある）。

### 終了コード

- `0` — 全アサーション pass
- `1` — 1件以上 fail（アサーション自体の失敗。ツールは正常動作）
- `2` — インフラ異常（入力不在・spec が不正 JSON・assertions が空配列・
  Chrome 失敗・結果要素が回収できない・`--out` 書き込み失敗、等。
  **ツール自身の故障**）

「アサーション失敗」と「ツールの故障」を必ず区別する。空の `assertions` を
黙って 0 で通すことはしない。`--out` の出力先ディレクトリが存在しない場合は
自動で作成する（作成自体に失敗したら exit 2。全 assertion が pass していた
のに書き込み失敗だけを理由に exit 1 [偽 fail] になることはない）。

### 検証チャネルの入力からの分離（セキュリティ）

採点対象の入力(.svg/.html)自身が `id="__fablize_results"` を持つ要素を
仕込んでいても（SVG は `<script>` 要素を持てる／HTML は言うまでもない）、
偽の合否データが採用されることはない。結果要素の id には実行のたびに
`lib/build_wrapper.py` がランダムなノンス（128bit）を付与し
（`__fablize_results__<nonce>`）、check.sh はこのノンスを使ってのみ
Chrome の `--dump-dom` 出力を照合する。入力ファイルはこのノンスを事前に
知り得ないため、偽の結果要素が拾われることは無い。ノンスが一致する結果
要素が2件以上見つかった場合も想定外としてインフラ異常（exit 2）扱いにする。

## spec (assertions.json) の形式

```json
{"assertions": [{"id": "...", "type": "...", ...}, ...]}
```

`assertions` が空配列だと `check.sh` は exit 2 で拒否する。`id` が重複した
2件目以降は自動的に fail（`duplicate assertion id`）。未知の `type` や必須
引数欠落もその assertion を fail にする（黙って pass にしない）。

bbox はすべて `getBoundingClientRect()` ベースの画面座標（CSS px、viewport
座標、y は下向き正）: `{x,y,w,h,cx,cy,top,bottom,left,right}`。

### アサーション種別

| type | 必須引数 | 意味 |
|---|---|---|
| `exists` | `selector` | セレクタが1件以上マッチすれば pass |
| `touches` | `a`, `b`（`eps` 既定0） | 2つの bbox の隙間距離 `gap=hypot(max(0,x軸gap),max(0,y軸gap))` ≤ eps（重なりは gap=0） |
| `no_overlap` | `selectors`（2件以上）（`eps` 既定0） | 全ペアで交差矩形の `min(幅,高さ)` ≤ eps。**注意**: これは「短軸方向の貫入深さ」であり、細長い帯状の重なり（例: 190px×5pxの重なり → 貫入深さ5px）は eps を大きくすると見逃す。`no_overlap` では eps=0 を既定運用にすることを推奨する（eps を上げてよいのは `contained_in`/`touches` のようなラベル系アサーションのみ） |
| `contained_in` | `inner`, `outer`（`eps` 既定0） | inner の bbox が outer の bbox に eps 余裕で包含される |
| `relative_position` | `of`, `to`, `position` | `position` ∈ `above/below/left/right/top-left/top-right/bottom-left/bottom-right`。**中心点(cx,cy)のみの比較**。片軸で大きく重なる要素同士だと視覚と食い違うことがあるので、サイズが近い/重なりうる要素の位置判定には向かない |
| `endpoint_near` | `selector`, `end`(`start`\|`end`), `target`, `eps`（`where` 既定 `attached`） | 線分/パスの端点と target bbox の距離判定。**注意**: `end` は `<line>`/`<path>` の座標記述順（`x1,y1`→`x2,y2` 等）に依存する。描き手はどちら向きに座標を書くかを画素を変えずに自由に選べるため、「コネクタが A と B の両方に接続していればよく、向きは問わない」場合は下記 `endpoints_touch` を使うこと。`where:"attached"`（既定）は target の bbox 内部を距離0とみなすため、target に大きな `<g>` 等を指定すると内部のどこでも自明に pass する — 採点者は狭い target を選ぶこと |
| `endpoints_touch` | `selector`, `a`, `b`（`eps` 既定0、`where` 既定 `attached`） | `endpoint_near` の順序非依存版。線分/パスの2端点を A・B どちらの割り当てでも試し、最良の組み合わせで判定する（`<line>` の座標記述順に依存しない） |
| `arrow_direction` | `selector`, `direction`(`up`\|`down`\|`left`\|`right`)、`head`（省略可） | **重要な意味論の注意**: `head` を省略した既定の挙動は、start→end の変位ベクトル（`<line>` の座標記述順）の支配軸・符号判定であり、**描画された矢頭の向きではない**。矢頭を別要素（polygon や marker）で描く idiom では、線の座標をどちら向きに書くかは描き手の自由（画素は不変）なので、座標順だけでは視覚上の矢印の向きを正しく判定できないことがある（見た目は正しい矢印が座標順の都合で fail する／見た目が逆向きの矢印が座標順の都合で pass する、のどちらも起こりうる）。単独で視覚方向の判定に使わないこと。視覚上の矢頭位置に基づいて判定したい場合は `head` を指定する: `head:"start"`/`head:"end"` は marker-start/marker-end 相当（既定は `"end"` で従来互換）、`head:"<selector>"` は矢頭を描く別要素（polygon 等）のセレクタで、その要素の中心に近い方の端点を矢頭側とみなし座標記述順に依存せず判定する、`head:"auto"` は対象要素の computed style の `marker-end`/`marker-start` を見て自動判定する（下記参照）。**生成物の採点には `head:"auto"` を使うことを推奨する**。なお正確に45°(dx=dy)の矢印は支配軸判定が同点になり、"down" と "right" のように両方向を pass することがある（厳密な縦/横を要求するルーブリックでは注意） |
| `compare` | `left`, `op`(`lt`\|`le`\|`gt`\|`ge`\|`eq`), `right`（`eps` 既定0） | `left`/`right` は数値または `{selector,prop}`。汎用の逃げ道 |
| `no_mirror` | `selector` | 対象要素の累積変換（`getScreenCTM()`）の行列式 `det = a*d - b*c` が `det > 0` なら pass（鏡映なし）、`det < 0` なら fail（鏡映を検出）。`actual` に行列成分 (`a,b,c,d`) と `det` を入れる。**180度回転は fail しない**（`a<0` かつ `d<0` になるが `det = (-1)*(-1) - 0*0 = 1 > 0` なので pass — 回転は鏡映ではないため、これは正しい挙動）。**v0 の制約**: SVG のグラフィック要素（`getScreenCTM()` を持つもの）のみ対応。HTML 要素（`getScreenCTM` を持たない）に使うと、黙って pass にはせず `unsupported element type` を含む明確な message で fail する |
| `visible_at_center` | `selector` | 要素 bbox の中心 + 四半点4箇所（中心と各辺中点の中間、計5点）で `document.elementFromPoint` を呼び、いずれかの点で命中した要素が対象要素自身・その子孫・またはその祖先（対象要素を包む `<g>` 等）であれば pass。5点すべてで別要素（遮蔽物）が命中したら fail。不透明要素の後描画による遮蔽（z順事故）の検出用。`actual` に各点の hit 要素の記述を入れる。**既知の限界**: (1) `fill="none"` の stroke のみの要素は中心点が自分自身に当たらないため誤 fail しうる — 塗りのある要素にのみ使うこと。(2) `opacity < 1` の半透明な遮蔽物も「遮蔽」と判定される。(3) viewport 外にある要素は `elementFromPoint` が `null` を返すため fail する（message に明示） |

### forall 型アサーション（文書全体を対象にする）

上記はすべて「セレクタで単一要素を特定できる」ことを前提にしたアサーション
だが、緩い仕様から複雑な内容を生成する採点（構造をエージェントの自由選択に
委ねる／id をプロンプト側で固定できない生成物）では、そもそも検査対象の
要素を id で名指しできないことがある。以下は文書全体（またはその可視部分）
を対象にする「forall 型」のアサーションで、そうした生成物のレイアウト事故
（テキスト同士の重なり・横はみ出し・ページ高さ超過・不透明要素による遮蔽・
必須テキストの不在）を id 非依存で検出する。

| type | 必須引数 | 意味 |
|---|---|---|
| `text_present` | `text`（`min_count` 既定1） | レンダリング後の可視テキスト（`display:none`/`visibility:hidden`・`script`/`style`/`template`/`noscript` 配下・空白のみのノードを除く）を文書順に連結し、`text` が `min_count` 回以上（非重複カウント）出現すれば pass。`actual` に実出現数を入れる |
| `no_overlap_text_leaves` | `eps`（`exclude_selector` 省略可） | 下記「テキスト葉」の全ペアについて、**一方が他方を包含する場合（親子入れ子）は除外**した上で、交差矩形の `min(幅,高さ)`（貫入深さ）が `eps` を超えるペアが1件でもあれば fail。`exclude_selector` を指定すると、そのセレクタにマッチする要素・その子孫であるテキスト葉を集合から除外できる（装飾用オーバーレイ等、意図的に重ねている要素を除外するため）。`actual` に違反ペアの記述（各要素のタグ・テキスト先頭20字・座標）を最大10件入れる |
| `no_horizontal_overflow` | `eps` | `document.documentElement.scrollWidth` が viewport幅+`eps` 以内、かつ全テキスト葉の `right` が viewport幅+`eps` 以内であれば pass。片方でも超えれば fail |
| `max_page_height` | `max`, `eps` | `document.documentElement.scrollHeight` が `max`+`eps` 以内であれば pass |
| `all_text_visible` | `max_elements`（既定200） | 全テキスト葉（`max_elements` 件まで）それぞれに `visible_at_center` と同じ5点プローブを適用し、1つでも完全遮蔽ならfail。`actual` に遮蔽された要素の記述を入れる。テキスト葉が `max_elements` を超えた場合、超過分は検査せず `message` にその旨を明記する（黙って全数検査したふりをしない） |

**「テキスト葉」の定義**: 空白以外の直下テキストノード（子孫要素のテキストは
含まない）を持ち、かつ可視（`display:none`/`visibility:hidden` の要素・その
子孫ではない）な要素。例えば `<div>Card Title <span>Details</span></div>`
では `div` と `span` の両方がテキスト葉になる（`div` の直下テキストは
"Card Title "、`span` の直下テキストは "Details"）。この場合 `span` の bbox
は通常 `div` の bbox に包含されるため、`no_overlap_text_leaves` はこの
親子ペアを除外して評価する（入れ子構造を偽陽性として検出しない）。

`no_horizontal_overflow`/`all_text_visible` は viewport 寸法・座標に依存する
ため、採点意図のビューポートを明示したい場合は `check.sh` の `--size WxH`
と組み合わせて使うことを推奨する。

`endpoint_near`/`arrow_direction` の端点取得は `<line>` の `x1/y1/x2/y2`、
`<polyline>/<polygon>` の points 先頭/末尾、`<path>` の
`getPointAtLength(0)`/`getPointAtLength(getTotalLength())`。いずれも要素の
`getScreenCTM()` で画面座標へ変換する（viewBox スケール・transform・入れ子
`<g>` を正しく反映するため）。

`endpoint_near` の `where`:
- `attached`（既定）: 端点が target bbox 内部なら距離0。外部なら bbox 境界
  までの距離。
- `boundary`: 内部でも外部でも bbox 境界までの最短距離（常に非負）。

セレクタが0件マッチ → fail（`selector not found`）。複数マッチ → 最初の
要素を使い、message に件数を注記する。

### `arrow_direction` の `head:"auto"`

対象要素の `getComputedStyle()` から `marker-end`/`marker-start` を読み、
以下の優先順位で矢頭側を自動判定する（黙って判定しない — どの分岐を
とったかを message に必ず残す）:

1. `marker-end` が `none` 以外（`marker-start` の有無は問わない） →
   `head:"end"` と同じ挙動。
2. `marker-end` が `none` かつ `marker-start` が `none` 以外 →
   marker-start が指す `<marker>` 要素の `orient` 属性を見てさらに判定する
   （下記「marker-start の orient 依存性」参照）。単純に `head:"start"` を
   決め打ちしない。
3. 両方 `none`、または両方とも `none` 以外（両端に矢頭がある） →
   `head:"end"` にフォールバックし、message に
   `auto fallback: marker が無い/両端にあるため end とみなした` 旨を残す。

marker を使わず別要素（polygon 等）で矢頭を描く idiom では `head:"auto"`
は effectively `head:"end"` と同じ判定（座標記述順ベース）になる —
その場合は引き続き `head:"<selector>"` を使うこと。**生成物の採点には
`head:"auto"` を既定として推奨する**（marker ベースの矢印なら座標記述順
に依存せず正しく判定でき、marker を使わない図でも従来互換の挙動に
フォールバックするため）。

#### marker-start の orient 依存性（重要）

`marker-start` は `orient` 属性の値によって視覚上の矢頭の向きが逆になる。
`head:"auto"` は marker-start が指す `<marker>` 要素の `orient` 属性の生値
を読み、次の3通りに分けて判定する:

- `orient="auto-start-reverse"`（慣用形）: ブラウザがパス方向を180度反転
  して marker を向けるため、矢頭は end→start 方向（線の外側）を指す →
  `head:"start"` と同じ挙動（`from=end, to=start`）。
- `orient="auto"`（非慣用形。marker-end 用に書く慣用の `orient="auto"` を
  marker-start にそのまま流用した形）: ブラウザはパス方向をそのまま
  marker に適用するため、矢頭は start→end 方向（線の内側）を指す →
  実質 `head:"end"` と同じ挙動（`from=start, to=end`）。marker-end の
  慣用形と見た目が紛らわしいが向きの意味論は逆になるので要注意。
- それ以外（固定角度の `orient`、または `orient` 省略時の SVG既定値
  `"0"`）: 矢頭の向きはパス方向と無関係な固定角度であり、線の start/end
  座標だけからは視覚上の向きを判定できない。`head:"auto"` はこの場合
  **黙って推測せずアサーションを fail** させる（message に理由を残す）。
  この形の矢印を採点したい場合は `head:"<selector>"` で矢頭を描く要素を
  明示すること。

marker-end 側（優先順位1）は「orient=auto でパス方向に沿う」慣用形が
支配的なため `head:"auto"` は常に `head:"end"` 相当として扱う（marker-end
に非慣用の固定角度 `orient` を使う図は本ツールの想定外）。

### eps の目安

テキスト要素の bbox はフォントレンダリングの微差で数px揺れることがあるため、
`contained_in` でラベルを検査する場合などは eps を大きめ（5〜10px程度）に
取ることを推奨する。座標が正確に一致するはずの接続点（`touches` /
`endpoint_near` の `attached`）は eps=0 でも通常は成立する。

## カナリアの実行

```
bash vision/tests/canary.sh
```

実モデル呼び出しなしの決定論カナリア。`tests/fixtures/fixed.svg`（正常な
A→B→C フロー図）と `tests/fixtures/broken.svg`（浮いたコネクタ + 上下逆矢印
の2欠陥だけを仕込んだ同じ図）を使い、`tests/fixtures/canary-assertions.json`
（10アサーション）で偽陽性・偽陰性の両方を検証する。加えて次の回帰テストも
含む: 偽の `__fablize_results` 要素を仕込んだ入力が偽合格しないこと
（`tests/fixtures/forged-results.svg`）、`--out` の親ディレクトリが無くても
自動作成され偽 fail にならないこと、`arrow_direction` の `head` パラメータ
と `endpoints_touch` が座標記述順と矢頭実位置の食い違いを正しく扱えること
（`tests/fixtures/arrow-head-mismatch.svg`）、`no_mirror`/`visible_at_center`
が鏡映テキスト・180度回転・不透明矩形による遮蔽を偽陽性・偽陰性なく検出
すること（`tests/fixtures/mirror-and-occlusion.svg`）、`no_mirror` が HTML
要素に対して黙って pass にならず明確な fail を返すこと
（`tests/fixtures/no-mirror-html-unsupported.html`）、`arrow_direction` の
`head:"auto"` が marker-end/marker-start/marker なしの3種いずれでも
視覚どおりの向きを正しく pass/fail すること
（`tests/fixtures/arrow-auto-head.svg`）、forall 型アサーション（拡張B）が
`tests/fixtures/forall-pass.html`（DOM構造が同一で5欠陥に対応するCSS値だけを
変えた `forall-fail.html` との対比）でテキスト重なり・横はみ出し・ページ高さ
超過・要素遮蔽・対象テキスト不在の5種を偽陽性・偽陰性なく検出すること、
`no_overlap_text_leaves` の `exclude_selector` と `all_text_visible` の
`max_elements` 打ち切り明記を `tests/fixtures/forall-nuance.html` で検証する
こと、`check.sh --size WxH`（拡張A）が実際に headless Chrome の viewport
寸法へ反映されること（`tests/fixtures/size-probe.html` を2種類の `--size` で
評価し、幅の一致と高さの差分一致を確認）。全項目 PASS なら exit 0、1件でも
FAIL があれば WORK_DIR を削除せず残す（事後解析用）。

## 実装メモ（この環境で実証済みの事実）

- headless Chrome (`--headless=new`) は `--screenshot`/`--dump-dom` の処理
  完了後もプロセスを自発的に終了しない（ハングする）。そのため
  `lib.sh` の `vision_wait_for` で poll-and-kill する（60秒 watchdog、
  自己マッチする `pgrep -f` は使わず起動時取得の PID を直接監視）。
- `requestAnimationFrame` は headless の `--dump-dom` +
  `--virtual-time-budget` の組み合わせで実コンポジタフレーム生成に紐づき、
  仮想時間バジェットの実時間側デッドラインとレースして偽陰性
  （結果要素が書き込まれる前に dump-dom が発火する）を起こすことを本機で
  再現した。`setTimeout(fn, 0)` の2段ネストに置き換えて解消した
  （`lib/build_wrapper.py` 参照）。
- `check.sh` のポーリング条件は軽量な `grep` を使う。ここで毎回 `python3`
  を fork すると、その CPU 消費が Chrome 自身のスケジューリングを圧迫し
  同様の偽陰性を誘発することを実機で確認したため。
- 高負荷時に上記の偽陰性（`--virtual-time-budget` の実時間側デッドライン
  競合）が間欠的に発生することを実機で観測した（決定論的な再現コマンドは
  無い。負荷依存）。対策として `--virtual-time-budget` を余裕を持たせた値
  （3000ms→5000ms）にし、watchdog タイムアウト／結果要素未回収の場合に
  限り最大3回まで自動リトライする（アサーション自体の fail はリトライ
  対象外）。
- プロファイルディレクトリ（`--user-data-dir`）のリークには2つの原因が
  あった。(1) 主因: `PROFILE_DIR="$(vision_make_profile_dir)"`
  のようにコマンド置換で呼ぶと、置換はサブシェルで実行されるため、関数内で
  EXIT クリーンアップを自己登録してもサブシェル内のコピーにしか反映されず、
  一度も登録されないまま毎回1ディレクトリ（約150〜230ファイル）がリーク
  していた（`lib.sh` を修正し、呼び出し側で明示的に
  `vision_add_cleanup "$PROFILE_DIR"` を呼ぶ形に変更）。(2) 副次的要因:
  headless Chrome のランチャプロセスは GPU/network/storage/renderer 等の
  ヘルパープロセス群を fork し、早期 kill 時にシャットダウン処理中の遅延
  fork（例: GPU プロセスの再起動）が発生することがある。ランチャの PID
  だけを TERM/KILL しても取りこぼしうるため、`render.sh`/`check.sh` で
  `set -m`（ジョブ制御）を有効にして Chrome を専用プロセスグループで起動し、
  `vision_drain_group` が `pgrep -g`（pgid ベース。親の再親化の影響を
  受けない）で複数ラウンドにわたりグループ全体を確実に空にする。
