"""Gateway-emulated section reachability (issue #112).

campus-gateway routes /admin/* to this app with X-Forwarded-Prefix:
/admin, and flask-campus PrefixFix (fc#50) moves that into SCRIPT_NAME —
so behind the gateway the app matches prefix-stripped paths. Pre-#112
that stripped shape was unrouted (the whole portal 404ed post-flip);
these tests emulate the gateway (X-Forwarded-Prefix header + gateway
Host) and assert every section route lands on a real answer: 200 where
the harness grants authority, a sign-in redirect when signed out —
NEVER a 404.
"""

import os
import unittest
from unittest import mock

from apps.admin.audit import client as audit_client
from tests.support import StubJsonClient, make_admin_app

GATEWAY_HOST = "campus.nyjc.app"
PREFIX = {"X-Forwarded-Prefix": "/admin", "Host": GATEWAY_HOST}

# Every public section URL the portal answers. Signed in, every route
# must reach a real page behind the gateway.
SIGNED_IN_SECTIONS = [
    "/admin/",
    "/admin/auth/dashboard",
    "/admin/auth/clients",
    "/admin/auth/users",
    "/admin/auth/sessions",
    "/admin/auth/vaults",
    "/admin/auth/credentials",
    "/admin/auth/grants",
    "/admin/auth/migrations",
    "/admin/audit/traces",
    "/admin/audit/keys",
    "/admin/api/circles",
    "/admin/api/venues",
    "/admin/api/files",
    "/admin/static/css/style.css",
]


def via_gateway(client, path, method="get", **kwargs):
    """A request the way campus-gateway forwards it."""
    return getattr(client, method)(path, headers=PREFIX, **kwargs)


class GatewaySectionReachabilityTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(
            os.environ,
            {
                "PUBLIC_URL": f"https://{GATEWAY_HOST}",
                "AUDIT_ADMINS": "staff@nyjc.edu.sg",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        anonymous_app, self.anonymous, _, anon_cleanup = make_admin_app(
            user_id=None,
        )
        self.addCleanup(anon_cleanup)
        # The keys page renders against the audit wire client; a
        # recording stub keeps the section sweep green without a service
        # (the page's own behavior is test_audit_keys's brief).
        restore = audit_client.json_client_factory
        audit_client.json_client_factory = lambda: StubJsonClient()
        self.addCleanup(
            lambda: setattr(audit_client, "json_client_factory", restore)
        )

    def test_every_section_reaches_a_real_page_behind_the_gateway(self):
        for path in SIGNED_IN_SECTIONS:
            response = via_gateway(self.client, path)
            self.assertNotEqual(
                response.status_code, 404, f"{path} 404ed behind the gateway"
            )
            self.assertEqual(
                response.status_code, 200,
                f"{path}: expected 200 behind the gateway",
            )
        # /admin/auth/enable is the step-up re-auth: for the harness
        # operator there is nothing to enable, so it answers a redirect
        # (to the dashboard) — the route resolves, never a 404.
        response = via_gateway(self.client, "/admin/auth/enable")
        self.assertEqual(response.status_code, 302)
        # /admin/login resolves (the login blueprint mounts at root;
        # PrefixFix namespaces it). It cannot complete a real OAuth
        # start against the harness's SDK double, so only the 404
        # guarantee — the #112 blocker class — is asserted here.
        response = via_gateway(self.client, "/admin/login")
        self.assertNotEqual(response.status_code, 404)
        # The section landings (/admin/audit/, /admin/api/) hop once to
        # their first resource view — reachable, just not 200 outright.
        for landing in ("/admin/audit/", "/admin/api/"):
            response = via_gateway(self.client, landing)
            self.assertEqual(response.status_code, 302, landing)
            self.assertTrue(
                response.headers["Location"].startswith("/admin/"),
                f"{landing}: hop target must stay namespaced",
            )

    def test_stripped_paths_route_onto_the_canonical_views(self):
        # The gateway-emulated request for /admin/auth/dashboard lands
        # directly on the dashboard render (PrefixFix stripped /admin;
        # the canonical rule matched) — no redirect hop at all.
        response = via_gateway(self.client, "/admin/auth/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dashboard", response.data)

    def test_signed_out_sections_redirect_to_the_namespaced_login(self):
        # A signed-out visitor gets the login redirect — and the login
        # URL itself must be namespaced (root-absolute /login on the
        # gateway host is unrouted there).
        for path in (
            "/admin/auth/dashboard", "/admin/audit/traces",
            "/admin/api/circles",
        ):
            response = via_gateway(self.anonymous, path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/admin/login", response.headers["Location"], path)
        # The home is public: signed out it renders (with the sign-in
        # affordance), it must not bounce.
        response = via_gateway(self.anonymous, "/admin/")
        self.assertEqual(response.status_code, 200)


class GatewayUrlEmissionTest(unittest.TestCase):
    """url_for behind the gateway must namespace every emitted URL.

    Root-absolute URLs in served pages resolve against the gateway
    host, where /auth/*, /audit/*, /api/* and /sign-in belong to other
    services or nothing at all — the exact #112 404 class.
    """

    def setUp(self):
        patcher = mock.patch.dict(
            os.environ,
            {
                "PUBLIC_URL": f"https://{GATEWAY_HOST}",
                "AUDIT_ADMINS": "staff@nyjc.edu.sg",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)

    def test_home_emits_only_namespaced_urls_behind_the_gateway(self):
        response = self.client.get("/", headers=PREFIX)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        # Static assets and page links ride the namespace...
        self.assertIn("/admin/static/css/style.css", page)
        self.assertIn('href="/admin/auth/dashboard"', page)
        # ...and no bare root-absolute section URL survives rendering.
        for literal in (
            'href="/auth/dashboard"', 'href="/sign-in"',
            'href="/static/css/style.css', 'href="/login"',
        ):
            self.assertNotIn(literal, page)

    def test_dashboard_emits_only_namespaced_urls_behind_the_gateway(self):
        response = self.client.get(
            "/auth/dashboard", headers=PREFIX,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('href="/admin/auth/clients"', page)
        self.assertNotIn('href="/auth/clients"', page)

    def test_audit_page_emits_only_namespaced_urls_behind_the_gateway(self):
        response = self.client.get("/audit/traces", headers=PREFIX)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('href="/admin/audit/keys"', page)
        self.assertIn("/admin/audit/static/js/", page)
        self.assertIn("window.CAMPUS_SCRIPT_ROOT = \"/admin\"", page)
        self.assertNotIn('href="/audit/keys"', page)

    def test_origin_render_emits_root_shapes(self):
        # On the origin (no prefix) the same pages degrade to the root
        # shapes — the single rule serves both hosts. The brand link is
        # the exception: the portal home lives at /admin on the origin
        # (campus-admin#97), which is a real origin route.
        response = self.client.get("/auth/dashboard")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('href="/auth/clients"', page)
        for namespaced in (
            'href="/admin/auth', 'href="/admin/audit', 'href="/admin/api',
            'href="/admin/static', 'href="/admin/login',
        ):
            self.assertNotIn(namespaced, page)


if __name__ == "__main__":
    unittest.main()
