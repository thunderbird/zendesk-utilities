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

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            # A <p> inside a list item stays on the "- " line.
            if not (self.parts and self.parts[-1] == "- "):
                self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(source: str) -> str:
    """Plain text of a macro's HTML comment, with paragraph breaks kept."""
    parser = _TextExtractor()
    parser.feed(source)
    parser.close()
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

    # Search ------------------------------------------------------------

    def search(self, q: str = "", status: str = "all", group: str = "", field: str = "") -> list[dict]:
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

        results = []
        for macro_id, title, snip in rows:
            m = self.macros[macro_id]
            if status == "active" and not m.get("active"):
                continue
            if status == "inactive" and m.get("active"):
                continue
            if group == "none" and group_ids(m):
                continue
            if group not in ("", "none") and group not in {str(g) for g in group_ids(m)}:
                continue
            if field and field not in {a.get("field") for a in actions(m)}:
                continue
            results.append({"id": macro_id, "title": title, "snippet": snip, "macro": m})
        return results


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
form.search { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
form.search input[type=search] { flex: 1 1 100%; font-size: 1.1rem; padding: .5rem .7rem; }
select, button { font-size: .95rem; padding: .3rem .4rem; }
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


def options(choices, selected: str) -> str:
    return "".join(
        f"<option value=\"{esc(v)}\"{' selected' if v == selected else ''}>{esc(label)}</option>"
        for v, label in choices
    )


def status_badge(macro: dict) -> str:
    return ('<span class="badge active">active</span>' if macro.get("active")
            else '<span class="badge inactive">inactive</span>')


def zendesk_link(index: MacroIndex, macro_id: int) -> str:
    return (f'<a href="{esc(index.admin_url(macro_id))}" target="_blank" '
            'rel="noopener noreferrer">Open in Zendesk</a>')


def render_search(index: MacroIndex, params: dict[str, str]) -> str:
    q, status, group, field = (params.get(k, "") for k in ("q", "status", "group", "field"))
    status = status if status in ("active", "inactive") else "all"
    results = index.search(q, status, group, field)
    qs = urlencode({k: v for k, v in params.items() if v})
    detail_suffix = f"?{qs}" if qs else ""

    form = (
        '<form class="search" method="get" action="/">'
        f'<input type="search" name="q" value="{esc(q)}" placeholder="Search titles, replies, and tags" autofocus>'
        '<select name="status" aria-label="Status">'
        + options([("all", "Active and inactive"), ("active", "Active"), ("inactive", "Inactive")], status)
        + '</select><select name="group" aria-label="Available for">'
        + options([("", "Any group")] + [(v, f"{label} ({n})") for v, label, n in index.group_choices()], group)
        + '</select><select name="field" aria-label="Changes field">'
        + options([("", "Any field")] + [(f, f"{f} ({n})") for f, n in index.field_choices()], field)
        + '</select><button type="submit">Search</button></form>'
    )

    shown = results[:MAX_RESULTS]
    items = []
    for r in shown:
        m = r["macro"]
        items.append(
            '<div class="result">'
            f'<h2><a href="/macro/{r["id"]}{esc(detail_suffix)}">{marked(r["title"])}</a></h2>'
            f'<div class="meta">{status_badge(m)} · {esc(index.available_for(m))} · {zendesk_link(index, r["id"])}</div>'
            + (f'<p class="snippet">{marked(r["snippet"])}</p>' if r["snippet"] else "")
            + "</div>"
        )
    count = f"{len(results)} of {len(index.macros)} macros"
    if len(results) > len(shown):
        count += f" (first {len(shown)} shown)"
    legend = ('<p class="legend"><span class="dc">{{pink}}</span> dynamic content · '
              '<span class="fill">[[orange]]</span> replace before sending</p>')
    exported = export_label(index.exported_at)
    if exported:
        count += f" · {exported}"
    body = (f'<h1>Zendesk macros</h1>{form}<p class="count">{esc(count)}</p>{legend}' + "".join(items))
    return page(f"{q} - Zendesk macros" if q else "Zendesk macros", body)


def render_value(index: MacroIndex, field: str, value) -> str:
    if field == "comment_value_html":
        return show(html_to_text(as_text(value)))
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
        + (f'<h2>Reply</h2><div class="reply">{show(reply)}</div>' if reply else "")
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
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
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
