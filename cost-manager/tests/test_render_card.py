#!/usr/bin/env python3
"""Chrome 用カード HTML の表示内容を検証するテスト。"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cost_lib as lib  # noqa: E402
import render_image  # noqa: E402


class TestBuildCardHtml(unittest.TestCase):
    def setUp(self):
        self.report = lib.Report(
            total_usd=1.0,
            total_jpy=150.0,
            usd_jpy=150.0,
            pricing_as_of="2026-08-24",
        )
        self.meta = {
            "task_name": "テストタスク",
            "date_jst": "2026-08-24",
            "start_jst": "10:00",
            "end_jst": "11:00",
            "duration": "1時間",
        }

    def build_html(self, **meta):
        html, _ = render_image._build_card_html(self.report, self.meta | meta, 1000)
        return html

    def test_includes_active_time_after_elapsed_label(self):
        html = self.build_html(active_text="45分")

        self.assertIn("経過 1時間", html)
        self.assertNotIn("実働", html)
        self.assertIn("実処理 45分", html)
        self.assertLess(html.index("経過 1時間"), html.index("実処理 45分"))

    def test_uses_dash_when_active_time_is_missing(self):
        html = self.build_html()

        self.assertIn("実処理 —", html)

    def test_escapes_active_time(self):
        html = self.build_html(active_text="<b>1時間")

        self.assertIn("実処理 &lt;b&gt;1時間", html)
        self.assertNotIn("実処理 <b>1時間", html)


if __name__ == "__main__":
    unittest.main()
