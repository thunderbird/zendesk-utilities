# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The macro creation rules that a program can check (issue #6).

The rules come from the team's private macro creation playbook, restated
here in our own words. Every function works on a macro in the official
Zendesk API structure, so the same checks apply to downloaded macros and to
new drafts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    brand_id: int
    service: str          # the <service> part of macro_<service>_<name> tags
    template_id: int | None  # the macro to clone; None = no team template yet


# IDs from the tbpro Zendesk instance. The services are the tag prefixes that
# most existing macros of each brand use.
BRANDS = {
    "thundermail": Brand("thundermail", "Thundermail", 38173138875795, "thundermail", 42980135784467),
    "donors": Brand("donors", "Donor Support", 43075490201747, "donors", None),
    "tfa": Brand("tfa", "Thunderbird App Store Reviews", 48140619564819, "tfa", 50467223418771),
}

# Category::Subcategory - Macro name. More :: levels are allowed, because
# the team templates use them.
NAME_RE = re.compile(r"^[^:]+(?:::[^:]+)+ - [^:]+$")
TAG_FIELDS = ("current_tags", "set_tags")
# The actions that every macro must set (rule 7). The ticket status is
# `custom_status_id` in this instance, or `status` without custom statuses.
REQUIRED = {
    "brand_id": "the brand",
    ("custom_status_id", "status"): "the ticket status",
    "comment_mode_is_public": "the comment mode",
    "group_id": "the group",
}
LONG_REPLY_WORDS = 250

# An en-us locale segment in a Knowledge Base link. Both sites send a link
# without a locale to the reader's own language:
#   https://support.tb.pro/hc/en-us/articles/123 -> /hc/articles/123
#   https://support.mozilla.org/en-US/kb/x       -> support.mozilla.org/kb/x
EN_US_RE = re.compile(r"(/hc|support\.mozilla\.org)/en-us(?=/)", re.IGNORECASE)


def action_values(macro: dict, *fields: str) -> list[str]:
    return [str(a.get("value")) for a in macro.get("actions") or []
            if isinstance(a, dict) and a.get("field") in fields]


def tags_of(macro: dict) -> list[str]:
    return [t for v in action_values(macro, *TAG_FIELDS) for t in v.split()]


def brand_of(macro: dict) -> Brand | None:
    ids = action_values(macro, "brand_id")
    return next((b for b in BRANDS.values() if ids and ids[0] == str(b.brand_id)), None)


def strip_en_us(html: str) -> str:
    return EN_US_RE.sub(r"\1", html)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def make_tag(brand: Brand, name: str) -> str:
    """macro_<service>_<name without its first category>.

    "Thundermail::Appointment - Adding calendars" becomes
    macro_thundermail_appointment_adding_calendars, as in the playbook.
    """
    rest = name.split("::", 1)[1] if "::" in name else name
    return f"macro_{brand.service}_{slug(rest)}"


def word_count(html_text: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html_text).split())


@dataclass
class Problem:
    rule: int        # playbook rule number; 0 = a note that is not a playbook rule
    message: str
    blocking: bool = True


# The team's fill-in markers, for example [[COUNTRY]].
FILL_IN_RE = re.compile(r"\[\[[^\[\]]*\]\]")


def check_macro(macro: dict, existing_titles: set[str] = frozenset()) -> list[Problem]:
    """Problems with a macro, by playbook rule number (see issue #6)."""
    problems: list[Problem] = []
    title = (macro.get("title") or "").strip()

    if title.casefold() in existing_titles:
        problems.append(Problem(1, f"a macro named {title!r} already exists"))
    if not NAME_RE.match(title):
        problems.append(Problem(2, f"name {title!r} does not match 'Category::Subcategory - Macro name'"))
    if not (macro.get("description") or "").strip():
        problems.append(Problem(3, "the description is empty"))

    replies = action_values(macro, "comment_value_html", "comment_value")
    if not any(r.strip() for r in replies):
        problems.append(Problem(4, "the macro adds no reply"))
    else:
        words = sum(word_count(r) for r in replies)
        if words > LONG_REPLY_WORDS:
            problems.append(Problem(4, f"the reply has {words} words; link to a Knowledge Base "
                                       "article for long instructions", blocking=False))
    markers = sorted({m for r in replies for m in FILL_IN_RE.findall(r)})
    if markers:
        problems.append(Problem(0, "the reply has fill-in markers that agents must replace "
                                   "before sending: " + ", ".join(markers), blocking=False))
    if any(EN_US_RE.search(r) for r in replies):
        problems.append(Problem(5, "a Knowledge Base link contains an en-us segment"))

    brand = brand_of(macro)
    pattern = rf"macro_{re.escape(brand.service)}_[a-z0-9_]+" if brand else r"macro_[a-z0-9]+_[a-z0-9_]+"
    if not any(re.fullmatch(pattern, t) for t in tags_of(macro)):
        want = f"macro_{brand.service}_<macro_name>" if brand else "macro_<service>_<macro_name>"
        problems.append(Problem(6, f"no tag in the format {want}"))

    fields = {a.get("field") for a in macro.get("actions") or [] if isinstance(a, dict)}
    for field, label in REQUIRED.items():
        names = field if isinstance(field, tuple) else (field,)
        if not fields.intersection(names):
            problems.append(Problem(7, f"the macro does not set {label}"))
    return problems
