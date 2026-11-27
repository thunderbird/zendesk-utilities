# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv run python -m unittest                                   # all tests, offline
uv run python -m unittest test_scrape_macros.Saving         # one test class
uv run scrape_macros.py --login                             # Roland signs in by hand
uv run scrape_macros.py --limit 3                           # trial download
uv run macro_search.py --open                               # search app on 127.0.0.1:8765
```

Use `uv` for all Python work. Each script has a PEP 723 inline metadata block for its dependencies. There is no `pyproject.toml`.

## Architecture

`scrape_macros.py` downloads the shared macros of `tbpro.zendesk.com` (GitHub issue #1). There is no API token yet. The Zendesk agent web app reads `/api/v2/` with its session cookie, so the script sends the same GET requests from a signed-in Firefox context. The saved JSON is the official API response with no changes. In 2027 the plan is to switch to token authentication, and only the request authentication will change.

Playwright drives the installed Firefox release (`/Applications/Firefox.app`) over WebDriver BiDi with `channel="moz-firefox"`. Do not switch to the bundled Playwright Firefox, because it is a patched Nightly build.

All requests go through `ctx.request`, not `fetch()` inside a page, as in thunderbird/sumo-linter `tools/scrape/scrape.mjs`. Tests replace `ctx.request` with a fake object, and Playwright is imported only inside `main()`. So the tests need no browser.

`scrape_macros.py` also downloads `GET /api/v2/groups.json?exclude_deleted=false`. Some macros are restricted to deleted groups, and the list omits those groups by default.

`macro_search.py` (issue #3) uses only the standard library. It loads `macros.json` and `groups.json`, builds an in-memory SQLite FTS5 index, and serves HTML from `http.server`. `fts_query()` puts every user term in double quotes, so FTS5 syntax in the input has no effect. Every macro value goes through `esc()` or `marked()`, and the CSP forbids scripts. The server binds `127.0.0.1` and refuses requests with another `Host` header, to block DNS rebinding.

## Data location

Never write macros, cookies, or the Firefox profile into this repository or into a folder that iCloud syncs. The defaults are `~/zendesk-data/macros` and `~/zendesk-data/firefox-profile`. `unsafe_location()` enforces this rule. Do not weaken it.
