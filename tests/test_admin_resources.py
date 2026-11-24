"""Tests for the /admin resource views (clients, users, sessions, vaults,
credentials).

Builds the real create_app() through tests.support.make_admin_app: the
Campus SDK client is swapped for a recording MagicMock, so the route
closures capture the double. The flask_campus login manager is installed
for real but its push_context no-ops (both has_session checks stubbed
False); a test before_request hook registered after create_app injects
the signed-in user. By default that user is a platform operator (admin
on the operator circle), so these view tests exercise the operator
lane; the gates and the designated-admin lane are covered in
tests/test_auth_model.py.

File: tests/test_admin_resources.py
"""

import re
import unittest
from types import SimpleNamespace
from unittest import mock

import flask

from apps.admin import split_scopes
from tests.support import TEST_USER_ID, make_admin_app

make_app = make_admin_app


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

    def test_vaults_new_vault_modal_renders_on_list_only(self):
        self.campus.auth.vaults.labels.return_value = ["deployment"]

        list_body = self.client.get("/admin/vaults").get_data(as_text=True)
        detail_body = self.client.get(
            "/admin/vaults?label=deployment"
        ).get_data(as_text=True)

        # The modal posts action=set with a new_vault marker; it lives
        # on the list page only — the detail page's set-key modal
        # already covers adding keys to an existing vault.
        self.assertEqual(list_body.count('name="new_vault"'), 1)
        self.assertIn('data-bs-target="#newVaultModal"', list_body)
        self.assertNotIn('name="new_vault"', detail_body)
        self.assertNotIn('data-bs-target="#newVaultModal"', detail_body)

    def test_vaults_create_new_label_writes_and_hints_grants(self):
        # labels() is called twice: once for the pre-write existence
        # check (label absent → hint), then by the detail view the
        # redirect lands on (label present → clean render).
        self.campus.auth.vaults.labels.side_effect = (
            ["deployment"], ["deployment", "campus.yapper"],
        )
        vault = mock.MagicMock()
        vault.keys.return_value = []
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"campus.yapper": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "set",
                "new_vault": "1",
                "label": "campus.yapper",
                "key": "API_KEY",
                "value": "secret",
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.campus.auth.vaults.__getitem__.assert_called_with("campus.yapper")
        vault.__setitem__.assert_called_once_with("API_KEY", "secret")
        body = resp.get_data(as_text=True)
        self.assertIn("Successfully set API_KEY in vault campus.yapper", body)
        self.assertIn("Created vault campus.yapper", body)
        self.assertIn("granted access", body)

    def test_vaults_new_vault_existing_label_no_hint(self):
        self.campus.auth.vaults.labels.return_value = ["deployment"]
        vault = mock.MagicMock()
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={
                "action": "set",
                "new_vault": "1",
                "label": "deployment",
                "key": "API_KEY",
                "value": "secret",
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Successfully set API_KEY in vault deployment",
                      resp.get_data(as_text=True))
        self.assertNotIn("Created vault deployment",
                         resp.get_data(as_text=True))

    def test_vaults_plain_set_skips_labels_lookup(self):
        vault = mock.MagicMock()
        self.campus.auth.vaults.__getitem__.side_effect = (
            lambda label: {"deployment": vault}[label]
        )

        resp = self.client.post(
            "/admin/vaults",
            data={"action": "set", "label": "deployment",
                  "key": "k", "value": "v"},
        )

        self.assertEqual(resp.status_code, 302)
        vault.__setitem__.assert_called_once_with("k", "v")
        # Ordinary sets pay no extra enumeration round-trip.
        self.campus.auth.vaults.labels.assert_not_called()

    def _client_row(self, permissions=None):
        """A client record shaped like campus.model.Client for the table."""
        return SimpleNamespace(
            id="uid-client-ef56f01c",
            name="campus-classroom-dev",
            description="campus-classroom Flask app (dev/testing)",
            is_public=False,
            redirect_uris=["http://localhost:5000/finalize_login"],
            upstream_scopes={
                "google": ["https://www.googleapis.com/auth/classroom.rosters"],
            },
            permissions=permissions or {},
            created_at="2026-09-29T00:00:00",
        )

    def test_clients_page_carries_no_scope_management(self):
        # Scope caps are client registration data that campus-cli edits;
        # the Clients page is records and vault access only, so the
        # upstream scope allowlist is neither shown nor editable here.
        self.campus.auth.clients.list.return_value = [self._client_row()]

        resp = self.client.get("/admin/clients")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("campus-classroom-dev", body)
        self.assertNotIn("Upstream scopes", body)
        self.assertNotIn("upstream_scopes", body)
        self.assertNotIn("classroom.rosters", body)
        self.assertNotIn(">Scopes</a>", body)
        self.assertNotIn("scopesModal", body)

    def test_clients_retired_scope_actions_are_not_routes(self):
        # The upstream-scope action URLs went with the editor. They are
        # not routes any more — nothing upstream is written.
        for action in ("add_upstream_scope", "remove_upstream_scope"):
            resp = self.client.post(
                f"/admin/clients/uid-client-x/{action}",
                data={"provider": "google", "scope": "https://a.example/s1"},
            )
            self.assertEqual(resp.status_code, 404, action)
        self.campus.auth.clients.__getitem__.assert_not_called()

    def test_clients_access_modal_links_to_the_grants_page(self):
        # Adding a client's vault access is a new grant: the Access
        # modal's "+ grant" leads to the Grants page opened on the
        # client, whether or not the client holds labels already.
        self.campus.auth.clients.list.return_value = [
            self._client_row(),
            self._client_row(permissions={"deployment": 15}),
        ]

        body = self.client.get("/admin/clients").get_data(as_text=True)

        target = (
            'href="/admin/grants?grantee_type=client'
            '&amp;grantee_id=uid-client-ef56f01c"'
        )
        # One link per modal: the client holding a label and the one
        # holding none.
        self.assertEqual(body.count(target), 2)
        self.assertIn("+ grant access</a>", body)
        self.assertIn("+ grant</a>", body)

    def _credential_record(self, scopes):
        """A redacted credential record whose token carries `scopes`."""
        return SimpleNamespace(
            id="uid-credential-1",
            client_id="uid-client-a",
            user_id="staff@nyjc.edu.sg",
            token=SimpleNamespace(
                token_type="Bearer",
                expires_at="2026-11-05T00:00:00",
                scopes=scopes,
            ),
            created_at="2026-11-04T00:00:00",
        )

    def _credentials_with(self, record):
        self.campus.auth.credentials.__getitem__.side_effect = (
            lambda provider: {"campus": mock.MagicMock(list=lambda: [record])}[
                provider
            ]
        )

    def test_credentials_list_renders_redacted_metadata(self):
        self._credentials_with(
            self._credential_record(["campus.profile", "users:read"])
        )

        resp = self.client.get("/admin/credentials")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("uid-credential-1", body)
        self.assertIn("Bearer", body)
        # The raw token (record.token.id) must never reach the page.
        self.assertNotIn("token.id", body)

    def test_credentials_render_one_badge_per_scope(self):
        # Each scope is its own badge — on the table and on the mobile
        # card — so a scope list reads as a list, not one run-on pill.
        self._credentials_with(self._credential_record(
            ["campus.profile", "timetable:read", "timetable:admin"]
        ))

        body = self.client.get("/admin/credentials").get_data(as_text=True)

        for scope in ("campus.profile", "timetable:read", "timetable:admin"):
            badge = f'<span class="badge bg-secondary">{scope}</span>'
            self.assertEqual(body.count(badge), 2, scope)
        # Never the joined string inside one badge.
        self.assertNotIn(">campus.profile timetable:read", body)
        self.assertEqual(body.count('class="scope-badges"'), 2)

    def test_credentials_without_scopes_show_a_dash(self):
        self._credentials_with(self._credential_record([]))

        body = self.client.get("/admin/credentials").get_data(as_text=True)

        self.assertNotIn('class="scope-badges"', body)
        self.assertNotIn('<span class="badge bg-secondary">', body)

    def test_split_scopes_reads_either_token_representation(self):
        # The token model carries the scopes as a list (`scopes`) and
        # as the RFC 6749 space-delimited string (`scope`).
        self.assertEqual(split_scopes("campus.profile users:read"), [
            "campus.profile", "users:read",
        ])
        self.assertEqual(split_scopes(["a", "b"]), ["a", "b"])
        self.assertEqual(split_scopes(None), [])
        self.assertEqual(split_scopes("  "), [])

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

    def test_legacy_resource_urls_are_retired(self):
        # The root-level resource paths (and their 308 shims) are
        # retired: the portal will sit behind a gateway proxy alongside
        # other frontend apps, so the root namespace must stay free.
        # Only the /admin-prefixed routes serve these resources.
        for path in ("/dashboard", "/users", "/sessions", "/vaults", "/credentials"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404, path)
        # The action POSTs carried their shims too; they must be gone
        # as well, not silently re-redirected.
        resp = self.client.post("/clients", data={"action": "create"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/clients/some-client/revoke_secret", data={})
        self.assertEqual(resp.status_code, 404)

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
            "/admin/grants": "Grants",
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
                "/admin/grants",
            ):
                self.assertIn(link, body, f"{link} missing on {path}")
            self.assertNotIn('class="sidebar-link disabled"', body, path)
            if active_label is None:
                self.assertNotIn('class="sidebar-link active"', body, path)
            else:
                self.assertIn('class="sidebar-link active"', body, path)
                self.assertEqual(body.count('class="sidebar-link active"'), 1)
                self.assertEqual(
                    re.findall(r'class="sidebar-link active"[^>]*>\s*'
                               r'<span class="sidebar-icon">▶</span> (\w+)',
                               body),
                    [active_label],
                    f"{active_label} is the active entry on {path}",
                )

    def test_top_nav_shared_with_audit_section(self):
        # Both sections include the same _top_nav.html partial:
        # identical markup and classes, themed per section via
        # body[data-section]. The admin bar is the audit bar's markup
        # with the brand, active pill, and accent swapped. The pills
        # are capability-aware: this harness sets no AUDIT_ADMINS, so
        # the audit pill renders disabled with the reason on hover
        # while the user's own sections stay clickable.
        self.campus.auth.clients.list.return_value = []

        page = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn('data-section="admin"', page)
        self.assertIn('class="top-header"', page)
        self.assertIn(">Campus Admin</a>", page)
        self.assertIn('class="section-link active" href="/admin/dashboard"', page)
        self.assertIn(
            'class="section-link disabled" href="/audit/"', page,
            "audit pill must render disabled for an account not on AUDIT_ADMINS",
        )
        self.assertIn(
            "title=\"Requires membership in the AUDIT_ADMINS list on this "
            "deployment.\"",
            page,
        )
        self.assertIn('class="section-link" href="/resources/"', page)
        # Signed-in sidebar page: the mobile toggle and account area.
        self.assertIn('id="sidebarToggle"', page)
        self.assertIn('class="auth-user"', page)
        self.assertIn('href="/logout"', page)
        # No bootstrap navbar remnants — the sections share one header.
        self.assertNotIn('class="navbar', page)

    def test_admin_gate_reads_admin_circle_env_override(self):
        # ADMIN_CIRCLE re-points the platform-operator circle without a
        # code change: the operator probe resolves that circle, and the
        # mocked API only grants the user membership there.
        _, client, campus, cleanup = make_app(
            circle_name="portal-admins",
            members=(TEST_USER_ID,),
            admin_circle_env="portal-admins",
        )
        self.addCleanup(cleanup)
        campus.auth.clients.list.return_value = []

        resp = client.get("/admin/clients")

        self.assertEqual(resp.status_code, 200)

    def test_admin_gate_denies_when_env_override_names_other_circle(self):
        # The env override names a circle the user is NOT a member of
        # (the mock grants membership in `campus` only): denied even
        # though the default circle would admit them — and they hold no
        # access grant either.
        _, client, _, cleanup = make_app(
            circle_name="campus",
            members=(TEST_USER_ID,),
            admin_circle_env="portal-admins",
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
        # A signed-in user holding neither authority (not a platform
        # operator, no access grant) gets the self-diagnosing 403: the
        # page names their identity and the authority gating the section.
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
            # The page names the authority the section refused on.
            self.assertIn("access grant", body, path)
            self.campus.auth.sessions.list.assert_not_called()

    def test_startup_logs_gate_summary(self):
        # create_app announces each section's authority and its
        # configuration (the audit lists' names and entry counts, never
        # the addresses) so a misconfigured gate surfaces at deploy, not
        # as 403s. The harness sets no AUDIT_ADMINS, so the line logs at
        # warning.
        with self.assertLogs("apps.admin", level="INFO") as captured:
            _, _, _, cleanup = make_app()
        self.addCleanup(cleanup)

        joined = "\n".join(captured.output)
        self.assertIn(
            "admin section = campus.auth grants + admin on circle 'campus' "
            "(ADMIN_CIRCLE, platform operators)",
            joined,
        )
        self.assertIn(
            "resources section = campus.api circles as the signed-in user",
            joined,
        )
        self.assertIn("audit section = AUDIT_ADMINS (0 entries)", joined)
        # AUDIT_KEY_ADMINS unset: key management falls back to the list.
        self.assertIn("key management = AUDIT_ADMINS (0 entries)", joined)


if __name__ == "__main__":
    unittest.main()
