# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for macro_search.html (issue #9).

The first class needs no browser. The other classes open the HTML file from
disk in headless Firefox and run test_cases/search.json, the same cases that
test_shared_cases.py runs against macro_search.py. They need Playwright:

  uv run --with playwright python -m unittest test_macro_search_html

Without Playwright or Firefox, those classes are skipped.
"""

import base64
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

import macro_search as ms

HERE = Path(__file__).parent
HTML = HERE / "macro_search.html"
CASES = json.loads((HERE / "test_cases" / "search.json").read_text(encoding="utf-8"))
SOURCE = HTML.read_text(encoding="utf-8")


def script_hash(source: str) -> str:
    script = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    return "sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()


class StaticChecks(unittest.TestCase):
    def test_csp_allows_only_this_script_and_no_network(self):
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', SOURCE).group(1)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-inline' ;", csp)
        self.assertNotRegex(csp, r"script-src[^;]*unsafe-inline")
        self.assertIn(f"script-src '{script_hash(SOURCE)}'", csp,
                      f"update the CSP in macro_search.html to script-src '{script_hash(SOURCE)}'")

    def test_one_inline_script_and_no_handlers(self):
        self.assertEqual(SOURCE.count("<script"), 1)
        self.assertNotRegex(SOURCE, r"<[^>]+\son[a-z]+=")
        self.assertNotIn("localStorage", SOURCE)
        self.assertNotIn("fetch(", SOURCE)

    def test_css_matches_the_python_app(self):
        self.assertIn(ms.STYLE.strip(), SOURCE)


def firefox():
    """A Playwright Firefox browser, or None to skip."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    pw = sync_playwright().start()
    for kwargs in ({"channel": "moz-firefox"}, {}):
        try:
            return pw, pw.firefox.launch(headless=True, **kwargs)
        except Exception:
            continue
    pw.stop()
    return None, None


class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw, cls.browser = firefox()
        if not cls.browser:
            raise unittest.SkipTest("Playwright Firefox is not available")
        cls.page = cls.browser.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.on("console", lambda m: cls.errors.append(m.text) if m.type == "error" else None)
        cls.page.goto(HTML.resolve().as_uri())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def tearDown(self):
        self.assertEqual(self.errors, [])


class SharedCasesInBrowser(BrowserTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page.evaluate("([m, g]) => window.macroSearchTest.load(m, g)",
                          [{"macros": CASES["macros"], "exported_at": CASES["exported_at"]},
                           {"groups": CASES["groups"]}])

    def test_search(self):
        for case in CASES["search"]:
            with self.subTest(q=case["q"], filters=case["filters"], sort=case["sort"]):
                got = self.page.evaluate("c => window.macroSearchTest.search(c.q, c.filters, c.sort)", case)
                self.assertEqual(got, case["results"])

    def test_marked(self):
        got = self.page.evaluate("ts => ts.map(t => window.macroSearchTest.marked(t))",
                                 [c["text"] for c in CASES["marked"]])
        self.assertEqual(got, [c["html"] for c in CASES["marked"]])

    def test_html_to_text(self):
        got = self.page.evaluate("hs => hs.map(h => window.macroSearchTest.htmlToText(h))",
                                 [c["html"] for c in CASES["html_to_text"]])
        self.assertEqual(got, [c["text"] for c in CASES["html_to_text"]])

    def test_linked(self):
        got = self.page.evaluate("ts => ts.map(t => window.macroSearchTest.linked(t))",
                                 [c["text"] for c in CASES["linked"]])
        self.assertEqual(got, [c["html"] for c in CASES["linked"]])

    def test_names_choices_and_export_label(self):
        t = "window.macroSearchTest"
        self.assertEqual(self.page.evaluate(f"ids => ids.map(i => {t}.availableFor(i))",
                                            [c["id"] for c in CASES["available_for"]]),
                         [c["text"] for c in CASES["available_for"]])
        self.assertEqual(self.page.evaluate(f"() => {t}.groupChoices()"), CASES["group_choices"])
        self.assertEqual(self.page.evaluate(f"() => {t}.fieldChoices()"), CASES["field_choices"])
        self.assertEqual(self.page.evaluate(f"() => {t}.categoryChoices()"), CASES["category_choices"])
        self.assertEqual(self.page.evaluate(f"vs => vs.map(v => {t}.exportLabel(v))",
                                            [c["value"] for c in CASES["export_label"]]),
                         [c["label"] for c in CASES["export_label"]])

    def test_tag_filters_and_sorting(self):
        t = "window.macroSearchTest"
        self.assertEqual(self.page.evaluate(f"() => {t}.facetChoices()"), CASES["facet_choices"])
        self.assertEqual(self.page.evaluate(f"cs => cs.map(c => {t}.parseFilters(c.params))", CASES["parse_filters"]),
                         [c["filters"] for c in CASES["parse_filters"]])
        self.assertEqual(self.page.evaluate(f"cs => cs.map(c => {t}.parseSort(c.value))", CASES["parse_sort"]),
                         [c["sort"] for c in CASES["parse_sort"]])
        self.assertEqual(self.page.evaluate(f"cs => cs.map(c => {t}.nextSort(c.current, c.column))", CASES["next_sort"]),
                         [c["sort"] for c in CASES["next_sort"]])
        self.assertEqual(self.page.evaluate(f"cs => cs.map(c => {t}.searchQuery(...c.args))", CASES["search_query"]),
                         [c["query"] for c in CASES["search_query"]])


class LoadAndSearch(BrowserTestCase):
    """The way a colleague uses the page: choose files, search, open a macro."""

    def test_load_files_search_and_open_a_macro(self):
        # A fresh page: after a load, the app goes back to the last search or macro page.
        self.page.goto(HTML.resolve().as_uri())
        self.page.wait_for_selector("#files")
        with tempfile.TemporaryDirectory() as tmp:
            macros = Path(tmp) / "macros.json"
            groups = Path(tmp) / "groups.json"
            macros.write_text(json.dumps({"macros": CASES["macros"], "count": len(CASES["macros"]),
                                          "exported_at": CASES["exported_at"]}))
            groups.write_text(json.dumps({"groups": CASES["groups"], "count": len(CASES["groups"])}))
            self.page.set_input_files("#files", [str(macros), str(groups)])
        self.page.wait_for_selector("form#search")
        self.assertEqual(self.page.inner_text(".count"),
                         f"{len(CASES['macros'])} of {len(CASES['macros'])} macros · "
                         "Macros exported 2026-11-27 18:00 UTC")

        self.page.click("summary:text('Filter by tag')")
        self.page.click(".tagpicker .chip:text('active')")
        self.page.wait_for_function("location.hash === '#/?status=active&picker=open'")
        self.assertTrue(self.page.eval_on_selector("details.tagpicker", "d => d.open"))
        self.page.fill("input[name=q]", "refund")
        self.page.click("button[type=submit]")
        self.page.wait_for_function("location.hash === '#/?q=refund&status=active'")
        self.assertEqual(self.page.inner_text(".selected"), "Status: active ✕ · Clear all")
        self.assertEqual(self.page.inner_text(".count"),
                         f"1 of {len(CASES['macros'])} macros · Macros exported 2026-11-27 18:00 UTC")
        self.assertEqual(self.page.inner_html(".result h2 a"), "Donation::<mark>Refund</mark> request")
        self.assertIn("Donor Support", self.page.inner_text(".result .meta"))

        self.page.click(".result h2 a")
        self.page.wait_for_selector("h2:text('Actions')")
        self.assertEqual(self.page.inner_text("h1"), "Donation::Refund request")
        self.assertIn('<span class="dc">{{ticket.requester.first_name}}</span>', self.page.inner_html("div.reply"))
        self.page.click("text=Back to search")
        self.page.wait_for_selector("form#search")
        self.assertEqual(self.page.input_value("input[name=q]"), "refund")

    def test_tag_chips_or_inside_a_group_and_between_groups(self):
        self.page.evaluate("([m, g]) => window.macroSearchTest.load(m, g)",
                           [{"macros": CASES["macros"]}, {"groups": CASES["groups"]}])
        self.page.evaluate("location.hash = '#/?q=zebra&picker=open'")
        self.page.wait_for_selector("details.tagpicker[open]")
        self.page.click(".tagpicker .chip:text('Tie')")
        self.page.wait_for_function("location.hash === '#/?q=zebra&category=Tie&picker=open'")
        self.assertEqual(self.page.locator(".result").count(), 2)
        self.page.click(".tagpicker .chip:text('Cut')")
        self.page.wait_for_function("location.hash === '#/?q=zebra&category=Tie&category=Cut&picker=open'")
        self.assertEqual(self.page.locator(".result").count(), 3)
        self.assertEqual(self.page.inner_text(".selected"), "Category: Tie ✕ or Cut ✕ · Clear all")
        self.page.click(".tagpicker .chip:text('inactive')")
        self.page.wait_for_selector("text=0 of 13 macros")
        self.page.click(".selected .chip:text('inactive ✕')")
        self.page.wait_for_selector("text=3 of 13 macros")
        # A chip on a result adds its value, and a submitted query keeps the filters.
        self.page.evaluate("location.hash = '#/'")
        self.page.wait_for_selector(".result")
        self.page.click(".result .meta a.chip:text('(no category)')")
        self.page.wait_for_function("location.hash === '#/?category=%3A%3A'")
        self.assertTrue(self.page.inner_text(".count").startswith("3 of "))
        self.page.fill("input[name=q]", "hello")
        self.page.click("button[type=submit]")
        self.page.wait_for_function("location.hash === '#/?q=hello&category=%3A%3A'")
        self.assertEqual(self.page.locator(".result").count(), 1)

    def test_table_view_and_sorting(self):
        self.page.evaluate("([m, g]) => window.macroSearchTest.load(m, g)",
                           [{"macros": CASES["macros"]}, {"groups": CASES["groups"]}])
        self.page.evaluate("location.hash = '#/?category=Tie&category=Donation'")
        self.page.wait_for_selector(".view-toggle")
        self.page.click(".view-toggle a:text('Table')")
        self.page.wait_for_selector("table.results")
        self.page.click("th a:text('Updated')")
        self.page.wait_for_function("location.hash.endsWith('view=table&sort=-updated')")
        expected = next(c for c in CASES["search"] if c["sort"] == "-updated" and c["filters"])["ids"]
        ids = self.page.eval_on_selector_all("table.results tbody td:first-child a",
                                             "as => as.map(a => Number(a.hash.match(/macro\\/(\\d+)/)[1]))")
        self.assertEqual(ids, expected)
        self.assertEqual(self.page.get_attribute("th[aria-sort=descending] a", "href"),
                         "#/?category=Tie&category=Donation&view=table&sort=updated")

    def test_add_groups_later_keeps_the_search(self):
        # A fresh page, so that no file from another test is loaded (issue #18).
        self.page.goto(HTML.resolve().as_uri())
        self.page.wait_for_selector("#files")
        search = "#/?q=hello&group=100&view=table"
        with tempfile.TemporaryDirectory() as tmp:
            macros = Path(tmp) / "macros.json"
            groups = Path(tmp) / "groups.json"
            macros.write_text(json.dumps({"macros": CASES["macros"]}))
            groups.write_text(json.dumps({"groups": CASES["groups"]}))
            self.page.set_input_files("#files", [str(macros)])
            self.page.wait_for_selector("form#search")
            self.page.evaluate(f"location.hash = '{search}'")
            self.page.wait_for_selector("text=Available for: 100 ✕")
            self.assertIn("Group names are missing.", self.page.inner_text(".notice"))

            # The load screen says that the macros stay, and goes back to the search.
            self.page.evaluate("location.hash = '#/load'")
            self.page.wait_for_selector("text=Choose groups.json to add group names. The loaded macros stay.")
            self.page.click("#show")
            self.page.wait_for_function(f"location.hash === '{search}'")

            # A file that is not groups.json is an error, and the search stays.
            self.page.set_input_files("#groups-file", [str(macros)])
            self.page.wait_for_selector("text=macros.json is not groups.json.")
            self.assertEqual(self.page.evaluate("location.hash"), search)

            self.page.set_input_files("#groups-file", [str(groups)])
            self.page.wait_for_selector("text=Available for: Donor Support ✕")
        self.assertEqual(self.page.evaluate("location.hash"), search)
        self.assertEqual(self.page.locator(".notice").count(), 0)
        self.assertEqual(self.page.locator(".error").count(), 0)
        self.assertEqual(self.page.input_value("input[name=q]"), "hello")
        self.assertIn("Donor Support", self.page.inner_text("table.results"))

    def test_escaped_title_renders_as_text(self):
        self.page.evaluate("([m, g]) => window.macroSearchTest.load(m, g)",
                           [{"macros": CASES["macros"]}, {"groups": CASES["groups"]}])
        self.page.evaluate("location.hash = '#/macro/9'")
        self.page.wait_for_selector("h2:text('Actions')")
        self.assertEqual(self.page.inner_text("h1"), "<b>Escape</b> & \"quotes\" 'single'")
        self.assertEqual(self.page.locator("h1 b").count(), 0)

    def test_reply_links_open_in_a_new_tab(self):
        self.page.evaluate("([m, g]) => window.macroSearchTest.load(m, g)",
                           [{"macros": CASES["macros"]}, {"groups": CASES["groups"]}])
        self.page.evaluate("location.hash = '#/macro/12'")
        self.page.wait_for_selector("h2:text('Actions')")
        links = self.page.eval_on_selector_all(
            "div.reply a", "as => as.map(a => [a.getAttribute('href'), a.target, a.rel, a.textContent])")
        self.assertEqual(links, [[url, "_blank", "noopener noreferrer", url]
                                 for url in ("https://roadmaps.example.org/ios/", "https://www.example.org")])
        self.page.evaluate("location.hash = '#/macro/13'")
        self.page.wait_for_selector("h1:text('Links::Unsafe and dynamic')")
        self.assertEqual(self.page.locator("div.reply a[href^='javascript']").count(), 0)
        self.assertEqual(self.page.locator("div.reply a .dc").inner_text(), "{{ticket.id}}")


if __name__ == "__main__":
    unittest.main()
