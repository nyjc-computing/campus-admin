"""Unit tests for the audit UI pages (apps.admin.audit.ui).

The pages are static shells — data loads client-side from the proxy
endpoints — so these tests check that each page renders, the shared
nav reflects the session's capabilities, and the URLs the JavaScript
hardcodes are served. The section root (/audit/) redirects straight
to the resource view, mirroring the admin section's root.

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


class TestSectionRoot(AuditUiTestBase):
    """/audit/ goes straight to the resource view, like the admin
    section's root opening its dashboard."""

    def test_root_redirects_to_traces_when_signed_in(self):
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], "/audit/traces"
        )

    def test_root_redirect_when_anonymous(self):
        """Anonymous browsers hop through the redirect into the traces
        gate, which sends them to sign in for the resource view."""
        _, client, cleanup = make_app(user_id=None, admins=None)
        self.addCleanup(cleanup)
        response = client.get("/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/audit/traces")


class TestPages(AuditUiTestBase):
    """Every page renders for a superadmin session."""

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

    def test_static_assets_served(self):
        """Audit JS lives in the blueprint's static dir; both sections
        share the single stylesheet at /static/css/style.css."""
        for path, marker in [
            ("/audit/static/js/main.js", b"/audit/traces?journey_id="),
            ("/audit/static/js/traces.js", b"/audit/api/traces"),
            ("/audit/static/js/trace.js", b"/audit/api/traces"),
            ("/static/css/style.css", b".trace-table"),
            ("/static/css/style.css", b".dashboard-sidebar"),
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.data, path)


class TestSidebarLayout(AuditUiTestBase):
    """Working pages use the admin portal's sidebar layout."""

    def test_traces_page_has_sidebar(self):
        page = self.client.get("/audit/traces").get_data(as_text=True)
        self.assertIn('id="auditSidebar"', page)
        self.assertIn('class="sidebar-header">', page)
        # Traces marked as the current view.
        self.assertIn('href="/audit/traces" class="sidebar-link active"', page)
        # Keys link for a key-managing session.
        self.assertIn('href="/audit/keys"', page)
        # Mobile toggle rides in the top bar.
        self.assertIn('id="auditSidebarToggle"', page)

    def test_keys_page_marks_keys_active(self):
        page = self.client.get("/audit/keys").get_data(as_text=True)
        self.assertIn(
            'href="/audit/keys" class="sidebar-link active"', page
        )
        self.assertIn("audit-content--flush", page)

    def test_top_bar_has_no_inline_section_nav(self):
        """Traces/Keys/Metrics live in the sidebar now, not the top bar."""
        page = self.client.get("/audit/traces").get_data(as_text=True)
        self.assertNotIn('class="main-nav"', page)


class TestSectionSwitcher(AuditUiTestBase):
    """Every audit top bar carries the Admin | Audit section switcher.

    The admin navbar carries the same pair (bootstrap pills), so the
    two sections are mutually one click away.
    """

    def test_switcher_on_gated_page(self):
        page = self.client.get("/audit/traces").get_data(as_text=True)
        self.assertIn('class="section-link active" href="/audit/"', page)
        self.assertIn('class="section-link" href="/admin/dashboard"', page)

    def test_body_carries_audit_section_tag(self):
        """The theme keys per-section accents off body[data-section]."""
        page = self.client.get("/audit/traces").get_data(as_text=True)
        self.assertIn('data-section="audit"', page)


if __name__ == "__main__":
    unittest.main()
