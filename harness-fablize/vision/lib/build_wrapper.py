#!/usr/bin/env python3
"""vision/lib/build_wrapper.py

check.sh から呼ばれる内部ヘルパ。入力の .html / .svg を「ラッパー文書」に
変換する。ラッパー文書は headless Chrome の --dump-dom で開かれ、
window.load 後に2ティック（setTimeout(fn, 0) を2段ネスト）待ってから
window.__fablizeGeometry.runAssertions() を実行し、結果 JSON を
<script type="application/json" id="__fablize_results__<nonce>"> へ書き込む。
requestAnimationFrame ではなく setTimeout を使う理由は RUNNER_TEMPLATE 内の
コメント参照（rAF は headless の --dump-dom + --virtual-time-budget 下で
実コンポジタフレーム生成とレースし偽陰性を起こすことを実機で確認したため）。

セキュリティ上重要: 結果要素の id には毎回ランダムなノンス（128bit,
secrets.token_hex）を付与する。採点対象の入力(.svg/.html)自身が
id="__fablize_results" を持つ要素を仕込んでいても（SVG は <script> 要素を
持てる／HTML は言うまでもない）、入力ファイルはこのノンスを事前に知り得ない
ため偽の結果要素を作れない。ノンスは成功時に標準出力へ1行で出す（それ以外は
標準出力に何も書かない）。呼び出し側 (check.sh) はこれを読み取って
grep 条件・extract_results.py の両方に渡し、検証チャネルを入力から
完全に分離する。

usage: build_wrapper.py <input.(html|svg)> <geometry.js> <spec.json> <out.html>

失敗時は stderr にメッセージを出して非0終了する（標準出力には何も書かない）。
成功時は生成したノンスを標準出力へ1行で書いて exit 0。
"""
import json
import re
import secrets
import sys


def fail(msg):
    print("error: " + msg, file=sys.stderr)
    sys.exit(2)


def strip_xml_prolog_and_doctype(svg_text):
    # 先頭の <?xml ... ?> を除去
    svg_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", svg_text, count=1, flags=re.IGNORECASE)
    # 先頭付近の <!DOCTYPE ...> を除去（内部サブセット [...] を含む形は v0 非対応）
    svg_text = re.sub(r"^\s*<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", "", svg_text, count=1, flags=re.IGNORECASE | re.DOTALL)
    return svg_text.strip()


RUNNER_TEMPLATE = """
<script id="__fablize_geometry_js">
%(geometry_js)s
</script>
<script id="__fablize_runner">
(function () {
  var spec = %(spec_json)s;
  function writeResult(out) {
    var el = document.createElement('script');
    el.type = 'application/json';
    el.id = %(result_id_json)s;
    var json;
    try {
      json = JSON.stringify(out);
    } catch (e) {
      json = JSON.stringify({
        results: [{id: 'serialize', type: 'internal', pass: false, actual: null, expected: null,
                   message: 'failed to JSON.stringify results: ' + (e && e.message ? e.message : String(e))}],
        pass_count: 0, fail_count: 1
      });
    }
    el.textContent = json.replace(/</g, '\\u003c');
    document.body.appendChild(el);
  }
  function runWhenReady() {
    // 実装メモ（2026-07-12 に本機で実証済みの環境事実）:
    // 当初は仕様どおり requestAnimationFrame を2回ネストして使っていたが、
    // headless Chrome の --dump-dom + --virtual-time-budget の組み合わせでは
    // rAF が実コンポジタフレーム生成に紐づいており、仮想時間バジェットの
    // 実時間側デッドラインとレースする（同一 fixture・同一環境で5回中0〜3回
    // ランダムに「結果要素が書き込まれる前に dump-dom が発火する」という
    // 偽陰性を再現した）。setTimeout(fn, 0) の2段ネストに置き換えたところ
    // 同条件で5/5連続成功に安定した（virtual-time-budget はペンディング
    // タイマーを仮想時間で確実に進めるため setTimeout は決定論的）。
    // 「load 後にもう数ティック待ってから計測する」という元の意図
    // （レイアウト確定を待つ）は維持しつつ、決定論を優先してこちらを採用する。
    setTimeout(function () {
      setTimeout(function () {
        var out;
        try {
          if (!window.__fablizeGeometry || typeof window.__fablizeGeometry.runAssertions !== 'function') {
            throw new Error('window.__fablizeGeometry.runAssertions is not available (geometry.js failed to load?)');
          }
          out = window.__fablizeGeometry.runAssertions(spec);
        } catch (e) {
          out = {
            results: [{id: 'runner', type: 'internal', pass: false, actual: null, expected: null,
                       message: 'infra_error: ' + (e && e.message ? e.message : String(e))}],
            pass_count: 0, fail_count: 1,
            infra_error: true
          };
        }
        writeResult(out);
      }, 0);
    }, 0);
  }
  if (document.readyState === 'complete') {
    runWhenReady();
  } else {
    window.addEventListener('load', runWhenReady);
  }
})();
</script>
"""


def main():
    if len(sys.argv) != 5:
        fail("usage: build_wrapper.py <input.(html|svg)> <geometry.js> <spec.json> <out.html>")
    input_path, geometry_path, spec_path, out_path = sys.argv[1:5]

    try:
        with open(geometry_path, "r", encoding="utf-8") as f:
            geometry_js = f.read()
    except OSError as e:
        fail("geometry.js を読み込めません: %s (%s)" % (geometry_path, e))

    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec_text = f.read()
    except OSError as e:
        fail("spec ファイルを読み込めません: %s (%s)" % (spec_path, e))

    try:
        spec_obj = json.loads(spec_text)
    except json.JSONDecodeError as e:
        fail("spec が JSON として不正です: %s (%s)" % (spec_path, e))

    if not isinstance(spec_obj, dict) or not isinstance(spec_obj.get("assertions"), list):
        fail("spec に assertions 配列がありません: %s" % spec_path)
    if len(spec_obj["assertions"]) == 0:
        fail("assertions が空配列です（空集合を黙って合格にしないため拒否します）: %s" % spec_path)

    # geometry.js 内に "</script>" 相当の文字列は無い前提だが、念のため防御
    geometry_js_safe = geometry_js.replace("</script", "<\\/script")
    spec_json_literal = json.dumps(spec_obj)
    # spec 側にも </script> が来うる（selector文字列等）ので同様に防御
    spec_json_literal_safe = spec_json_literal.replace("</script", "<\\/script")

    # 結果要素の id は毎回ランダムなノンスを付与する（入力からの検証チャネル
    # 分離。モジュールdocstring参照）。16進数のみなので shell の grep -E /
    # Python の正規表現に生のまま埋め込んでも安全。
    nonce = secrets.token_hex(16)
    result_id = "__fablize_results__" + nonce

    runner_block = RUNNER_TEMPLATE % {
        "geometry_js": geometry_js_safe,
        "spec_json": spec_json_literal_safe,
        "result_id_json": json.dumps(result_id),
    }

    ext = input_path.rsplit(".", 1)[-1].lower() if "." in input_path else ""

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            input_text = f.read()
    except OSError as e:
        fail("入力ファイルを読み込めません: %s (%s)" % (input_path, e))

    if ext == "svg":
        svg_body = strip_xml_prolog_and_doctype(input_text)
        doc = (
            "<!doctype html>\n"
            "<html><head><meta charset=\"utf-8\">"
            "<style>html,body{margin:0;padding:0;}</style></head>\n"
            "<body>\n" + svg_body + "\n" + runner_block + "\n</body></html>\n"
        )
    elif ext == "html" or ext == "htm":
        # 自己完結 HTML のみサポート（v0）。</body> の直前にランナーを注入する。
        # 大文字小文字混在の </BODY> 等も一応拾う。
        m = re.search(r"</body\s*>", input_text, flags=re.IGNORECASE)
        if m:
            idx = m.start()
            doc = input_text[:idx] + runner_block + "\n" + input_text[idx:]
        else:
            # </body> が無い自己完結断片HTMLも許容し、末尾に追記する。
            doc = input_text + runner_block
    else:
        fail("入力ファイルの拡張子は .html / .svg のみ対応しています: %s" % input_path)

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(doc)
    except OSError as e:
        fail("ラッパー文書を書き込めません: %s (%s)" % (out_path, e))

    # 呼び出し側 (check.sh) がこのノンスを読み取り、grep 条件・
    # extract_results.py の両方に渡す。成功パスで標準出力に書くのはこの
    # 1行だけ。
    print(nonce)


if __name__ == "__main__":
    main()
