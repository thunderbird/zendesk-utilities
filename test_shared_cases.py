# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run test_cases/search.json against macro_search.py (issue #9).

test_macro_search_html.py runs the same cases against macro_search.html, so
the two apps must give the same results. The macros in the file are made up.
"""

import json
import unittest
from pathlib import Path

import macro_search as ms

CASES = json.loads((Path(__file__).parent / "test_cases" / "search.json").read_text(encoding="utf-8"))


def label(case) -> str:
    return f"q={case['q']!r} status={case['status']} group={case['group']!r} field={case['field']!r} category={case['category']!r}"


class SharedCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = ms.MacroIndex(CASES["macros"], CASES["groups"], exported_at=CASES["exported_at"])

    def test_search(self):
        for case in CASES["search"]:
            with self.subTest(label(case)):
                res = self.index.search(case["q"], case["status"], case["group"], case["field"], case["category"])
                got = [{"id": r["id"], "title_html": ms.marked(r["title"]),
                        "snippet_html": ms.marked(r["snippet"])} for r in res]
                self.assertEqual(got, case["results"])
                self.assertEqual([r["id"] for r in res], case["ids"])

    def test_marked(self):
        for case in CASES["marked"]:
            with self.subTest(case["text"]):
                self.assertEqual(ms.marked(case["text"]), case["html"])

    def test_html_to_text(self):
        for case in CASES["html_to_text"]:
            with self.subTest(case["html"]):
                self.assertEqual(ms.html_to_text(case["html"]), case["text"])

    def test_names_choices_and_export_label(self):
        macros = {m["id"]: m for m in CASES["macros"]}
        for case in CASES["available_for"]:
            self.assertEqual(self.index.available_for(macros[case["id"]]), case["text"])
        self.assertEqual([list(c) for c in self.index.group_choices()], CASES["group_choices"])
        self.assertEqual([list(c) for c in self.index.field_choices()], CASES["field_choices"])
        self.assertEqual([list(c) for c in self.index.category_choices()], CASES["category_choices"])
        for case in CASES["export_label"]:
            self.assertEqual(ms.export_label(case["value"]), case["label"])


if __name__ == "__main__":
    unittest.main()
