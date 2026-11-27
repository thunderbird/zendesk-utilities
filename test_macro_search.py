# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline tests for macro_search.py. The macros here are made up."""

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import macro_search as ms


def macro(i, title, reply="", tags="", active=True, groups=None, extra=None, description=""):
    acts = []
    if reply:
        acts.append({"field": "comment_value_html", "value": reply})
    if tags:
        acts.append({"field": "current_tags", "value": tags})
    acts += extra or []
    return {
        "id": i, "title": title, "raw_title": title, "description": description,
        "active": active, "default": False, "position": i, "actions": acts,
        "restriction": {"type": "Group", "id": groups[0], "ids": groups} if groups else None,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
        "url": f"https://tbpro.zendesk.com/api/v2/macros/{i}.json",
    }


MACROS = [
    macro(1, "Donation::Refund request", "<p>We sent your <b>refund</b>.</p>", "donor refund",
          groups=[100]),
    macro(2, "Thunderbird::Translate emails", "<p>Open the add-ons page.</p><p>Search for a translator.</p>",
          "tb_translate"),
    macro(3, "Old greeting", "<p>Hello, the refund team here.</p>", active=False,
          extra=[{"field": "priority", "value": "high"}]),
    macro(4, "Café closing", "Bye", groups=[999]),
]
GROUPS = [{"id": 100, "name": "Donor Support"}, {"id": 999, "name": "Old team", "deleted": True}]


class Text(unittest.TestCase):
    def test_html_to_text_keeps_paragraphs_and_drops_scripts(self):
        self.assertEqual(ms.html_to_text("<p>Hi&nbsp;<b>there</b></p><p>Bye</p><script>x()</script>"),
                         "Hi there\n\nBye")

    def test_list_items(self):
        self.assertEqual(ms.html_to_text("<ul><li>one</li><li>two</li></ul>"), "- one\n\n- two")
        self.assertEqual(ms.html_to_text("<ul><li><p>one</p></li><li><p>two</p></li></ul>"),
                         "- one\n\n- two")

    def test_reply_text_and_tags(self):
        self.assertEqual(ms.reply_text(MACROS[0]), "We sent your refund.")
        self.assertEqual(ms.tags(MACROS[0]), ["donor", "refund"])

    def test_plain_comment_value(self):
        m = macro(9, "x", extra=[{"field": "comment_value", "value": " plain text "}])
        self.assertEqual(ms.reply_text(m), "plain text")


class Query(unittest.TestCase):
    def test_words_are_prefix_and(self):
        self.assertEqual(ms.fts_query("refun team"), '"refun"* "team"*')

    def test_quoted_phrase_is_exact(self):
        self.assertEqual(ms.fts_query('"add-ons page" open'), '"add ons page" "open"*')

    def test_underscores_split_like_the_index(self):
        self.assertEqual(ms.fts_query("tb_translate"), '"tb translate"*')

    def test_operators_and_punctuation_are_harmless(self):
        self.assertEqual(ms.fts_query("a OR NEAR(b) *"), '"a"* "OR"* "NEAR b"*')
        self.assertIsNone(ms.fts_query(' ** "" ( '))


class Search(unittest.TestCase):
    def setUp(self):
        self.index = ms.MacroIndex(MACROS, GROUPS)

    def ids(self, **kw):
        return [r["id"] for r in self.index.search(**kw)]

    def test_empty_query_lists_all_by_title(self):
        self.assertEqual(self.ids(), [4, 1, 3, 2])

    def test_title_hit_ranks_above_reply_hit(self):
        self.assertEqual(self.ids(q="refund"), [1, 3])

    def test_prefix_diacritics_and_tags(self):
        self.assertEqual(self.ids(q="refu"), [1, 3])
        self.assertEqual(self.ids(q="cafe"), [4])
        self.assertEqual(self.ids(q="tb_translate"), [2])

    def test_snippet_and_title_are_marked(self):
        r = self.index.search(q="refund")[0]
        self.assertIn(ms.HIT_START + "Refund" + ms.HIT_END, r["title"])
        self.assertIn(ms.HIT_START + "refund" + ms.HIT_END, r["snippet"])

    def test_filters(self):
        self.assertEqual(self.ids(status="inactive"), [3])
        self.assertEqual(self.ids(q="refund", status="active"), [1])
        self.assertEqual(self.ids(group="100"), [1])
        self.assertEqual(self.ids(group="none"), [3, 2])
        self.assertEqual(self.ids(field="priority"), [3])

    def test_group_names_fall_back_to_ids(self):
        self.assertEqual(self.index.available_for(MACROS[0]), "Donor Support")
        self.assertEqual(self.index.available_for(MACROS[3]), "Old team (deleted)")
        self.assertEqual(self.index.group_name(12345), "12345")
        self.assertEqual(self.index.available_for(MACROS[1]), "All agents")

    def test_admin_url(self):
        self.assertEqual(self.index.admin_url(1),
                         "https://tbpro.zendesk.com/admin/workspaces/agent-workspace/macros/1")


class Pages(unittest.TestCase):
    def test_macro_text_is_escaped(self):
        evil = macro(5, "<script>alert(1)</script>", '<img src=x onerror="alert(2)">Hi', "x")
        index = ms.MacroIndex([evil], [])
        for body in (ms.render_search(index, {"q": "script"}), ms.render_search(index, {}),
                     ms.render_macro(index, 5, "")):
            self.assertNotIn("<script>alert", body)
            self.assertNotIn("<img", body)
        self.assertIn("&lt;script&gt;", ms.render_macro(index, 5, ""))

    def test_search_input_is_escaped(self):
        body = ms.render_search(ms.MacroIndex(MACROS, GROUPS), {"q": '"><script>x</script>'})
        self.assertNotIn("<script>x", body)


class Dynamic(unittest.TestCase):
    def test_placeholders_and_liquid_are_wrapped(self):
        self.assertEqual(
            ms.show("Hi {{ticket.requester.first_name}}, {% if x %}yes{% endif %}"),
            'Hi <span class="dc">{{ticket.requester.first_name}}</span>, '
            '<span class="dc">{% if x %}</span>yes<span class="dc">{% endif %}</span>')

    def test_plain_braces_are_left_alone(self):
        self.assertEqual(ms.show("a { b } {single}"), "a { b } {single}")

    def test_html_inside_placeholder_stays_escaped(self):
        self.assertEqual(ms.show("{{<b>x</b>}}"), '<span class="dc">{{&lt;b&gt;x&lt;/b&gt;}}</span>')

    def test_search_hit_inside_placeholder_nests(self):
        text = "{{ticket." + ms.HIT_START + "requester" + ms.HIT_END + ".name}}"
        self.assertEqual(ms.marked(text),
                         '<span class="dc">{{ticket.<mark>requester</mark>.name}}</span>')

    def test_placeholders_cut_by_a_snippet(self):
        self.assertEqual(ms.marked("Hi {{x}}. {{dc.donors…"),
                         'Hi <span class="dc">{{x}}</span>. <span class="dc">{{dc.donors…</span>')
        self.assertEqual(ms.marked("…first_name}}, hello"),
                         '<span class="dc">…first_name}}</span>, hello')
        self.assertEqual(ms.marked("…plain text…"), "…plain text…")

    def test_pages_highlight_dynamic_content(self):
        m = macro(7, "Greeting", "<p>Hello {{ticket.requester.first_name}},</p><p>{{dc.signature}}</p>")
        index = ms.MacroIndex([m], [])
        self.assertIn('<span class="dc">{{ticket.requester.first_name}}</span>',
                      ms.render_search(index, {"q": "hello"}))
        detail = ms.render_macro(index, 7, "")
        self.assertEqual(detail.count('<span class="dc">{{dc.signature}}</span>'), 2)  # reply + action


class FillIn(unittest.TestCase):
    def test_markers_are_orange(self):
        self.assertEqual(ms.show("Donations from [[COUNTRY]] on [[DATE]]"),
                         'Donations from <span class="fill">[[COUNTRY]]</span> on '
                         '<span class="fill">[[DATE]]</span>')

    def test_single_brackets_and_escaping(self):
        self.assertEqual(ms.show("a [b] [[<x>]]"), 'a [b] <span class="fill">[[&lt;x&gt;]]</span>')

    def test_triple_bracket_and_cut_markers(self):
        self.assertEqual(ms.show("[[[ ___ ]]"), '[<span class="fill">[[ ___ ]]</span>')
        self.assertEqual(ms.marked("Hi. [[Insert questions…"),
                         'Hi. <span class="fill">[[Insert questions…</span>')
        self.assertEqual(ms.marked("…AMOUNT]] was charged"),
                         '<span class="fill">…AMOUNT]]</span> was charged')

    def test_hit_inside_marker_and_placeholder_together(self):
        text = "{{ticket.id}} [[" + ms.HIT_START + "DATE" + ms.HIT_END + "]]"
        self.assertEqual(ms.marked(text), '<span class="dc">{{ticket.id}}</span> '
                                          '<span class="fill">[[<mark>DATE</mark>]]</span>')


class ExportDate(unittest.TestCase):
    def test_label(self):
        self.assertEqual(ms.export_label("2026-11-27T18:00:05Z"), "Macros exported 2026-11-27 18:00 UTC")
        self.assertEqual(ms.export_label(None), "")
        self.assertEqual(ms.export_label("yesterday"), "")

    def test_search_page_shows_it(self):
        index = ms.MacroIndex(MACROS, GROUPS, exported_at="2026-11-27T18:00:00Z")
        self.assertIn("4 of 4 macros · Macros exported 2026-11-27 18:00 UTC", ms.render_search(index, {}))
        self.assertIn("4 of 4 macros</p>", ms.render_search(ms.MacroIndex(MACROS, GROUPS), {}))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ms.make_server(ms.MacroIndex(MACROS, GROUPS), port=0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        body = res.read().decode()
        conn.close()
        return res, body

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_search_page(self):
        res, body = self.get("/?q=refund&status=active")
        self.assertEqual(res.status, 200)
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)  # no scripts may run
        self.assertNotIn("script", csp)
        self.assertIn("1 of 4 macros", body)
        self.assertIn("<mark>Refund</mark>", body)
        self.assertIn("/admin/workspaces/agent-workspace/macros/1", body)

    def test_macro_page(self):
        res, body = self.get("/macro/2?q=translate")
        self.assertEqual(res.status, 200)
        self.assertIn("Search for a translator.", body)
        self.assertIn('href="/?q=translate"', body)

    def test_localhost_name_is_allowed(self):
        self.assertEqual(self.get("/", host=f"localhost:{self.port}")[0].status, 200)

    def test_other_host_is_refused(self):
        self.assertEqual(self.get("/", host="evil.example:80")[0].status, 403)

    def test_unknown_paths(self):
        self.assertEqual(self.get("/macro/12345")[0].status, 404)
        self.assertEqual(self.get("/etc/passwd")[0].status, 404)


class Main(unittest.TestCase):
    def test_missing_data_is_a_clear_error(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(err):
            self.assertEqual(ms.main(["--data", tmp, "--port", "0"]), 1)
        self.assertIn("macros.json not found. Run: uv run scrape_macros.py", err.getvalue())

    def test_load_list_reads_api_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "macros.json"
            p.write_text(json.dumps({"macros": MACROS, "count": len(MACROS)}))
            self.assertEqual(len(ms.load_list(p, "macros")), 4)


if __name__ == "__main__":
    unittest.main()
