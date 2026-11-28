# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline tests for new_macro_app.py. No browser and no network.

The macros here are made up.
"""

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import urlencode

import macro_rules as rules
import new_macro as nm
import new_macro_app as app_mod
from test_new_macro import other, template

GOOD = {"brand": "thundermail", "name": "Thundermail::Accounts - IMAP support",
        "description": "Use for IMAP questions. Example: #1234",
        "reply": "We support IMAP.\n\nSee https://support.tb.pro/hc/en-us/articles/1-imap"}


def form(**kw):
    params = {**GOOD, **kw}
    return app_mod.Form.parse({k: [v] for k, v in params.items()})


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drafts = Path(self.tmp.name) / "drafts"
        self.pages = {}
        macros = [template(), other(2, "Thundermail::Accounts - POP3", "<p>No POP3 with Thundermail.</p>")]
        self.app = app_mod.App(macros, [{"id": 1, "name": "Tier 1"}], self.drafts, get=self.fake_get)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_get(self, url):
        if url not in self.pages:
            raise nm.InputError(f"{url} returned HTTP 404")
        return self.pages[url]


class Drafts(AppTestCase):
    def test_pasted_text_becomes_a_saved_draft(self):
        res = self.app.make_draft(form())
        self.assertEqual(res.errors, [])
        self.assertTrue(res.ready, res.problems)
        draft = json.loads(res.path.read_text())["macro"]
        reply = rules.action_values(draft, "comment_value_html")[0]
        self.assertTrue(reply.startswith("<p>Hello {{ticket.requester.first_name}},</p><p>We support IMAP."))
        self.assertIn("https://support.tb.pro/hc/articles/1-imap", reply)  # rule 5
        self.assertEqual(rules.tags_of(draft), ["macro_thundermail_accounts_imap_support"])
        self.assertEqual(res.path.stat().st_mode & 0o777, 0o600)

    def test_html_is_sanitized_and_options_apply(self):
        res = self.app.make_draft(form(reply='<p onclick="x()">Hi <script>bad()</script><b>there</b></p>',
                                       format="html", no_greeting="1", tag="macro_thundermail_custom"))
        reply = rules.action_values(res.draft, "comment_value_html")[0]
        self.assertEqual(reply, "<p>Hi <b>there</b></p>")
        self.assertEqual(rules.tags_of(res.draft), ["macro_thundermail_custom"])

    def test_blocking_problem_saves_nothing(self):
        res = self.app.make_draft(form(name="Thundermail::Accounts - POP3"))
        self.assertFalse(res.ready)
        self.assertIn(1, [p.rule for p in res.problems])
        self.assertFalse(self.drafts.exists())

    def test_missing_fields_and_two_sources(self):
        res = self.app.make_draft(app_mod.Form())
        self.assertEqual(res.errors, ["Give the brand.", "Give the name.", "Give the description.",
                                      "Give a URL or paste the reply."])
        res = self.app.make_draft(form(url="https://example.com/a"))
        self.assertEqual(res.errors, ["Give a URL or a reply, not both."])

    def test_template_choices(self):
        self.assertIn("no team template", self.app.make_draft(form(brand="donors")).errors[0])
        self.assertIn("not in macros.json", self.app.make_draft(form(template="99")).errors[0])
        self.assertIn("macro ID", self.app.make_draft(form(template="abc")).errors[0])
        res = self.app.make_draft(form(brand="tfa", template=str(rules.BRANDS["thundermail"].template_id)))
        self.assertIn("is not a Thunderbird App Store Reviews macro", res.errors[0])

    def test_similar_macros_are_listed(self):
        res = self.app.make_draft(form(reply="Thundermail has no POP3 support."))
        self.assertEqual(res.similar[0]["id"], 2)


class Urls(AppTestCase):
    def test_kb_article_comes_from_the_api(self):
        api = "https://tbpro.zendesk.com/api/v2/help_center/articles/57.json"
        self.pages[api] = (api, json.dumps({"article": {
            "body": '<p>Use <a href="/hc/en-us/articles/58">IMAP</a>.</p>',
            "html_url": "https://support.tb.pro/hc/en-us/articles/57"}}))
        res = self.app.make_draft(form(reply="", url="https://support.tb.pro/hc/en-us/articles/57-imap"))
        reply = rules.action_values(res.draft, "comment_value_html")[0]
        self.assertIn('<a href="https://support.tb.pro/hc/articles/58">IMAP</a>', reply)

    def test_private_article_asks_for_pasted_text(self):
        res = self.app.make_draft(form(reply="", url="https://support.tb.pro/hc/en-us/articles/9"))
        self.assertIn("paste its text instead", res.errors[0])

    def test_page_keeps_main_content(self):
        url = "https://blog.example.com/post"
        self.pages[url] = (url, "<nav>Menu</nav><main><p>The answer.</p></main><footer>F</footer>")
        res = self.app.make_draft(form(reply="", url=url))
        self.assertTrue(rules.action_values(res.draft, "comment_value_html")[0].endswith("<p>The answer.</p>"))

    def test_redirect_to_another_site_is_a_sign_in_page(self):
        url = "https://wiki.example.com/page"
        self.pages[url] = ("https://login.example.org/sso", "<main><p>Sign in</p></main>")
        res = self.app.make_draft(form(reply="", url=url))
        self.assertIn("sign-in page", res.errors[0])

    def test_only_web_urls(self):
        res = self.app.make_draft(form(reply="", url="file:///etc/passwd"))
        self.assertIn("must start with https://", res.errors[0])


class Pages(AppTestCase):
    def test_ready_draft_shows_copy_values_and_clone_link(self):
        res = self.app.make_draft(form())
        body = app_mod.render_page(self.app, form(), res)
        self.assertIn("/admin/workspaces/agent-workspace/macros/new?macro_id=42980135784467", body)
        self.assertIn('value="Thundermail::Accounts - IMAP support"', body)
        self.assertIn('<div class="richreply"><p>Hello', body)
        self.assertIn("Draft saved:", body)
        self.assertIn("Tier 1", body)

    def test_values_are_escaped(self):
        bad = '"><script>alert(1)</script>'
        res = self.app.make_draft(form(name="A::B - " + bad, description=bad, reply=bad))
        body = app_mod.render_page(self.app, form(name="A::B - " + bad, description=bad, reply=bad), res)
        self.assertNotIn("<script>", body)

    def test_problems_page_saves_nothing(self):
        f = form(name="no format")
        body = app_mod.render_page(self.app, f, self.app.make_draft(f))
        self.assertIn("Rule 2:", body)
        self.assertIn("No draft was saved.", body)
        self.assertNotIn("macros/new?macro_id", body)


class Server(AppTestCase):
    def setUp(self):
        super().setUp()
        self.server = app_mod.make_server(self.app, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, body=None, host=None, origin="same"):
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin == "same":
            origin = f"http://127.0.0.1:{self.port}"
        if origin:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, "/", body=body, headers=headers)
        res = conn.getresponse()
        text = res.read().decode()
        conn.close()
        return res, text

    def test_form_page(self):
        res, body = self.request("GET")
        self.assertEqual(res.status, 200)
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script", csp)
        self.assertIn('<form class="draft" method="post" action="/">', body)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_post_from_own_page_makes_a_draft(self):
        res, body = self.request("POST", urlencode(GOOD))
        self.assertEqual(res.status, 200)
        self.assertIn("Draft saved:", body)
        self.assertEqual(len(list(self.drafts.glob("*.json"))), 1)

    def test_post_from_localhost_name_is_allowed(self):
        res, _ = self.request("POST", urlencode(GOOD), host=f"localhost:{self.port}",
                              origin=f"http://localhost:{self.port}")
        self.assertEqual(res.status, 200)

    def test_cross_site_post_is_refused(self):
        for origin in ("https://evil.example", "null", None):
            res, _ = self.request("POST", urlencode(GOOD), origin=origin)
            self.assertEqual(res.status, 403, origin)
        self.assertFalse(self.drafts.exists())

    def test_other_host_is_refused(self):
        self.assertEqual(self.request("GET", host="evil.example:80")[0].status, 403)


class Main(unittest.TestCase):
    def test_missing_data_is_a_clear_error(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(err):
            self.assertEqual(app_mod.main(["--data", tmp, "--port", "0"]), 1)
        self.assertIn("macros.json not found. Run: uv run scrape_macros.py", err.getvalue())

    def test_drafts_in_repository_are_refused(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(app_mod.main(["--data", str(nm.scrape.REPO / "data"), "--port", "0"]), 1)
        self.assertIn("refusing drafts directory", err.getvalue())


if __name__ == "__main__":
    unittest.main()
