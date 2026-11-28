#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Search the downloaded Zendesk macros in a web app on this computer.

Reads the files that scrape_macros.py writes, in the official Zendesk API
structure, and builds an in-memory SQLite FTS5 full-text index at start-up.
Nothing is written to disk, and the server listens on 127.0.0.1 only.

  uv run macro_search.py               # http://127.0.0.1:8765
  uv run macro_search.py --open        # also open it in the default browser
  uv run macro_search.py --port 9000

Restart the app after a new download to see the new macros.

Macro text comes from Zendesk, so every value is HTML-escaped before it is
shown, and the Content-Security-Policy forbids scripts on every page.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import threading
import unicodedata
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

HOST = "127.0.0.1"  # never 0.0.0.0: other computers must not connect
DEFAULT_PORT = 8765
DEFAULT_DATA = Path.home() / "zendesk-data"
DEFAULT_SUBDOMAIN = "tbpro"
ADMIN_MACRO_URL = "https://{subdomain}.zendesk.com/admin/workspaces/agent-workspace/macros/{id}"

# FTS5 column weights for bm25(): a hit in the title counts most.
COLUMNS = ("title", "description", "reply", "tags")
WEIGHTS = (10.0, 3.0, 1.0, 5.0)
TAG_FIELDS = ("current_tags", "set_tags")
HIT_START, HIT_END = "\x02", "\x03"  # snippet markers, replaced by <mark>
MAX_RESULTS = 500

CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")


# --- macro text ------------------------------------------------------------


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "ul", "ol", "table", "blockquote",
             "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}
    SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.link: tuple[int, str] | None = None  # (start in parts, href) of the open <a>

    def end_link(self):
        if self.link:
            start, href = self.link
            self.parts.append(link_suffix("".join(self.parts[start:]), href))
            self.link = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "a":
            self.end_link()  # a new <a> closes the open one, as in a browser
            href = next((v for k, v in attrs if k == "href"), None)
            self.link = (len(self.parts), href or "")
        elif tag in self.BLOCK:
            # A <p> inside a list item stays on the "- " line.
            if not (self.parts and self.parts[-1] == "- "):
                self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag == "a":
            self.end_link()
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def link_suffix(text: str, href: str) -> str:
    """ " (URL)" after the text of a link, or "" if the text is the URL (issue #13).

    The URL becomes part of the reply text, so the search finds it, and the
    detail page makes it a link with linked().
    """
    href = href.strip()
    label = " ".join(text.split())
    if not href or href in (label, "mailto:" + label):
        return ""
    return f" ({href})"


def html_to_text(source: str) -> str:
    """Plain text of a macro's HTML comment, with paragraph breaks kept.

    A link keeps its text, followed by its URL in parentheses.
    """
    parser = _TextExtractor()
    parser.feed(source)
    parser.close()
    parser.end_link()  # an <a> without </a> ends with the text
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean(text: str) -> str:
    """Remove the characters that the index uses as hit markers."""
    return text.replace(HIT_START, "").replace(HIT_END, "")


def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(as_text(v) for v in value)
    return str(value)


def actions(macro: dict) -> list[dict]:
    return [a for a in macro.get("actions") or [] if isinstance(a, dict)]


def reply_text(macro: dict) -> str:
    """The reply that the macro adds to a ticket, as plain text."""
    parts = []
    for action in actions(macro):
        if action.get("field") == "comment_value_html":
            parts.append(html_to_text(as_text(action.get("value"))))
        elif action.get("field") == "comment_value":
            parts.append(as_text(action.get("value")).strip())
    return "\n\n".join(p for p in parts if p)


def tags(macro: dict) -> list[str]:
    found = []
    for action in actions(macro):
        if action.get("field") in TAG_FIELDS:
            found.extend(as_text(action.get("value")).split())
    return found


def group_ids(macro: dict) -> list[int]:
    """Groups that may use the macro. Empty means all agents."""
    r = macro.get("restriction") or {}
    if r.get("type") != "Group":
        return []
    return list(r.get("ids") or ([r["id"]] if r.get("id") else []))


NO_CATEGORY = "::"  # filter value for titles without a category; no category contains "::"


def macro_category(macro: dict) -> str:
    """The text before the first "::" in the title, or "" without one (issue #11)."""
    title = as_text(macro.get("title"))
    return title.split("::", 1)[0].strip() if "::" in title else ""


# --- query and matching -----------------------------------------------------
#
# macro_search.html repeats every function in this section in JavaScript, so
# that both apps find, rank, and highlight the same way (issue #9). Change both
# together, and add a case to test_cases/search.json.

# A token is a run of letters and digits, as in the SQLite unicode61 tokenizer.
# Underscores and punctuation separate tokens.
TOKEN_RE = re.compile(r"[^\W_]+")
SNIPPET_TOKENS = 24


def norm(token: str) -> str:
    """Lowercase without accents, like unicode61 with remove_diacritics 2."""
    decomposed = unicodedata.normalize("NFD", token)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def query_terms(text: str) -> list[tuple[list[str], bool]]:
    """What the user typed, as (tokens, prefix) phrases.

    Every phrase must match (AND). A word matches as a prefix, so "refun"
    finds "refund". "Double quotes" keep an exact phrase.
    """
    terms = []
    for quoted, word in re.findall(r'"([^"]*)"|(\S+)', text):
        parts = TOKEN_RE.findall(quoted or word)
        if parts:
            terms.append((parts, not quoted))
    return terms


def fts_query(text: str) -> str | None:
    """The same phrases as a safe FTS5 query.

    FTS5 operators and punctuation in the input have no special meaning,
    because every phrase goes inside double quotes.
    """
    terms = ['"' + " ".join(parts) + '"' + ("*" if prefix else "") for parts, prefix in query_terms(text)]
    return " ".join(terms) or None


def tokens_with_spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), norm(m.group())) for m in TOKEN_RE.finditer(text)]


def phrase_starts(tokens: list[str], phrase: list[str], prefix: bool) -> list[int]:
    """Token positions where the phrase starts. With `prefix`, the last
    phrase token only has to start the document token, as in FTS5."""
    n = len(phrase)
    starts = []
    for i in range(len(tokens) - n + 1):
        ok = all(tokens[i + k] == phrase[k] for k in range(n - 1))
        last = tokens[i + n - 1]
        if ok and (last.startswith(phrase[-1]) if prefix else last == phrase[-1]):
            starts.append(i)
    return starts


def hit_ranges(toks, terms) -> list[tuple[int, int]]:
    """Merged (first, last) token index ranges of all phrase matches."""
    words = [t[2] for t in toks]
    ranges = sorted((i, i + len(parts) - 1)
                    for parts, prefix in terms
                    for i in phrase_starts(words, [norm(p) for p in parts], prefix))
    merged: list[list[int]] = []
    for a, b in ranges:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _mark(text: str, toks, ranges, first: int, last: int, cs: int, ce: int) -> str:
    out, pos = [], cs
    for a, b in ranges:
        a, b = max(a, first), min(b, last)
        if a > b:
            continue
        s, e = toks[a][0], toks[b][1]
        out += [text[pos:s], HIT_START, text[s:e], HIT_END]
        pos = e
    out.append(text[pos:ce])
    return "".join(out)


def _space_before(text: str, pos: int) -> int:
    while pos > 0 and not text[pos - 1].isspace():
        pos -= 1
    return pos


def _space_after(text: str, pos: int) -> int:
    while pos < len(text) and not text[pos].isspace():
        pos += 1
    return pos


def highlight(text: str, terms) -> str:
    """The whole text, with hit markers around every match."""
    toks = tokens_with_spans(text)
    return _mark(text, toks, hit_ranges(toks, terms), 0, len(toks) - 1, 0, len(text))


def snippet(text: str, terms, size: int = SNIPPET_TOKENS) -> str:
    """A window of `size` tokens with the most matches, with hit markers.

    The window starts 3 tokens before a match, so the reader sees some
    context. Near the end of the text, the window moves back, so it always
    has `size` tokens when the text has them. "…" marks text that the
    window leaves out.
    """
    toks = tokens_with_spans(text)
    if not toks:
        return ""
    ranges = hit_ranges(toks, terms)
    hits = sorted({i for a, b in ranges for i in range(a, b + 1)})
    start, best = 0, -1
    for s in sorted({max(0, h - 3) for h in hits}):
        count = sum(1 for h in hits if s <= h < s + size)
        if count > best:
            start, best = s, count
    start = max(0, min(start, len(toks) - size))  # a short text shows whole
    end = min(len(toks), start + size)
    # Cut at spaces, not inside a word such as "you'd" or "{{ticket.requester.first_name}}".
    cs = 0 if start == 0 else _space_before(text, toks[start][0])
    ce = len(text) if end == len(toks) else _space_after(text, toks[end - 1][1])
    body = _mark(text, toks, ranges, start, end - 1, cs, ce)
    return ("…" if start > 0 else "") + body + ("…" if end < len(toks) else "")


# --- tag filters and sorting -----------------------------------------------
#
# macro_search.html repeats this section too (issue #15). A search URL holds
# the query, the selected values of each tag group, the view, and the sort, for
# example ?q=refund&category=Billing&category=Donation&status=active&view=table.
# A URL can repeat a group parameter, so the old one-value URLs still work.

FACETS = (("category", "Category"), ("group", "Available for"), ("status", "Status"),
          ("field", "Changes field"), ("tag", "Zendesk tags"))
STATUSES = ("active", "inactive")
SORTS = ("title", "category", "group", "updated")
OPEN_GROUP_LIMIT = 30  # a tag group with more values starts collapsed


def facet_values(macro: dict, key: str) -> list[str]:
    """The values that a macro has in a tag group."""
    if key == "category":
        return [macro_category(macro) or NO_CATEGORY]
    if key == "group":
        return [str(g) for g in group_ids(macro)] or ["none"]
    if key == "status":
        return ["active" if macro.get("active") else "inactive"]
    if key == "field":
        return list(dict.fromkeys(a["field"] for a in actions(macro) if a.get("field")))
    return list(dict.fromkeys(tags(macro)))


def same_value(key: str, a: str, b: str) -> bool:
    """Letter case does not matter for a category (issue #11)."""
    return a.lower() == b.lower() if key == "category" else a == b


def parse_filters(params) -> dict[str, list[str]]:
    """The selected values of each tag group, from a mapping of value lists.

    Empty values, repeats, and unknown statuses, for example the old "all",
    are left out. A group without a selection has no key.
    """
    filters = {}
    for key, _ in FACETS:
        raw = params.get(key) or []
        chosen: list[str] = []
        for value in [raw] if isinstance(raw, str) else raw:
            value = as_text(value)
            if not value or (key == "status" and value not in STATUSES):
                continue
            if not any(same_value(key, value, c) for c in chosen):
                chosen.append(value)
        if chosen:
            filters[key] = chosen
    return filters


def matches(macro: dict, filters: dict[str, list[str]]) -> bool:
    """One of the selected values inside each group (OR), and every group (AND)."""
    return all(any(same_value(key, v, s) for v in facet_values(macro, key) for s in chosen)
               for key, chosen in filters.items())


def with_value(filters: dict[str, list[str]], key: str, value: str) -> dict[str, list[str]]:
    chosen = filters.get(key, [])
    if any(same_value(key, value, c) for c in chosen):
        return filters
    return {**filters, key: chosen + [value]}


def without_value(filters: dict[str, list[str]], key: str, value: str) -> dict[str, list[str]]:
    out = {k: [v for v in vs if not (k == key and same_value(key, v, value))] for k, vs in filters.items()}
    return {k: vs for k, vs in out.items() if vs}


def parse_sort(value: str) -> str:
    """ "title", "-title" (descending), and so on, or "" for the default order."""
    return value if value.removeprefix("-") in SORTS and value.count("-") <= 1 else ""


def next_sort(current: str, column: str) -> str:
    """The sort after a click on a column heading. Updated starts with the newest."""
    if current == column:
        return "-" + column
    if current == "-" + column:
        return column
    return "-updated" if column == "updated" else column


def search_query(q: str, filters: dict[str, list[str]], view: str = "", sort: str = "",
                 picker: bool = False) -> str:
    """The query string of a search page, without "?". The list view is the default."""
    pairs = [("q", q)] if q else []
    for key, _ in FACETS:
        pairs += [(key, v) for v in filters.get(key, [])]
    if view == "table":
        pairs.append(("view", "table"))
    if sort:
        pairs.append(("sort", sort))
    if picker:
        pairs.append(("picker", "open"))
    return urlencode(pairs)


# --- index -----------------------------------------------------------------


class MacroIndex:
    def __init__(self, macros: list[dict], groups: list[dict], subdomain: str = DEFAULT_SUBDOMAIN,
                 exported_at: str | None = None):
        self.subdomain = subdomain
        self.exported_at = exported_at
        self.macros = {m["id"]: m for m in macros}
        # lower(), not casefold(): JavaScript has toLowerCase() only.
        self.order = sorted(self.macros, key=lambda i: (self.macros[i].get("title") or "").lower())
        self.group_names = {
            g["id"]: (g.get("name") or str(g["id"])) + (" (deleted)" if g.get("deleted") else "")
            for g in groups if "id" in g
        }
        self.replies = {i: clean(reply_text(m)) for i, m in self.macros.items()}

        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = threading.Lock()  # one sqlite connection, many server threads
        self.db.execute(
            "CREATE VIRTUAL TABLE fts USING fts5("
            + ", ".join(COLUMNS)
            + ", tokenize = 'unicode61 remove_diacritics 2')"
        )
        self.db.executemany(
            "INSERT INTO fts (rowid, title, description, reply, tags) VALUES (?, ?, ?, ?, ?)",
            [(i, clean(m.get("title") or ""), clean(m.get("description") or ""),
              self.replies[i], clean(" ".join(tags(m))))
             for i, m in self.macros.items()],
        )

    # Names and links ---------------------------------------------------

    def group_name(self, gid) -> str:
        return self.group_names.get(gid, str(gid))

    def available_for(self, macro: dict) -> str:
        ids = group_ids(macro)
        return ", ".join(self.group_name(g) for g in ids) if ids else "All agents"

    def admin_url(self, macro_id: int) -> str:
        return ADMIN_MACRO_URL.format(subdomain=self.subdomain, id=macro_id)

    def value_label(self, key: str, value: str) -> str:
        """The text of a tag chip."""
        if key == "category":
            return category_label(value)
        if key == "group":
            if value == "none":
                return "All agents"
            return self.group_name(int(value)) if value.isascii() and value.isdigit() else value
        return value

    def sort_key(self, macro: dict, column: str) -> str:
        if column == "title":
            return as_text(macro.get("title")).lower()
        if column == "category":
            return macro_category(macro).lower()
        if column == "group":
            return self.available_for(macro).lower()
        return as_text(macro.get("updated_at"))

    # Filter choices ----------------------------------------------------

    def group_choices(self) -> list[tuple[str, str, int]]:
        """(value, label, count) for the group filter."""
        counts: dict[int, int] = {}
        unrestricted = 0
        for m in self.macros.values():
            ids = group_ids(m)
            unrestricted += not ids
            for g in ids:
                counts[g] = counts.get(g, 0) + 1
        choices = [("none", "All agents (no restriction)", unrestricted)]
        choices += sorted(((str(g), self.group_name(g), n) for g, n in counts.items()),
                          key=lambda c: c[1].lower())
        return choices

    def field_choices(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for m in self.macros.values():
            for f in {a.get("field") for a in actions(m) if a.get("field")}:
                counts[f] = counts.get(f, 0) + 1
        return sorted(counts.items())

    def category_choices(self) -> list[tuple[str, int]]:
        """(category, count), sorted by name, then NO_CATEGORY if any title has none.

        Letter case does not matter. The label is the spelling of the first
        macro in title order.
        """
        labels: dict[str, str] = {}
        counts: dict[str, int] = {}
        for i in self.order:
            c = macro_category(self.macros[i])
            labels.setdefault(c.lower(), c)
            counts[c.lower()] = counts.get(c.lower(), 0) + 1
        choices = sorted(((labels[k], n) for k, n in counts.items() if k), key=lambda c: c[0].lower())
        if "" in counts:
            choices.append((NO_CATEGORY, counts[""]))
        return choices

    def facet_choices(self) -> list[tuple[str, str, list[tuple[str, str, int]]]]:
        """(key, label, [(value, label, count)]) of each tag group with values.

        The counts are over all macros, not over the results.
        """
        counts: dict[str, dict[str, int]] = {key: {} for key, _ in FACETS}
        for m in self.macros.values():
            for key, _ in FACETS[1:]:
                for v in facet_values(m, key):
                    counts[key][v] = counts[key].get(v, 0) + 1
        counts["category"] = dict(self.category_choices())  # letter case does not matter
        order = {
            "category": list(counts["category"]),
            "group": [g for g, _, _ in self.group_choices() if g in counts["group"]],
            "status": [v for v in STATUSES if v in counts["status"]],
            "field": sorted(counts["field"]),
            "tag": sorted(counts["tag"]),
        }
        return [(key, label, [(v, self.value_label(key, v), counts[key][v]) for v in order[key]])
                for key, label in FACETS if order[key]]

    # Search ------------------------------------------------------------

    def search(self, q: str = "", filters=None, sort: str = "") -> list[dict]:
        """Matching macros, in rank order with a query, else in title order.

        `filters` maps tag group keys to value lists (see parse_filters), and
        `sort` is a column from SORTS, with "-" for descending.
        """
        filters = parse_filters(filters or {})
        sort = parse_sort(sort)
        query = fts_query(q)
        if query:
            terms = query_terms(q)
            with self.lock:
                ids = [r[0] for r in self.db.execute(
                    "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, ?, ?, ?, ?), rowid",
                    (query, *WEIGHTS),
                ).fetchall()]
            rows = [(i, highlight(clean(self.macros[i].get("title") or ""), terms),
                     snippet(self.replies[i], terms)) for i in ids]
        else:
            rows = [(i, self.macros[i].get("title", ""), shorten(self.replies[i])) for i in self.order]

        if sort:
            column = sort.removeprefix("-")
            # A stable sort: equal keys keep the rank or title order.
            rows.sort(key=lambda r: self.sort_key(self.macros[r[0]], column), reverse=sort.startswith("-"))
        return [{"id": i, "title": title, "snippet": snip, "macro": self.macros[i]}
                for i, title, snip in rows if matches(self.macros[i], filters)]


def export_label(exported_at) -> str:
    """ "Macros exported 2026-11-27 18:00 UTC", or "" without a usable date."""
    value = exported_at if isinstance(exported_at, str) else ""
    if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", value):
        return ""
    return f"Macros exported {value[:10]} {value[11:16]} UTC"


def shorten(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


# --- pages -----------------------------------------------------------------


def esc(value) -> str:
    return html.escape(as_text(value), quote=True)


# Zendesk dynamic content: placeholders such as {{ticket.requester.first_name}}
# and {{dc.some_item}}, and Liquid tags such as {% if ... %}. html.escape()
# leaves braces and % alone, so this runs safely on already-escaped text.
DYNAMIC = re.compile(r"\{\{[^{}]*\}\}|\{%.*?%\}", re.DOTALL)


def dynamic(escaped: str) -> str:
    """Wrap dynamic content in already-escaped text in a pink <span>."""
    return DYNAMIC.sub(lambda m: f'<span class="dc">{m.group(0)}</span>', escaped)


# The team's fill-in markers, for example [[COUNTRY]]. Zendesk does nothing
# with them: the agent must replace them before sending, or the customer
# sees them. Escaping leaves square brackets alone, as it does braces.
FILL_IN = re.compile(r"\[\[[^\[\]]*\]\]")


def fill_in(escaped: str) -> str:
    """Wrap fill-in markers in already-escaped text in an orange <span>."""
    return FILL_IN.sub(lambda m: f'<span class="fill">{m.group(0)}</span>', escaped)


def show(value) -> str:
    """Escape a macro value and highlight dynamic content and fill-in markers."""
    return fill_in(dynamic(esc(value)))


# URLs that the detail page makes clickable. Other schemes, for example
# javascript:, stay plain text.
URL = re.compile(r"(?:https?://|mailto:)[^\s<>\"']+", re.IGNORECASE)
URL_START = re.compile(r"(?:https?://|mailto:).", re.IGNORECASE)


def _trim_url(url: str) -> str:
    """Cut a URL at a ")" without its "(", and remove punctuation at the end.

    html_to_text() writes "text (URL)", and text can follow the ")" directly.
    """
    depth = 0
    for i, c in enumerate(url):
        if c == "(":
            depth += 1
        elif c == ")":
            if not depth:
                url = url[:i]
                break
            depth -= 1
    return url.rstrip(".,;:!?")


def linked(text: str) -> str:
    """show() for the reply text, with every http, https, and mailto URL as a link."""
    out, pos = [], 0
    for m in URL.finditer(text):
        url = _trim_url(m.group())
        if not URL_START.match(url):
            continue
        out.append(show(text[pos:m.start()]))
        out.append(f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{show(url)}</a>')
        pos = m.start() + len(url)
    out.append(show(text[pos:]))
    return "".join(out)


# A snippet can cut a placeholder or a fill-in marker at either end:
# "{{dc.donors…", "…name}}", "[[Insert questions…", or "…COUNTRY]]".
PARTIAL = re.compile(r"\{\{[^{}]*…$|^…[^{}]*\}\}")
PARTIAL_FILL = re.compile(r"\[\[[^\[\]]*…$|^…[^\[\]]*\]\]")


def marked(text: str) -> str:
    """Escape text, highlight dynamic content, and turn hit markers into <mark>.

    Dynamic content is wrapped before the markers become tags, so a search
    hit inside a placeholder ends up as a <mark> inside the pink <span>.
    """
    html_text = PARTIAL.sub(lambda m: f'<span class="dc">{m.group(0)}</span>', show(text))
    html_text = PARTIAL_FILL.sub(lambda m: f'<span class="fill">{m.group(0)}</span>', html_text)
    return html_text.replace(HIT_START, "<mark>").replace(HIT_END, "</mark>")


STYLE = """
body { font: 16px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 60rem; padding: 1rem 1.5rem 3rem; color: #1d1d1f; }
a { color: #0b57d0; }
h1 { font-size: 1.4rem; margin: .5rem 0 1rem; }
form.search { display: flex; gap: .5rem; margin-bottom: .5rem; }
form.search input[type=search] { flex: 1 1 auto; font-size: 1.1rem; padding: .5rem .7rem; }
button { font-size: .95rem; padding: .3rem .6rem; }
.chip { display: inline-block; font-size: .8rem; line-height: 1.5; padding: 0 .55rem; border-radius: 999px; background: #eef3fb; color: #1e4f91; text-decoration: none; border: 1px solid transparent; white-space: nowrap; }
.chip:hover { border-color: #1e4f91; }
.chip.on { background: #1e4f91; color: #fff; }
.chip .n { opacity: .7; margin-left: .3rem; }
.tags { display: flex; flex-wrap: wrap; gap: .3rem; margin: .3rem 0 .5rem; }
.selected { margin: .5rem 0; line-height: 2; }
details.tagpicker { border: 1px solid #ddd; border-radius: .5rem; padding: .4rem .8rem; margin: .5rem 0; }
details.tagpicker > summary { cursor: pointer; font-weight: 600; }
details.tag-group > summary { cursor: pointer; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: #555; margin-top: .4rem; }
.view-toggle { margin: .7rem 0 .3rem; font-size: .9rem; }
.view-toggle a { padding: .15rem .7rem; border: 1px solid #bbb; text-decoration: none; }
.view-toggle a.first { border-radius: .3rem 0 0 .3rem; }
.view-toggle a.last { border-radius: 0 .3rem .3rem 0; border-left: 0; }
.view-toggle a.on { background: #0b57d0; border-color: #0b57d0; color: #fff; }
table.results th a { color: inherit; text-decoration: none; white-space: nowrap; }
table.results td.updated { white-space: nowrap; }
.count { color: #555; margin: .5rem 0; }
.result { border-top: 1px solid #ddd; padding: .7rem 0; }
.result h2 { font-size: 1.05rem; margin: 0 0 .2rem; }
.meta { color: #555; font-size: .9rem; }
.snippet { margin: .3rem 0 0; }
mark { background: #ffe58a; }
.dc { background: #ffd6ec; color: #8c1257; border-radius: .2rem; padding: 0 .1rem; }
.fill { background: #ffe2c4; color: #8a4500; border-radius: .2rem; padding: 0 .1rem; }
.legend { color: #555; font-size: .85rem; }
.badge { border-radius: .3rem; font-size: .8rem; padding: 0 .35rem; }
.active { background: #d9f2df; } .inactive { background: #eee; color: #555; }
.reply { white-space: pre-wrap; background: #f6f6f6; padding: .8rem; border-radius: .3rem; }
table { border-collapse: collapse; width: 100%; } td, th { border-top: 1px solid #ddd; padding: .35rem .5rem; text-align: left; vertical-align: top; }
td.value { white-space: pre-wrap; word-break: break-word; }
pre.json { overflow-x: auto; background: #f6f6f6; padding: .8rem; font-size: .8rem; }
"""


def page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
            f"<body>{body}</body></html>")


def status_badge(macro: dict) -> str:
    return ('<span class="badge active">active</span>' if macro.get("active")
            else '<span class="badge inactive">inactive</span>')


def zendesk_link(index: MacroIndex, macro_id: int) -> str:
    return (f'<a href="{esc(index.admin_url(macro_id))}" target="_blank" '
            'rel="noopener noreferrer">Open in Zendesk</a>')


def category_label(value: str) -> str:
    return "(no category)" if value == NO_CATEGORY else value


def first(params, key: str) -> str:
    """One parameter value from a mapping of strings or of value lists."""
    value = params.get(key, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return as_text(value)


def chip(label: str, href: str, on: bool = False, count: int | None = None, title: str = "") -> str:
    return (f'<a class="chip{" on" if on else ""}" href="{esc(href)}"'
            + (f' title="{esc(title)}"' if title else "") + f">{esc(label)}"
            + (f'<span class="n">{count}</span>' if count is not None else "") + "</a>")


class SearchState:
    """The query, tag filters, view, and sort of a search page (issue #15)."""

    def __init__(self, params):
        self.q = first(params, "q")
        self.filters = parse_filters(params)
        self.view = "table" if first(params, "view") == "table" else "list"
        self.sort = parse_sort(first(params, "sort"))
        self.picker = first(params, "picker") == "open"

    def url(self, filters=None, view=None, sort=None, picker=False) -> str:
        qs = search_query(self.q, self.filters if filters is None else filters,
                          self.view if view is None else view, self.sort if sort is None else sort, picker)
        return "/?" + qs if qs else "/"

    def selected(self, key: str, value: str) -> bool:
        return any(same_value(key, value, c) for c in self.filters.get(key, []))


def selected_row(index: MacroIndex, st: SearchState) -> str:
    """ "Category: Billing ✕ or Donation ✕ · Status: active ✕ · Clear all" """
    if not st.filters:
        return ""
    parts = []
    for key, label in FACETS:
        chips = [chip(f"{index.value_label(key, v)} ✕", st.url(without_value(st.filters, key, v), picker=st.picker),
                      on=True, title="Remove") for v in st.filters.get(key, [])]
        if chips:
            parts.append(f"{esc(label)}: " + " or ".join(chips))
    clear = f'<a href="{esc(st.url({}, picker=st.picker))}">Clear all</a>'
    return f'<p class="selected">{" · ".join(parts)} · {clear}</p>'


def tag_picker(index: MacroIndex, st: SearchState) -> str:
    """All tag groups in a collapsed section. A click on a chip selects or removes it."""
    groups = []
    for key, label, choices in index.facet_choices():
        chips = "".join(
            chip(text, st.url((without_value if st.selected(key, v) else with_value)(st.filters, key, v), picker=True),
                 st.selected(key, v), n)
            for v, text, n in choices)
        expanded = key in st.filters or len(choices) <= OPEN_GROUP_LIMIT
        groups.append(f'<details class="tag-group"{" open" if expanded else ""}>'
                      f'<summary>{esc(label)} ({len(choices)})</summary><div class="tags">{chips}</div></details>')
    return (f'<details class="tagpicker"{" open" if st.picker else ""}><summary>Filter by tag</summary>'
            + "".join(groups) + "</details>")


def macro_chips(index: MacroIndex, st: SearchState, macro: dict, keys=("category", "group", "tag")) -> list[str]:
    """Chips for a result. A click adds the value to the filter."""
    return [" ".join(chip(index.value_label(key, v), st.url(with_value(st.filters, key, v)), st.selected(key, v))
                     for v in facet_values(macro, key))
            for key in keys if facet_values(macro, key)]


def view_toggle(st: SearchState) -> str:
    links = []
    for view, label, cls in (("list", "List", "first"), ("table", "Table", "last")):
        on = st.view == view
        links.append(f'<a class="{cls}{" on" if on else ""}" href="{esc(st.url(view=view))}"'
                     + (' aria-current="true"' if on else "") + f">{label}</a>")
    return '<p class="view-toggle">Show as: ' + "".join(links) + "</p>"


def results_table(index: MacroIndex, st: SearchState, shown: list[dict], detail_suffix: str) -> str:
    def th(column: str, label: str) -> str:
        on = st.sort.removeprefix("-") == column
        down = st.sort.startswith("-")
        aria = ("descending" if down else "ascending") if on else "none"
        arrow = (" ▼" if down else " ▲") if on else ""
        return (f'<th aria-sort="{aria}"><a href="{esc(st.url(sort=next_sort(st.sort, column)))}">'
                f"{label}{arrow}</a></th>")

    rows = "".join(
        f'<tr><td><a href="/macro/{r["id"]}{esc(detail_suffix)}">{marked(r["title"])}</a> {status_badge(r["macro"])}</td>'
        + "".join(f"<td>{c}</td>" for c in macro_chips(index, st, r["macro"], ("category", "group")))
        + f'<td class="updated">{esc(as_text(r["macro"].get("updated_at"))[:10])}</td></tr>'
        for r in shown)
    return ('<table class="results"><thead><tr>' + th("title", "Title") + th("category", "Category")
            + th("group", "Available for") + th("updated", "Updated") + f"</tr></thead><tbody>{rows}</tbody></table>")


def render_search(index: MacroIndex, params) -> str:
    st = SearchState(params)
    results = index.search(st.q, st.filters, st.sort)
    qs = search_query(st.q, st.filters, st.view, st.sort)
    detail_suffix = f"?{qs}" if qs else ""

    hidden = "".join(f'<input type="hidden" name="{key}" value="{esc(v)}">'
                     for key, _ in FACETS for v in st.filters.get(key, []))
    if st.view == "table":
        hidden += '<input type="hidden" name="view" value="table">'
    form = (
        '<form class="search" method="get" action="/">'
        f'<input type="search" name="q" value="{esc(st.q)}" placeholder="Search titles, replies, and tags" autofocus>'
        f'{hidden}<button type="submit">Search</button></form>'
    )

    shown = results[:MAX_RESULTS]
    if st.view == "table":
        items = results_table(index, st, shown, detail_suffix) if shown else ""
    else:
        items = "".join(
            '<div class="result">'
            f'<h2><a href="/macro/{r["id"]}{esc(detail_suffix)}">{marked(r["title"])}</a></h2>'
            f'<div class="meta">{" · ".join([status_badge(r["macro"]), *macro_chips(index, st, r["macro"]), zendesk_link(index, r["id"])])}</div>'
            + (f'<p class="snippet">{marked(r["snippet"])}</p>' if r["snippet"] else "")
            + "</div>"
            for r in shown)
    count = f"{len(results)} of {len(index.macros)} macros"
    if len(results) > len(shown):
        count += f" (first {len(shown)} shown)"
    legend = ('<p class="legend"><span class="dc">{{pink}}</span> dynamic content · '
              '<span class="fill">[[orange]]</span> replace before sending</p>')
    exported = export_label(index.exported_at)
    if exported:
        count += f" · {exported}"
    body = (f'<h1>Zendesk macros</h1>{form}{selected_row(index, st)}{tag_picker(index, st)}{view_toggle(st)}'
            f'<p class="count">{esc(count)}</p>{legend}{items}')
    return page(f"{st.q} - Zendesk macros" if st.q else "Zendesk macros", body)


def render_value(index: MacroIndex, field: str, value) -> str:
    if field == "comment_value_html":
        return linked(html_to_text(as_text(value)))
    if field == "comment_value":
        return linked(as_text(value))
    if field == "group_id" and str(value).isdigit():
        return esc(f"{index.group_name(int(value))} ({value})")
    return show(value)


def render_macro(index: MacroIndex, macro_id: int, back_qs: str) -> str:
    m = index.macros[macro_id]
    rows = "".join(
        f'<tr><th>{esc(a.get("field"))}</th><td class="value">{render_value(index, a.get("field"), a.get("value"))}</td></tr>'
        for a in actions(m)
    )
    reply = index.replies[macro_id]
    body = (
        f'<p><a href="/{esc("?" + back_qs if back_qs else "")}">Back to search</a></p>'
        f'<h1>{esc(m.get("title"))}</h1>'
        f'<p class="meta">{status_badge(m)} · {esc(index.available_for(m))} · {zendesk_link(index, macro_id)}<br>'
        f'Created {esc(m.get("created_at"))} · Updated {esc(m.get("updated_at"))} · ID {macro_id}</p>'
        + (f'<h2>Description</h2><p class="reply">{show(m.get("description"))}</p>' if m.get("description") else "")
        + (f'<h2>Reply</h2><div class="reply">{linked(reply)}</div>' if reply else "")
        + f'<h2>Actions</h2><table>{rows}</table>'
        + '<details><summary>API JSON</summary><pre class="json">'
        + esc(json.dumps(m, indent=2, ensure_ascii=False)) + "</pre></details>"
    )
    return page(f"{m.get('title')} - Zendesk macros", body)


# --- server ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "macro_search"

    def do_GET(self):
        index: MacroIndex = self.server.index
        port = self.server.server_address[1]
        # A Host check blocks DNS rebinding: a web page cannot point its own
        # domain at 127.0.0.1 and read the macros through the user's browser.
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self.reply(403, page("Forbidden", "<h1>Forbidden</h1>"))
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        if url.path == "/":
            return self.reply(200, render_search(index, params))
        match = re.fullmatch(r"/macro/(\d+)", url.path)
        if match and int(match.group(1)) in index.macros:
            return self.reply(200, render_macro(index, int(match.group(1)), url.query))
        return self.reply(404, page("Not found", '<h1>Not found</h1><p><a href="/">Search</a></p>'))

    def reply(self, status: int, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # search words are private; do not print them


def make_server(index: MacroIndex, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.index = index
    return server


# --- entry point -----------------------------------------------------------


def load_document(path: Path, key: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"{path} has no `{key}` list")
    return data


def load_list(path: Path, key: str) -> list[dict]:
    return load_document(path, key)[key]


def normalize_subdomain(value: str) -> str:
    name = re.sub(r"^https?://", "", value.strip().lower()).rstrip("/").removesuffix(".zendesk.com")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"not a Zendesk subdomain: {value!r}")
    return name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Search the downloaded Zendesk macros in a local web app.")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="directory with macros/ and groups/ (default: %(default)s)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (default: %(default)s)")
    ap.add_argument("--subdomain", default=DEFAULT_SUBDOMAIN,
                    help="Zendesk subdomain for the links (default: %(default)s)")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser")
    args = ap.parse_args(argv)

    data = args.data.expanduser()
    macros_path = data / "macros" / "macros.json"
    groups_path = data / "groups" / "groups.json"
    try:
        subdomain = normalize_subdomain(args.subdomain)
        macros_doc = load_document(macros_path, "macros")
        macros = macros_doc["macros"]
    except FileNotFoundError:
        print(f"error: {macros_path} not found. Run: uv run scrape_macros.py", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        groups = load_list(groups_path, "groups")
    except (FileNotFoundError, ValueError):
        print(f"warning: {groups_path} not readable; showing group IDs, not names. "
              "Run uv run scrape_macros.py to download the groups.", file=sys.stderr)
        groups = []

    index = MacroIndex(macros, groups, subdomain, macros_doc.get("exported_at"))
    try:
        server = make_server(index, args.port)
    except OSError as exc:
        print(f"error: cannot listen on {HOST}:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"{len(macros)} macros, {len(groups)} groups. Search at {url}  (Ctrl-C stops)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
