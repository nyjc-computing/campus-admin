"""Rendered-HTML sweep: no root-absolute URL literals survive rendering
(issue #112, the CI-enforced regression guard; the classroom #175
pattern).

The blocker class: hardcoded host-absolute literals —
href="/auth/dashboard", action="/admin/api/circles",
src="/admin/audit/static/js/main.js" — bypass url_for, so behind
campus-gateway they hit the gateway root, where /auth/*, /api/* and
/audit/* belong to other services (the gateway routes only /admin/* to
this app). Every page must compose its URLs from url_for (or the
window.CAMPUS_SCRIPT_ROOT global the JS builds from), which carries the
serving namespace automatically.

This test renders every main page through the REAL app factory in both
shapes:

  origin  — script_root empty; section URLs must degrade to the root
            shapes (/auth/..., /audit/..., /api/...) and no namespaced
            /admin/... section link may appear (the brand's portal-home
            link is the one deliberate exception: /admin is a real
            origin route, campus-admin#97).
  gateway — X-Forwarded-Prefix -> SCRIPT_NAME via PrefixFix; every
            emitted href/action/src must carry the /admin namespace.

and scans the rendered HTML plus the static JS for surviving literals.
Run:  python -m unittest tests.test_no_root_absolute_urls
"""

import os
import re
import unittest
from unittest import mock

from apps.admin.audit import client as audit_client
from tests.support import StubJsonClient, make_admin_app

GATEWAY_HOST = "campus.nyjc.app"
PREFIX = {"X-Forwarded-Prefix": "/admin", "Host": GATEWAY_HOST}

# Every main page, in its origin-canonical (stripped) shape.
PAGES = [
    "/",
    "/admin/",
    "/auth/dashboard",
    "/auth/clients",
    "/auth/users",
    "/auth/sessions",
    "/auth/vaults?label=ops",
    "/auth/credentials",
    "/auth/grants",
    "/auth/migrations",
    "/audit/traces",
    "/audit/traces/" + "a" * 32,
    "/audit/keys",
    "/api/circles",
    "/api/venues",
    "/api/timetable",
    "/api/assignments",
    "/api/submissions",
    "/api/subjcombi",
    "/api/files",
    "/api/hostedapps",
]

# Root-absolute emitted URLs allowed on the origin: the portal-home
# brand link (#97) and same-page fragments / anchors.
ORIGIN_ALLOWED = ('href="/admin/"', 'href="#')

HREF_RE = re.compile(r'(?:href|src|action)="([^"]*)"')


def rendered_urls(page: str) -> list[str]:
    return HREF_RE.findall(page)


class NoRootAbsoluteUrlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        patcher = mock.patch.dict(
            os.environ,
            {
                "PUBLIC_URL": f"https://{GATEWAY_HOST}",
                "AUDIT_ADMINS": "staff@nyjc.edu.sg",
            },
        )
        patcher.start()
        cls._patcher = patcher
        cls.app, cls.client, cls.campus, cleanup = make_admin_app()
        cls._cleanup = cleanup
        restore = audit_client.json_client_factory
        audit_client.json_client_factory = lambda: StubJsonClient()
        cls._restore = restore

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()
        cls._patcher.stop()
        audit_client.json_client_factory = cls._restore

    def test_gateway_render_carries_the_namespace_everywhere(self):
        for page_path in PAGES:
            response = self.client.get(page_path, headers=PREFIX)
            self.assertEqual(
                response.status_code, 200,
                f"{page_path}: expected 200 behind the gateway",
            )
            page = response.get_data(as_text=True)
            for url in rendered_urls(page):
                if not url or url.startswith(("#", "http://", "https://")):
                    continue
                # /logout rides url_for and is namespace-carried; a bare
                # root-absolute ANYTHING else must be /admin/-prefixed.
                self.assertTrue(
                    url.startswith("/admin/"),
                    f"{page_path}: root-absolute literal {url!r} would "
                    "hit the gateway root, where that prefix belongs to "
                    "another service",
                )

    def test_origin_render_degrades_to_root_shapes(self):
        for page_path in PAGES:
            response = self.client.get(page_path)
            self.assertEqual(
                response.status_code, 200,
                f"{page_path}: expected 200 on the origin",
            )
            page = response.get_data(as_text=True)
            for url in rendered_urls(page):
                if url.startswith(ORIGIN_ALLOWED):
                    continue
                if url.startswith(("#", "http://", "https://")):
                    continue
                # No namespaced section URL may survive an origin
                # render — the namespace is the gateway's to declare.
                self.assertFalse(
                    url.startswith("/admin/auth")
                    or url.startswith("/admin/audit")
                    or url.startswith("/admin/api")
                    or url.startswith("/admin/static")
                    or url.startswith("/admin/login"),
                    f"{page_path}: namespaced literal {url!r} leaked "
                    "into an origin render",
                )

    def test_static_js_composes_urls_from_the_script_root(self):
        # The client-side JS builds page/data URLs at runtime; any
        # root-absolute section literal there hits the gateway root.
        js_files = [
            "apps/admin/static/js/main.js",
            "apps/admin/static/js/migrations.js",
            "apps/admin/audit/static/js/main.js",
            "apps/admin/audit/static/js/traces.js",
            "apps/admin/audit/static/js/trace.js",
        ]
        # A root-absolute section path inside quotes, anywhere outside a
        # comment line: what scriptRoot() composition replaced.
        literal_re = re.compile(
            "['\"]/(?:admin/)?(?:auth|audit|api|static)(?:/|['\"])"
        )
        for rel in js_files:
            with open(rel, encoding="utf-8") as fh:
                source = fh.read()
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("//", "*", "/*")):
                    continue  # comments may name paths for humans
                if "scriptRoot()" in stripped:
                    continue  # already composed from the namespace
                match = literal_re.search(stripped)
                self.assertIsNone(
                    match,
                    f"{rel}: root-absolute literal in {stripped!r} — "
                    "compose from scriptRoot()/CAMPUS_SCRIPT_ROOT",
                )

    def test_script_root_global_renders_in_both_shapes(self):
        gateway = self.client.get("/auth/dashboard", headers=PREFIX)
        self.assertIn(
            'window.CAMPUS_SCRIPT_ROOT = "/admin"',
            gateway.get_data(as_text=True),
        )
        origin = self.client.get("/auth/dashboard")
        self.assertIn(
            'window.CAMPUS_SCRIPT_ROOT = ""',
            origin.get_data(as_text=True),
        )


if __name__ == "__main__":
    unittest.main()
