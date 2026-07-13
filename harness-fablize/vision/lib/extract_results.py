#!/usr/bin/env python3
"""vision/lib/extract_results.py

check.sh から呼ばれる内部ヘルパ。--dump-dom でダンプされた DOM ログから
<script type="application/json" id="__fablize_results__<nonce>"> の中身を
取り出し、JSON として妥当か検証してから標準出力へ吐く。

usage:
  extract_results.py <dump_log_file> <nonce>

<nonce> は build_wrapper.py が今回の実行専用に生成したランダムトークン
（check.sh が標準出力から受け取って渡す）。これを完全一致で要求することで、
採点対象の入力(.svg/.html)自身が id="__fablize_results" を持つ偽の要素を
仕込んでいても（SVG は <script> 要素を持てる／HTML は言うまでもない）、
偽物が拾われることは無い（入力ファイルはノンスを事前に知り得ないため）。
検証チャネルを入力コンテンツから分離するのがこの引数の目的。

さらに、ノンスが一致する結果要素が2件以上見つかった場合も「想定外の状態」
としてツール故障（インフラ異常）扱いにする（本来 1 件しか存在し得ない）。

「結果要素が回収できない」（未挿入・複数存在・壊れたJSON・想定キー欠落）は
すべてツール自身の故障として扱う（check.sh 側で exit 2 に対応させる）。
"""
import json
import re
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: extract_results.py <dump_log_file> <nonce>", file=sys.stderr)
        sys.exit(2)
    log_path, nonce = sys.argv[1], sys.argv[2]

    def die(msg):
        print("error: " + msg, file=sys.stderr)
        sys.exit(2)

    if not re.fullmatch(r"[0-9a-fA-F]+", nonce or ""):
        die("nonce の形式が不正です（16進数文字列を想定）: %r" % (nonce,))
        return

    result_re = re.compile(
        r'<script[^>]*\bid="__fablize_results__' + re.escape(nonce) + r'"[^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            dump = f.read()
    except OSError as e:
        die("dump ログを読み込めません: %s (%s)" % (log_path, e))
        return

    matches = result_re.findall(dump)
    if len(matches) == 0:
        die("__fablize_results 要素が dump-dom 出力内に見つかりません（runner が実行されなかった可能性）")
        return
    if len(matches) > 1:
        die("__fablize_results 要素が複数見つかりました（想定外。インフラ異常として扱います）")
        return

    raw = matches[0]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        die("__fablize_results の中身が JSON として不正です: %s" % e)
        return

    if not isinstance(obj, dict) or not isinstance(obj.get("results"), list) \
            or "pass_count" not in obj or "fail_count" not in obj:
        die("__fablize_results の形が想定外です（results/pass_count/fail_count が必要）")
        return

    print(json.dumps(obj))
    sys.exit(0)


if __name__ == "__main__":
    main()
