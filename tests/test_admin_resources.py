"""Tests for the /admin resource views (sessions, vaults, credentials).

Builds the real create_app() with the Campus SDK client swapped for a
recording MagicMock via mock.patch("campus_python.Campus"), so the
route closures capture the double. The flask_campus login manager is
installed for real but its push_context no-ops (both has_session
checks stubbed False); a test before_request hook registered after
create_app injects the signed-in admin that the admin_required gate
requires.

File: tests/test_admin_resources.py
"""

import os
import typing
import unittest
from types import SimpleNamespace
from unittest import mock

import flask

TEST_USER_ID = "staff@nyjc.edu.sg"


def make_app(
        *,
        admins_key: str = "AUTH_ADMINS",
        admins: str = TEST_USER_ID,
        legacy_admins: str | None = None,
) -> tuple[
    flask.Flask, typing.Any, mock.MagicMock, typing.Callable[[], None],
]:
    """Build create_app() with a mocked Campus client and a signed-in admin.

    admins_key/admins install the admin-section allowlist under the
    given env name (AUTH_ADMINS by default, ADMINS for the legacy-name
    tests); legacy_admins additionally sets the legacy ADMINS name for
    precedence coverage.

    Returns (app, test client, campus mock, cleanup).
    """
    campus_mock = mock.MagicMock()
    campus_mock.auth.sessions.has_session.return_value = False
    campus_mock.auth.logins.has_session.return_value = False

    env = {
        "SECRET_KEY": "test-secret",
        admins_key: admins,
    }
    if legacy_admins is not None:
        env["ADMINS"] = legacy_admins
    patchers = [
        mock.patch.dict(os.environ, env, clear=False),
        mock.patch("campus_python.Campus", return_value=campus_mock),
    ]
    for patcher in patchers:
        patcher.start()

    from apps.admin import create_app

    app = create_app()

    # Runs after the login manager's push_context hook (registration
    # order), restoring the signed-in admin it cleared.
    @app.before_request
    def _inject_user():
        flask.g.user = SimpleNamespace(id=TEST_USER_ID, email=TEST_USER_ID)

    cleanup = lambda: [patcher.stop() for patcher in patchers]  # noqa: E731
    return app, app.test_client(), campus_mock, cleanup


class AdminResourcesTestCase(unittest.TestCase):
    def setUp(self):
        self.app, self.client, self.campus, cleanup = make_app()
        self.addCleanup(cleanup)

    def test_sessions_list_renders_metadata_rows(self):
        record = SimpleNamespace(
            id="uid-campus_session-1",
            provider="campus",
            client_id="uid-client-a",
            user_id="staff@nyjc.edu.sg",
            created_at="2026-11-04T01:00:00",
            expires_at="2026-11-05T01:00:00",
            scopes=["users:read"],
        )
        self.campus.auth.sessions.list.return_value = [record]

        resp = self.client.get("/admin/sessions")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("uid-campus_session-1", body)
        self.assertIn("staff@nyjc.edu.sg", body)
        self.campus.auth.sessions.list.assert_called_once_with(
            provider=None, client_id=None, user_id=None, limit=500
        )

    def test_sessions_list_passes_filters_and_none_provider(self):
        self.campus.auth.sessions.list.return_value = []

        self.client.get(
            "/admin/sessions?provider=google&client_id=c1&user_id=u@x"
        )

        self.campus.auth.sessions.list.assert_called_once_with(
            provider="google", client_id="c1", user_id="u@x", limit=500
        )

    def test_sessions_revoke_finalizes_via_sdk(self):
        resp = self.client.post(
            "/admin/sessions",
            data={
                "action": "revoke",
                "provider": "google",
                "session_id": "uid-google_session-9",
            },
        )

        self.assertEqual(resp.status_code, 302)
        self.campus.auth.sessions.__getitem__.assert_called_with("google")
        self.campus.auth.sessions["google"][
            "uid-google_session-9"
        ].finalize.assert_called_once()

    def test_sessions_sweep_reports_count(self):
        self.campus.auth.sessions.sweep.return_value = 7

        resp = self.client.post(
            "/admin/sessions", data={"action": "sweep"}, follow_redirects=True
        )

        self.assertEqual(resp.status_code, 200)
        self.campus.auth.sessions.sweep.assert_called_once()
        self.assertIn(
            "Successfully swept 7 expired session(s)",
            resp.get_data(as_text=True),
        )

    def test_vaults_lists_labels(self):
        self.campus.auth.vaults.labels.return_value = ["deployment", "dev"]

        resp = self.client.get("/admin/vaults")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("deployment", body)
        self.assertIn("dev", body)

    def test_vaults_label_view_lists_key_values(self):
        self.campus.auth.vaults.labels.return_value = ["deployment"]
        vault = mock.MagicMock()
        vault.keys.return_value = ["google.client_id"]
        vault.__getitem__.side_effect = lambda key: {"google.client_id": "abc"}[key]
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.get("/admin/vaults?label=deployment")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("google.client_id", body)
        self.assertIn("abc", body)

    def test_vaults_unknown_label_falls_back_to_listing(self):
        self.campus.auth.vaults.labels.return_value = ["deployment"]

        resp = self.client.get("/admin/vaults?label=nope", follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("No vault labeled nope", resp.get_data(as_text=True))

    def test_vaults_set_and_delete_key(self):
        vault = mock.MagicMock()
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={"action": "set", "label": "deployment", "key": "k", "value": "v"},
        )
        self.assertEqual(resp.status_code, 302)
        vault.__setitem__.assert_called_once_with("k", "v")

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "delete_key",
                "label": "deployment",
                "key": "k",
            },
        )
        self.assertEqual(resp.status_code, 302)
        vault.__delitem__.assert_called_once_with("k")

    def test_vaults_scopes_editor_renders_per_scope_rows(self):
        self.campus.auth.vaults.labels.return_value = ["deployment"]
        vault = mock.MagicMock()
        vault.keys.return_value = ["SCOPES", "CLIENT_ID"]
        vault.__getitem__.side_effect = lambda key: {
            "SCOPES": "https://a.example/s1 https://a.example/s2",
            "CLIENT_ID": "abc",
        }[key]
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.get("/admin/vaults?label=deployment")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # SCOPES gets the add/remove editor; other keys stay plain.
        # The table and mobile-card views both render, so each form
        # appears twice and each scope's remove button twice more.
        self.assertEqual(body.count('value="add_scope"'), 2)
        self.assertEqual(body.count('value="remove_scope"'), 4)
        self.assertIn("https://a.example/s1", body)
        # The editor's hidden label field must carry the vault label,
        # not render empty — if the macro ever loses context-variable
        # access this goes absent.
        self.assertIn(
            '<input type="hidden" name="label" value="deployment">', body
        )

    def test_vaults_scopes_add_appends_and_writes_back(self):
        vault = mock.MagicMock()
        vault.__getitem__.return_value = "https://a.example/s1"
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "add_scope",
                "label": "deployment",
                "key": "SCOPES",
                "scope": "https://a.example/s2",
            },
        )

        self.assertEqual(resp.status_code, 302)
        vault.__setitem__.assert_called_once_with(
            "SCOPES", "https://a.example/s1 https://a.example/s2"
        )

    def test_vaults_scopes_add_duplicate_writes_nothing(self):
        vault = mock.MagicMock()
        vault.__getitem__.return_value = "https://a.example/s1"
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "add_scope",
                "label": "deployment",
                "key": "SCOPES",
                "scope": "https://a.example/s1",
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn("already in SCOPES", resp.get_data(as_text=True))
        vault.__setitem__.assert_not_called()

    def test_vaults_scopes_remove_rewrites_remaining(self):
        vault = mock.MagicMock()
        vault.__getitem__.return_value = "https://a.example/s1 https://a.example/s2"
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "remove_scope",
                "label": "deployment",
                "key": "SCOPES",
                "scope": "https://a.example/s1",
            },
        )

        self.assertEqual(resp.status_code, 302)
        vault.__setitem__.assert_called_once_with(
            "SCOPES", "https://a.example/s2"
        )

    def _client_row(self, upstream_scopes=None):
        """A client record shaped like campus.model.Client for the table."""
        return SimpleNamespace(
            id="uid-client-ef56f01c",
            name="campus-classroom-dev",
            description="campus-classroom Flask app (dev/testing)",
            is_public=False,
            redirect_uris=["http://localhost:5000/finalize_login"],
            upstream_scopes=upstream_scopes or {},
            permissions={},
            created_at="2026-09-29T00:00:00",
        )

    def test_clients_table_renders_upstream_scopes_editor(self):
        self.campus.auth.clients.list.return_value = [self._client_row({
            "google": [
                "https://www.googleapis.com/auth/classroom.rosters",
                "https://www.googleapis.com/auth/classroom.coursework.me",
            ],
            "microsoft": ["https://graph.microsoft.com/Mail.Read"],
        })]

        resp = self.client.get("/admin/clients")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # The scopes modal renders once per client: one remove form per
        # scope plus one add form, each posting to the per-client
        # action route. The desktop and mobile summary cells carry no
        # forms.
        self.assertEqual(
            body.count(
                'action="/admin/clients/uid-client-ef56f01c'
                '/remove_upstream_scope"'
            ),
            3,
        )
        self.assertEqual(
            body.count(
                'action="/admin/clients/uid-client-ef56f01c'
                '/add_upstream_scope"'
            ),
            1,
        )
        self.assertIn(
            "https://www.googleapis.com/auth/classroom.coursework.me", body
        )
        self.assertIn("microsoft", body)

    def test_clients_upstream_scope_add_appends_full_map(self):
        resource = mock.MagicMock()
        resource.get.return_value = SimpleNamespace(
            upstream_scopes={"google": ["https://a.example/s1"]}
        )
        self.campus.auth.clients.__getitem__.side_effect = (
            lambda client_id: {"uid-client-x": resource}[client_id]
        )

        resp = self.client.post(
            "/admin/clients/uid-client-x/add_upstream_scope",
            data={"provider": "google", "scope": "https://a.example/s2"},
        )

        self.assertEqual(resp.status_code, 302)
        # The PATCH replaces the whole map, so the write carries the
        # existing scopes plus the new one.
        resource.update.assert_called_once_with(
            upstream_scopes={"google": [
                "https://a.example/s1", "https://a.example/s2",
            ]}
        )

    def test_clients_upstream_scope_add_duplicate_writes_nothing(self):
        resource = mock.MagicMock()
        resource.get.return_value = SimpleNamespace(
            upstream_scopes={"google": ["https://a.example/s1"]}
        )
        self.campus.auth.clients.__getitem__.side_effect = (
            lambda client_id: {"uid-client-x": resource}[client_id]
        )

        resp = self.client.post(
            "/admin/clients/uid-client-x/add_upstream_scope",
            data={"provider": "google", "scope": "https://a.example/s1"},
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "already allowed for google", resp.get_data(as_text=True)
        )
        resource.update.assert_not_called()

    def test_clients_upstream_scope_remove_keeps_other_providers(self):
        resource = mock.MagicMock()
        resource.get.return_value = SimpleNamespace(
            upstream_scopes={
                "google": ["https://a.example/s1"],
                "microsoft": ["https://b.example/m1"],
            }
        )
        self.campus.auth.clients.__getitem__.side_effect = (
            lambda client_id: {"uid-client-x": resource}[client_id]
        )

        resp = self.client.post(
            "/admin/clients/uid-client-x/remove_upstream_scope",
            data={"provider": "google", "scope": "https://a.example/s1"},
        )

        self.assertEqual(resp.status_code, 302)
        # The emptied provider's key is dropped (absent ≡ empty list
        # fail-closed upstream); other providers pass through.
        resource.update.assert_called_once_with(
            upstream_scopes={"microsoft": ["https://b.example/m1"]}
        )

    def test_clients_upstream_scope_remove_missing_writes_nothing(self):
        resource = mock.MagicMock()
        resource.get.return_value = SimpleNamespace(
            upstream_scopes={"google": ["https://a.example/s1"]}
        )
        self.campus.auth.clients.__getitem__.side_effect = (
            lambda client_id: {"uid-client-x": resource}[client_id]
        )

        resp = self.client.post(
            "/admin/clients/uid-client-x/remove_upstream_scope",
            data={"provider": "google", "scope": "https://a.example/s2"},
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "not allowed for google", resp.get_data(as_text=True)
        )
        resource.update.assert_not_called()

    def test_credentials_list_renders_redacted_metadata(self):
        record = SimpleNamespace(
            id="uid-credential-1",
            client_id="uid-client-a",
            user_id="staff@nyjc.edu.sg",
            token=SimpleNamespace(
                token_type="Bearer",
                expires_at="2026-11-05T00:00:00",
                scope="openid email",
            ),
            created_at="2026-11-04T00:00:00",
        )
        self.campus.auth.credentials.__getitem__.side_effect = (
            lambda provider: {"campus": mock.MagicMock(list=lambda: [record])}[provider]
        )

        resp = self.client.get("/admin/credentials")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("uid-credential-1", body)
        self.assertIn("Bearer", body)
        # The raw token (record.token.id) must never reach the page.
        self.assertNotIn("token.id", body)

    def test_credentials_disconnect_targets_record_client(self):
        provider = mock.MagicMock()
        self.campus.auth.credentials.__getitem__.side_effect = (
            lambda p: {"campus": provider}[p]
        )

        resp = self.client.post(
            "/admin/credentials",
            data={
                "action": "disconnect",
                "user_id": "staff@nyjc.edu.sg",
                "client_id": "uid-client-a",
            },
        )

        self.assertEqual(resp.status_code, 302)
        provider.__getitem__.assert_called_with("staff@nyjc.edu.sg")
        provider["staff@nyjc.edu.sg"].delete.assert_called_once_with(
            client_id="uid-client-a"
        )

    def test_legacy_resource_urls_redirect_with_method_preserved(self):
        for path, endpoint in [
            ("/sessions", "/admin/sessions"),
            ("/vaults", "/admin/vaults"),
            ("/credentials", "/admin/credentials"),
        ]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 308)
            location = resp.headers.get("Location", "")
            self.assertTrue(
                location.endswith(endpoint),
                f"{path} should redirect to {endpoint}, got {location}",
            )

    def test_shared_sidebar_renders_across_resource_views(self):
        # The resource views extend base_resource.html, which owns the
        # layout wrapper + sidebar structurally; each view's `active`
        # set marks its sidebar entry.
        self.campus.auth.clients.list.return_value = []
        self.campus.auth.users.list.return_value = []

        active_by_path = {
            "/admin/dashboard": None,
            "/admin/clients": "Clients",
            "/admin/users": "Users",
            "/admin/vaults": "Vaults",
            "/admin/sessions": "Sessions",
            "/admin/credentials": "Credentials",
        }
        for path, active_label in active_by_path.items():
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            body = resp.get_data(as_text=True)
            # Sidebar and content sit side by side only inside the
            # flex wrapper; a missing wrapper stacks the content below
            # a full-width sidebar. Resource views use the app-shell
            # modifier (fixed viewport, table is the only scroller).
            expected_layout = (
                '<div class="dashboard-layout dashboard-layout--app">'
                if path != "/admin/dashboard"
                else '<div class="dashboard-layout">'
            )
            self.assertIn(expected_layout, body, path)
            self.assertIn('class="dashboard-content"', body, path)
            for link in (
                "/admin/clients",
                "/admin/users",
                "/admin/vaults",
                "/admin/sessions",
                "/admin/credentials",
            ):
                self.assertIn(link, body, f"{link} missing on {path}")
            self.assertNotIn('class="sidebar-link disabled"', body, path)
            if active_label is None:
                self.assertNotIn('class="sidebar-link active"', body, path)
            else:
                self.assertIn('class="sidebar-link active"', body, path)

    def test_top_nav_shared_with_audit_section(self):
        # Both sections include the same _top_nav.html partial:
        # identical markup and classes, themed per section via
        # body[data-section]. The admin bar is the audit bar's markup
        # with the brand, active pill, and accent swapped.
        self.campus.auth.clients.list.return_value = []

        page = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn('data-section="admin"', page)
        self.assertIn('class="top-header"', page)
        self.assertIn(">Campus Admin</a>", page)
        self.assertIn('class="section-link active" href="/admin/dashboard"', page)
        self.assertIn('class="section-link" href="/audit/"', page)
        # Signed-in sidebar page: the mobile toggle and account area.
        self.assertIn('id="sidebarToggle"', page)
        self.assertIn('class="auth-user"', page)
        self.assertIn('href="/logout"', page)
        # No bootstrap navbar remnants — the sections share one header.
        self.assertNotIn('class="navbar', page)

    def test_admin_gate_falls_back_to_legacy_admins_name(self):
        # Pre-rename deployments keep working: the legacy ADMINS name
        # is honored when AUTH_ADMINS is unset.
        _, client, campus, cleanup = make_app(
            admins_key="ADMINS", admins=TEST_USER_ID
        )
        self.addCleanup(cleanup)
        campus.auth.clients.list.return_value = []

        resp = client.get("/admin/clients")

        self.assertEqual(resp.status_code, 200)

    def test_admin_gate_prefers_auth_admins_over_legacy(self):
        # When both names are set, AUTH_ADMINS wins: a user listed
        # only in the legacy ADMINS is denied.
        _, client, campus, cleanup = make_app(
            admins_key="AUTH_ADMINS",
            admins="other-admin@nyjc.edu.sg",
            legacy_admins=TEST_USER_ID,
        )
        self.addCleanup(cleanup)

        resp = client.get("/admin/clients")

        self.assertEqual(resp.status_code, 403)

    def test_resource_views_suppress_the_page_footer(self):
        # App-shell layout: the table is the page's only scroller, so
        # resource views drop the footer block. The landing page (not
        # an app-shell view) keeps it.
        self.campus.auth.clients.list.return_value = []

        landing = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn("2025 Campus Admin Portal", landing)

        clients = self.client.get("/admin/clients").get_data(as_text=True)
        self.assertNotIn("2025 Campus Admin Portal", clients)

    def test_views_require_admin(self):
        # A signed-in non-admin gets the self-diagnosing 403: the page
        # names their identity and the allowlist that gates the
        # section.
        @self.app.before_request
        def _swap_to_non_admin():
            flask.g.user = SimpleNamespace(
                id="other@nyjc.edu.sg", email="other@nyjc.edu.sg"
            )

        for path in [
            "/admin/sessions",
            "/admin/vaults",
            "/admin/credentials",
        ]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 403, path)
            body = resp.get_data(as_text=True)
            self.assertIn("other@nyjc.edu.sg", body, path)
            self.assertIn("AUTH_ADMINS", body, path)
            self.campus.auth.sessions.list.assert_not_called()

    def test_startup_logs_gate_summary(self):
        # create_app announces the effective gates (var name + entry
        # count, never the addresses) so a misconfigured allowlist
        # surfaces at deploy, not as 403s. The harness sets AUTH_ADMINS
        # but not AUDIT_ADMINS, so the line logs at warning level.
        with self.assertLogs("apps.admin", level="INFO") as captured:
            _, _, _, cleanup = make_app()
        self.addCleanup(cleanup)

        joined = "\n".join(captured.output)
        self.assertIn("admin section = AUTH_ADMINS (1 entry)", joined)
        self.assertIn("audit section = AUDIT_ADMINS (0 entries)", joined)


if __name__ == "__main__":
    unittest.main()
