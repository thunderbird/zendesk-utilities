#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright"]
# ///
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Download all shared Zendesk macros through a signed-in Firefox session.

We have no Zendesk API token yet. The Zendesk agent web app reads its own
data from /api/v2/ with the browser session cookie, so a signed-in browser
context can send the same GET requests. The saved JSON is therefore exactly
what the official API returns, and switching to token auth later changes
only how requests are authenticated, not the files on disk.

All requests go through the browser context's request API (ctx.request), not
fetch() inside a page. Context requests share the cookie jar but do not depend
on page navigation, so a page that moves to the login screen cannot break them.
This is the lesson from thunderbird/sumo-linter tools/scrape/scrape.mjs.

The browser is the installed Firefox release (/Applications/Firefox.app),
driven over WebDriver BiDi with Playwright's `moz-firefox` channel. Playwright's
own bundled Firefox is a patched build that calls itself Nightly; we do not use it.

  uv run scrape_macros.py --login             # sign in by hand in Firefox
  uv run scrape_macros.py                     # download all shared macros and groups
  uv run scrape_macros.py --limit 3           # trial run
  uv run scrape_macros.py --force             # rewrite every file

Macros, cookies and the browser profile stay on this computer. The script
refuses any output or profile path inside this repository or inside a folder
that iCloud can sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
DEFAULT_DATA = HOME / "zendesk-data"
DEFAULT_OUT = DEFAULT_DATA / "macros"
DEFAULT_PROFILE = DEFAULT_DATA / "firefox-profile"
DEFAULT_GROUPS_OUT = DEFAULT_DATA / "groups"
DEFAULT_SUBDOMAIN = "tbpro"
FIREFOX_CHANNEL = "moz-firefox"  # the monthly release, not Nightly or Beta

PAGE_SIZE = 100  # the maximum for cursor pagination
LOGIN_TIMEOUT_S = 600
LOGIN_POLL_S = 5
MAX_WAIT_S = 3600
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT_MS = 30_000


class ScrapeError(Exception):
    """A failure that ends the run with a message, not a traceback."""


class SessionError(ScrapeError):
    """The browser session is missing or expired."""


# --- where files may go ----------------------------------------------------


def synced_locations(home: Path = HOME, repo: Path = REPO) -> list[Path]:
    """Places where macros must never land.

    The repository, because a stray `git add` would publish them. ~/Documents
    and ~/Desktop, because iCloud syncs them when "Desktop & Documents Folders"
    is on (and this repository normally lives in ~/Documents/GIT). And
    ~/Library/Mobile Documents, which is iCloud Drive itself.
    """
    return [
        repo,
        home / "Documents",
        home / "Desktop",
        home / "Library" / "Mobile Documents",
    ]


def unsafe_location(path: Path, locations: list[Path]) -> Path | None:
    """Return the synced location that contains `path`, or None if it is safe.

    Both sides are resolved, so a symlink cannot smuggle a path into iCloud.
    """
    target = path.expanduser().resolve()
    for loc in locations:
        loc = loc.expanduser().resolve()
        if target == loc or target.is_relative_to(loc):
            return loc
    return None


def normalize_subdomain(value: str) -> str:
    """Accept `tbpro`, `tbpro.zendesk.com` or `https://tbpro.zendesk.com/`."""
    name = re.sub(r"^https?://", "", value.strip().lower()).rstrip("/")
    name = name.removesuffix(".zendesk.com")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ScrapeError(f"not a Zendesk subdomain: {value!r}")
    return name


# --- HTTP ------------------------------------------------------------------


def retry_wait(retry_after: str | None, attempt: int) -> int:
    """Seconds to stay quiet after a 429.

    Honour Retry-After, then wait longer on each new attempt, up to an hour.
    Retry-After can also be an HTTP date; then fall back to 60 seconds.
    """
    try:
        base = int(retry_after) if retry_after is not None else 60
    except ValueError:
        base = 60
    return min((base + 5) * attempt, MAX_WAIT_S)


def get_json(request, url: str, sleep=time.sleep) -> dict:
    """GET `url` with the session cookie and return the parsed JSON body."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        res = request.get(
            url,
            timeout=REQUEST_TIMEOUT_MS,
            headers={"Accept": "application/json"},
        )
        if res.status == 429:
            wait = retry_wait(res.headers.get("retry-after"), attempt)
            print(f"  rate limited; waiting {wait}s (attempt {attempt})", file=sys.stderr)
            sleep(wait)
            continue
        if 500 <= res.status < 600:
            wait = min(10 * attempt, 120)
            print(f"  HTTP {res.status}; retrying in {wait}s", file=sys.stderr)
            sleep(wait)
            continue
        if res.status in (401, 403):
            raise SessionError(
                f"GET {url} returned HTTP {res.status}. "
                "Sign in again with --login."
            )
        if res.status != 200:
            raise ScrapeError(f"GET {url} returned HTTP {res.status}: {res.text()[:200]}")
        # A lapsed session redirects to an HTML login page with status 200.
        if "json" not in res.headers.get("content-type", ""):
            raise SessionError(
                f"GET {url} returned a non-JSON page, probably the login screen. "
                "Sign in again with --login."
            )
        return res.json()
    raise ScrapeError(f"GET {url} still failing after {MAX_ATTEMPTS} attempts")


def signed_in_user(request, base: str, sleep=time.sleep) -> dict | None:
    """Return the signed-in agent, or None for an anonymous session.

    Anonymous sessions get a user with `id: null` and role `end-user`.
    """
    try:
        data = get_json(request, f"{base}/api/v2/users/me.json", sleep=sleep)
    except SessionError:
        return None
    user = data.get("user") or {}
    if user.get("id") and user.get("role") in ("agent", "admin"):
        return user
    return None


# --- macros ----------------------------------------------------------------


def first_page_url(base: str) -> str:
    # access=shared: macros shared with a group or the whole account. Personal
    # macros of single agents are out of scope (issue #1). Inactive macros are
    # included because the API returns them unless `active` is set.
    return f"{base}/api/v2/macros.json?access=shared&page%5Bsize%5D={PAGE_SIZE}"


def next_page_url(page: dict) -> str | None:
    """Follow cursor pagination, or offset pagination as a fallback."""
    if "meta" in page:
        if (page.get("meta") or {}).get("has_more"):
            return (page.get("links") or {}).get("next")
        return None
    return page.get("next_page")


def groups_url(base: str) -> str:
    # The web app (issue #3) shows group names for macro restrictions.
    # exclude_deleted=false: some macros are still restricted to deleted
    # groups, and the list leaves those out by default.
    return f"{base}/api/v2/groups.json?exclude_deleted=false&page%5Bsize%5D={PAGE_SIZE}"


def list_resource(request, url: str | None, key: str, delay: float,
                  limit: int | None, sleep=time.sleep):
    """Page through a list endpoint. Return (items, complete).

    `key` is the list's name in the response, for example "macros" or
    "groups". `complete` is False when `limit` cut the list.
    """
    items: list[dict] = []
    pages = 0
    while url:
        page = get_json(request, url, sleep=sleep)
        pages += 1
        batch = page.get(key)
        if not isinstance(batch, list):
            raise ScrapeError(f"page {pages} has no `{key}` list: keys {sorted(page)}")
        items.extend(batch)
        print(f"  page {pages}: {len(batch)} {key} ({len(items)} so far)")
        if limit and len(items) >= limit:
            return items[:limit], False
        url = next_page_url(page)
        if url:
            sleep(delay)
    return items, True


def list_macros(request, base: str, delay: float, limit: int | None, sleep=time.sleep):
    return list_resource(request, first_page_url(base), "macros", delay, limit, sleep)


def list_groups(request, base: str, delay: float, sleep=time.sleep):
    groups, _ = list_resource(request, groups_url(base), "groups", delay, None, sleep)
    return groups


def write_json(path: Path, data) -> None:
    """Write atomically, readable only by the current user."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def needs_write(path: Path, macro: dict, force: bool) -> bool:
    """Skip a macro already on disk unless it changed or --force is set."""
    if force or not path.exists():
        return True
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return old != macro


def save_macros(out: Path, macros: list[dict], complete: bool, force: bool) -> dict:
    """Write <id>.json per macro, and macros.json for a complete listing."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = skipped = 0
    for macro in macros:
        if not isinstance(macro.get("id"), int):
            raise ScrapeError(f"macro without an integer id: {str(macro)[:200]}")
        path = out / f"{macro['id']}.json"
        if needs_write(path, macro, force):
            write_json(path, macro)
            written += 1
        else:
            skipped += 1

    stale: list[str] = []
    if complete:
        # Same shape as the list response. Pagination links are dropped
        # because they point at a session-specific cursor.
        write_json(out / "macros.json", {"macros": macros, "count": len(macros)})
        listed = {f"{m['id']}.json" for m in macros}
        stale = sorted(
            p.name for p in out.glob("*.json")
            if p.name != "macros.json" and p.name not in listed
        )
    return {"written": written, "skipped": skipped, "stale": stale}


def save_groups(out: Path, groups: list[dict]) -> Path:
    """Write groups.json in the shape of the groups list response."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = out / "groups.json"
    write_json(path, {"groups": groups, "count": len(groups)})
    return path


# --- browser ---------------------------------------------------------------


def open_context(playwright, profile: Path, headless: bool):
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        return playwright.firefox.launch_persistent_context(
            str(profile), channel=FIREFOX_CHANNEL, headless=headless
        )
    except Exception as exc:  # Playwright raises a plain Error here
        if "Executable doesn't exist" in str(exc) or "not found" in str(exc).lower():
            raise ScrapeError(
                "Firefox is not installed. Install the release version from "
                "https://www.mozilla.org/firefox/ into /Applications."
            ) from exc
        raise


def login(ctx, base: str) -> dict:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"{base}/agent/")
    print(f"Sign in to {base} in the Firefox window.")
    print(f"The script waits up to {LOGIN_TIMEOUT_S // 60} minutes.")
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            user = signed_in_user(ctx.request, base)
        except ScrapeError:
            user = None  # mid-login pages can fail; keep waiting
        if user:
            print(f"Signed in as {user.get('name')} ({user.get('role')}).")
            return user
        time.sleep(LOGIN_POLL_S)
    raise SessionError("Timed out waiting for sign-in.")


# --- entry point -----------------------------------------------------------


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Download all shared Zendesk macros through a signed-in Firefox session."
    )
    ap.add_argument("--subdomain", default=DEFAULT_SUBDOMAIN,
                    help="Zendesk subdomain (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output directory (default: %(default)s)")
    ap.add_argument("--groups-out", type=Path, default=DEFAULT_GROUPS_OUT,
                    help="groups output directory (default: %(default)s)")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                    help="Firefox profile with the session (default: %(default)s)")
    ap.add_argument("--login", action="store_true",
                    help="open Firefox and wait for you to sign in")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N macros (trial run; macros.json is not written)")
    ap.add_argument("--force", action="store_true",
                    help="rewrite files even if they are unchanged")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between page requests (default: %(default)s)")
    ap.add_argument("--headless", action="store_true",
                    help="hide the Firefox window while downloading")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        base = f"https://{normalize_subdomain(args.subdomain)}.zendesk.com"
        out = args.out.expanduser().resolve()
        profile = args.profile.expanduser().resolve()
        groups_out = args.groups_out.expanduser().resolve()
        for label, path in (("output", out), ("profile", profile), ("groups", groups_out)):
            bad = unsafe_location(path, synced_locations())
            if bad:
                raise ScrapeError(
                    f"refusing {label} directory {path}: it is inside {bad}, "
                    "which is the repository or a folder that iCloud can sync"
                )

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = open_context(pw, profile, headless=args.headless and not args.login)
            try:
                if args.login:
                    login(ctx, base)
                    return 0
                user = signed_in_user(ctx.request, base)
                if not user:
                    raise SessionError("Not signed in. Run: uv run scrape_macros.py --login")
                print(f"Signed in as {user.get('name')} ({user.get('role')}). Listing macros...")
                macros, complete = list_macros(ctx.request, base, args.delay, args.limit)
                print("Listing groups...")
                groups = list_groups(ctx.request, base, args.delay)
            finally:
                ctx.close()

        groups_path = save_groups(groups_out, groups)
        print(f"{len(groups)} groups in {groups_path}")
        result = save_macros(out, macros, complete, args.force)
        print(f"{len(macros)} macros: {result['written']} written, "
              f"{result['skipped']} unchanged, in {out}")
        if not complete:
            print("Partial run (--limit): macros.json not written.")
        if result["stale"]:
            print(f"{len(result['stale'])} files are no longer in the listing "
                  f"(deleted or no longer shared); not removed: "
                  + ", ".join(result["stale"]))
        return 0
    except ScrapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
