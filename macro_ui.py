# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Create a macro through the Zendesk Admin Center pages (issue #6).

All page automation is in this module. When we have API write access, a
`POST /api/v2/macros` with the draft replaces `create_macro()`, and nothing
else changes.

Measured on 2026-11-27 with the installed Firefox release (moz-firefox):

- The macro form is inside `iframe[data-testid=mountpoint-iframe]`.
- Actions > Clone opens an unsaved form at /macros/new?macro_id=<template>.
  Nothing is created until the Create button is clicked, so the script
  opens that URL directly.
- Locator.fill() fails in this iframe over WebDriver BiDi
  ("__pw_bidiInsertText is not a function"). Typing key by key works.
- The reply editor is CKEditor 5. Its element has a `ckeditorInstance`
  with setData() and getData().
"""

from __future__ import annotations

import re
import time

IFRAME = "iframe[data-testid=mountpoint-iframe]"
NAME = "main-section-input-name"
DESCRIPTION = "main-section-textarea-description"
EDITOR = ".ck-editor__editable"
TAG_INPUT = "tags-input"
TAG_REMOVE = "tags-input-remove-tag"
SAVE = "macro-form-save-button"
NEW_ID_RE = re.compile(r"/admin/workspaces/agent-workspace/macros/(\d+)(?:[/?#]|$)")


class UIError(Exception):
    """The Admin Center page did not behave as expected."""


def clone_url(base: str, template_id: int) -> str:
    return f"{base}/admin/workspaces/agent-workspace/macros/new?macro_id={template_id}"


def macro_url(base: str, macro_id: int) -> str:
    return f"{base}/admin/workspaces/agent-workspace/macros/{macro_id}"


def _replace_text(locator, text: str) -> None:
    locator.click()
    locator.press("ControlOrMeta+a")
    locator.press("Backspace")
    # press_sequentially sends "\n" as Enter. The description textarea keeps
    # it as a line break. new_macro.py removes line breaks from the name, so
    # Enter never reaches the name input, where it can submit the form.
    locator.press_sequentially(text)


def fill_form(page, base: str, template_id: int, title: str, description: str,
              reply_html: str, tag: str) -> dict:
    """Open an unsaved clone of the template and fill it in. Never saves.

    Returns what the form contains afterwards, read back from the page.
    """
    page.goto(clone_url(base, template_id), wait_until="networkidle", timeout=60_000)
    form = page.frame_locator(IFRAME)
    name = form.get_by_test_id(NAME)
    name.wait_for(timeout=30_000)

    _replace_text(name, title)
    _replace_text(form.get_by_test_id(DESCRIPTION), description)

    editors = form.locator(EDITOR)
    if editors.count() != 1:
        raise UIError(f"expected one reply editor in the template, found {editors.count()}")
    result = editors.first.evaluate(
        "(el, html) => { const ed = el.ckeditorInstance; if (!ed) return null;"
        " ed.setData(html); return ed.getData(); }", reply_html)
    if result is None:
        raise UIError("the reply editor has no CKEditor instance")

    removes = form.get_by_test_id(TAG_REMOVE)
    while removes.count():
        removes.first.click()
    tags_input = form.get_by_test_id(TAG_INPUT)
    tags_input.click()
    tags_input.press_sequentially(tag)
    tags_input.press("Enter")
    page.wait_for_timeout(500)

    state = {
        "title": name.input_value(),
        "description": form.get_by_test_id(DESCRIPTION).input_value(),
        "reply_html": result,
        "tags": form.get_by_test_id(TAG_REMOVE).count(),
    }
    if state["title"] != title:
        raise UIError(f"the name field contains {state['title']!r}, not {title!r}")
    if state["description"] != description:
        raise UIError("the description field does not contain the draft description")
    if state["tags"] != 1:
        raise UIError(f"expected one tag in the form, found {state['tags']}")
    return state


def submit(page, timeout_s: float = 30) -> int:
    """Click Create and return the ID of the new macro."""
    page.frame_locator(IFRAME).get_by_test_id(SAVE).click()
    return wait_for_new_id(page, timeout_s)


def wait_for_new_id(page, timeout_s: float) -> int:
    """After Create, Admin Center moves from /macros/new to /macros/<id>."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        match = NEW_ID_RE.search(page.url)
        if match:
            return int(match.group(1))
        page.wait_for_timeout(500)
    raise UIError(f"no new macro ID after {timeout_s:.0f}s; the page is at {page.url}")
