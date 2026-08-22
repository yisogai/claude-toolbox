#!/usr/bin/env python3
"""プロンプトテンプレートのプレースホルダ（`{{KEY}}`）を埋めて stdout へ出す。

未充足のプレースホルダが残った場合は **exit 1**（曖昧なままの依頼を Codex に投げない）。

実行例:
  python3 render_prompt.py templates/prompts/implement.md \\
      --set OBJECTIVE="X を実装する" --set-file CONTEXT=notes.md > /tmp/prompt.md
  python3 render_prompt.py review --set-file DIFF=/tmp/diff.patch

第 1 引数はテンプレートのパス、または `templates/prompts/` 配下の名前（`implement` / `review`）。
終了コード: 0=正常 / 1=未充足プレースホルダ / 2=テンプレート・入力ファイルが読めない。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_lib as lib  # noqa: E402

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def resolve_template(spec: str) -> Path:
    p = Path(spec).expanduser()
    if p.exists():
        return p
    for cand in (lib.templates_dir() / "prompts" / spec,
                 lib.templates_dir() / "prompts" / f"{spec}.md",
                 lib.templates_dir() / spec):
        if cand.exists():
            return cand
    return p


def parse_kv(pairs, is_file: bool) -> dict:
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"KEY=VALUE 形式ではありません: {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"KEY が空です: {item}")
        if is_file:
            path = Path(v).expanduser()
            out[k] = path.read_text(encoding="utf-8", errors="replace")
        else:
            out[k] = v
    return out


def render(text: str, values: dict):
    """置換結果と、未充足キー一覧を返す。"""
    missing = []

    def sub(m):
        k = m.group(1)
        if k in values:
            return values[k]
        missing.append(k)
        return m.group(0)

    return PLACEHOLDER.sub(sub, text), sorted(set(missing))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="render_prompt.py",
                                description="{{KEY}} を埋めてプロンプトを出力する")
    p.add_argument("template", help="テンプレートのパス、または implement / review")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--set-file", dest="set_files", action="append", default=[], metavar="KEY=PATH")
    p.add_argument("--out", default=None, help="ファイルに書き出す（既定は stdout）")
    p.add_argument("--allow-missing", action="store_true",
                   help="未充足プレースホルダがあっても出力して exit 0（既定は exit 1）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    tpl = resolve_template(args.template)
    try:
        text = tpl.read_text(encoding="utf-8")
    except OSError as e:
        lib.eprint(f"エラー: テンプレートを読めません: {e}")
        return 2

    try:
        values = parse_kv(args.sets, False)
        values.update(parse_kv(args.set_files, True))
    except (ValueError, OSError) as e:
        lib.eprint(f"エラー: {e}")
        return 2

    rendered, missing = render(text, values)

    if missing and not args.allow_missing:
        lib.eprint("エラー: 未充足のプレースホルダがあります: " + ", ".join("{{%s}}" % m for m in missing))
        return 1

    if args.out:
        lib.atomic_write_text(Path(args.out).expanduser(), rendered)
    else:
        sys.stdout.write(rendered)
    if missing:
        lib.eprint("警告: 未充足のまま出力しました: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
