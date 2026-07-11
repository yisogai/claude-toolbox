#!/usr/bin/env python3
"""merge_claude_md.py — ~/.claude/CLAUDE.md に opus-fable-protocol.md の本文を
「作業プロトコル」節として挿入（既存があれば置換）した新しい CLAUDE.md 本文を
標準出力へ書き出す。

- ファイルへの書き込みは一切行わない（stdout に出すだけ）。dry-run/apply の
  制御・実書き込みは呼び出し側（install.sh）が行う。
- 既存に同名節（見出しが同じ prefix で始まる節）があれば置換し、無ければ
  ファイル末尾に追加する。同じ入力に対して複数回実行しても出力が変わらない
  （冪等）。
- この節以外の既存節・先頭の前置き（タイトル行等）は一切変更しない。

使い方:
  python3 merge_claude_md.py <CLAUDE.md> <opus-fable-protocol.md>

（この公開版は fablize リポジトリの原本 harness/lib/merge_claude_md.py から
 model-policy 節の書き換えロジックを取り除いた簡略版。model-policy はユーザー
 固有の運用規範のため、このハーネスの配布物には含めない。）
"""
import sys

PROTO_HEADER_PREFIX = "## 作業プロトコル"


def extract_section_body(path: str) -> str:
    """タイトル行・HTMLコメント等の前置きを除き、最初の '## ' 見出し行から
    ファイル末尾までを本文として返す（末尾は改行1つに正規化。末尾改行なし）。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:]).rstrip("\n")
    raise ValueError(f"'## ' 見出しが見つかりません: {path}")


def split_sections(text: str):
    """CLAUDE.md 本文を (preamble, [ (header_line, block_text) ... ]) に分割する。
    block_text はその見出し行を含む本文（末尾改行なし）。"""
    lines = text.splitlines()
    header_idx = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not header_idx:
        return text.rstrip("\n"), []
    preamble = "\n".join(lines[: header_idx[0]]).rstrip("\n")
    sections = []
    for n, start in enumerate(header_idx):
        end = header_idx[n + 1] if n + 1 < len(header_idx) else len(lines)
        block = "\n".join(lines[start:end]).rstrip("\n")
        sections.append((lines[start], block))
    return preamble, sections


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "使い方: merge_claude_md.py <CLAUDE.md> <opus-fable-protocol.md>",
            file=sys.stderr,
        )
        return 1

    claude_md_path, proto_path = sys.argv[1:3]

    try:
        with open(claude_md_path, encoding="utf-8") as f:
            original = f.read()
        new_proto_body = extract_section_body(proto_path)
    except OSError as e:
        print(f"エラー: ファイルを読めません: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    preamble, sections = split_sections(original)

    proto_idx = next(
        (i for i, (h, _) in enumerate(sections) if h.startswith(PROTO_HEADER_PREFIX)),
        None,
    )
    new_proto_entry = (new_proto_body.splitlines()[0], new_proto_body)
    if proto_idx is not None:
        sections[proto_idx] = new_proto_entry
    else:
        sections.append(new_proto_entry)

    parts = []
    if preamble.strip():
        parts.append(preamble)
    parts.extend(block for _, block in sections)

    new_text = "\n\n".join(parts).rstrip("\n") + "\n"
    sys.stdout.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
