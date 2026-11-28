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
            "created_at": "2026-01-01T00:00:00Z", "updated_at": f"2026-01-{i * 7 % 28 + 1:02d}T00:00:00Z",
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
    macro(11, "  donation :: Card :: Update", "<p>Change the card.</p>"),
    # Issue #13: links keep their URL, and the search finds words in the URL.
    macro(12, "Roadmap::Mobile app", '<p>Thanks for asking!</p><p><a href="https://roadmaps.example.org/ios/">'
          "<strong><u>Here is our roadmap</u></strong></a>. Read <a href=\"https://www.example.org\">"
          "https://www.example.org</a> or write to <a href=\"mailto:help@example.org\">help@example.org</a>.</p>"),
    macro(13, "Links::Unsafe and dynamic", '<p><a href="javascript:alert(1)">Click <b>here</b></a> or open '
          '<a href="https://forms.example.org/?ticket={{ticket.id}}&amp;country=[[COUNTRY]]">the form</a>'
          'now.</p>'),
]
groups = [{"id": 100, "name": "Donor Support"}, {"id": 200, "name": "Tier 2"},
          {"id": 999, "name": "Old team", "deleted": True}]
index = ms.MacroIndex(macros, groups, exported_at="2026-11-27T18:00:00Z")

def query(q="", sort="", **filters):
    """A search case. Filter values are lists, or one string as in an old URL."""
    return {"q": q, "filters": filters, "sort": sort}

queries = [
    query(), query("refund"), query("refu"), query("REFUND"), query("cafe"), query("tb_translate"),
    query("\"add ons page\""), query("\"ons add\""), query("hello"), query("hello refund"),
    query("zebra"), query("identity"), query("thunderbird desktop"), query("dc signature"),
    query("country"), query("a OR NEAR(b) *"), query("  ** \"\" ( "), query("nothingmatches"),
    query("refund", status="active"), query(status="inactive"), query(group="100"),
    query(group="none"), query(field="priority"), query("hello", group="200"),
    query("escape"), query("settings"), query("zebrafish"), query("requester"),
    query(category="Donation"), query(category="DONATION"), query("refund", category="donation"),
    query(category="Tie"), query(category="::"), query(category="Card"),
    query(category="Lists::Nested"), query(category="nothing"),
    query(status="active", group="100", category="Thundermail"),
    query("roadmaps"), query("forms example"), query("javascript"), query("ticket"),
    # Issue #15: OR inside a group, AND between groups, and old URL values.
    query(category=["Donation", "Tie"]), query(category=["donation", "DONATION", ""]),
    query(category=["Donation", "::"], status=["inactive"]), query(group=["none", "999"]),
    query(group=["100", "200"]), query(tag=["donor", "tb_translate"]), query(tag=["donor"], group=["200"]),
    query(status="all"), query(status=["active", "inactive"]), query(status=["bogus"]),
    query(field=["priority", "group_id"]), query(field=["priority"], status=["active"]),
    query("hello", category=["Donation", "Thundermail"], group=["100"]),
    query(sort="title"), query(sort="-title"), query(sort="category"), query(sort="-category"),
    query(sort="group"), query(sort="-group"), query(sort="updated"), query(sort="-updated"),
    query(sort="--title"), query(sort="bogus"), query("hello", sort="-title"), query("zebra", sort="updated"),
    query(category=["Tie", "Donation"], sort="-updated"),
]
cases = []
for qd in queries:
    res = index.search(qd["q"], qd["filters"], qd["sort"])
    cases.append({**qd, "ids": [r["id"] for r in res],
                  "results": [{"id": r["id"], "title_html": ms.marked(r["title"]), "snippet_html": ms.marked(r["snippet"])} for r in res]})

filter_inputs = [{}, {"status": "all"}, {"category": ["A", "a", "B", ""], "tag": "x", "unknown": ["y"]},
                 {"status": ["inactive", "active", "inactive"], "group": ["none"], "field": [], "q": "z"}]
sort_inputs = ["", "title", "-updated", "--title", "-", "Title", "group-", "bogus"]
query_inputs = [("", {}, "", "", False), ("refund me", {"category": ["Donation", "::"], "tag": ["a&b"]}, "table", "-updated", True),
                ("é/ü?", {"group": ["none"], "status": ["active"]}, "list", "", False)]
marked_texts = ["Hi {{ticket.requester.first_name}}, {% if x %}yes{% endif %}", "a { b } {single}", "{{<b>x</b>}}",
                "Donations from [[COUNTRY]] on [[DATE]]", "a [b] [[<x>]]", "[[[ ___ ]]", "Hi. [[Insert questions…",
                "…AMOUNT]] was charged", "Hi {{x}}. {{dc.donors…", "…first_name}}, hello", "…plain text…",
                "{{ticket." + ms.HIT_START + "requester" + ms.HIT_END + ".name}}", "<script>&amp;\"'"]
html_cases = ["<p>Hi&nbsp;<b>there</b></p><p>Bye</p><script>x()</script>", "<ul><li>one</li><li>two</li></ul>",
              "<ul><li><p>one</p></li><li><p>two</p></li></ul>", "<p>a</p>\n\n\n<p>b</p>", "plain <br> text", "  &lt;tag&gt;  ",
              '<a href="https://example.org/a">Our <b>page</b></a>, more', '<a href=" https://example.org ">https://example.org</a>',
              '<a href="mailto:a@example.org">a@example.org</a>', '<a href="javascript:x()">run</a>', '<a name="top">Top</a>',
              '<a href="#">empty</a><a href="">none</a>', '<a href="https://a.example">one<a href="https://b.example">two</a>',
              '<a href="https://a.example"><div>block</div></a>', '<a href="https://a.example">unclosed']
linked_texts = ["Our page (https://example.org/a), more", "x (https://a.example)then text",
                "Wiki (https://en.example.org/Foo_(bar)).", "See https://a.example/x?y=1&z=2, then mailto:a@example.org.",
                "run (javascript:alert(1)) and ftp://a.example", "form (https://f.example/?t={{ticket.id}}&c=[[COUNTRY]])",
                "HTTPS://UPPER.example/ and https:// alone and https://.", "<b>https://a.example/\"onmouseover=x</b>",
                "no links {{dc.sig}} [[DATE]]"]
doc = {
    "comment": "Shared test cases for macro_search.py and macro_search.html (issue #9). The macros are made up. "
               "Regenerate the expected values only after you check that a change is intended.",
    "exported_at": "2026-11-27T18:00:00Z",
    "macros": macros, "groups": groups,
    "search": cases,
    "marked": [{"text": t, "html": ms.marked(t)} for t in marked_texts],
    "html_to_text": [{"html": h, "text": ms.html_to_text(h)} for h in html_cases],
    "linked": [{"text": t, "html": ms.linked(t)} for t in linked_texts],
    "available_for": [{"id": m["id"], "text": index.available_for(m)} for m in macros],
    "export_label": [{"value": v, "label": ms.export_label(v)} for v in ("2026-11-27T18:00:05Z", None, "yesterday")],
    "group_choices": [list(c) for c in index.group_choices()],
    "field_choices": [list(c) for c in index.field_choices()],
    "category_choices": [list(c) for c in index.category_choices()],
    "facet_choices": [[k, label, [list(c) for c in cs]] for k, label, cs in index.facet_choices()],
    "parse_filters": [{"params": f, "filters": ms.parse_filters(f)} for f in filter_inputs],
    "parse_sort": [{"value": v, "sort": ms.parse_sort(v)} for v in sort_inputs],
    "next_sort": [{"current": c, "column": col, "sort": ms.next_sort(c, col)}
                  for c in ("", "title", "-title", "-updated") for col in ms.SORTS],
    "search_query": [{"args": list(a), "query": ms.search_query(*a)} for a in query_inputs],
}
out = REPO / "test_cases" / "search.json"
out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
for c in cases:
    print(repr(c["q"]), c["filters"], c["sort"], "->", c["ids"])
for qq in ("identity", "zebrafish", "requester"):
    for r in next(c for c in cases if c["q"] == qq)["results"]: print("snippet:", qq, r["id"], r["snippet_html"])
for h in html_cases[6:]: print("html_to_text:", repr(h), "->", repr(ms.html_to_text(h)))
for t in linked_texts: print("linked:", ms.linked(t))
for qq in ("roadmaps", "forms example"):
    for r in next(c for c in cases if c["q"] == qq)["results"]: print("snippet:", qq, r["id"], r["snippet_html"])
print("categories:", index.category_choices())
for r in next(c for c in cases if c["q"] == "refund")["results"]: print("refund:", r["title_html"], "|", r["snippet_html"])
