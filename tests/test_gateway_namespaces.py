"""Origin-shape route wiring (issues #97 / #112, gateway umbrella
campus#1295).

Since #112 every content surface's canonical rule is its ROOT shape:
the admin section at /auth/*, the campus.api resource views under
/api/*, the audit UI under /audit/*. Behind campus-gateway the
X-Forwarded-Prefix strips to those exact shapes (PrefixFix, fc#50), so
one rule serves both hosts; the literal /admin/auth/*, /admin/audit/*
and /admin/api/* paths live on as redirect endpoints so stale pages and
old bookmarks survive the swap. The old /admin/<page> shortcuts and the
root-level catch-alls keep redirecting, now onto the canonical shapes.
"""

import os
import unittest
from unittest import mock

import flask

from apps.admin.audit import client as audit_client
from tests.support import StubJsonClient, make_admin_app


def follow(client, path, max_hops=8):
    """Follow redirects with a hard hop cap (loop guard).

    Returns (final response, hops taken). A path that exceeds the cap
    fails the test — the #112 loop hazard (an endpoint holding both a
    literal and a root rule) manifests exactly as a redirect cycle.
    """
    response = client.get(path)
    hops = 1
    while 300 <= response.status_code < 400 and hops < max_hops:
        location = response.headers["Location"]
        # Absolute locations (the forward/canonicalize hooks) resolve
        # against the test client's host; strip to the path for the
        # next hop. Off-site hops are returned untouched.
        if location.startswith(("http://", "https://")):
            path = location.split("/", 3)
            if len(path) < 4:
                break
            path = "/" + path[3]
        else:
            path = location
        response = client.get(path)
        hops += 1
    return response, hops


class CanonicalShapesTest(unittest.TestCase):
    """Every section answers at its root shape on the origin."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"AUDIT_ADMINS": "staff@nyjc.edu.sg"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)

    def test_admin_section_answers_at_root(self):
        for path in (
            "/auth/dashboard", "/auth/clients", "/auth/users",
            "/auth/sessions", "/auth/vaults", "/auth/credentials",
            "/auth/grants", "/auth/migrations",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        # /auth/enable is the step-up re-auth: for the harness operator
        # there is nothing to enable, so it answers a redirect (to the
        # dashboard), never a 404 — the route resolves.
        response = self.client.get("/auth/enable")
        self.assertEqual(response.status_code, 302)

    def test_audit_section_answers_at_root(self):
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/audit/traces"))
        for path in ("/audit/traces", "/audit/keys", "/audit/api/traces"):
            self.assertNotEqual(self.client.get(path).status_code, 404, path)

    def test_resources_section_answers_at_root(self):
        # /api/ is the landing: it hops to the first family the user may
        # read (the audit section's pattern). The family pages answer
        # 200 outright.
        response = self.client.get("/api/")
        self.assertEqual(response.status_code, 302)
        for path in ("/api/circles", "/api/venues", "/api/files"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_url_for_emits_the_root_shape(self):
        # The canonical rule is the ONLY rule on each endpoint, so
        # url_for emits the root path — the loop-hazard guarantee that
        # aliases redirect INTO the canonical shapes and never back.
        with self.app.test_request_context("/"):
            for endpoint, path in (
                ("dashboard", "/auth/dashboard"),
                ("clients", "/auth/clients"),
                ("users", "/auth/users"),
                ("admin_grants_ui.grants_page", "/auth/grants"),
                ("admin_migrations_ui.migrations_page", "/auth/migrations"),
                ("audit_ui.traces", "/audit/traces"),
                ("resources.circles", "/api/circles"),
            ):
                self.assertEqual(
                    flask.url_for(endpoint), path, endpoint,
                )


class LegacyRedirectsTest(unittest.TestCase):
    """The literal /admin/* shapes redirect onto the canonical ones,
    with a bounded hop count (never a loop)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"AUDIT_ADMINS": "staff@nyjc.edu.sg"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        # The keys page (a hop target below) renders against the audit
        # wire client; a recording stub keeps the chain green without a
        # service (the page's own behavior is test_audit_keys's brief).
        restore = audit_client.json_client_factory
        audit_client.json_client_factory = lambda: StubJsonClient()
        self.addCleanup(
            lambda: setattr(audit_client, "json_client_factory", restore)
        )

    def test_literal_admin_auth_paths_redirect_to_the_root_shapes(self):
        cases = [
            ("/admin/auth/dashboard", "/auth/dashboard"),
            ("/admin/auth/clients", "/auth/clients"),
            ("/admin/auth/users", "/auth/users"),
            ("/admin/auth/sessions", "/auth/sessions"),
            ("/admin/auth/vaults", "/auth/vaults"),
            ("/admin/auth/credentials", "/auth/credentials"),
            ("/admin/auth/enable", "/auth/enable"),
            ("/admin/auth/grants", "/auth/grants"),
            ("/admin/auth/migrations", "/auth/migrations"),
            ("/admin/auth/migrations/auth", "/auth/migrations/auth"),
            ("/admin/audit/traces", "/audit/traces"),
            ("/admin/api/circles", "/api/circles"),
        ]
        for old, new in cases:
            response = self.client.get(old)
            self.assertEqual(response.status_code, 302, old)
            self.assertTrue(
                response.headers["Location"].endswith(new), f"{old} → {new}"
            )

    def test_literal_post_aliases_preserve_the_method(self):
        # 307 on POST aliases: a form post from a stale page survives
        # the hop with method and body intact.
        response = self.client.post("/admin/auth/users", data={})
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["Location"].endswith("/auth/users"))
        response = self.client.post("/admin/api/circles", data={})
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["Location"].endswith("/api/circles"))

    def test_legacy_shortcuts_now_target_the_root_shapes(self):
        for old, new in [
            ("/admin/dashboard", "/auth/dashboard"),
            ("/admin/clients", "/auth/clients"),
            ("/admin/users", "/auth/users"),
            ("/admin/sessions", "/auth/sessions"),
            ("/admin/vaults", "/auth/vaults"),
            ("/admin/credentials", "/auth/credentials"),
            ("/admin/enable", "/auth/enable"),
            ("/admin/migrations", "/auth/migrations"),
            ("/admin/grants", "/auth/grants"),
        ]:
            response = self.client.get(old)
            self.assertEqual(response.status_code, 302, old)
            self.assertTrue(
                response.headers["Location"].endswith(new), f"{old} → {new}"
            )

    def test_gateway_shortcuts_reach_the_sections_at_both_shapes(self):
        # /admin/<page> behind the gateway strips to /<page> (PrefixFix);
        # the shortcut rules exist at both shapes so the gateway's
        # /admin/dashboard entry (the campus-landing card link) and its
        # section shortcuts never 404 — they redirect, bounded, onto
        # the canonical pages.
        for path, canonical in [
            ("/dashboard", "/auth/dashboard"),
            ("/users", "/auth/users"),
            ("/clients", "/auth/clients"),
            ("/sessions", "/auth/sessions"),
            ("/vaults", "/auth/vaults"),
            ("/credentials", "/auth/credentials"),
            ("/enable", "/auth/enable"),
            ("/migrations", "/auth/migrations"),
            ("/grants", "/auth/grants"),
        ]:
            origin_response = self.client.get(path)
            self.assertEqual(origin_response.status_code, 302, path)
            self.assertTrue(
                origin_response.headers["Location"].endswith(canonical),
                f"{path} → {canonical}",
            )
            gateway_response = self.client.get(
                f"/admin{path}",
                headers={"X-Forwarded-Prefix": "/admin"},
            )
            self.assertEqual(gateway_response.status_code, 302, f"/admin{path}")
            self.assertTrue(
                gateway_response.headers["Location"].endswith(canonical),
                f"/admin{path} → {canonical}",
            )

    def test_old_section_roots_map_onto_the_canonical_namespaces(self):
        # /resources/<family> maps to /api/<family> (the section moved
        # namespaces twice: /resources → /admin/api → /api; family
        # paths unchanged).
        response = self.client.get("/resources/circles")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/api/circles"))

    def test_every_legacy_path_terminates_within_the_hop_cap(self):
        # The #112 loop hazard, exercised end-to-end: no legacy path may
        # redirect-cycle. Each chain must land on a real route (200) or
        # a gate's sign-in/403 answer — never exhaust the cap.
        legacy_paths = [
            "/admin/dashboard", "/admin/clients", "/admin/users",
            "/admin/sessions", "/admin/vaults", "/admin/credentials",
            "/admin/enable", "/admin/migrations", "/admin/grants",
            "/admin/auth/dashboard", "/admin/auth/clients",
            "/admin/auth/users", "/admin/auth/sessions",
            "/admin/auth/vaults", "/admin/auth/credentials",
            "/admin/auth/enable", "/admin/auth/grants",
            "/admin/auth/migrations", "/admin/auth/migrations/auth",
            "/admin/audit", "/admin/audit/", "/admin/audit/traces",
            "/admin/audit/keys", "/admin/api", "/admin/api/",
            "/admin/api/circles", "/audit", "/audit/traces",
            "/audit/unknown-junk", "/resources", "/resources/circles",
        ]
        for path in legacy_paths:
            response, hops = follow(self.client, path)
            self.assertLess(hops, 8, f"{path} exceeded the hop cap")
            self.assertLess(
                response.status_code, 500, f"{path} errored mid-chain"
            )

    def test_unknown_audit_path_lands_on_the_real_landing(self):
        # /audit/<unknown> must NOT redirect to a mapped /audit/<unknown>
        # — that is the same catch-all: an unbounded self-loop. It lands
        # on the section's real landing instead.
        response = self.client.get("/audit/unknown-junk")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/audit/traces"))
        landing, hops = follow(self.client, "/audit/unknown-junk")
        self.assertEqual(landing.status_code, 200)
        self.assertLess(hops, 8)

    def test_remapped_surfaces_answer_at_their_new_homes(self):
        # The blueprint's static assets are public; the gated pages
        # resolve and serve the harness's signed-in user (the audit
        # landing redirects a signed-in user to the trace list) —
        # never a 404: the routes resolve.
        response = self.client.get("/audit/static/js/main.js")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/audit/traces")
        )
        for path in ("/auth/dashboard", "/api/circles"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
