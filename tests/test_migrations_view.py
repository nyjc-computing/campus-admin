"""Unit tests for the Migrations admin view (apps.admin.migrations, #75).

The view is campus-admin's surface over each service's migration
management routes (campus#1169): per-service plan/history sections,
apply/abort/rollback actions, grant-aware rendering. These tests
build the real create_app() with the Campus SDK mocked and the
per-service wire calls stubbed (tests.support.make_admin_app),
verifying:

- the page renders every service's section, with the api/audit 403
  fail-closed caveat (campus#1183) and the auth leg's plain errors;
- grant-aware rendering from the session token's scopes (read <
  write < admin; fail-closed when the scopes cannot be read);
- the apply/abort/rollback form posts relay to the right service
  paths and bodies, flash the service's refusals, and gate on CSRF;
- the polling JSON endpoint's gating (401/403/503 JSON) and shape.

File: tests/test_migrations_view.py
"""

import json
import os
import unittest
from unittest import mock

import campus_python

from tests.support import StubJsonClient, StubResponse, make_admin_app

# A realistic GET payload (migrations/routes.py #1169 shape).
AUTH_STATUS = {
    "service": "auth",
    "status": [
        {"revision": "010", "filename": "010_create_access_grants.py",
         "state": "applied"},
        {"revision": "012", "filename": "012_add_apikey_trace_binds.py",
         "state": "pending"},
    ],
    "plan": [{
        "revision": "012",
        "filename": "012_add_apikey_trace_binds.py",
        "hash": "a" * 64,
        "rollback": "ddl",
        "summary": "Add trace-bind columns to the apikeys store.",
    }],
    "history": [
        {
            "revision": "010",
            "filename": "010_create_access_grants.py",
            "status": "applied",
            "applied_at": "2026-11-17T08:30:00",
            "applied_by": "staff@nyjc.edu.sg",
            "duration_ms": 142,
            "error": None,
            "file_hash": "b" * 64,
        },
        {
            "revision": "011",
            "filename": "011_create_calendar_table.py",
            "status": "failed",
            "applied_at": "2026-11-17T09:00:00",
            "applied_by": "staff@nyjc.edu.sg",
            "duration_ms": 88,
            "error": "column \"venue_id\" referenced but missing",
            "file_hash": "c" * 64,
        },
    ],
    "running": False,
}

AUTH_RUNNING = dict(AUTH_STATUS, running=True)

# What the api/audit gates answer while campus#1183 is open.
CAPABILITY_403 = {
    "error": {
        "code": "FORBIDDEN",
        "message": (
            "The migrations capability leg cannot be verified on this "
            "service yet (#1169): the request context carries no token "
            "scopes."
        ),
    },
}


def make_wire(auth_body=AUTH_STATUS, auth_status=200):
    """Wire stubs for the three services: auth answering, api/audit
    403-ing with the fail-closed envelope (the #1183 reality)."""
    return {
        "auth": StubJsonClient({
            ("GET", "/auth/v1/admin/migrations"):
                StubResponse(auth_body, status_code=auth_status),
        }),
        "api": StubJsonClient({
            ("GET", "/api/v1/admin/migrations"):
                StubResponse(CAPABILITY_403, status_code=403),
        }),
        "audit": StubJsonClient({
            ("GET", "/audit/v1/admin/migrations"):
                StubResponse(CAPABILITY_403, status_code=403),
        }),
    }


class MigrationsViewTestCase(unittest.TestCase):
    """Shared harness: one app per test, patch restored after."""

    def setUp(self):
        self.wire = make_wire()
        (self.app, self.client, self.campus,
         cleanup) = make_admin_app(wire_clients=self.wire)
        self.addCleanup(cleanup)

    def _csrf(self) -> str:
        """Fetch a fresh CSRF token from the page."""
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        start = page.index(marker) + len(marker)
        return page[start:page.index('"', start)]

    def _post(self, path: str, **form):
        form["csrf_token"] = self._csrf()
        return self.client.post(path, data=form)

    def _wire_call(self, stub, method: str, path_prefix: str):
        """Find a recorded wire call by method and path prefix."""
        return next(
            call for call in stub.calls
            if call[0] == method and call[1].startswith(path_prefix)
        )


class TestPage(MigrationsViewTestCase):
    """The page renders every service's section."""

    def test_renders_sections_with_plan_and_history(self):
        response = self.client.get("/auth/migrations")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        # Section headers, plan metadata, ledger history.
        self.assertIn("012_add_apikey_trace_binds.py", page)
        self.assertIn("Add trace-bind columns to the apikeys store.", page)
        self.assertIn("ddl downgrade", page)
        self.assertIn("staff@nyjc.edu.sg", page)
        self.assertIn("column &#34;venue_id&#34; referenced but missing", page)
        # The service's own 403 text renders in its section.
        self.assertIn("carries no token scopes", page)

    def test_api_audit_sections_carry_the_1183_caveat(self):
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Expected while campus#1183 is open", page)
        self.assertIn("campus#1183", page)

    def test_up_to_date_badge_when_nothing_pending(self):
        up_to_date = dict(AUTH_STATUS, plan=[], status=[
            {"revision": "010", "filename": "010.py", "state": "applied"},
        ])
        self.wire["auth"].handlers = {
            ("GET", "/auth/v1/admin/migrations"):
                StubResponse(up_to_date),
        }
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("up to date", page)

    def test_history_is_newest_first(self):
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertLess(
            page.index("011_create_calendar_table.py"),
            page.index("010_create_access_grants.py"),
        )

    def test_running_section_shows_abort_and_badge(self):
        self.wire["auth"].handlers = {
            ("GET", "/auth/v1/admin/migrations"):
                StubResponse(AUTH_RUNNING),
        }
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn('data-running="true"', page)
        self.assertIn("applying", page)
        self.assertIn("abort", page)


class TestGrantRendering(MigrationsViewTestCase):
    """Buttons follow the session token's migrations scopes."""

    def test_full_vocabulary_enables_everything(self):
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("apply-modal-auth", page)
        self.assertIn("migration-rollback-btn", page)
        self.assertNotIn("migrations:write scope on your campus token", page)

    def test_read_only_disables_apply_and_hides_rollback(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), scopes=("campus.profile", "migrations:read"),
        )
        self.addCleanup(cleanup)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        # The apply affordance renders disabled with the gap named...
        self.assertIn("migrations:write scope on your campus token", page)
        self.assertNotIn("apply-modal-auth", page)
        # ...and no rollback affordance at all (admin cap).
        self.assertNotIn("migration-rollback-btn", page)

    def test_write_without_admin_disables_rollback_only(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(),
            scopes=("campus.profile", "migrations:write"),
        )
        self.addCleanup(cleanup)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("apply-modal-auth", page)
        self.assertNotIn("migration-rollback-btn", page)

    def test_unreadable_scopes_render_fail_closed(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(),
            get_token_error=RuntimeError("login session gone"),
        )
        self.addCleanup(cleanup)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("scopes could not be read", page)
        self.assertIn("migrations:write scope on your campus token", page)
        self.assertNotIn("apply-modal-auth", page)


class TestApply(MigrationsViewTestCase):
    """POST apply relays to the service and flashes the outcome."""

    def test_apply_relayed_without_up_to(self):
        self.wire["auth"].handlers[("POST", "/auth/v1/admin/migrations/apply")] = (
            StubResponse({"started": True, "service": "auth"}, status_code=202)
        )
        response = self._post("/auth/migrations/auth/apply")
        self.assertEqual(response.status_code, 302)
        method, path, _params, body = self._wire_call(
            self.wire["auth"], "POST", "/auth/v1/admin/migrations/apply",
        )
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/auth/v1/admin/migrations/apply")
        self.assertIsNone(body)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Apply started for Auth", page)

    def test_apply_with_staged_up_to(self):
        self.wire["auth"].handlers[("POST", "/auth/v1/admin/migrations/apply")] = (
            StubResponse({"started": True, "service": "auth"}, status_code=202)
        )
        response = self._post("/auth/migrations/auth/apply", up_to="012")
        self.assertEqual(response.status_code, 302)
        _, _, _, body = self._wire_call(
            self.wire["auth"], "POST", "/auth/v1/admin/migrations/apply",
        )
        self.assertEqual(body, {"up_to": "012"})

    def test_apply_conflict_flashes_service_text(self):
        self.wire["auth"].handlers[("POST", "/auth/v1/admin/migrations/apply")] = (
            StubResponse(
                {"error": {"code": "CONFLICT", "message": "An apply is "
                           "already running for the 'auth' migration scope"}},
                status_code=409,
            )
        )
        response = self._post("/auth/migrations/auth/apply")
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("already running", page)

    def test_unknown_service_404s(self):
        response = self.client.post(
            "/auth/migrations/nope/apply", data={"csrf_token": "x"},
        )
        self.assertEqual(response.status_code, 404)

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post("/auth/migrations/auth/apply", data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "form session expired", response.get_data(as_text=True),
        )


class TestAbort(MigrationsViewTestCase):
    """POST abort relays and flashes; visible only while running."""

    def test_abort_relayed(self):
        self.wire["auth"].handlers[("POST", "/auth/v1/admin/migrations/abort")] = (
            StubResponse({"aborting": True})
        )
        response = self._post("/auth/migrations/auth/abort")
        self.assertEqual(response.status_code, 302)
        method, path, _, body = self._wire_call(
            self.wire["auth"], "POST", "/auth/v1/admin/migrations/abort",
        )
        self.assertEqual(path, "/auth/v1/admin/migrations/abort")
        self.assertIsNone(body)
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Abort requested for Auth", page)

    def test_abort_with_nothing_running_flashes_conflict(self):
        self.wire["auth"].handlers[("POST", "/auth/v1/admin/migrations/abort")] = (
            StubResponse(
                {"error": {"code": "CONFLICT",
                           "message": "No apply is running for the "
                                      "'auth' migration scope"}},
                status_code=409,
            )
        )
        self._post("/auth/migrations/auth/abort")
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("No apply is running", page)

    def test_abort_button_absent_when_not_running(self):
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertNotIn('action="/auth/migrations/auth/abort"', page)


class TestRollback(MigrationsViewTestCase):
    """POST rollback relays revision and from_backup."""

    def test_rollback_relayed(self):
        self.wire["auth"].handlers[
            ("POST", "/auth/v1/admin/migrations/rollback")
        ] = StubResponse({"rolled_back": "010"})
        response = self._post("/auth/migrations/auth/rollback", revision="010")
        self.assertEqual(response.status_code, 302)
        _, _, _, body = self._wire_call(
            self.wire["auth"], "POST", "/auth/v1/admin/migrations/rollback",
        )
        self.assertEqual(body, {"revision": "010"})
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Rolled back 010", page)

    def test_rollback_with_backup_envelope(self):
        self.wire["auth"].handlers[
            ("POST", "/auth/v1/admin/migrations/rollback")
        ] = StubResponse({"rolled_back": "005"})
        self._post(
            "/auth/migrations/auth/rollback", revision="005",
            from_backup="bucket://campus-backups/migrations/005/predump",
        )
        _, _, _, body = self._wire_call(
            self.wire["auth"], "POST", "/auth/v1/admin/migrations/rollback",
        )
        self.assertEqual(body, {
            "revision": "005",
            "from_backup": "bucket://campus-backups/migrations/005/predump",
        })

    def test_rollback_failure_flashes(self):
        self.wire["auth"].handlers[
            ("POST", "/auth/v1/admin/migrations/rollback")
        ] = StubResponse(
            {"error": {"code": "CONFLICT",
                       "message": "Rollback of 010 failed — 009 is not "
                                  "declared rollback-capable"}},
            status_code=409,
        )
        self._post("/auth/migrations/auth/rollback", revision="010")
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Rollback of 010 failed", page)

    def test_rollback_requires_revision(self):
        response = self._post("/auth/migrations/auth/rollback")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Rollback needs a revision",
                      response.get_data(as_text=True))


class TestStatusEndpoint(MigrationsViewTestCase):
    """The polling endpoint's gating and shape."""

    def test_status_json_shape(self):
        self.wire["auth"].handlers = {
            ("GET", "/auth/v1/admin/migrations"):
                StubResponse(AUTH_RUNNING),
        }
        response = self.client.get("/auth/migrations/auth")
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.get_data(as_text=True))
        self.assertEqual(body["service"], "auth")
        self.assertIs(body["running"], True)
        self.assertIsNone(body["error"])
        self.assertEqual(body["body"]["service"], "auth")

    def test_status_json_error_shape(self):
        response = self.client.get("/auth/migrations/api")
        body = json.loads(response.get_data(as_text=True))
        self.assertIsNone(body["running"])
        self.assertIn("token scopes", body["error"])

    def test_unknown_service_404s(self):
        self.assertEqual(
            self.client.get("/auth/migrations/nope").status_code, 404,
        )

    def test_anonymous_gets_401_json(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), user_id=None,
        )
        self.addCleanup(cleanup)
        response = self.client.get("/auth/migrations/auth")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            json.loads(response.get_data(as_text=True))["error"],
            "Authentication required. Visit /login.",
        )

    def test_user_without_campus_auth_authority_gets_403_json(self):
        # Neither a platform operator nor an access-grant holder: the
        # Admin section's gate refuses, and the JSON endpoint says so.
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), members=("someone@nyjc.edu.sg",),
        )
        self.addCleanup(cleanup)
        response = self.client.get("/auth/migrations/auth")
        self.assertEqual(response.status_code, 403)
        self.assertIn(
            "access grant",
            json.loads(response.get_data(as_text=True))["error"],
        )

    def test_page_redirects_anonymous_to_sign_in(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), user_id=None,
        )
        self.addCleanup(cleanup)
        response = self.client.get("/auth/migrations")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


class TestTransportFailures(MigrationsViewTestCase):
    """Transport-level failures render as section errors."""

    def test_unreachable_service_shows_error_text(self):
        class Exploding:
            """A wire double whose transport always fails."""

            def get(self, path, query=None):  # noqa: ARG002 - stub shape
                raise RuntimeError("connection refused")

            def post(self, path, json=None):  # noqa: ARG002 - stub shape
                raise RuntimeError("connection refused")

        self.wire["auth"] = Exploding()
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("API Integration Issue", page)
        self.assertIn("connection refused", page)


class TestLevelsFromScopes(unittest.TestCase):
    """The scope-to-affordance mapping (pure function)."""

    def test_hierarchy(self):
        from apps.admin.migrations.client import levels_from_scopes

        self.assertEqual(
            levels_from_scopes(["migrations:read"]),
            {"read": True, "write": False, "admin": False},
        )
        self.assertEqual(
            levels_from_scopes(["migrations:write"]),
            {"read": True, "write": True, "admin": False},
        )
        self.assertEqual(
            levels_from_scopes(["migrations:admin"]),
            {"read": True, "write": True, "admin": True},
        )

    def test_ignores_other_vocabularies_and_junk(self):
        from apps.admin.migrations.client import levels_from_scopes

        self.assertEqual(
            levels_from_scopes(
                ["vaults:admin", "migrations:bogus", "migrations"]
            ),
            {"read": False, "write": False, "admin": False},
        )

    def test_none_and_empty_fail_closed(self):
        from apps.admin.migrations.client import levels_from_scopes

        self.assertEqual(
            levels_from_scopes(None),
            {"read": False, "write": False, "admin": False},
        )
        self.assertEqual(
            levels_from_scopes([]),
            {"read": False, "write": False, "admin": False},
        )


class TestCapabilityPanel(MigrationsViewTestCase):
    """The two-leg capability panel: the grant leg via the grant store.

    The authz caches are process-wide; each test clears them so probe
    answers never leak between tests.
    """

    def _reset_authz_caches(self):
        from apps.admin import authz

        authz._grant_cache._entries.clear()
        authz._scopes_cache.clear()

    def setUp(self):
        super().setUp()
        self._reset_authz_caches()

    def test_grant_missing_blocks_with_reason(self):
        self.campus.auth.grants.check.return_value = False
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("holds no migrations grant", page)
        # No enable affordance: a step-up cannot manufacture a grant.
        self.assertNotIn("Enable migrations access", page)
        # The sidebar link renders disabled with the reason on hover.
        self.assertIn('class="sidebar-link active disabled"', page)
        self.assertIn("title=\"Your account holds no migrations grant", page)

    def test_grant_store_unreachable_renders_unknown_state(self):
        self.campus.auth.grants.check.side_effect = RuntimeError("grants down")
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("could not be reached", page)
        self.assertNotIn("Enable migrations access", page)
        self.assertIn('class="sidebar-link active disabled"', page)

    def test_grant_present_without_scopes_offers_step_up(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), scopes=("campus.profile",),
        )
        self.addCleanup(cleanup)
        self._reset_authz_caches()
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertIn("Enable migrations access", page)
        self.assertIn('href="/auth/migrations/enable"', page)
        # The sidebar link stays clickable in this state (the view
        # offers the fix), so no disabled class.
        self.assertNotIn('class="sidebar-link active disabled"', page)

    def test_grant_and_scopes_present_render_no_panel(self):
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertNotIn("Enable migrations access", page)
        self.assertNotIn("holds no migrations grant", page)
        self.assertNotIn('class="sidebar-link active disabled"', page)

    def test_subset_row_grant_enables_despite_no_vocabulary_row(self):
        # campus-admin#86 regression: the migrations vocabulary has no
        # vocabulary-level rows (#1168) — every row names its service —
        # so a store-faithful check answers False to the old
        # resource_id-less probe and True only for per-service subsets.
        def faithful_check(**kwargs):
            return kwargs.get("resource_id") in ("auth", "api", "audit")

        self.campus.auth.grants.check.side_effect = faithful_check
        page = self.client.get("/auth/migrations").get_data(as_text=True)
        self.assertNotIn("holds no migrations grant", page)
        self.assertNotIn('class="sidebar-link active disabled"', page)
        # Every probe call must scope to a per-service subset.
        for call in self.campus.auth.grants.check.call_args_list:
            self.assertIn(call.kwargs.get("resource_id"),
                          ("auth", "api", "audit"))


class TestStepUp(MigrationsViewTestCase):
    """GET /auth/migrations/enable: the capability-acquisition hop."""

    def setUp(self):
        super().setUp()
        from apps.admin import authz

        authz._grant_cache._entries.clear()
        authz._scopes_cache.clear()

    def _capture_start_login(self):
        import flask

        captured = {}

        def fake_start_login(_campus, *, target, scopes):
            captured["target"] = target
            captured["scopes"] = list(scopes)
            with self.app.test_request_context():
                return flask.redirect("https://auth.example.test/authorize")

        return captured, fake_start_login

    def test_scopes_present_redirects_straight_back(self):
        with mock.patch("campus.flask_campus.context.start_login") as m:
            resp = self.client.get("/auth/migrations/enable")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/auth/migrations")
        m.assert_not_called()

    def test_scopes_missing_reauths_with_migration_vocabulary(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), scopes=("campus.profile",),
        )
        self.addCleanup(cleanup)
        public_url = mock.patch.dict(
            os.environ, {"PUBLIC_URL": "https://portal.example.test"},
        )
        public_url.start()
        self.addCleanup(public_url.stop)
        captured, fake = self._capture_start_login()
        with mock.patch(
            "campus.flask_campus.context.start_login", side_effect=fake,
        ):
            resp = self.client.get("/auth/migrations/enable")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("auth.example.test", resp.headers["Location"])
        from apps.admin import MIGRATION_SCOPES

        self.assertEqual(captured["scopes"], list(MIGRATION_SCOPES))
        self.assertTrue(captured["target"].endswith("/finalize_login"))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("login_next"), "/auth/migrations")

    def test_cap_rejection_flashes_reason_instead_of_500(self):
        (self.app, self.client, _campus, cleanup) = make_admin_app(
            wire_clients=make_wire(), scopes=("campus.profile",),
        )
        self.addCleanup(cleanup)
        public_url = mock.patch.dict(
            os.environ, {"PUBLIC_URL": "https://portal.example.test"},
        )
        public_url.start()
        self.addCleanup(public_url.stop)
        with mock.patch(
            "campus.flask_campus.context.start_login",
            side_effect=campus_python.errors.BadRequestError(
                "Requested scopes not allowed for this client: "
                "migrations:read",
            ),
        ):
            resp = self.client.get(
                "/auth/migrations/enable", follow_redirects=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("allowed_scopes cap", resp.get_data(as_text=True))


class TestLoginScopesSplit(unittest.TestCase):
    """Sign-in requests the structural baseline only (#Nov-2026 outage).

    The full vocabulary used to ride every login, so one allowed_scopes
    cap entry lagging a new feature scope 500ed every sign-in at once.
    """

    def test_login_requests_baseline_only(self):
        from apps.admin import LOGIN_SCOPES_BASE, MIGRATION_SCOPES

        self.assertEqual(LOGIN_SCOPES_BASE, ["campus.profile", "users:read"])
        self.assertEqual(
            MIGRATION_SCOPES,
            ["migrations:read", "migrations:write", "migrations:admin"],
        )
        # No overlap: sign-in never requests the feature vocabulary.
        self.assertFalse(set(LOGIN_SCOPES_BASE) & set(MIGRATION_SCOPES))



if __name__ == "__main__":
    unittest.main()
