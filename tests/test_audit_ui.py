"""Unit tests for the audit UI pages (apps.admin.audit.ui).

The pages are static shells — data loads client-side from the proxy
endpoints — so these tests check that each page renders, the shared
nav reflects the session's capabilities, and the URLs the JavaScript
hardcodes are served.

File: tests/test_audit_ui.py
"""

import unittest

from tests.support import StubJsonClient, make_app


class AuditUiTestBase(unittest.TestCase):
    """Shared harness: one app per test, patches restored after."""

    def setUp(self):
        self.json_client = StubJsonClient()
        self.app, self.client, cleanup = make_app(json_client=self.json_client)
        self.addCleanup(cleanup)


class TestPages(AuditUiTestBase):
    """Every page renders for a superadmin session."""

    def test_landing_page_renders(self):
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Browse traces", response.get_data(as_text=True))

    def test_traces_page_renders(self):
        response = self.client.get("/audit/traces")
        self.assertEqual(response.status_code, 200)
        self.assertIn("trace-table-body", response.get_data(as_text=True))

    def test_trace_detail_renders_with_trace_id(self):
        response = self.client.get(f"/audit/traces/{'a' * 32}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("a" * 32, response.get_data(as_text=True))

    def test_keys_page_renders(self):
        response = self.client.get("/audit/keys")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Mint a key", response.get_data(as_text=True))

    def test_static_assets_served_at_audit_static(self):
        """The JS fetches /audit/api/* and loads from /audit/static/*."""
        for path, marker in [
            ("/audit/static/js/main.js", b"/audit/traces?journey_id="),
            ("/audit/static/js/traces.js", b"/audit/api/traces"),
            ("/audit/static/js/trace.js", b"/audit/api/traces"),
            ("/audit/static/css/main.css", b".trace-table"),
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.data, path)


class TestNavCapabilities(AuditUiTestBase):
    """The header shows the Keys link only to key-managing sessions."""

    def test_superadmin_sees_keys_link(self):
        page = self.client.get("/audit/traces").get_data(as_text=True)
        self.assertIn(">Keys</a>", page)

    def test_non_admin_does_not_see_keys_link(self):
        # The landing page is public: it renders the nav for an
        # anonymous session too.
        _, client, cleanup = make_app(user_id=None, admins=None)
        self.addCleanup(cleanup)
        page = client.get("/audit/").get_data(as_text=True)
        self.assertNotIn(">Keys</a>", page)


class TestLandingAuthLinks(AuditUiTestBase):
    """The landing page links the portal login for anonymous browsers."""

    def test_anonymous_landing_links_login(self):
        _, client, cleanup = make_app(user_id=None, admins=None)
        self.addCleanup(cleanup)
        page = client.get("/audit/").get_data(as_text=True)
        self.assertIn("/login?next=/audit/traces", page)
        self.assertNotIn("/audit/login", page)

    def test_signed_in_landing_hides_login_link(self):
        page = self.client.get("/audit/").get_data(as_text=True)
        self.assertNotIn("/login?next=", page)


if __name__ == "__main__":
    unittest.main()
