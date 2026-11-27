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

## Share the search with colleagues

`macro_search.html` is the same search app as one HTML file. It runs in any modern browser on Windows, macOS, and Linux, with nothing to install.

1. Send your colleague `macro_search.html` from the latest GitHub release, together with `~/zendesk-data/macros/macros.json` and `~/zendesk-data/groups/groups.json`. Use Thundermail Send or another secure channel for the two JSON files.
2. The colleague opens `macro_search.html` in a browser.
3. The colleague drags the two JSON files onto the page, or chooses them with the file picker.

The page sends nothing over the network, and it keeps the macros only in memory. When the tab closes, the data is gone. The page shows when you exported the files, for example "Macros exported 2026-11-27 18:24 UTC". To send newer data, run `uv run scrape_macros.py` again.

## Create a new macro

`new_macro.py` creates a new macro that follows the team's macro creation process. You give the reply text, the brand, the name, and the description.

```sh
uv run new_macro.py --brand thundermail \
    --name "Thundermail::Accounts - Macro name" \
    --description "Use when ... Example ticket: #1234" \
    https://support.tb.pro/hc/en-us/articles/57205706556179-Does-Thundermail-support-POP3
```

### The reply text

The URL is the page with the reply text. It is not the template. Copy the address from the browser. You can give one of these sources:

- A Knowledge Base article on `support.tb.pro`. The script reads the article through your Zendesk session.
- Any other web page, for example a blog post. The script opens the page in Firefox and keeps only the main article text. It drops menus, headers, footers, scripts, and images.
- `--clipboard`: the text that you copied (macOS only). The script keeps the formatting and the links.
- `--file reply.html` or `--file reply.txt`: a file with the reply.

If a web page needs a sign-in, the script stops with an error. Then sign in one time in the script's Firefox profile, and run the command again:

```sh
uv run new_macro.py --sign-in https://example.com/page
```

### The template

You do not give the template. `--brand` selects it:

| `--brand` | Brand | Template |
|---|---|---|
| `thundermail` | Thundermail | 42980135784467, "Thundermail::Category::Subcategory::New macro template" |
| `tfa` | Thunderbird App Store Reviews | 50467223418771, "Thunderbird::Play Store Reviews::Macro Template (CLONE ME)" |
| `donors` | Donor Support | None. You must give `--template <macro ID>`. |

The macro ID is the number at the end of the macro address in Admin Center, for example `https://tbpro.zendesk.com/admin/workspaces/agent-workspace/macros/42980135784467`. The template must be in your local copy, so run `uv run scrape_macros.py` first. The new macro gets all the actions of the template, except the reply and the tag.

### What the script does

1. It lists similar macros. If one of them already covers the case, stop.
2. It removes `en-us` from Knowledge Base links. If the reply does not greet the requester, it adds the greeting of the template. To prevent this, add `--no-greeting`.
3. It writes a draft to `~/zendesk-data/drafts/` and checks it against the process rules. If there is a problem, it stops.
4. After you type `create`, it clones the template in Admin Center, fills in the form, and clicks Create. You can watch it in the Firefox window.
5. It downloads the new macro and compares it with the draft.

To stop before Admin Center, add `--dry-run`. After the script finishes, test the macro on a new ticket, delete the test ticket, and ask for a review. The script never changes or deletes an existing macro.

## Tests

The tests use no browser and no network.

```sh
uv run python -m unittest
```

The tests of `macro_search.html` need a browser. They run in headless Firefox:

```sh
uv run --with playwright python -m unittest test_macro_search_html
```
