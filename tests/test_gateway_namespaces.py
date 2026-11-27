"""Gateway-namespace wiring (#97 / #95, campus umbrella campus#1295).

The portal's surfaces live under the gateway-dispatchable
/admin/<platform> namespaces: auth-ops under /admin/auth, the campus.api
resource views under /admin/api, the audit UI under /admin/audit, and the
portal home at /admin. The flask_campus login blueprint mounts at root
and relies on the 0.2.8 PrefixFix middleware (fc#50) for the gateway's
/admin namespace (X-Forwarded-Prefix → SCRIPT_NAME). The old root-level
and pre-remap paths redirect so bookmarks survive the move.
"""

import unittest

from tests.support import make_admin_app


class NamespaceWiringTest(unittest.TestCase):
    def setUp(self):
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)

    def test_cookie_uses_the_fleet_scheme(self):
        self.assertEqual(
            self.app.config["SESSION_COOKIE_NAME"], "campus_admin_session"
        )

    def test_login_blueprint_mounts_at_root(self):
        # Root-first mount (fc#50): PrefixFix namespaces it behind the
        # gateway (X-Forwarded-Prefix → SCRIPT_NAME), so url_for emits
        # /admin/login there and /login on the origin.
        self.assertIn("/login", [r.rule for r in self.app.url_map.iter_rules()])

    def test_admin_serves_the_portal_home(self):
        # Both slash shapes: the gateway's nginx 301s /admin → /admin/
        # for the namespace root, so the slashed form must serve too
        # (otherwise the entry point 404s through the gateway).
        for path in ("/admin", "/admin/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(b"Campus Admin Portal", response.data)

    def test_root_redirects_to_the_portal_home(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.headers["Location"])

    def test_sign_in_folds_into_the_namespaced_login(self):
        response = self.client.get("/sign-in")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_old_auth_ops_paths_redirect_under_admin_auth(self):
        for old, new in [
            ("/admin/dashboard", "/admin/auth/dashboard"),
            ("/admin/clients", "/admin/auth/clients"),
            (
                "/admin/clients/uid-x/revoke_secret",
                "/admin/auth/clients/uid-x/revoke_secret",
            ),
            ("/admin/users", "/admin/auth/users"),
            ("/admin/sessions", "/admin/auth/sessions"),
            ("/admin/vaults", "/admin/auth/vaults"),
            ("/admin/credentials", "/admin/auth/credentials"),
            ("/admin/enable", "/admin/auth/enable"),
            ("/admin/migrations", "/admin/auth/migrations"),
            ("/admin/migrations/auth/apply", "/admin/auth/migrations/auth/apply"),
            ("/admin/grants", "/admin/auth/grants"),
        ]:
            response = self.client.get(old)
            self.assertEqual(response.status_code, 302, old)
            self.assertTrue(
                response.headers["Location"].endswith(new), f"{old} → {new}"
            )

    def test_old_audit_paths_redirect_under_admin_audit(self):
        response = self.client.get("/audit/traces")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/admin/audit/traces")
        )

    def test_old_resources_paths_redirect_under_admin_api(self):
        response = self.client.get("/resources/circles")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/api/circles"))

    def test_remapped_surfaces_answer_at_their_new_homes(self):
        # The blueprint's static assets are public; the gated pages
        # resolve and serve the harness's signed-in user (the audit
        # landing redirects a signed-in user to the trace list) —
        # never a 404: the routes resolve.
        response = self.client.get("/admin/audit/static/js/main.js")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/admin/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/admin/audit/traces")
        )
        for path in ("/admin/auth/dashboard", "/admin/api/circles"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
