# zendesk-utilities

Utilities for the Thunderbird Pro Zendesk instance (`tbpro.zendesk.com`).

## Download the shared macros

A macro is a saved set of ticket actions that an agent applies with one click. `scrape_macros.py` downloads all shared macros through a signed-in Firefox session, because we do not have Zendesk API access yet. The saved JSON has the structure of the official [Zendesk Macros API](https://developer.zendesk.com/api-reference/ticketing/business-rules/macros/).

1. If Firefox is not in `/Applications`, install the release version from [mozilla.org](https://www.mozilla.org/firefox/). The script uses this monthly release, not Firefox Nightly and not the Firefox build that Playwright downloads.

2. Sign in. A Firefox window opens. Sign in to Zendesk by hand. The script stops after it finds the session.

   ```sh
   uv run scrape_macros.py --login
   ```

3. Download the macros.

   ```sh
   uv run scrape_macros.py
   ```

The script writes one `<id>.json` file per macro and one `macros.json` file to `~/zendesk-data/macros`. It also writes all groups, deleted groups included, to `~/zendesk-data/groups/groups.json`. It keeps the browser session in `~/zendesk-data/firefox-profile`. These directories are not in iCloud Drive and not in this repository. If you give an `--out` or `--profile` directory inside this repository, `~/Documents`, `~/Desktop`, or `~/Library/Mobile Documents`, the script stops with an error.

For a trial run, add `--limit 3`. To rewrite all files, add `--force`. If the session expires, run `--login` again. For all options, run `uv run scrape_macros.py --help`.

## Search the macros

`macro_search.py` is a search web app for the downloaded macros. It runs only on this computer and reads the files in `~/zendesk-data`.

```sh
uv run macro_search.py --open
```

The app is at `http://127.0.0.1:8765/`. It searches the titles, descriptions, replies, and tags. You can filter the results by status, by group, and by the ticket field that a macro changes. Each result links to the macro in Zendesk Admin Center. After you download the macros again, restart the app.

## Create a new macro

`new_macro.py` creates a new macro from a Knowledge Base article, another web page, the clipboard, or a file. It follows the team's macro creation process.

```sh
uv run new_macro.py --brand thundermail \
    --name "Thundermail::Accounts - Macro name" \
    --description "Use when ... Example ticket: #1234" \
    https://support.tb.pro/hc/en-us/articles/<id>
```

Instead of the URL, you can give `--clipboard` (macOS) or `--file reply.html`. The brands are `thundermail`, `donors`, and `tfa`. Donor Support has no team template yet, so for `donors` you must also give `--template <macro ID>`.

The script does these steps:

1. It lists similar macros. If one of them already covers the case, stop.
2. It removes `en-us` from Knowledge Base links and adds the template greeting.
3. It writes a draft to `~/zendesk-data/drafts/` and checks it against the process rules.
4. After you type `create`, it clones the brand template in Admin Center, fills in the form, and clicks Create.
5. It downloads the new macro and compares it with the draft.

To stop before Admin Center, add `--dry-run`. After the script finishes, test the macro on a new ticket, delete the test ticket, and ask for a review.

## Tests

The tests use no browser and no network.

```sh
uv run python -m unittest
```
