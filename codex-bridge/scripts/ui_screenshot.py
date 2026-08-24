#!/usr/bin/env python3
"""headless Chrome で複数ビューポートの UI スクリーンショットを撮影する。"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_VIEWPORTS = "1440x900,768x1024,375x812"
DEFAULT_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
VIEWPORT = re.compile(r"([1-9][0-9]*)x([1-9][0-9]*)$")


class ArgumentParser(argparse.ArgumentParser):
    """利用者が直せる引数エラーを、このスクリプトの exit 1 にそろえる。"""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def parse_viewports(value: str) -> list[tuple[int, int]]:
    viewports = []
    for item in value.split(","):
        match = VIEWPORT.fullmatch(item.strip())
        if not match:
            raise ValueError(f"不正なビューポートです: {item!r}（例: 1440x900）")
        viewports.append((int(match.group(1)), int(match.group(2))))
    if not viewports:
        raise ValueError("--viewports は少なくとも 1 つ必要です")
    return viewports


def is_valid_png(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return f.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


def executable_path(candidate: str | Path | None) -> str | None:
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    except OSError:
        pass
    return None


def resolve_chrome(explicit: str | None) -> str | None:
    """指定値、環境変数、macOS の標準位置、PATH の順で Chrome を探す。"""
    if explicit:
        return executable_path(explicit)

    chrome_bin = executable_path(os.environ.get("CHROME_BIN"))
    if chrome_bin:
        return chrome_bin
    chrome_bin = executable_path(DEFAULT_CHROME_PATH)
    if chrome_bin:
        return chrome_bin
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        chrome_bin = executable_path(found)
        if chrome_bin:
            return chrome_bin
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(description="headless Chrome で UI スクリーンショットを撮影する")
    parser.add_argument("--url", help="撮影する URL")
    parser.add_argument("--html", help="撮影するローカル HTML ファイル")
    parser.add_argument("--out-dir", required=True, help="PNG の出力ディレクトリ")
    parser.add_argument("--viewports", default=DEFAULT_VIEWPORTS,
                        help=f"幅x高さのカンマ区切り（既定: {DEFAULT_VIEWPORTS}）")
    parser.add_argument("--chrome-bin", default=None, help="Chrome / Chromium 実行ファイル")
    parser.add_argument("--prefix", default="shot", help="出力ファイル名の接頭辞（既定: shot）")
    parser.add_argument("--wait-ms", type=int, default=0,
                        help="正の値なら Chrome の仮想時間待機に使う（既定: 0）")
    parser.add_argument("--timeout-sec", type=float, default=60.0,
                        help="1 枚あたりのタイムアウト秒（既定: 60）")
    return parser


def source_url(args: argparse.Namespace) -> str | None:
    if bool(args.url) == bool(args.html):
        eprint("エラー: --url または --html のどちらか一方だけを指定してください")
        return None
    if args.url:
        return args.url

    html = Path(args.html).expanduser().resolve()
    if not html.is_file():
        eprint(f"エラー: HTML ファイルを読めません: {html}")
        return None
    return html.as_uri()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url = source_url(args)
    if url is None:
        return 1
    try:
        viewports = parse_viewports(args.viewports)
    except ValueError as exc:
        eprint(f"エラー: {exc}")
        return 1
    if args.timeout_sec <= 0:
        eprint("エラー: --timeout-sec は 0 より大きくしてください")
        return 1

    chrome = resolve_chrome(args.chrome_bin)
    if chrome is None:
        eprint("エラー: Chrome / Chromium が見つかりません。--chrome-bin または CHROME_BIN を指定してください")
        return 4

    out_dir = Path(args.out_dir).expanduser().resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        eprint(f"エラー: 出力ディレクトリを作成できません: {exc}")
        return 1

    succeeded = 0
    for width, height in viewports:
        output = out_dir / f"{args.prefix}-{width}x{height}.png"
        # 前回の有効な PNG を今回の撮影成功と誤認しないよう、対象出力だけを先に除く。
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            eprint(f"失敗 {width}x{height}: 既存の出力を削除できません: {exc}")
            continue
        command = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--screenshot={output}",
            f"--window-size={width}x{height}",
        ]
        if args.wait_ms > 0:
            command.append(f"--virtual-time-budget={args.wait_ms}")
        command.append(url)
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=args.timeout_sec)
        except subprocess.TimeoutExpired:
            eprint(f"失敗 {width}x{height}: {args.timeout_sec:g} 秒でタイムアウトしました")
            continue
        except OSError as exc:
            eprint(f"失敗 {width}x{height}: Chrome を実行できません: {exc}")
            continue

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            eprint(f"失敗 {width}x{height}: Chrome が exit {result.returncode} で終了しました{suffix}")
            continue
        if not is_valid_png(output):
            eprint(f"失敗 {width}x{height}: 有効な PNG が生成されませんでした: {output}")
            continue
        print(output)
        succeeded += 1

    if succeeded == len(viewports):
        return 0
    if succeeded == 0:
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
