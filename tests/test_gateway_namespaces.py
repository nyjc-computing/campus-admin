"""Gateway-namespace wiring (#97 / #95, campus umbrella campus#1295).

The portal's surfaces live under the gateway-dispatchable
/admin/<platform> namespaces: auth-ops under /admin/auth, the campus.api
resource views under /admin/api, the audit UI under /admin/audit, the
portal home at /admin, and the flask_campus login blueprint under
/admin (login_prefix). The old root-level and pre-remap paths redirect
so bookmarks survive the move.
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

    def test_login_blueprint_mounts_under_admin(self):
        self.assertIn("/admin/login", [r.rule for r in self.app.url_map.iter_rules()])

    def test_admin_serves_the_portal_home(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Campus Auth Portal", response.data)

    def test_root_redirects_to_the_portal_home(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin"))

    def test_sign_in_folds_into_the_namespaced_login(self):
        response = self.client.get("/sign-in")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

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
