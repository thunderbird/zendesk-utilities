# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline tests for new_macro.py, macro_rules.py and macro_ui.py helpers.

No browser and no network. The macros here are made up.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import macro_rules as rules
import macro_ui
import new_macro as nm
from macro_search import MacroIndex

TM = rules.BRANDS["thundermail"]


def template(i=TM.template_id, brand=TM, extra=()):
    return {
        "id": i, "title": "Thundermail::Category::Subcategory::New macro template",
        "description": "Clone me", "active": True,
        "restriction": {"type": "Group", "id": 1, "ids": [1, 2]},
        "actions": [
            {"field": "brand_id", "value": str(brand.brand_id)},
            {"field": "custom_status_id", "value": "33974533354003"},
            {"field": "comment_mode_is_public", "value": "true"},
            {"field": "comment_value_html", "value": "<p>Hello {{ticket.requester.first_name}},</p><p>&nbsp;</p>"},
            {"field": "current_tags", "value": "macro_thundermail_macroname"},
            {"field": "group_id", "value": "current_groups"},
            {"field": "assignee_id", "value": "current_user"},
            *extra,
        ],
    }


def other(i, title, reply):
    m = template(i)
    m["title"] = title
    m["actions"][3] = {"field": "comment_value_html", "value": reply}
    return m


class Rules(unittest.TestCase):
    def draft(self, **kw):
        args = dict(title="Thundermail::Accounts - POP3 support", description="Use for POP3 questions",
                    reply_html="<p>We do not support POP3.</p>", tag="macro_thundermail_accounts_pop3_support")
        args.update(kw)
        return nm.build_draft(template(), TM, **{k: args[k] for k in
                              ("title", "description", "reply_html", "tag")})

    def test_good_draft_has_no_problems(self):
        self.assertEqual(rules.check_macro(self.draft()), [])

    def test_each_rule(self):
        cases = {
            2: self.draft(title="POP3 support"),
            3: self.draft(description=""),
            5: self.draft(reply_html='<a href="https://support.tb.pro/hc/en-us/articles/1">x</a>'),
            6: self.draft(tag="pop3"),
        }
        for rule, d in cases.items():
            self.assertIn(rule, [p.rule for p in rules.check_macro(d)], rule)

    def test_duplicate_name(self):
        d = self.draft()
        self.assertEqual([p.rule for p in rules.check_macro(d, {d["title"].casefold()})], [1])

    def test_tag_must_use_the_brand_service(self):
        self.assertIn(6, [p.rule for p in rules.check_macro(self.draft(tag="macro_donors_pop3"))])

    def test_missing_required_actions(self):
        d = self.draft()
        d["actions"] = [a for a in d["actions"] if a["field"] not in ("group_id", "custom_status_id")]
        msgs = [p.message for p in rules.check_macro(d) if p.rule == 7]
        self.assertEqual(msgs, ["the macro does not set the ticket status", "the macro does not set the group"])

    def test_long_reply_is_only_a_warning(self):
        problems = rules.check_macro(self.draft(reply_html="<p>" + "word " * 300 + "</p>"))
        self.assertEqual([(p.rule, p.blocking) for p in problems], [(4, False)])

    def test_name_format(self):
        for ok in ("A::B - C", "Thundermail::Accounts::Login - Reset password"):
            self.assertRegex(ok, rules.NAME_RE)
        for bad in ("A - C", "A::B", "A::B - C::D"):
            self.assertNotRegex(bad, rules.NAME_RE)

    def test_make_tag_and_strip_en_us(self):
        self.assertEqual(rules.make_tag(TM, "Thundermail::Appointment - Adding calendars"),
                         "macro_thundermail_appointment_adding_calendars")
        self.assertEqual(rules.strip_en_us('<a href="https://support.tb.pro/hc/en-US/articles/1-x">'),
                         '<a href="https://support.tb.pro/hc/articles/1-x">')
        self.assertEqual(rules.strip_en_us("/hc/en-us-old/x"), "/hc/en-us-old/x")


class Draft(unittest.TestCase):
    def test_draft_keeps_template_actions_and_replaces_reply_and_tag(self):
        d = nm.build_draft(template(), TM, "T::S - N", "desc", "<p>new</p>", "macro_thundermail_s_n")
        fields = {a["field"]: a["value"] for a in d["actions"]}
        self.assertEqual(fields["comment_value_html"], "<p>new</p>")
        self.assertEqual(fields["current_tags"], "macro_thundermail_s_n")
        self.assertEqual(fields["assignee_id"], "current_user")
        self.assertEqual(d["restriction"], template()["restriction"])
        self.assertNotIn("id", d)

    def test_wrong_brand_and_plain_fallback_are_refused(self):
        with self.assertRaises(nm.InputError):
            nm.build_draft(template(), rules.BRANDS["donors"], "a", "b", "c", "d")
        with self.assertRaises(nm.InputError):
            nm.build_draft(template(extra=[{"field": "comment_value", "value": "x"}]), TM, "a", "b", "c", "d")

    def test_greeting_comes_from_the_template(self):
        self.assertEqual(nm.greeting(template()), "<p>Hello {{ticket.requester.first_name}},</p>")
        self.assertEqual(nm.greeting(other(5, "x", "<p>Hi there</p>")), "")


class Sources(unittest.TestCase):
    def test_sanitize_keeps_formatting_and_drops_danger(self):
        html_in = ('<p onclick="x()">Hi <b>you</b> <a href="javascript:evil()">bad</a> '
                   '<a href="https://support.tb.pro/hc/articles/1" class="c">good</a></p>'
                   '<script>alert(1)</script><img src=x onerror=alert(2)><style>p{}</style>')
        self.assertEqual(nm.sanitize_html(html_in),
                         '<p>Hi <b>you</b> bad <a href="https://support.tb.pro/hc/articles/1">good</a></p>')

    def test_sanitize_main_only_drops_page_chrome(self):
        page = ("<html><head><title>T</title></head><body><nav>Menu</nav>"
                "<article><h2>Title</h2><p>Body &amp; more</p></article><footer>F</footer></body></html>")
        self.assertEqual(nm.sanitize_html(page, main_only=True), "<h2>Title</h2><p>Body &amp; more</p>")

    def test_relative_links_become_absolute(self):
        self.assertEqual(nm.sanitize_html('<a href="/hc/articles/2">x</a> <a href="#top">y</a>',
                                          base_url="https://support.tb.pro/hc/articles/1"),
                         '<a href="https://support.tb.pro/hc/articles/2">x</a> '
                         '<a href="https://support.tb.pro/hc/articles/1#top">y</a>')
        self.assertEqual(nm.sanitize_html('<a href="/hc/articles/2">x</a>'), "x")

    def test_text_to_html(self):
        self.assertEqual(nm.text_to_html("Hi <you>,\nline two\n\nSee https://support.tb.pro/hc/articles/1."),
                         '<p>Hi &lt;you&gt;,<br>line two</p><p>See <a href="https://support.tb.pro/hc/articles/1">'
                         'https://support.tb.pro/hc/articles/1</a>.</p>')

    def test_kb_url_pattern(self):
        m = nm.KB_RE.match("https://support.tb.pro/hc/en-us/articles/57205706556179-Does-Thundermail-support-POP3")
        self.assertEqual(m.group(1), "57205706556179")
        self.assertIsNone(nm.KB_RE.match("https://evil.example/hc/en-us/articles/1"))


class Similar(unittest.TestCase):
    def test_finds_macros_with_shared_rare_words(self):
        macros = [template(), other(2, "Thundermail::Accounts - POP3", "<p>We do not support POP3 protocol.</p>"),
                  other(3, "Thundermail::Billing - Refund", "<p>Your refund is on the way.</p>"),
                  other(4, "Thundermail::Login - Password", "<p>Reset your password.</p>")]
        index = MacroIndex(macros, [])
        found = [m["id"] for m in nm.similar_macros(index, "Does Thundermail support POP3 protocol?")]
        self.assertEqual(found[0], 2)
        self.assertNotIn(3, found)
        self.assertEqual(nm.similar_macros(index, "the and of"), [])


class Compare(unittest.TestCase):
    def test_compare_ignores_html_differences_but_not_text(self):
        d = nm.build_draft(template(), TM, "T::S - N", "desc", "<p>Hello&nbsp;there</p>", "macro_thundermail_s_n")
        saved = json.loads(json.dumps(d))
        saved["actions"][3]["value"] = "<p>Hello there</p>"
        self.assertEqual(nm.compare(d, saved), [])
        saved["actions"][3]["value"] = "<p>Goodbye</p>"
        saved["title"] = "Other"
        self.assertEqual(nm.compare(d, saved), ["title: 'Other'", "reply text"])


class UIHelpers(unittest.TestCase):
    def test_urls_and_new_id(self):
        self.assertEqual(macro_ui.clone_url("https://tbpro.zendesk.com", 42),
                         "https://tbpro.zendesk.com/admin/workspaces/agent-workspace/macros/new?macro_id=42")
        self.assertIsNone(macro_ui.NEW_ID_RE.search(macro_ui.clone_url("https://b", 42)))
        self.assertEqual(macro_ui.NEW_ID_RE.search("https://b/admin/workspaces/agent-workspace/macros/77").group(1), "77")


class CommandLine(unittest.TestCase):
    """End to end with --file and --dry-run: no browser starts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "zendesk-data"
        (self.data / "macros").mkdir(parents=True)
        macros = [template(), other(2, "Thundermail::Accounts - POP3", "<p>No POP3.</p>")]
        (self.data / "macros" / "macros.json").write_text(json.dumps({"macros": macros, "count": 2}))
        self.reply = Path(self.tmp.name) / "reply.txt"
        self.reply.write_text("We support IMAP.\n\nSee https://support.tb.pro/hc/en-us/articles/1-imap")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = nm.main(["--file", str(self.reply), "--brand", "thundermail", "--data", str(self.data),
                          "--description", "Use for IMAP questions", "--dry-run", *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_writes_a_clean_draft(self):
        rc, out, err = self.run_main("--name", "Thundermail::Accounts - IMAP support")
        self.assertEqual(rc, 0, err)
        self.assertIn("Dry run: nothing was sent to Zendesk.", out)
        [path] = (self.data / "drafts").glob("*.json")
        draft = json.loads(path.read_text())["macro"]
        reply = rules.action_values(draft, "comment_value_html")[0]
        self.assertTrue(reply.startswith("<p>Hello {{ticket.requester.first_name}},</p><p>We support IMAP."))
        self.assertIn("https://support.tb.pro/hc/articles/1-imap", reply)
        self.assertEqual(rules.tags_of(draft), ["macro_thundermail_accounts_imap_support"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rule_problems_stop_before_zendesk(self):
        rc, out, _ = self.run_main("--name", "Thundermail::Accounts - POP3")
        self.assertEqual(rc, 1)
        self.assertIn("rule 1: a macro named", out)
        self.assertIn("Nothing was sent to Zendesk.", out)

    def test_brand_without_template_needs_template_option(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = nm.main(["--file", str(self.reply), "--brand", "donors", "--data", str(self.data),
                          "--name", "Donors::A - B", "--description", "d", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("--template", err.getvalue())

    def test_drafts_in_repository_are_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = nm.main(["--file", str(self.reply), "--brand", "thundermail", "--name", "A::B - C",
                          "--description", "d", "--dry-run", "--data", str(nm.scrape.REPO / "data")])
        self.assertEqual(rc, 1)
        self.assertIn("refusing drafts directory", err.getvalue())


if __name__ == "__main__":
    unittest.main()
