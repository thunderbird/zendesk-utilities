# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline tests for scrape_macros.py. No browser and no network."""

import json
import tempfile
import unittest
from pathlib import Path

import scrape_macros as sm


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self._body = body if body is not None else {}
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.headers.update(headers or {})

    def json(self):
        return self._body

    def text(self):
        return json.dumps(self._body)


class FakeRequest:
    """Stands in for BrowserContext.request: returns queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **_):
        self.urls.append(url)
        return self.responses.pop(0)


def no_sleep(_):
    pass


def macro(i, title="m"):
    return {"id": i, "title": title, "actions": [{"field": "status", "value": "solved"}],
            "updated_at": "2026-01-01T00:00:00Z"}


class PathSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.repo = self.home / "Documents" / "GIT" / "zendesk-utilities"
        self.repo.mkdir(parents=True)
        self.locs = sm.synced_locations(home=self.home, repo=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_subdirectory_is_safe(self):
        self.assertIsNone(sm.unsafe_location(self.home / "zendesk-data" / "macros", self.locs))

    def test_synced_folders_are_refused(self):
        for p in (self.repo / "out", self.home / "Documents" / "x", self.home / "Desktop",
                  self.home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "m"):
            self.assertIsNotNone(sm.unsafe_location(p, self.locs), p)

    def test_symlink_into_synced_folder_is_refused(self):
        link = self.home / "innocent"
        link.symlink_to(self.home / "Documents")
        self.assertIsNotNone(sm.unsafe_location(link / "macros", self.locs))

    def test_similar_prefix_is_not_confused(self):
        self.assertIsNone(sm.unsafe_location(self.home / "Documents-local", self.locs))


class Subdomain(unittest.TestCase):
    def test_forms(self):
        for v in ("tbpro", "tbpro.zendesk.com", "https://tbpro.zendesk.com/", " TBPRO "):
            self.assertEqual(sm.normalize_subdomain(v), "tbpro")

    def test_rejects_junk(self):
        with self.assertRaises(sm.ScrapeError):
            sm.normalize_subdomain("evil.com/x")


class Http(unittest.TestCase):
    def test_retry_wait_grows_and_caps(self):
        self.assertEqual(sm.retry_wait("60", 1), 65)
        self.assertEqual(sm.retry_wait("60", 2), 130)
        self.assertEqual(sm.retry_wait("Wed, 21 Oct 2026 07:28:00 GMT", 1), 65)
        self.assertEqual(sm.retry_wait("600", 9), sm.MAX_WAIT_S)

    def test_429_then_success(self):
        req = FakeRequest([FakeResponse(429, headers={"retry-after": "1"}),
                           FakeResponse(200, {"ok": True})])
        waits = []
        self.assertEqual(sm.get_json(req, "u", sleep=waits.append), {"ok": True})
        self.assertEqual(waits, [6])

    def test_html_login_page_is_session_error(self):
        req = FakeRequest([FakeResponse(200, headers={"content-type": "text/html"})])
        with self.assertRaises(sm.SessionError):
            sm.get_json(req, "u", sleep=no_sleep)

    def test_401_is_session_error(self):
        with self.assertRaises(sm.SessionError):
            sm.get_json(FakeRequest([FakeResponse(401)]), "u", sleep=no_sleep)

    def test_anonymous_user_is_not_signed_in(self):
        anon = {"user": {"id": None, "role": "end-user"}}
        agent = {"user": {"id": 7, "role": "agent", "name": "A"}}
        self.assertIsNone(sm.signed_in_user(FakeRequest([FakeResponse(200, anon)]), "b"))
        self.assertEqual(sm.signed_in_user(FakeRequest([FakeResponse(200, agent)]), "b")["id"], 7)


class Listing(unittest.TestCase):
    def test_first_page_asks_for_shared_macros(self):
        url = sm.first_page_url("https://tbpro.zendesk.com")
        self.assertIn("/api/v2/macros.json?", url)
        self.assertIn("access=shared", url)

    def test_cursor_pagination(self):
        req = FakeRequest([
            FakeResponse(200, {"macros": [macro(1), macro(2)],
                               "meta": {"has_more": True}, "links": {"next": "p2"}}),
            FakeResponse(200, {"macros": [macro(3)],
                               "meta": {"has_more": False}, "links": {"next": None}}),
        ])
        macros, complete = sm.list_macros(req, "b", 0, None, sleep=no_sleep)
        self.assertEqual([m["id"] for m in macros], [1, 2, 3])
        self.assertTrue(complete)
        self.assertEqual(req.urls[1], "p2")

    def test_offset_pagination_fallback(self):
        req = FakeRequest([
            FakeResponse(200, {"macros": [macro(1)], "next_page": "p2", "count": 2}),
            FakeResponse(200, {"macros": [macro(2)], "next_page": None, "count": 2}),
        ])
        macros, complete = sm.list_macros(req, "b", 0, None, sleep=no_sleep)
        self.assertEqual(len(macros), 2)
        self.assertTrue(complete)

    def test_limit_marks_listing_incomplete(self):
        req = FakeRequest([FakeResponse(200, {"macros": [macro(1), macro(2)],
                                              "meta": {"has_more": True},
                                              "links": {"next": "p2"}})])
        macros, complete = sm.list_macros(req, "b", 0, 1, sleep=no_sleep)
        self.assertEqual(len(macros), 1)
        self.assertFalse(complete)

    def test_unexpected_page_shape(self):
        with self.assertRaises(sm.ScrapeError):
            sm.list_macros(FakeRequest([FakeResponse(200, {"error": "x"})]), "b", 0, None,
                           sleep=no_sleep)


class Saving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "macros"

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_run_writes_files_in_api_shape(self):
        r = sm.save_macros(self.out, [macro(1), macro(2)], complete=True, force=False)
        self.assertEqual(r["written"], 2)
        self.assertEqual(json.loads((self.out / "1.json").read_text()), macro(1))
        listing = json.loads((self.out / "macros.json").read_text())
        self.assertEqual(listing, {"macros": [macro(1), macro(2)], "count": 2})
        self.assertEqual((self.out / "1.json").stat().st_mode & 0o777, 0o600)

    def test_unchanged_is_skipped_changed_is_rewritten(self):
        sm.save_macros(self.out, [macro(1), macro(2)], complete=True, force=False)
        r = sm.save_macros(self.out, [macro(1), macro(2, "new")], complete=True, force=False)
        self.assertEqual((r["written"], r["skipped"]), (1, 1))
        r = sm.save_macros(self.out, [macro(1), macro(2, "new")], complete=True, force=True)
        self.assertEqual(r["written"], 2)

    def test_partial_run_skips_listing_and_stale_report(self):
        sm.save_macros(self.out, [macro(1), macro(2)], complete=True, force=False)
        r = sm.save_macros(self.out, [macro(1)], complete=False, force=False)
        self.assertEqual(r["stale"], [])
        self.assertEqual(json.loads((self.out / "macros.json").read_text())["count"], 2)

    def test_stale_files_are_reported_not_deleted(self):
        sm.save_macros(self.out, [macro(1), macro(2)], complete=True, force=False)
        r = sm.save_macros(self.out, [macro(1)], complete=True, force=False)
        self.assertEqual(r["stale"], ["2.json"])
        self.assertTrue((self.out / "2.json").exists())

    def test_macro_without_id_is_an_error(self):
        with self.assertRaises(sm.ScrapeError):
            sm.save_macros(self.out, [{"title": "x"}], complete=True, force=False)


class Browser(unittest.TestCase):
    def test_uses_installed_firefox_release(self):
        calls = []

        class FakeFirefox:
            def launch_persistent_context(self, profile, **kw):
                calls.append(kw)
                return "ctx"

        class FakePlaywright:
            firefox = FakeFirefox()

        with tempfile.TemporaryDirectory() as tmp:
            sm.open_context(FakePlaywright(), Path(tmp) / "profile", headless=True)
        self.assertEqual(calls, [{"channel": "moz-firefox", "headless": True}])


class Main(unittest.TestCase):
    def test_refuses_output_in_repository(self):
        rc = sm.main(["--out", str(sm.REPO / "macros-out")])
        self.assertEqual(rc, 1)
        self.assertFalse((sm.REPO / "macros-out").exists())


if __name__ == "__main__":
    unittest.main()
