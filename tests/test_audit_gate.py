"""Unit tests for the audit web UI's access gate (apps.admin.audit.gate).

campus.audit authorizes by API-key scope and never authenticates a
person; the portal authenticates the session and decides locally who
may drive its shared UI key, through two deployment lists of
comma-separated emails, compared case-insensitively, fail-closed when
unset or empty, re-read per request:

- AUDIT_ADMINS — who may read traces;
- AUDIT_KEY_ADMINS — the audit superadmins, who may additionally manage
  API keys. Unset, it falls back to AUDIT_ADMINS (the 2026-11-17
  ruling: key management never opens to the students a development
  deployment may list as trace viewers).

The section root and static assets stay public; everything else needs a
signed-in trace viewer; the data endpoints answer fetch() callers in
JSON.

File: tests/test_audit_gate.py
"""

import os
import unittest
from unittest import mock

from tests.support import TEST_USER_ID, StubJsonClient, StubResponse, make_app


class GatePageTestBase(unittest.TestCase):
    """Shared harness: one app per test, patches restored after."""

    def setUp(self, **make_app_kwargs):
        self.json_client = StubJsonClient()
        self.app, self.client, cleanup = make_app(
            json_client=self.json_client, **make_app_kwargs
        )
        self.addCleanup(cleanup)


class TestPublicEndpoints(GatePageTestBase):
    """The section root (a redirect to the trace list) and static
    assets need no login."""

    def test_root_redirects_when_logged_out(self):
        _, client, cleanup = make_app(user_id=None)
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/audit/traces")

    def test_root_redirects_when_logged_in(self):
        response = self.client.get("/admin/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/audit/traces")

    def test_static_assets_are_public_when_logged_out(self):
        _, client, cleanup = make_app(user_id=None)
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/static/js/main.js")
        self.assertEqual(response.status_code, 200)

    def test_unknown_static_asset_404s(self):
        response = self.client.get("/admin/audit/static/css/nope.css")
        self.assertEqual(response.status_code, 404)


class TestPageGate(GatePageTestBase):
    """Gated pages: anonymous redirects to sign in, non-superadmin 403s."""

    def test_anonymous_is_redirected_to_sign_in_with_next(self):
        _, client, cleanup = make_app(user_id=None)
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/traces")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].startswith("/login?next="),
            f"unexpected redirect: {response.headers['Location']}",
        )
        self.assertIn("/admin/audit/traces", response.headers["Location"])

    def test_superadmin_reaches_traces(self):
        response = self.client.get("/admin/audit/traces")
        self.assertEqual(response.status_code, 200)

    def test_signed_in_non_admin_gets_403_page(self):
        _, client, cleanup = make_app(admins="other@nyjc.edu.sg")
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/traces")
        self.assertEqual(response.status_code, 403)
        self.assertIn(TEST_USER_ID, response.get_data(as_text=True))
        self.assertIn("AUDIT_ADMINS", response.get_data(as_text=True))

    def test_allowlist_is_case_insensitive(self):
        _, client, cleanup = make_app(admins="STAFF@NYJC.EDU.SG")
        self.addCleanup(cleanup)
        self.assertEqual(client.get("/admin/audit/traces").status_code, 200)

    def test_allowlist_tolerates_spaces_and_empties(self):
        _, client, cleanup = make_app(
            admins=" first@nyjc.edu.sg , , staff@nyjc.edu.sg ,"
        )
        self.addCleanup(cleanup)
        self.assertEqual(client.get("/admin/audit/traces").status_code, 200)

    def test_empty_allowlist_denies_everyone(self):
        _, client, cleanup = make_app(admins="")
        self.addCleanup(cleanup)
        self.assertEqual(client.get("/admin/audit/traces").status_code, 403)

    def test_unset_allowlist_denies_everyone(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUDIT_ADMINS", None)
            _, client, cleanup = make_app(admins=None)
            self.addCleanup(cleanup)
            self.assertEqual(client.get("/admin/audit/traces").status_code, 403)

    def test_allowlist_is_read_per_request(self):
        """Edits to AUDIT_ADMINS take effect without a new session."""
        self.assertEqual(self.client.get("/admin/audit/traces").status_code, 200)
        os.environ["AUDIT_ADMINS"] = "someoneelse@nyjc.edu.sg"
        try:
            self.assertEqual(
                self.client.get("/admin/audit/traces").status_code, 403
            )
        finally:
            os.environ["AUDIT_ADMINS"] = TEST_USER_ID

    def test_journey_url_redirects_to_traces_view(self):
        response = self.client.get("/admin/audit/journeys/j-123")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "/admin/audit/traces?journey_id=j-123",
        )


class TestKeysPageGate(GatePageTestBase):
    """The keys page gates on the key-management capability."""

    def test_superadmin_reaches_keys_page(self):
        response = self.client.get("/admin/audit/keys")
        self.assertEqual(response.status_code, 200)

    def test_signed_in_non_admin_gets_403_page(self):
        _, client, cleanup = make_app(admins="other@nyjc.edu.sg")
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/keys")
        self.assertEqual(response.status_code, 403)
        self.assertIn("cannot manage API keys", response.get_data(as_text=True))

    def test_anonymous_is_redirected_to_sign_in(self):
        _, client, cleanup = make_app(user_id=None)
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/keys")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/login?next="))


class TestDataGate(GatePageTestBase):
    """The data endpoints answer fetch() callers in JSON."""

    def test_anonymous_gets_401_json(self):
        _, client, cleanup = make_app(user_id=None)
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/api/traces")
        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.get_json())

    def test_non_admin_gets_403_json(self):
        _, client, cleanup = make_app(admins="other@nyjc.edu.sg")
        self.addCleanup(cleanup)
        response = client.get("/admin/audit/api/traces")
        self.assertEqual(response.status_code, 403)
        self.assertIn("AUDIT_ADMINS", response.get_json()["error"])

    def test_superadmin_passes_gate(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse({"traces": [], "cursor": {"next": None, "has_more": False}})
        response = self.client.get("/admin/audit/api/traces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["traces"], [])


class TestKeyAdminsList(GatePageTestBase):
    """AUDIT_KEY_ADMINS keeps key management superadmin-only wherever
    the trace-viewer list is wider (the 2026-11-17 ruling). This
    harness signs in TEST_USER_ID as an AUDIT_ADMINS trace viewer."""

    def test_trace_viewer_without_key_rights_reads_traces_not_keys(self):
        superadmins = mock.patch.dict(
            os.environ, {"AUDIT_KEY_ADMINS": "super@nyjc.edu.sg"},
        )
        superadmins.start()
        self.addCleanup(superadmins.stop)

        self.assertEqual(self.client.get("/admin/audit/traces").status_code, 200)
        response = self.client.get("/admin/audit/keys")
        self.assertEqual(response.status_code, 403)
        body = response.get_data(as_text=True)
        self.assertIn("cannot manage API keys", body)
        self.assertIn("AUDIT_KEY_ADMINS", body)

    def test_keys_link_is_withheld_from_trace_viewers(self):
        superadmins = mock.patch.dict(
            os.environ, {"AUDIT_KEY_ADMINS": "super@nyjc.edu.sg"},
        )
        superadmins.start()
        self.addCleanup(superadmins.stop)

        page = self.client.get("/admin/audit/traces").get_data(as_text=True)
        self.assertIn('href="#keys" class="sidebar-link disabled"', page)
        self.assertNotIn('href="/admin/audit/keys"', page)

    def test_key_admin_on_the_viewer_list_reaches_both(self):
        superadmins = mock.patch.dict(
            os.environ, {"AUDIT_KEY_ADMINS": TEST_USER_ID},
        )
        superadmins.start()
        self.addCleanup(superadmins.stop)

        self.assertEqual(self.client.get("/admin/audit/traces").status_code, 200)
        self.assertEqual(self.client.get("/admin/audit/keys").status_code, 200)

    def test_key_admin_off_the_viewer_list_manages_keys_but_not_traces(self):
        _, client, cleanup = make_app(admins="other@nyjc.edu.sg")
        self.addCleanup(cleanup)
        superadmins = mock.patch.dict(
            os.environ, {"AUDIT_KEY_ADMINS": TEST_USER_ID},
        )
        superadmins.start()
        self.addCleanup(superadmins.stop)

        self.assertEqual(client.get("/admin/audit/keys").status_code, 200)
        self.assertEqual(client.get("/admin/audit/traces").status_code, 403)

    def test_unset_key_admins_list_falls_back_to_the_viewer_list(self):
        # Railway stores a cleared variable as the empty string.
        with mock.patch.dict(os.environ, {"AUDIT_KEY_ADMINS": ""}):
            self.assertEqual(self.client.get("/admin/audit/keys").status_code, 200)

    def test_emptied_key_admins_list_allows_no_one(self):
        # A list that is set but names no one is a decision, not an
        # omission: fail closed rather than widening to the viewers.
        for value in (" , ", "   "):
            with mock.patch.dict(os.environ, {"AUDIT_KEY_ADMINS": value}):
                self.assertEqual(
                    self.client.get("/admin/audit/keys").status_code, 403, repr(value),
                )

    def test_key_admin_list_is_case_insensitive(self):
        superadmins = mock.patch.dict(
            os.environ, {"AUDIT_KEY_ADMINS": "STAFF@NYJC.EDU.SG"},
        )
        superadmins.start()
        self.addCleanup(superadmins.stop)

        self.assertEqual(self.client.get("/admin/audit/keys").status_code, 200)


if __name__ == "__main__":
    unittest.main()
