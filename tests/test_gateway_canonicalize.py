"""Origin-host canonicalization (issue #112; the classroom #175/#176
pattern).

After PUBLIC_URL flips to the gateway origin, links and bookmarks on
the old origin domain would dead-end there: the origin can no longer
hold a session (the OAuth callback is built from PUBLIC_URL, so the
cookie lands on the gateway host). CANONICALIZE_ORIGIN_HOST=1 bounces
direct origin hits to their gateway-namespaced shape — PUBLIC_URL +
/admin + path; admin's origin paths are root-shaped post-#112, so the
namespace is added exactly once (never double-prefixed). The flag is
inert until set, and while PUBLIC_URL still names the origin the
host-equality guard keeps it a no-op (no pre-flip loop with the #110
forward). 302, never 301/308: the redirect must not outlive the
cutover.
"""

import os
import unittest
from unittest import mock

from tests.support import make_admin_app

ORIGIN = "https://campus-admin.up.railway.app"
ORIGIN_HOST = "campus-admin.up.railway.app"
GATEWAY_HOST = "campus.nyjc.app"


def via_gateway(client, path, host=GATEWAY_HOST):
    """A request the way campus-gateway forwards it."""
    return client.get(
        path,
        headers={"X-Forwarded-Prefix": "/admin", "Host": host},
    )


class CanonicalizeOriginHostTest(unittest.TestCase):
    def build(self, public_url=ORIGIN, canonicalize="1"):
        env = {}
        if public_url is not None:
            env["PUBLIC_URL"] = public_url
        if canonicalize is not None:
            env["CANONICALIZE_ORIGIN_HOST"] = canonicalize
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("PUBLIC_URL", "CANONICALIZE_ORIGIN_HOST"):
            if var not in env:
                os.environ.pop(var, None)
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        return client

    def test_flag_off_by_default_origin_hits_are_served(self):
        client = self.build(canonicalize=None)
        response = client.get("/auth/dashboard", headers={"Host": ORIGIN_HOST})
        self.assertEqual(response.status_code, 200)

    def test_flag_on_bounces_origin_content_to_the_gateway_shape(self):
        client = self.build(public_url=f"https://{GATEWAY_HOST}")
        response = client.get(
            "/auth/dashboard?tab=1", headers={"Host": ORIGIN_HOST},
        )
        self.assertEqual(response.status_code, 302)
        # Namespace + root-shaped path, exactly once — never
        # /admin/auth + /auth double-prefixed.
        self.assertEqual(
            response.headers["Location"],
            f"https://{GATEWAY_HOST}/admin/auth/dashboard?tab=1",
        )

    def test_flag_on_bounces_origin_auth_trio_to_the_namespace(self):
        client = self.build(public_url=f"https://{GATEWAY_HOST}")
        response = client.get("/login", headers={"Host": ORIGIN_HOST})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"https://{GATEWAY_HOST}/admin/login",
        )

    def test_flag_on_bounces_origin_section_roots_whole(self):
        client = self.build(public_url=f"https://{GATEWAY_HOST}")
        for path in ("/audit/traces", "/api/circles"):
            response = client.get(path, headers={"Host": ORIGIN_HOST})
            self.assertEqual(response.status_code, 302, path)
            self.assertEqual(
                response.headers["Location"],
                f"https://{GATEWAY_HOST}/admin{path}",
                path,
            )

    def test_flag_on_origin_home_still_bounces(self):
        # Post-flip the origin has no session to serve; the home joins
        # the bounce so the gateway namespace is the single entry point.
        client = self.build(public_url=f"https://{GATEWAY_HOST}")
        response = client.get("/", headers={"Host": ORIGIN_HOST})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], f"https://{GATEWAY_HOST}/admin/"
        )

    def test_flag_on_with_public_url_still_the_origin_is_a_noop(self):
        # Pre-flip (PUBLIC_URL = origin): the host-equality guard keeps
        # the flag inert — otherwise it would loop against the #110
        # forward on the gateway.
        client = self.build(public_url=ORIGIN)
        response = client.get("/auth/dashboard", headers={"Host": ORIGIN_HOST})
        self.assertEqual(response.status_code, 200)
        response = via_gateway(client, "/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], f"{ORIGIN}/dashboard"
        )

    def test_flag_on_gateway_hosted_requests_keep_the_forward(self):
        # PUBLIC_URL = gateway (post-flip): gateway-mounted requests
        # answer in place (hosts equal); origin hits bounce.
        client = self.build(public_url=f"https://{GATEWAY_HOST}")
        response = via_gateway(client, "/auth/dashboard")
        self.assertEqual(response.status_code, 200)
        response = client.get(
            "/auth/dashboard", headers={"Host": ORIGIN_HOST},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"https://{GATEWAY_HOST}/admin/auth/dashboard",
        )

    def test_flag_on_fails_open_without_public_url(self):
        # canonical_origin raises when PUBLIC_URL is unset (local dev);
        # the hook fails open and the request is served as-is.
        client = self.build(public_url=None)
        os.environ.pop("PUBLIC_URL", None)
        response = client.get("/auth/dashboard", headers={"Host": ORIGIN_HOST})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
