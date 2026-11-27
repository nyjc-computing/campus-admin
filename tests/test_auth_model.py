"""Tests for the portal's authorization model (apps.admin.authz).

One authority per service: campus.auth's access-grant store gates the
Admin section (with the portal's platform operators), campus.api's
circles gate the Resources section, and each section's wire calls run
as the authority that admitted the user. The audit lists are covered
in tests/test_audit_gate.py.

Harness: tests.support.make_admin_app builds the real create_app()
with the Campus SDK client swapped for a recording mock and answers
the gates' probes from fakes (operator circle, per-family access
probe, grant store, token scopes).

File: tests/test_auth_model.py
"""

import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import campus_python
import flask

from apps.admin import authz
from tests.support import GROUP_READ, TEST_USER_ID, grant, make_admin_app

LOGIN_SESSION_KEY = "logins_login_id"


def designated(grants, scopes, **kwargs):
    """A signed-in user who is not a platform operator but holds
    `grants`, with a session token carrying `scopes`."""
    return make_admin_app(members=(), grants=grants, scopes=scopes, **kwargs)


def session_lane(campus):
    """Give the harness's with_user_session() its own `auth` surface.

    The operator lane calls `campus.auth`; the designated lane calls
    the `auth` of the client yielded inside the user's session. With a
    distinct mock for the latter, a test can tell which lane a view's
    upstream call went out on.
    """
    user_auth = mock.MagicMock()
    campus.with_user_session.return_value.__enter__.return_value = (
        SimpleNamespace(auth=user_auth, api=campus.api)
    )
    return user_auth


def capture_start_login(app):
    """A start_login stand-in recording its arguments and answering
    with a real redirect (built in a request context, as a view must)."""
    captured = {}

    def fake_start_login(_campus, *, target, scopes):
        captured["target"] = target
        captured["scopes"] = list(scopes)
        with app.test_request_context():
            return flask.redirect("https://auth.example.test/authorize")

    return captured, fake_start_login


class TestAccessValueAlgebra(unittest.TestCase):
    """The campus.api access-value and campus.auth scope readings
    (apps.admin.authz), pinned against the ratified contracts."""

    def test_only_the_admin_constant_carries_platform_operator_status(self):
        for name, value in authz.ACCESS_CONSTANTS.items():
            self.assertEqual(
                authz.has_permission(value, "group", "create"),
                name == "admin",
                name,
            )

    def test_group_field_bits(self):
        admin = authz.ACCESS_CONSTANTS["admin"]
        editor = authz.ACCESS_CONSTANTS["editor"]
        viewer = authz.ACCESS_CONSTANTS["viewer"]
        self.assertTrue(authz.has_permission(admin, "group", "modify"))
        self.assertTrue(authz.has_permission(editor, "group", "modify"))
        self.assertFalse(authz.has_permission(editor, "group", "create"))
        self.assertTrue(authz.has_permission(viewer, "group", "read"))
        self.assertFalse(authz.has_permission(viewer, "group", "modify"))

    def test_self_only_constants_hold_no_group_authority(self):
        for name in ("self", "guest"):
            value = authz.ACCESS_CONSTANTS[name]
            self.assertFalse(authz.has_permission(value, "group", "read"), name)
        self.assertTrue(
            authz.has_permission(authz.ACCESS_CONSTANTS["guest"], "self", "read")
        )

    def test_constants_are_the_ratified_values(self):
        self.assertEqual(
            authz.ACCESS_CONSTANTS,
            {"admin": 126, "editor": 54, "viewer": 18, "self": 14, "guest": 2},
        )

    def test_scope_held_is_monotonic_within_a_vocabulary(self):
        self.assertTrue(authz.scope_held(["users:admin"], "users", "read"))
        self.assertTrue(authz.scope_held(["users:write"], "users", "mod"))
        self.assertTrue(authz.scope_held(["users:mod"], "users", "read"))
        self.assertFalse(authz.scope_held(["users:mod"], "users", "write"))
        self.assertFalse(authz.scope_held(["users:read"], "sessions", "read"))

    def test_scope_held_fails_closed(self):
        self.assertFalse(authz.scope_held(None, "users", "read"))
        self.assertFalse(authz.scope_held([], "users", "read"))
        self.assertFalse(authz.scope_held(["users:bogus"], "users", "read"))


class TestAdminGate(unittest.TestCase):
    """Entry to the Admin section is campus.auth authority: a platform
    operator (admin on the operator circle) or an account holding at
    least one access grant (the 2026-11-17 ruling, campus-admin#72)."""

    def test_platform_operator_is_admitted(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.auth.clients.list.return_value = []
        self.assertEqual(client.get("/admin/auth/clients").status_code, 200)

    def test_membership_below_admin_is_not_the_operator_grant(self):
        # Reachable in the operator circle, but only the `admin`
        # constant confers the operator console.
        app, client, campus, cleanup = make_admin_app(
            members={TEST_USER_ID: authz.ACCESS_CONSTANTS["viewer"]},
        )
        self.addCleanup(cleanup)
        self.assertEqual(client.get("/admin/auth/clients").status_code, 403)

    def test_grant_holder_is_admitted_without_the_operator_circle(self):
        # campus-admin#72 acceptance: a grant-holder who is not in the
        # operator circle can use the portal.
        app, client, campus, cleanup = designated(
            [grant("users", "read")], ("campus.profile", "users:read"),
            circle_name=None,
        )
        self.addCleanup(cleanup)
        campus.auth.users.list.return_value = []
        self.assertEqual(client.get("/admin/auth/dashboard").status_code, 200)
        self.assertEqual(client.get("/admin/auth/users").status_code, 200)

    def test_holding_neither_authority_is_refused(self):
        app, client, campus, cleanup = make_admin_app(members=())
        self.addCleanup(cleanup)
        resp = client.get("/admin/auth/clients")
        self.assertEqual(resp.status_code, 403)
        body = resp.get_data(as_text=True)
        self.assertIn(TEST_USER_ID, body)
        self.assertIn("access grant", body)
        campus.auth.clients.list.assert_not_called()

    def test_anonymous_is_sent_to_sign_in(self):
        app, client, campus, cleanup = make_admin_app(user_id=None)
        self.addCleanup(cleanup)
        resp = client.get("/admin/auth/clients")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login?next=/admin/auth/clients", resp.headers["Location"])

    def test_unanswerable_authorities_fail_closed_with_503(self):
        app, client, campus, cleanup = make_admin_app(members=())
        self.addCleanup(cleanup)
        campus.api.circles.list.side_effect = RuntimeError("circles down")
        campus.auth.grants.list.side_effect = RuntimeError("grants down")
        self.assertEqual(client.get("/admin/auth/clients").status_code, 503)

    def test_circles_outage_still_admits_a_grant_holder(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")], ("campus.profile", "users:read"),
        )
        self.addCleanup(cleanup)
        campus.api.circles.list.side_effect = RuntimeError("circles down")
        campus.auth.users.list.return_value = []
        self.assertEqual(client.get("/admin/auth/users").status_code, 200)

    def test_grant_store_outage_refuses_a_non_operator(self):
        app, client, campus, cleanup = make_admin_app(members=())
        self.addCleanup(cleanup)
        campus.auth.grants.list.side_effect = RuntimeError("grants down")
        self.assertEqual(client.get("/admin/auth/clients").status_code, 503)

    def test_migration_polling_endpoint_gates_the_same_authority(self):
        import json

        app, client, campus, cleanup = make_admin_app(members=())
        self.addCleanup(cleanup)
        response = client.get("/admin/auth/migrations/auth")
        self.assertEqual(response.status_code, 403)
        self.assertIn(
            "access grant", json.loads(response.get_data(as_text=True))["error"],
        )


class TestLanes(unittest.TestCase):
    """The wire principal matches the authority that admitted the
    user: platform operators act as the operator client, designated
    admins as themselves."""

    def test_operator_lane_acts_as_the_operator_client(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        campus.auth.sessions.list.return_value = []
        self.assertEqual(client.get("/admin/auth/sessions").status_code, 200)
        campus.auth.sessions.list.assert_called_once()
        user_auth.sessions.list.assert_not_called()

    def test_designated_lane_acts_as_the_user(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")],
            ("campus.profile", "sessions:read"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        user_auth.sessions.list.return_value = []
        self.assertEqual(client.get("/admin/auth/sessions").status_code, 200)
        user_auth.sessions.list.assert_called_once_with(
            provider=None, client_id=None, user_id=None, limit=500,
        )
        campus.auth.sessions.list.assert_not_called()

    def test_resources_act_as_the_user_even_for_an_operator(self):
        # campus.api denies client principals on gated routes (#854):
        # the section has no operator lane.
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        session_lane(campus)
        seen = []
        campus.api.venues.list.side_effect = lambda: seen.append("called")
        self.assertEqual(client.get("/admin/api/venues").status_code, 200)
        self.assertEqual(seen, ["called"])


class TestVocabularyGate(unittest.TestCase):
    """A designated admin reaches exactly the vocabularies their grant
    rows cover, at the level granted, once the token carries the
    scope — the same two legs campus.auth enforces on the wire."""

    def test_vocabulary_without_a_grant_is_refused_before_any_call(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")], ("campus.profile", "users:read"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        resp = client.get("/admin/auth/sessions")
        self.assertEqual(resp.status_code, 403)
        body = resp.get_data(as_text=True)
        self.assertIn("no sessions grant", body)
        user_auth.sessions.list.assert_not_called()
        # The sidebar says the same: Users open, Sessions disabled (it
        # is also the page being viewed, hence active).
        self.assertIn('href="/admin/auth/users" class="sidebar-link"\n', body)
        self.assertIn(
            'href="/admin/auth/sessions" class="sidebar-link active disabled"', body,
        )

    def test_grant_and_scope_admit(self):
        app, client, campus, cleanup = designated(
            [grant("users", "write")],
            ("campus.profile", "users:read", "users:write"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        user_auth.users.list.return_value = []
        body = client.get("/admin/auth/users").get_data(as_text=True)
        self.assertIn("+ new user", body)
        user_auth.users.list.assert_called_once_with()
        self.assertIn('href="/admin/auth/users" class="sidebar-link active"', body)

    def test_grant_without_token_scope_offers_the_step_up(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile",),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        resp = client.get("/admin/auth/sessions")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Enable Sessions access", body)
        self.assertIn('href="/admin/auth/enable?next=/admin/auth/sessions"', body)
        user_auth.sessions.list.assert_not_called()
        # Clickable: the page itself offers the fix.
        self.assertIn('href="/admin/auth/sessions" class="sidebar-link active"', body)
        self.assertNotIn("sidebar-link active disabled", body)

    def test_scope_the_portal_cap_excludes_names_the_cap(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile",),
            scope_cap=("campus.profile", "users:read"),
        )
        self.addCleanup(cleanup)
        body = client.get("/admin/auth/sessions").get_data(as_text=True)
        self.assertIn("allowed_scopes cap", body)
        self.assertNotIn("Enable Sessions access", body)

    def test_unreadable_token_scopes_render_the_unknown_state(self):
        app, client, campus, cleanup = make_admin_app(
            members=(), grants=[grant("sessions", "read")],
            get_token_error=RuntimeError("token route down"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        resp = client.get("/admin/auth/sessions")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("could not be read", resp.get_data(as_text=True))
        self.assertNotIn("Enable Sessions access", resp.get_data(as_text=True))
        user_auth.sessions.list.assert_not_called()

    def test_read_level_grant_refuses_writes(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")],
            ("campus.profile", "sessions:read"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        user_auth.sessions.list.return_value = []
        page = client.get("/admin/auth/sessions").get_data(as_text=True)
        self.assertNotIn(">revoke<", page)
        self.assertNotIn("sweep expired", page)

        resp = client.post(
            "/admin/auth/sessions",
            data={"action": "revoke", "session_id": "uid-s1"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("covers read, not write", resp.get_data(as_text=True))
        user_auth.sessions.__getitem__.assert_not_called()

    def test_vault_rows_are_per_label(self):
        app, client, campus, cleanup = designated(
            [grant("vault", "write", "deployment")],
            ("campus.profile", "vaults:read", "vaults:write"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        client.post(
            "/admin/auth/vaults",
            data={
                "action": "set", "label": "google.classroom",
                "key": "k", "value": "v",
            },
            follow_redirects=True,
        )
        user_auth.vaults.__getitem__.assert_not_called()

        client.post(
            "/admin/auth/vaults",
            data={"action": "set", "label": "deployment", "key": "k", "value": "v"},
        )
        user_auth.vaults.__getitem__.assert_called_with("deployment")

    def test_clients_operator_level_actions_are_refused(self):
        # clients:admin rows are operator-equivalent upstream; a
        # clients:write designated admin edits records and nothing more.
        app, client, campus, cleanup = designated(
            [grant("clients", "write")],
            ("campus.profile", "clients:read", "clients:write"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)
        user_auth.clients.list.return_value = [
            SimpleNamespace(
                id="c1", name="C1", description="d", redirect_uris=[],
                upstream_scopes={}, is_public=False, permissions={},
                created_at=None, created=None,
            ),
        ]
        page = client.get("/admin/auth/clients").get_data(as_text=True)
        self.assertIn("Edit", page)
        self.assertNotIn("+ new client", page)
        self.assertNotIn("Revoke secret", page)

        resp = client.post(
            "/admin/auth/clients",
            data={"action": "create", "name": "n", "description": "d"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn(
            "reserved for the deployment operator",
            resp.get_data(as_text=True),
        )

        for path in (
            "/admin/auth/clients/c1/revoke_secret", "/admin/auth/clients/c1/grant",
        ):
            self.assertEqual(
                client.post(path, data={"label": "x"}).status_code, 403, path,
            )
        user_auth.clients.new.assert_not_called()
        user_auth.clients.__getitem__.assert_not_called()

        resp = client.post(
            "/admin/auth/clients", data={"client_id": "c1", "name": "Renamed"},
        )
        self.assertEqual(resp.status_code, 302)
        user_auth.clients["c1"].update.assert_called_once_with(name="Renamed")


def delete_user(client, user_id, confirm):
    """POST the Users page's delete action with the page's CSRF token,
    following the redirect."""
    page = client.get("/admin/auth/users").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return client.post(
        "/admin/auth/users",
        data={"action": "delete", "user_id": user_id, "confirm": confirm,
              "csrf_token": token},
        follow_redirects=True,
    )


class TestUserDeletion(unittest.TestCase):
    """Deleting an account is users:admin upstream: the Users page
    offers it, and posts it, exactly where both legs of that grant
    hold — and never for the signed-in account."""

    STRAY = "stray.account@nyjc.edu.sg"

    def test_write_level_grant_is_not_offered_delete_nor_heard(self):
        app, client, campus, cleanup = designated(
            [grant("users", "write")],
            ("campus.profile", "users:read", "users:write"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)

        page = client.get("/admin/auth/users").get_data(as_text=True)

        self.assertIn("+ new user", page)
        self.assertNotIn('data-user-id=', page)
        self.assertNotIn('id="deleteUserModal"', page)
        # Without a form on the page there is no token to post with.
        self.assertNotIn('name="csrf_token"', page)

        resp = client.post("/admin/auth/users", data={
            "action": "delete", "user_id": self.STRAY,
            "confirm": self.STRAY,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn("covers write, not admin", resp.get_data(as_text=True))
        user_auth.users.__getitem__.assert_not_called()

    def test_users_admin_deletes_as_themselves(self):
        app, client, campus, cleanup = designated(
            [grant("users", "admin")],
            ("campus.profile", "users:read", "users:admin"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)

        page = client.get("/admin/auth/users").get_data(as_text=True)

        self.assertIn('id="deleteUserModal"', page)
        # Their own account is not offered, here either.
        self.assertNotIn(f'data-user-id="{TEST_USER_ID}"', page)

        resp = delete_user(client, self.STRAY, self.STRAY)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"Deleted user {self.STRAY}", resp.get_data(as_text=True))
        # The operator client made no call of its own.
        campus.auth.users.__getitem__.assert_not_called()

        refused = delete_user(client, TEST_USER_ID, TEST_USER_ID)
        self.assertIn("cannot delete the account you are signed in with",
                      refused.get_data(as_text=True))
        # Exactly one lookup went out: the one account deleted.
        user_auth.users.__getitem__.assert_called_once_with(self.STRAY)
        user_auth.users[self.STRAY].delete.assert_called_once_with()

    def test_admin_grant_without_the_scope_gets_the_step_up(self):
        app, client, campus, cleanup = designated(
            [grant("users", "admin")], ("campus.profile",),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)

        resp = client.post("/admin/auth/users", data={
            "action": "delete", "user_id": self.STRAY,
            "confirm": self.STRAY,
        })

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Enable Users access", body)
        self.assertNotIn("Deleted user", body)
        user_auth.users.__getitem__.assert_not_called()


class TestStepUp(unittest.TestCase):
    """GET /admin/auth/enable: the capability-acquisition hop for designated
    admins (sign-in requests only the structural baseline)."""

    def setUp(self):
        public_url = mock.patch.dict(
            os.environ, {"PUBLIC_URL": "https://portal.example.test"},
        )
        public_url.start()
        self.addCleanup(public_url.stop)

    def test_enable_requests_exactly_the_granted_scopes(self):
        app, client, campus, cleanup = designated(
            [grant("clients", "write"), grant("sessions", "read")],
            ("campus.profile",),
        )
        self.addCleanup(cleanup)
        captured, fake = capture_start_login(app)
        with mock.patch(
            "campus.flask_campus.context.start_login", side_effect=fake,
        ):
            resp = client.get("/admin/auth/enable?next=/admin/auth/clients")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("auth.example.test", resp.headers["Location"])
        self.assertEqual(
            captured["scopes"],
            ["clients:read", "clients:write", "sessions:read"],
        )
        self.assertTrue(captured["target"].endswith("/finalize_login"))
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("login_next"), "/admin/auth/clients")

    def test_enable_skips_vocabularies_the_portal_cap_excludes(self):
        app, client, campus, cleanup = designated(
            [grant("clients", "write"), grant("sessions", "read")],
            ("campus.profile",),
            scope_cap=("campus.profile", "clients:read", "clients:write"),
        )
        self.addCleanup(cleanup)
        captured, fake = capture_start_login(app)
        with mock.patch(
            "campus.flask_campus.context.start_login", side_effect=fake,
        ):
            client.get("/admin/auth/enable")
        self.assertEqual(captured["scopes"], ["clients:read", "clients:write"])

    def test_operator_has_nothing_to_enable(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        with mock.patch("campus.flask_campus.context.start_login") as m:
            resp = client.get(
                "/admin/auth/enable?next=/admin/auth/clients", follow_redirects=True,
            )
        m.assert_not_called()
        self.assertIn("Nothing to enable", resp.get_data(as_text=True))

    def test_unsafe_next_falls_back_to_the_dashboard(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile",),
        )
        self.addCleanup(cleanup)
        captured, fake = capture_start_login(app)
        with mock.patch(
            "campus.flask_campus.context.start_login", side_effect=fake,
        ):
            client.get("/admin/auth/enable?next=https://evil.example/x")
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("login_next"), "/admin/auth/dashboard")

    def test_cap_rejection_flashes_reason_instead_of_500(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile",),
        )
        self.addCleanup(cleanup)
        with mock.patch(
            "campus.flask_campus.context.start_login",
            side_effect=campus_python.errors.BadRequestError(
                "Requested scopes not allowed for this client: sessions:read",
            ),
        ):
            resp = client.get(
                "/admin/auth/enable?next=/admin/auth/sessions", follow_redirects=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("allowed_scopes cap", resp.get_data(as_text=True))

    def test_new_login_session_re_reads_the_token_scopes(self):
        # A step-up ends in a NEW login session; the wider token must
        # show on the next request, not after the scope cache's TTL.
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile",),
        )
        self.addCleanup(cleanup)
        campus.auth.sessions.list.return_value = []
        first = client.get("/admin/auth/sessions")
        self.assertIn("Enable Sessions access", first.get_data(as_text=True))

        campus.auth.get_token.return_value = SimpleNamespace(
            scopes=["campus.profile", "sessions:read"],
        )
        # Same login session: the cached answer stands.
        self.assertIn(
            "Enable Sessions access",
            client.get("/admin/auth/sessions").get_data(as_text=True),
        )
        with client.session_transaction() as sess:
            sess[LOGIN_SESSION_KEY] = "uid-campus_login-new"
        third = client.get("/admin/auth/sessions")
        self.assertNotIn("Enable Sessions access", third.get_data(as_text=True))
        campus.auth.sessions.list.assert_called_once()


class TestResourcesAccess(unittest.TestCase):
    """The Resources section follows the signed-in user's campus.api
    access: reads need the family's group-scope read, mutations its
    group-scope create."""

    def all_readable(self):
        return dict.fromkeys(authz.API_FAMILIES, GROUP_READ)

    def test_read_only_user_sees_no_write_controls_and_cannot_post(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")],
            ("campus.profile", "users:read"),
            family_access=self.all_readable(),
        )
        self.addCleanup(cleanup)
        body = client.get("/admin/api/circles").get_data(as_text=True)
        self.assertNotIn("+ new circle", body)
        self.assertNotIn('class="sidebar-link disabled"', body)

        resp = client.post(
            "/admin/api/circles", data={"name": "x", "tag": "group"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"circles", resp.data)
        campus.api.circles.new.assert_not_called()

    def test_operator_sees_the_write_controls(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        body = client.get("/admin/api/circles").get_data(as_text=True)
        self.assertIn("+ new circle", body)

    def test_family_without_access_is_disabled_in_the_sidebar(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")],
            ("campus.profile", "users:read"),
            family_access={"timetable": GROUP_READ},
        )
        self.addCleanup(cleanup)
        campus.api.timetable.view.return_value = {}
        campus.api.timetable.list_metadata.return_value = []
        body = client.get("/admin/api/timetable").get_data(as_text=True)
        self.assertIn('href="/admin/api/timetable" class="sidebar-link active"', body)
        self.assertIn('href="/admin/api/venues" class="sidebar-link disabled"', body)

    def test_unanswerable_probe_fails_closed_with_503(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")],
            ("campus.profile", "users:read"),
            family_access={"timetable": GROUP_READ},
        )
        self.addCleanup(cleanup)
        campus.api.client.get.side_effect = RuntimeError("api down")
        self.assertEqual(client.get("/admin/api/timetable").status_code, 503)

    def test_section_pills_follow_each_service_authority(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")],
            ("campus.profile", "users:read"),
            family_access={},
        )
        self.addCleanup(cleanup)
        body = client.get("/admin/auth/dashboard").get_data(as_text=True)
        self.assertIn('class="section-link active" href="/admin/auth/dashboard"', body)
        self.assertIn('class="section-link disabled" href="/admin/api/"', body)
        self.assertIn("no campus.api access", body)
        self.assertIn('class="section-link disabled" href="/admin/audit/"', body)


def post_grant(client, **data):
    """POST a Grants action with the page's CSRF token, following the
    redirect back to the list."""
    body = client.get("/admin/auth/grants").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    return client.post(
        "/admin/auth/grants", data={**data, "csrf_token": token},
        follow_redirects=True,
    )


def store_row(grantee_id, resource_type, level, resource_id="") -> dict:
    """One access-grant store row, as the API returns it."""
    return {
        "id": f"uid-grant-{resource_type}-{level}-{grantee_id}", "grantee_type": "user",
        "grantee_id": grantee_id, "resource_type": resource_type,
        "resource_id": resource_id, "bits": None, "level": level,
        "created_at": "2026-11-01T00:00:00",
    }


class TestGrantAdministration(unittest.TestCase):
    """The Grants page follows the grant store's own administration
    gate (campus campus/auth/routes/grants.py): the operator
    administers every vocabulary; a designated admin needs admin on
    the vocabulary — grant row AND token scope — which upstream's
    shape rules leave reachable for `users` alone; and nobody
    administers their own rows."""

    def users_admin(self, scopes=("campus.profile", "users:admin"), **kwargs):
        """A designated admin holding users:admin, with the session
        lane's auth surface on its own mock."""
        harness = designated([grant("users", "admin")], scopes, **kwargs)
        for cleanup in harness[3:]:
            self.addCleanup(cleanup)
        campus = harness[2]
        return (*harness[:3], session_lane(campus))

    def test_users_admin_acts_as_themselves_on_users_rows(self):
        app, client, campus, user_auth = self.users_admin()
        user_auth.grants.list.return_value = {"grants": [
            store_row("other@nyjc.edu.sg", "users", "read"),
        ]}

        resp = client.get("/admin/auth/grants")

        self.assertEqual(resp.status_code, 200)
        # A designated admin's unfiltered list is refused upstream: the
        # read is scoped to the one vocabulary they administer, and it
        # rides their own session.
        user_auth.grants.list.assert_called_once_with(resource_type="users")
        self.assertIn("other@nyjc.edu.sg", resp.get_data(as_text=True))
        # The operator client answered only the nav's own-grants probe.
        self.assertEqual(campus.auth.grants.list.call_args_list, [
            mock.call(grantee_type="user", grantee_id=TEST_USER_ID),
        ])

        resp = post_grant(
            client, action="grant", grantee_type="user",
            grantee_id="other@nyjc.edu.sg", resource_type="users",
            resource_id="", level="write",
        )

        self.assertEqual(resp.status_code, 200)
        user_auth.grants.grant.assert_called_once_with(
            grantee_type="user", grantee_id="other@nyjc.edu.sg",
            resource_type="users", resource_id="", level="write", bits=None,
        )
        campus.auth.grants.grant.assert_not_called()

    def test_the_form_offers_users_only(self):
        app, client, campus, user_auth = self.users_admin()

        body = client.get("/admin/auth/grants").get_data(as_text=True)

        self.assertIn('<option value="users"', body)
        self.assertNotIn('<option value="vault"', body)
        self.assertNotIn('<option value="clients"', body)
        # A client's grants are vault rows: no client grantees here.
        self.assertNotIn("client — access to a vault label", body)
        self.assertIn("You administer the users grants only", body)

    def test_nobody_administers_their_own_rows(self):
        app, client, campus, user_auth = self.users_admin()
        user_auth.grants.list.return_value = {"grants": [
            store_row(TEST_USER_ID, "users", "admin"),
            store_row("other@nyjc.edu.sg", "users", "read"),
        ]}

        body = client.get("/admin/auth/grants").get_data(as_text=True)

        # One revoke per view for the other admin's row; none for the
        # signed-in user's own.
        self.assertEqual(body.count('data-confirm="Revoke users:read from '
                                    'user other@nyjc.edu.sg?"'), 2)
        self.assertNotIn(f'Revoke users:admin from user {TEST_USER_ID}',
                         body)
        self.assertIn("listed without controls", body)

        resp = post_grant(
            client, action="grant", grantee_type="user",
            grantee_id=TEST_USER_ID, resource_type="users", level="admin",
        )

        self.assertIn("cannot change your own access grants",
                      resp.get_data(as_text=True))
        user_auth.grants.grant.assert_not_called()

    def test_every_other_vocabulary_is_the_operator_s(self):
        app, client, campus, user_auth = self.users_admin()

        listed = client.get("/admin/auth/grants?resource_type=vault")

        self.assertEqual(listed.status_code, 200)
        user_auth.grants.list.assert_not_called()
        self.assertIn("operator's to administer", listed.get_data(as_text=True))

        resp = post_grant(
            client, action="grant", grantee_type="user",
            grantee_id="other@nyjc.edu.sg", resource_type="vault",
            resource_id="deployment", level="read",
        )

        self.assertEqual(resp.status_code, 403)
        self.assertIn("reserved for the deployment operator",
                      resp.get_data(as_text=True))
        user_auth.grants.grant.assert_not_called()

    def test_without_an_admin_level_grant_the_page_is_refused(self):
        app, client, campus, cleanup = designated(
            [grant("users", "read")], ("campus.profile", "users:read"),
        )
        self.addCleanup(cleanup)
        user_auth = session_lane(campus)

        resp = client.get("/admin/auth/grants")

        self.assertEqual(resp.status_code, 403)
        body = resp.get_data(as_text=True)
        self.assertIn("takes admin on the users vocabulary", body)
        self.assertIn("covers read, not admin", body)
        # The sidebar says the same.
        self.assertIn(
            '<a href="/admin/auth/grants" class="sidebar-link active disabled"',
            body,
        )
        user_auth.grants.list.assert_not_called()

    def test_without_any_users_grant_the_page_says_so(self):
        app, client, campus, cleanup = designated(
            [grant("sessions", "read")], ("campus.profile", "sessions:read"),
        )
        self.addCleanup(cleanup)

        resp = client.get("/admin/auth/grants")

        self.assertEqual(resp.status_code, 403)
        self.assertIn("holds no users grant", resp.get_data(as_text=True))

    def test_admin_row_without_the_scope_offers_the_step_up(self):
        app, client, campus, user_auth = self.users_admin(
            scopes=("campus.profile",),
        )

        resp = client.get("/admin/auth/grants")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Enable Grants access", body)
        self.assertIn('href="/admin/auth/enable?next=/admin/auth/grants"', body)
        # Clickable: the page itself offers the fix.
        self.assertIn('href="/admin/auth/grants" class="sidebar-link active"',
                      body)
        self.assertNotIn("sidebar-link active disabled", body)
        user_auth.grants.list.assert_not_called()

    def test_scope_the_portal_cap_excludes_names_the_cap(self):
        app, client, campus, user_auth = self.users_admin(
            scopes=("campus.profile",), scope_cap=("campus.profile",),
        )

        body = client.get("/admin/auth/grants").get_data(as_text=True)

        self.assertIn("allowed_scopes cap", body)
        self.assertNotIn("Enable Grants access", body)
        user_auth.grants.list.assert_not_called()

    def test_unreadable_token_scopes_render_the_unknown_state(self):
        app, client, campus, user_auth = self.users_admin(
            get_token_error=RuntimeError("token route down"),
        )

        resp = client.get("/admin/auth/grants")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("could not be read", body)
        self.assertNotIn("Enable Grants access", body)
        user_auth.grants.list.assert_not_called()


if __name__ == "__main__":
    unittest.main()
