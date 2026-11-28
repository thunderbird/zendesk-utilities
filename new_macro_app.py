#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Prepare a new Zendesk macro in a web app on this computer.

The same draft and checks as new_macro.py, with only the standard library:

  uv run new_macro_app.py              # http://127.0.0.1:8766
  uv run new_macro_app.py --open       # also open it in the default browser

You give the brand, the name, the description, and the reply: pasted text,
pasted HTML, or the URL of a public page. The app lists similar macros,
checks the draft against the macro creation rules (macro_rules.py), and
writes a draft without problems to ~/zendesk-data/drafts/.

The app never sends anything to Zendesk. It shows the values to copy into
the cloned template in Admin Center, where you click Create yourself. A
page that needs a sign-in does not work here. Copy its text instead, or use
new_macro.py. Issue #7 adds `POST /api/v2/macros` when we have a token.

The server listens on 127.0.0.1 only, refuses other Host headers (DNS
rebinding), and accepts a POST only from its own pages (Origin check), so
another web site cannot submit the form. The CSP forbids scripts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import macro_rules as rules
import macro_ui
import macro_search as ms
import new_macro as nm
import scrape_macros as scrape
from macro_search import MacroIndex, esc, html_to_text

HOST = "127.0.0.1"  # never 0.0.0.0: other computers must not connect
DEFAULT_PORT = 8766  # macro_search.py uses 8765
MAX_BODY = 2_000_000  # bytes of form data
MAX_PAGE = 5_000_000  # bytes of a fetched page
USER_AGENT = "zendesk-utilities-new-macro-app"
FORMATS = ("text", "html")

CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")


# --- reply from a URL ------------------------------------------------------


def fetch(url: str, timeout: float = 30) -> tuple[str, str]:
    """(final URL, text) of a page, without cookies, as a signed-out visitor."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as res:
            data = res.read(MAX_PAGE + 1)
            charset = res.headers.get_content_charset() or "utf-8"
            final = res.geturl()
    except HTTPError as exc:
        raise nm.InputError(f"{url} returned HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise nm.InputError(f"cannot open {url}: {getattr(exc, 'reason', exc)}") from exc
    if len(data) > MAX_PAGE:
        raise nm.InputError(f"{url} is larger than {MAX_PAGE // 1_000_000} MB")
    return final, data.decode(charset, "replace")


def reply_from_url(url: str, base: str, get=fetch) -> str:
    """Reply HTML from a public Knowledge Base article or another public page.

    An article comes from the Help Center API. Another page keeps only its
    <article> or <main> content. Pages that build their text with JavaScript
    come out empty. A page that sends the visitor to another site is a
    sign-in page, so the app stops.
    """
    if not re.match(r"^https?://", url, re.I):
        raise nm.InputError("the URL must start with https:// or http://")
    match = nm.KB_RE.match(url)
    if match:
        api = f"{base}/api/v2/help_center/articles/{match.group(1)}.json"
        try:
            _, text = get(api)
        except nm.InputError as exc:
            raise nm.InputError(f"cannot read article {match.group(1)} ({exc}). If the article is "
                                "not public, paste its text instead") from exc
        article = json.loads(text).get("article") or {}
        if not article.get("body"):
            raise nm.InputError(f"article {match.group(1)} has no body")
        return nm.sanitize_html(article["body"], base_url=article.get("html_url") or url)
    final, text = get(url)
    if not nm.same_site(url, final):
        raise nm.InputError(f"{url} sent the app to {urlsplit(final).hostname}, which is probably a "
                            "sign-in page. Copy the text from your browser and paste it instead")
    return nm.sanitize_html(text, main_only=True, base_url=final)


# --- draft -----------------------------------------------------------------


@dataclass
class Form:
    brand: str = ""
    name: str = ""
    description: str = ""
    tag: str = ""
    template: str = ""
    url: str = ""
    reply: str = ""
    format: str = "text"
    no_greeting: bool = False

    @classmethod
    def parse(cls, params: dict[str, list[str]]) -> "Form":
        def one(key):
            return (params.get(key) or [""])[0]
        fmt = one("format")
        return cls(brand=one("brand"), name=one("name"), description=one("description"),
                   tag=one("tag").strip(), template=one("template").strip(), url=one("url").strip(),
                   reply=one("reply"), format=fmt if fmt in FORMATS else "text",
                   no_greeting=bool(one("no_greeting")))


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    draft: dict | None = None
    template: dict | None = None
    problems: list = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)
    path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.draft is not None and not any(p.blocking for p in self.problems)


class App:
    """The loaded macros and the settings of one server."""

    def __init__(self, macros: list[dict], groups: list[dict], drafts: Path,
                 subdomain: str = ms.DEFAULT_SUBDOMAIN, exported_at: str | None = None, get=fetch):
        self.macros = macros
        self.index = MacroIndex(macros, groups, subdomain, exported_at)
        self.titles = {m["title"].casefold() for m in macros if m.get("title")}
        self.drafts = drafts
        self.base = f"https://{subdomain}.zendesk.com"
        self.get = get

    def make_draft(self, form: Form) -> Result:
        """The CLI steps 1 to 3 of new_macro.py, for one form submission."""
        res = Result()
        brand = rules.BRANDS.get(form.brand)
        title = " ".join(form.name.split())
        description = form.description.strip()
        for ok, label in ((brand, "the brand"), (title, "the name"), (description, "the description")):
            if not ok:
                res.errors.append(f"Give {label}.")
        if form.url and form.reply.strip():
            res.errors.append("Give a URL or a reply, not both.")
        elif not form.url and not form.reply.strip():
            res.errors.append("Give a URL or paste the reply.")
        template_id = None
        if form.template:
            if not form.template.isdigit():
                res.errors.append("The template must be a macro ID, for example 42980135784467.")
            else:
                template_id = int(form.template)
        elif brand:
            template_id = brand.template_id
            if not template_id:
                res.errors.append(f"{brand.name} has no team template yet. Give a template macro ID.")
        if template_id:
            res.template = next((m for m in self.macros if m.get("id") == template_id), None)
            if res.template is None:
                res.errors.append(f"Template {template_id} is not in macros.json. "
                                  "Run: uv run scrape_macros.py")
        if res.errors:
            return res

        try:
            if form.url:
                reply = reply_from_url(form.url, self.base, self.get)
            elif form.format == "html":
                reply = nm.sanitize_html(form.reply)
            else:
                reply = nm.text_to_html(form.reply)
            if not reply.strip():
                raise nm.InputError("the source has no reply text")
            reply = rules.strip_en_us(reply)                                # rule 5
            if not form.no_greeting and "{{ticket.requester" not in reply:
                reply = nm.greeting(res.template) + reply
            tag = form.tag or rules.make_tag(brand, title)                  # rule 6
            res.draft = nm.build_draft(res.template, brand, title, description, reply, tag)
        except (nm.InputError, ValueError) as exc:
            res.errors.append(f"{str(exc)[:1].upper()}{str(exc)[1:]}.")
            return res

        res.similar = nm.similar_macros(self.index, f"{title} {html_to_text(reply)}")
        res.problems = rules.check_macro(res.draft, self.titles)
        if res.ready:
            self.drafts.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            res.path = self.drafts / f"{stamp}-{rules.slug(title)[:60]}.json"
            scrape.write_json(res.path, {"macro": res.draft})             # POST /api/v2/macros body
        return res


# --- pages -----------------------------------------------------------------


STYLE = ms.STYLE + """
form.draft label { display: block; font-weight: 600; margin: .8rem 0 .2rem; }
form.draft .hint { font-weight: 400; color: #555; font-size: .9rem; }
form.draft input[type=text], form.draft input[type=url], form.draft select, form.draft textarea,
.copy input, .copy textarea { width: 100%; box-sizing: border-box; font: inherit; padding: .35rem .5rem; }
form.draft textarea[name=reply] { min-height: 12rem; }
form.draft fieldset { border: 1px solid #ddd; border-radius: .5rem; margin: 1rem 0; }
form.draft .inline { display: inline; font-weight: 400; margin-right: 1rem; }
form.draft button { margin-top: 1rem; font-size: 1.05rem; padding: .4rem 1rem; }
.errors, .problems { border-left: 4px solid #c5221f; background: #fdecea; padding: .5rem 1rem; }
.warning { color: #8a4500; }
.ok { border-left: 4px solid #188038; background: #e6f4ea; padding: .5rem 1rem; }
.copy label { display: block; font-weight: 600; margin: .8rem 0 .2rem; }
.richreply { border: 1px solid #bbb; border-radius: .3rem; padding: .3rem .8rem; background: #fff; }
ol.steps li { margin: .4rem 0; }
"""


def page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
            f"<body>{body}</body></html>")


def render_form(app: App, form: Form) -> str:
    brands = "".join(
        f'<option value="{esc(key)}"{" selected" if form.brand == key else ""}>{esc(b.name)}'
        + ("" if b.template_id else " (no template: give one below)") + "</option>"
        for key, b in rules.BRANDS.items())
    formats = "".join(
        f'<label class="inline"><input type="radio" name="format" value="{v}"'
        f'{" checked" if form.format == v else ""}> {label}</label>'
        for v, label in (("text", "Plain text: blank lines separate paragraphs"), ("html", "HTML source")))
    return (
        '<form class="draft" method="post" action="/">'
        '<label for="brand">Brand <span class="hint">selects the template to clone</span></label>'
        f'<select id="brand" name="brand" required><option value="">Choose a brand</option>{brands}</select>'
        '<label for="name">Name <span class="hint">Category::Subcategory - Macro name</span></label>'
        f'<input type="text" id="name" name="name" value="{esc(form.name)}" required>'
        '<label for="description">Description <span class="hint">when to use the macro; '
        'add an example ticket ID</span></label>'
        f'<textarea id="description" name="description" rows="3" required>{esc(form.description)}</textarea>'
        '<fieldset><legend>Reply: give a URL or paste the text</legend>'
        '<label for="url">URL <span class="hint">a public Knowledge Base article or web page</span></label>'
        f'<input type="url" id="url" name="url" value="{esc(form.url)}" placeholder="https://support.tb.pro/hc/en-us/articles/...">'
        '<label for="reply">Reply</label>'
        f'<textarea id="reply" name="reply">{esc(form.reply)}</textarea>'
        f'<p>{formats}</p></fieldset>'
        '<details><summary>More options</summary>'
        '<label for="tag">Tag <span class="hint">empty: macro_&lt;service&gt;_&lt;name&gt; from the name</span></label>'
        f'<input type="text" id="tag" name="tag" value="{esc(form.tag)}">'
        '<label for="template">Template macro ID <span class="hint">empty: the brand template</span></label>'
        f'<input type="text" id="template" name="template" value="{esc(form.template)}" inputmode="numeric">'
        f'<label class="inline"><input type="checkbox" name="no_greeting" value="1"'
        f'{" checked" if form.no_greeting else ""}> Do not add the greeting of the template</label>'
        '</details>'
        '<button type="submit">Check the draft</button></form>'
    )


def render_result(app: App, res: Result) -> str:
    if res.errors:
        return '<div class="errors"><h2>Fix these first</h2><ul>' + "".join(
            f"<li>{esc(e)}</li>" for e in res.errors) + "</ul><p>Nothing was saved.</p></div>"
    draft, template = res.draft, res.template
    out = []
    if res.problems:
        items = "".join(
            f'<li class="{"" if p.blocking else "warning"}">'
            + esc((f"Rule {p.rule}: " if p.rule else "Note: ") + p.message + ("" if p.blocking else " (warning)"))
            + "</li>" for p in res.problems)
        out.append(f'<div class="problems"><h2>Problems</h2><ul>{items}</ul>'
                   + ("<p>Fix the problems and check again. No draft was saved.</p>" if not res.ready else "")
                   + "</div>")
    similar = "".join(
        f'<li><a href="{esc(macro_ui.macro_url(app.base, m["id"]))}" target="_blank" '
        f'rel="noopener noreferrer">{esc(m.get("title"))}</a></li>' for m in res.similar)
    out.append("<h2>Similar macros</h2><p>If one of them already covers the case, stop.</p>"
               + (f"<ul>{similar}</ul>" if similar else "<p>None found.</p>"))
    reply = " ".join(rules.action_values(draft, "comment_value_html"))
    tag = " ".join(rules.tags_of(draft))
    out.append(
        "<h2>Draft</h2><table>"
        f"<tr><th>Brand</th><td>{esc(rules.brand_of(draft).name)} (template {template['id']}: "
        f"{esc(template.get('title'))})</td></tr>"
        f"<tr><th>Available for</th><td>{esc(app.index.available_for(draft))}</td></tr>"
        f"<tr><th>Tag</th><td>{esc(tag)}</td></tr></table>")
    if res.ready:
        clone = macro_ui.clone_url(app.base, template["id"])
        out.append(
            f'<p class="ok">Draft saved: {esc(res.path)}</p>'
            "<h2>Create it in Admin Center</h2><ol class=\"steps\">"
            f'<li><a href="{esc(clone)}" target="_blank" rel="noopener noreferrer">Open the clone of the '
            "template</a>. Nothing is created until you click Create.</li>"
            "<li>Copy each value below into the form. Click in a box, select all, and copy.</li>"
            "<li>In the reply editor, replace the template text with the reply. Copy the reply "
            "from the box below, not the HTML source, so that the formatting and links stay.</li>"
            "<li>Remove the template tag and add the new tag.</li>"
            "<li>Click Create. Then apply the macro to a new test ticket, make sure that all actions "
            "work, delete the test ticket, ask for a review, and run uv run scrape_macros.py.</li></ol>"
            '<div class="copy">'
            f'<label for="c-name">Name</label><input id="c-name" type="text" readonly value="{esc(draft["title"])}">'
            f'<label for="c-desc">Description</label><textarea id="c-desc" rows="3" readonly>'
            f'{esc(draft["description"])}</textarea>'
            f'<label for="c-tag">Tag</label><input id="c-tag" type="text" readonly value="{esc(tag)}">'
            f'<label>Reply</label><div class="richreply">{nm.sanitize_html(reply)}</div></div>')
    else:
        out.append(f'<h3>Reply</h3><div class="reply">{ms.linked(html_to_text(reply))}</div>')
    out.append('<details><summary>API JSON (body of POST /api/v2/macros)</summary><pre class="json">'
               + esc(json.dumps({"macro": draft}, indent=2, ensure_ascii=False)) + "</pre></details>")
    return "".join(out)


def render_page(app: App, form: Form, res: Result | None = None) -> str:
    count = f"{len(app.macros)} macros"
    exported = ms.export_label(app.index.exported_at)
    if exported:
        count += f" · {exported}"
    body = (f'<h1>New Zendesk macro</h1><p class="count">{esc(count)}. This app never sends '
            "anything to Zendesk.</p>" + render_form(app, form)
            + (render_result(app, res) if res else ""))
    return page("New Zendesk macro", body)


# --- server ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "new_macro_app"

    def allowed_origins(self) -> tuple[str, ...]:
        port = self.server.server_address[1]
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def host_ok(self) -> bool:
        # A Host check blocks DNS rebinding: a web page cannot point its own
        # domain at 127.0.0.1 and read the drafts through the user's browser.
        return self.headers.get("Host") in self.allowed_origins()

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, page("Forbidden", "<h1>Forbidden</h1>"))
        if urlsplit(self.path).path != "/":
            return self.reply(404, page("Not found", '<h1>Not found</h1><p><a href="/">New macro</a></p>'))
        return self.reply(200, render_page(self.server.app, Form()))

    def do_POST(self):
        # The Origin check blocks cross-site form posts: another web site can
        # send a form to 127.0.0.1, but the browser then sends its own Origin.
        origin = self.headers.get("Origin", "")
        if not self.host_ok() or origin not in tuple(f"http://{o}" for o in self.allowed_origins()):
            return self.reply(403, page("Forbidden", "<h1>Forbidden</h1>"))
        if urlsplit(self.path).path != "/":
            return self.reply(404, page("Not found", "<h1>Not found</h1>"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.reply(413, page("Too large", "<h1>The form is too large</h1>"))
        params = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        form = Form.parse(params)
        app: App = self.server.app
        return self.reply(200, render_page(app, form, app.make_draft(form)))

    def reply(self, status: int, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        # Not no-referrer: with it, the browser sends "Origin: null" on a POST
        # from our own page, and the Origin check refuses the form.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # drafts are private; do not print them


def make_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.app = app
    return server


# --- entry point -----------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prepare a new Zendesk macro in a local web app.")
    ap.add_argument("--data", type=Path, default=scrape.DEFAULT_DATA,
                    help="directory with macros/, groups/ and drafts/ (default: %(default)s)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (default: %(default)s)")
    ap.add_argument("--subdomain", default=ms.DEFAULT_SUBDOMAIN,
                    help="Zendesk subdomain (default: %(default)s)")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser")
    args = ap.parse_args(argv)

    data = args.data.expanduser().resolve()
    drafts = data / "drafts"
    bad = scrape.unsafe_location(drafts, scrape.synced_locations())
    if bad:
        print(f"error: refusing drafts directory {drafts}: it is inside {bad}", file=sys.stderr)
        return 1
    macros_path = data / "macros" / "macros.json"
    try:
        subdomain = ms.normalize_subdomain(args.subdomain)
        doc = ms.load_document(macros_path, "macros")
    except FileNotFoundError:
        print(f"error: {macros_path} not found. Run: uv run scrape_macros.py", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        groups = ms.load_list(data / "groups" / "groups.json", "groups")
    except (FileNotFoundError, ValueError):
        groups = []

    app = App(doc["macros"], groups, drafts, subdomain, doc.get("exported_at"))
    try:
        server = make_server(app, args.port)
    except OSError as exc:
        print(f"error: cannot listen on {HOST}:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"{len(app.macros)} macros. New macro at {url}  (Ctrl-C stops)", flush=True)
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
