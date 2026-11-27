# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Write test_cases/search.json from made-up macros and macro_search.py.

  uv run test_cases/generate.py

Run it after an intended change to the search rules, read the printed
summary and `git diff test_cases/search.json`, and then make macro_search.html
pass the new cases. Never regenerate only to make a failing test pass.
"""
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import macro_search as ms  # noqa: E402

def macro(i, title, reply="", tags="", active=True, groups=None, extra=(), description="", plain=False):
    acts = []
    if reply:
        acts.append({"field": "comment_value" if plain else "comment_value_html", "value": reply})
    if tags:
        acts.append({"field": "current_tags", "value": tags})
    acts += list(extra)
    return {"id": i, "title": title, "raw_title": title, "description": description, "active": active,
            "default": False, "position": i, "actions": acts,
            "restriction": {"type": "Group", "id": groups[0], "ids": groups} if groups else None,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
            "url": f"https://tbpro.zendesk.com/api/v2/macros/{i}.json"}

long_reply = ("<p>{{dc.signature_block}} Hello {{ticket.requester.first_name}},</p>"
              "<p>Thanks for writing to us about your account. We read every message and we answer as soon as we can. "
              "Before you change anything, please make a backup of your profile and your messages. "
              "Then open the account settings and look at the list of addresses that belong to the account. "
              "Each alias needs its own identity in Thunderbird Desktop, not a separate account, "
              "so that replies go out from the right address in [[COUNTRY]] and everywhere else.</p>"
              "<p>If you still have problems after that, reply to this message and tell us what you see.</p>")
macros = [
    macro(1, "Donation::Refund request", "<p>Hello {{ticket.requester.first_name}},</p><p>We sent your <b>refund</b> today.</p>",
          "macro_donors_refund donor", groups=[100], description="Use for refund questions"),
    macro(2, "Thunderbird::Translate emails", "<p>Open the add-ons page.</p><p>Search for a translator.</p>", "tb_translate"),
    macro(3, "Old greeting", "<p>Hello, the refund team here.</p>", active=False,
          extra=[{"field": "priority", "value": "high"}]),
    macro(4, "Café closing", "Bye for now", groups=[999], plain=True),
    macro(5, "Thundermail::Aliases - Identity setup", long_reply, "macro_thundermail_aliases", groups=[100, 200]),
    macro(6, "Tie::A - Same", "<p>Unique zebra word.</p>"),
    macro(7, "Tie::B - Same", "<p>Unique zebra word.</p>"),
    macro(8, "Lists::Nested - Steps", "<ul><li><p>Open&nbsp;settings</p></li><li>Pick <i>one</i></li></ul><script>alert(1)</script>",
          extra=[{"field": "group_id", "value": "200"}]),
    macro(9, "<b>Escape</b> & \"quotes\" 'single'", "<p>&lt;img src=x onerror=alert(1)&gt; [[DATE]]</p>"),
    macro(10, "Cut::Words - Apostrophe", "<p>" + " ".join(f"word{i}" for i in range(30))
          + " we know you'd like a zebrafish now, and {{ticket.requester.first_name}} too</p>"),
]
groups = [{"id": 100, "name": "Donor Support"}, {"id": 200, "name": "Tier 2"},
          {"id": 999, "name": "Old team", "deleted": True}]
index = ms.MacroIndex(macros, groups, exported_at="2026-11-27T18:00:00Z")

queries = [
    {"q": ""}, {"q": "refund"}, {"q": "refu"}, {"q": "REFUND"}, {"q": "cafe"}, {"q": "tb_translate"},
    {"q": "\"add ons page\""}, {"q": "\"ons add\""}, {"q": "hello"}, {"q": "hello refund"},
    {"q": "zebra"}, {"q": "identity"}, {"q": "thunderbird desktop"}, {"q": "dc signature"},
    {"q": "country"}, {"q": "a OR NEAR(b) *"}, {"q": "  ** \"\" ( "}, {"q": "nothingmatches"},
    {"q": "refund", "status": "active"}, {"q": "", "status": "inactive"}, {"q": "", "group": "100"},
    {"q": "", "group": "none"}, {"q": "", "field": "priority"}, {"q": "hello", "group": "200"},
    {"q": "escape"}, {"q": "settings"}, {"q": "zebrafish"}, {"q": "requester"},
]
cases = []
for qd in queries:
    params = {"q": qd.get("q", ""), "status": qd.get("status", "all"), "group": qd.get("group", ""), "field": qd.get("field", "")}
    res = index.search(**params)
    cases.append({**params, "ids": [r["id"] for r in res],
                  "results": [{"id": r["id"], "title_html": ms.marked(r["title"]), "snippet_html": ms.marked(r["snippet"])} for r in res]})

marked_texts = ["Hi {{ticket.requester.first_name}}, {% if x %}yes{% endif %}", "a { b } {single}", "{{<b>x</b>}}",
                "Donations from [[COUNTRY]] on [[DATE]]", "a [b] [[<x>]]", "[[[ ___ ]]", "Hi. [[Insert questions…",
                "…AMOUNT]] was charged", "Hi {{x}}. {{dc.donors…", "…first_name}}, hello", "…plain text…",
                "{{ticket." + ms.HIT_START + "requester" + ms.HIT_END + ".name}}", "<script>&amp;\"'"]
html_cases = ["<p>Hi&nbsp;<b>there</b></p><p>Bye</p><script>x()</script>", "<ul><li>one</li><li>two</li></ul>",
              "<ul><li><p>one</p></li><li><p>two</p></li></ul>", "<p>a</p>\n\n\n<p>b</p>", "plain <br> text", "  &lt;tag&gt;  "]
doc = {
    "comment": "Shared test cases for macro_search.py and macro_search.html (issue #9). The macros are made up. "
               "Regenerate the expected values only after you check that a change is intended.",
    "exported_at": "2026-11-27T18:00:00Z",
    "macros": macros, "groups": groups,
    "search": cases,
    "marked": [{"text": t, "html": ms.marked(t)} for t in marked_texts],
    "html_to_text": [{"html": h, "text": ms.html_to_text(h)} for h in html_cases],
    "available_for": [{"id": m["id"], "text": index.available_for(m)} for m in macros],
    "export_label": [{"value": v, "label": ms.export_label(v)} for v in ("2026-11-27T18:00:05Z", None, "yesterday")],
    "group_choices": [list(c) for c in index.group_choices()],
    "field_choices": [list(c) for c in index.field_choices()],
}
out = REPO / "test_cases" / "search.json"
out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
for c in cases:
    print(repr(c["q"]), c["status"], c["group"], c["field"], "->", c["ids"])
for qq in ("identity", "zebrafish", "requester"):
    for r in next(c for c in cases if c["q"] == qq)["results"]: print("snippet:", qq, r["id"], r["snippet_html"])
for r in next(c for c in cases if c["q"] == "refund")["results"]: print("refund:", r["title_html"], "|", r["snippet_html"])
