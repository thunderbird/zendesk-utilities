#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright"]
# ///
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Create a new Zendesk macro from a URL or the clipboard (issue #6).

  uv run new_macro.py --brand thundermail \\
      --name "Thundermail::Accounts - Does Thundermail support POP3" \\
      --description "Use when a user asks about POP3. Example: #1234" \\
      https://support.tb.pro/hc/en-us/articles/57205706556179
  uv run new_macro.py --brand tfa --name ... --description ... --clipboard
  uv run new_macro.py ... --file reply.html --dry-run

The script lists similar macros, writes a draft in the official API
structure to ~/zendesk-data/drafts/, and checks the draft against the macro
creation rules (macro_rules.py). After you type `create`, it clones the brand
template in Admin Center, fills in the form, and clicks Create (macro_ui.py).
It then downloads the new macro and compares it with the draft.

It never changes or deletes an existing macro. --dry-run stops before
Admin Center.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import macro_rules as rules
import macro_ui
import scrape_macros as scrape
from macro_search import MacroIndex, html_to_text, load_list

DEFAULT_DATA = scrape.DEFAULT_DATA
KB_RE = re.compile(r"^https://(?:support\.tb\.pro|[a-z0-9-]+\.zendesk\.com)/hc/(?:[A-Za-z-]+/)?articles/(\d+)")
STOP_WORDS = {"this", "that", "with", "your", "have", "from", "will", "they", "what", "when",
              "which", "there", "their", "about", "would", "could", "should", "these", "those",
              "into", "just", "than", "then", "them", "were", "been", "being", "also", "please",
              "hello", "thanks", "thank", "ticket", "requester", "first_name"}


class InputError(Exception):
    """The input cannot become a macro. Nothing was sent to Zendesk."""


# --- reply sources ---------------------------------------------------------


class SafeHTML(HTMLParser):
    """Keep only simple formatting, so pasted or fetched HTML is safe in a macro.

    Scripts, styles, forms, navigation and all attributes except link targets
    are dropped. With `region`, only the content of the first <article> or
    <main> element is kept, which leaves out page headers and footers.
    """

    KEEP = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "a", "code", "pre",
            "blockquote", "h2", "h3", "h4", "hr"}
    VOID = {"br", "hr"}
    DROP = {"script", "style", "noscript", "nav", "header", "footer", "form", "button",
            "svg", "iframe", "template", "head", "title"}

    def __init__(self, region: str | None = None, base_url: str | None = None):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.dropping = 0
        self.region = region
        self.region_depth = 0 if region else 1
        self.region_done = False

    def _active(self) -> bool:
        return self.region_depth > 0 and not self.dropping and not self.region_done

    def handle_starttag(self, tag, attrs):
        if self.region and tag == self.region and not self.region_done:
            self.region_depth += 1
            return
        if tag in self.DROP:
            self.dropping += 1
            return
        if not self._active() or tag not in self.KEEP:
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if self.base_url and href and not re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                href = urljoin(self.base_url, href)  # relative link on the source page
            if re.match(r"^(https?:|mailto:)", href, re.I):
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
            else:
                self.out.append("<a>")
            return
        self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if self.region and tag == self.region and self.region_depth > 0:
            self.region_depth -= 1
            if self.region_depth == 0:
                self.region_done = True
            return
        if tag in self.DROP:
            self.dropping = max(0, self.dropping - 1)
            return
        if self._active() and tag in self.KEEP and tag not in self.VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._active():
            self.out.append(html.escape(data, quote=False))


def sanitize_html(source: str, main_only: bool = False, base_url: str | None = None) -> str:
    region = None
    if main_only:
        region = "article" if re.search(r"<article\b", source, re.I) else (
            "main" if re.search(r"<main\b", source, re.I) else None)
    parser = SafeHTML(region, base_url)
    parser.feed(source)
    parser.close()
    result = "".join(parser.out)
    result = re.sub(r"<a>(.*?)</a>", r"\1", result, flags=re.S)  # links without a safe target
    result = re.sub(r"<(p|li|h[234])>\s*</\1>", "", result)
    return re.sub(r"\n\s*\n+", "\n", result).strip()


URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")


def text_to_html(text: str) -> str:
    """Plain text to paragraphs. Blank lines separate paragraphs; URLs become links."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]

    def line(s: str) -> str:
        pieces, last = [], 0
        for m in URL_RE.finditer(s):
            pieces.append(html.escape(s[last:m.start()], quote=False))
            url = html.escape(m.group(0), quote=True)
            pieces.append(f'<a href="{url}">{url}</a>')
            last = m.end()
        pieces.append(html.escape(s[last:], quote=False))
        return "".join(pieces)

    return "".join("<p>" + "<br>".join(line(l) for l in p.split("\n")) + "</p>" for p in paragraphs)


def read_clipboard() -> str:
    """The clipboard as reply HTML. macOS only: HTML if present, else plain text."""
    if sys.platform != "darwin":
        raise InputError("--clipboard works only on macOS; save the reply to a file and use --file")
    res = subprocess.run(["osascript", "-e", "the clipboard as «class HTML»"],
                         capture_output=True, text=True)
    match = re.search(r"«data HTML([0-9A-Fa-f]*)»", res.stdout)
    if res.returncode == 0 and match and match.group(1):
        return sanitize_html(bytes.fromhex(match.group(1)).decode("utf-8", "replace"))
    text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    if not text.strip():
        raise InputError("the clipboard is empty")
    return text_to_html(text)


def read_file(path: Path) -> str:
    text = path.expanduser().read_text(encoding="utf-8")
    return sanitize_html(text) if path.suffix.lower() in (".html", ".htm") else text_to_html(text)


# Runs in the page. Inside <article>, <main>, or <body>, find the element
# whose direct children hold the most paragraph, list, and heading text. That
# is the post body. Its neighbors (title, date, author, tags, buttons,
# reference lists) stay out of the reply.
MAIN_CONTENT_JS = """() => {
  const root = document.querySelector('article') || document.querySelector('main') || document.body;
  const blocks = ['P', 'UL', 'OL', 'H2', 'H3', 'H4', 'PRE', 'BLOCKQUOTE', 'TABLE'];
  let best = root, bestScore = -1;
  for (const el of [root, ...root.querySelectorAll('div, section, article, main')]) {
    let score = 0;
    for (const c of el.children) {
      if (blocks.includes(c.tagName)) score += (c.innerText || '').trim().length;
    }
    if (score > bestScore) { best = el; bestScore = score; }
  }
  return bestScore > 0 ? best.innerHTML : '';
}"""


def same_site(url: str, final_url: str) -> bool:
    return urlsplit(url).hostname == urlsplit(final_url).hostname


def read_url(ctx, base: str, url: str) -> str:
    """Reply HTML from a Knowledge Base article or another web page.

    Other pages open in a browser tab, so pages that need JavaScript work,
    and so do sites where you signed in with --sign-in. A page that sends
    the browser to another site is a sign-in page, and the script stops
    instead of using the sign-in form as the reply.
    """
    match = KB_RE.match(url)
    if match:
        data = scrape.get_json(ctx.request, f"{base}/api/v2/help_center/articles/{match.group(1)}.json")
        article = data.get("article") or {}
        if not article.get("body"):
            raise InputError(f"article {match.group(1)} has no body")
        return sanitize_html(article["body"], base_url=article.get("html_url") or url)
    from playwright.sync_api import Error as PlaywrightError

    page = ctx.new_page()
    try:
        try:
            res = page.goto(url, wait_until="load", timeout=60_000)
        except PlaywrightError as exc:
            raise InputError(f"cannot open {url}: {str(exc).splitlines()[0]}") from exc
        # Many pages never go quiet (analytics, live updates), so wait for
        # network idle only for a short time, for content that loads late.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightError:
            pass
        if not same_site(url, page.url):
            raise InputError(
                f"{url} sent the browser to {urlsplit(page.url).hostname}, which is probably a "
                f"sign-in page. Sign in once with: uv run new_macro.py --sign-in {url}")
        if res is not None and res.status >= 400:
            raise InputError(f"{url} returned HTTP {res.status}")
        body = page.evaluate(MAIN_CONTENT_JS)
        if body.strip():
            return sanitize_html(body, base_url=page.url)
        return sanitize_html(page.content(), main_only=True, base_url=page.url)
    finally:
        page.close()


def sign_in(ctx, url: str) -> None:
    """Let the agent sign in to a site in the script's Firefox profile."""
    page = ctx.new_page()
    page.goto(url, timeout=60_000)
    print(f"Sign in to {urlsplit(url).hostname} in the Firefox window.")
    try:
        input("Press Enter here after the page shows its content: ")
    except EOFError:
        pass
    if not same_site(url, page.url):
        raise InputError(f"the browser is still at {urlsplit(page.url).hostname}; the sign-in did not finish")
    print("Signed in. The Firefox profile keeps the session for later runs.")


# --- draft -----------------------------------------------------------------


def greeting(template: dict) -> str:
    """The template's first paragraph when it greets the requester by name."""
    for value in rules.action_values(template, "comment_value_html"):
        match = re.match(r"\s*(<p>[^<]*\{\{ticket\.requester[^<]*</p>)", value)
        if match:
            return match.group(1)
    return ""


def build_draft(template: dict, brand: rules.Brand, title: str, description: str,
                reply_html: str, tag: str) -> dict:
    """The template's actions, with the new reply and tag, in API structure."""
    if rules.brand_of(template) != brand:
        raise InputError(f"template {template.get('id')} is not a {brand.name} macro")
    actions, has_reply, has_tag = [], False, False
    for action in template.get("actions") or []:
        field = action.get("field")
        if field == "comment_value":
            raise InputError(f"template {template.get('id')} has a plain text fallback, "
                             "which this script does not fill in")
        if field == "comment_value_html":
            actions.append({"field": field, "value": reply_html})
            has_reply = True
        elif field in rules.TAG_FIELDS:
            if not has_tag:
                actions.append({"field": field, "value": tag})
                has_tag = True
        else:
            actions.append(dict(action))
    if not (has_reply and has_tag):
        raise InputError(f"template {template.get('id')} must have a reply and a tag action")
    return {"title": title, "description": description, "active": True,
            "actions": actions, "restriction": template.get("restriction")}


def similar_macros(index: MacroIndex, text: str, limit: int = 5) -> list[dict]:
    """The existing macros that share the most distinctive words with `text`."""
    text = re.sub(r"\{\{.*?\}\}", " ", text)
    words = {w.lower() for w in re.findall(r"[^\W\d_]{4,}", text)} - STOP_WORDS
    with index.lock:
        index.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vocab USING fts5vocab(fts, 'row')")
        docs = dict(index.db.execute("SELECT term, doc FROM vocab").fetchall())
        common = max(3, len(index.macros) * 0.3)
        terms = sorted((w for w in words if 0 < docs.get(w, 0) <= common), key=lambda w: docs[w])[:20]
        if not terms:
            return []
        rows = index.db.execute(
            "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, ?, ?, ?, ?) LIMIT ?",
            (" OR ".join(f'"{t}"' for t in terms), *(10.0, 3.0, 1.0, 5.0), limit),
        ).fetchall()
    return [index.macros[r[0]] for r in rows]


def find_created(request, base: str, title: str, since: dt.datetime,
                 attempts: int = 6, sleep=time.sleep) -> int:
    """The ID of the macro named `title` that was created after `since`.

    The search index can lag a few seconds behind, so try more than once.
    """
    for attempt in range(attempts):
        data = scrape.get_json(request, f"{base}/api/v2/macros/search.json?query={quote(title)}", sleep=sleep)
        for m in data.get("macros") or []:
            created = dt.datetime.fromisoformat((m.get("created_at") or "").replace("Z", "+00:00"))
            if m.get("title") == title and created >= since:
                return m["id"]
        sleep(5)
    raise macro_ui.UIError(f"Zendesk accepted the form, but no macro named {title!r} appeared in the "
                           "search. Look for it in Admin Center before you run the script again")


def normalized_text(reply_html: str) -> str:
    return " ".join(html_to_text(reply_html).split())


def compare(draft: dict, saved: dict) -> list[str]:
    """Differences between the draft and the macro that Zendesk saved."""
    diffs = []
    for key in ("title", "description"):
        if (draft.get(key) or "").strip() != (saved.get(key) or "").strip():
            diffs.append(f"{key}: {saved.get(key)!r}")
    if sorted(rules.tags_of(draft)) != sorted(rules.tags_of(saved)):
        diffs.append(f"tags: {rules.tags_of(saved)}")
    if rules.brand_of(draft) != rules.brand_of(saved):
        diffs.append("brand")
    want = normalized_text(" ".join(rules.action_values(draft, "comment_value_html")))
    got = normalized_text(" ".join(rules.action_values(saved, "comment_value_html")))
    if want != got:
        diffs.append("reply text")
    return diffs


# --- entry point -----------------------------------------------------------


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Create a new Zendesk macro from a URL or the clipboard.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("url", nargs="?",
                     help="the page with the reply text: a Knowledge Base article or any web page. "
                          "This is not the template; --brand selects the template")
    src.add_argument("--clipboard", action="store_true", help="read the reply from the clipboard (macOS)")
    src.add_argument("--file", type=Path, help="read the reply from an .html or text file")
    src.add_argument("--sign-in", metavar="URL", dest="sign_in",
                     help="open URL in Firefox to sign in to its site once, then stop")
    ap.add_argument("--brand", choices=sorted(rules.BRANDS),
                    help="thundermail, donors, or tfa; selects the template to clone")
    ap.add_argument("--name", help="Category::Subcategory - Macro name")
    ap.add_argument("--description", help="when to use the macro; add an example ticket ID")
    ap.add_argument("--tag", help="override the macro_<service>_<name> tag")
    ap.add_argument("--template", type=int, help="macro ID to clone (default: the brand template)")
    ap.add_argument("--no-greeting", action="store_true", help="do not add the template's greeting")
    ap.add_argument("--dry-run", action="store_true", help="stop before Admin Center")
    ap.add_argument("--headless", action="store_true", help="hide the Firefox window")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="directory with macros/, groups/ and drafts/ (default: %(default)s)")
    ap.add_argument("--profile", type=Path, default=scrape.DEFAULT_PROFILE,
                    help="Firefox profile with the session (default: %(default)s)")
    ap.add_argument("--subdomain", default=scrape.DEFAULT_SUBDOMAIN)
    return ap.parse_args(argv)


def confirm() -> bool:
    try:
        return input("\nType 'create' to create this macro in Zendesk: ").strip() == "create"
    except EOFError:
        return False


def main(argv=None) -> int:
    args = parse_args(argv)
    ctx_holder: dict = {}

    def context(zendesk: bool = True):
        """Start Firefox only when a step needs it.

        `zendesk=False` skips the Zendesk session check, for --sign-in.
        """
        if "ctx" not in ctx_holder:
            from playwright.sync_api import sync_playwright
            ctx_holder["pw"] = sync_playwright().start()
            headless = (args.headless or args.dry_run) and not args.sign_in
            ctx = scrape.open_context(ctx_holder["pw"], profile, headless=headless)
            ctx_holder["ctx"] = ctx
            if zendesk and not scrape.signed_in_user(ctx.request, base):
                raise scrape.SessionError("Not signed in. Run: uv run scrape_macros.py --login")
        return ctx_holder["ctx"]

    try:
        base = f"https://{scrape.normalize_subdomain(args.subdomain)}.zendesk.com"
        data = args.data.expanduser().resolve()
        profile = args.profile.expanduser().resolve()
        drafts = data / "drafts"
        for label, path in (("drafts", drafts), ("profile", profile)):
            bad = scrape.unsafe_location(path, scrape.synced_locations())
            if bad:
                raise InputError(f"refusing {label} directory {path}: it is inside {bad}")

        if args.sign_in:
            sign_in(context(zendesk=False), args.sign_in)
            return 0
        missing = [f"--{n}" for n in ("brand", "name", "description") if not getattr(args, n)]
        if missing:
            raise InputError("missing " + ", ".join(missing))

        brand = rules.BRANDS[args.brand]
        title = " ".join(args.name.split())
        description = args.description.strip()
        template_id = args.template or brand.template_id
        if not template_id:
            raise InputError(f"{brand.name} has no team template yet; give one with --template <macro ID>")

        try:
            macros = load_list(data / "macros" / "macros.json", "macros")
        except FileNotFoundError:
            raise InputError(f"{data / 'macros' / 'macros.json'} not found. Run: uv run scrape_macros.py")
        try:
            groups = load_list(data / "groups" / "groups.json", "groups")
        except (FileNotFoundError, ValueError):
            groups = []
        template = next((m for m in macros if m.get("id") == template_id), None)
        if template is None:
            raise InputError(f"template {template_id} is not in macros.json. Run: uv run scrape_macros.py")

        # 1. The reply.
        if args.clipboard:
            reply = read_clipboard()
        elif args.file:
            reply = read_file(args.file)
        else:
            reply = read_url(context(), base, args.url)
        if not reply.strip():
            raise InputError("the source has no reply text")
        reply = rules.strip_en_us(reply)                                # rule 5
        if not args.no_greeting and "{{ticket.requester" not in reply:
            reply = greeting(template) + reply

        # 2. The draft.
        tag = args.tag or rules.make_tag(brand, title)                  # rule 6
        draft = build_draft(template, brand, title, description, reply, tag)
        drafts.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        draft_path = drafts / f"{stamp}-{rules.slug(title)[:60]}.json"
        scrape.write_json(draft_path, {"macro": draft})                # POST /api/v2/macros body

        # 3. Similar macros and the checks.
        index = MacroIndex(macros, groups, scrape.normalize_subdomain(args.subdomain))
        similar = similar_macros(index, f"{title} {html_to_text(reply)}")
        problems = rules.check_macro(draft, {m["title"].casefold() for m in macros if m.get("title")})

        print(f"Draft: {draft_path}\n")
        print(f"Name:        {title}")
        print(f"Description: {description}")
        print(f"Brand:       {brand.name} (template {template_id}: {template.get('title')})")
        print(f"Available:   {index.available_for(draft)}")
        print(f"Tag:         {tag}")
        print("Reply:\n  " + html_to_text(reply).replace("\n", "\n  "))
        print("\nSimilar macros (stop if one already covers this case):")
        for m in similar or []:
            print(f"  - {m['title']}  {macro_ui.macro_url(base, m['id'])}")
        if not similar:
            print("  none found")
        if problems:
            print("\nProblems:")
            for p in problems:
                label = f"rule {p.rule}" if p.rule else "note"
                print(f"  - {label}: {p.message}" + ("" if p.blocking else " (warning)"))
        if any(p.blocking for p in problems):
            print("\nFix the problems above. Nothing was sent to Zendesk.")
            return 1
        if args.dry_run:
            print("\nDry run: nothing was sent to Zendesk.")
            return 0
        if not confirm():
            print("Not confirmed. Nothing was sent to Zendesk.")
            return 1

        # 4. Admin Center.
        page = context().new_page()
        state = macro_ui.fill_form(page, base, template_id, title, description, reply, tag)
        if normalized_text(state["reply_html"]) != normalized_text(reply):
            raise macro_ui.UIError("the reply editor changed the reply text; nothing was created")
        # One minute of tolerance for a clock difference with Zendesk.
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        new_id = macro_ui.submit(page) or find_created(context().request, base, title, since)
        print(f"\nCreated macro {new_id}: {macro_ui.macro_url(base, new_id)}")

        # 5. Compare what Zendesk saved with the draft.
        saved = scrape.get_json(context().request, f"{base}/api/v2/macros/{new_id}.json").get("macro") or {}
        scrape.write_json(draft_path.with_name(draft_path.stem + f".created-{new_id}.json"), {"macro": saved})
        diffs = compare(draft, saved)
        if diffs:
            print("Zendesk saved something different from the draft. Fix it in Admin Center: "
                  + "; ".join(diffs))
        else:
            print("The saved macro matches the draft.")
        print("\nStill to do by hand:\n"
              "  1. Apply the macro to a new test ticket and make sure that all actions work.\n"
              "  2. Delete the test ticket.\n"
              "  3. Ask for a review, with the link above.\n"
              "  4. Run uv run scrape_macros.py to add the macro to the local copy.")
        return 0
    except (InputError, scrape.ScrapeError, macro_ui.UIError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if "ctx" in ctx_holder:
            ctx_holder["ctx"].close()
        if "pw" in ctx_holder:
            ctx_holder["pw"].stop()


if __name__ == "__main__":
    sys.exit(main())
