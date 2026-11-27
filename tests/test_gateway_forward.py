"""Pre-cutover gateway forward (issue #110; the classroom #171/#173
pattern), post-#112 shape.

While PUBLIC_URL names the origin, a sign-in started behind
campus-gateway (/admin namespace, X-Forwarded-Prefix → SCRIPT_NAME via
PrefixFix) cannot complete: the OAuth callback is built from PUBLIC_URL
and the session cookie is set on that host, so no gateway-hosted page
holds a session. The forward sends gateway-mounted requests to the same
resource on the PUBLIC_URL origin — the path unchanged, since the #112
routing swap made every root-shaped path the origin-canonical one —
and stops once the two hosts agree. Origin traffic (no prefix header)
is never redirected.
"""

import os
import unittest
from unittest import mock

from tests.support import make_admin_app

ORIGIN = "https://campus-admin.up.railway.app"
GATEWAY_HOST = "campus.nyjc.app"


def via_gateway(client, path, host=GATEWAY_HOST):
    """A request the way campus-gateway forwards it."""
    return client.get(
        path,
        headers={"X-Forwarded-Prefix": "/admin", "Host": host},
    )


class GatewayForwardTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PUBLIC_URL": ORIGIN})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)

    def test_gateway_login_forwards_to_the_origin_root_shape(self):
        # 302, never 301/308: the redirect must not outlive the cutover.
        response = via_gateway(self.client, "/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{ORIGIN}/login")

    def test_gateway_finalize_login_forwards_with_query_intact(self):
        response = via_gateway(self.client, "/finalize_login?code=c&state=s")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"{ORIGIN}/finalize_login?code=c&state=s",
        )

    def test_gateway_content_forwards_at_the_root_shape(self):
        # The stripped path is the origin-canonical shape since #112:
        # the forward rides it unchanged (the old /admin re-prefixing
        # is gone — the origin's content no longer lives there).
        response = via_gateway(self.client, "/dashboard?tab=1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{ORIGIN}/dashboard?tab=1")

    def test_gateway_namespace_root_forwards_to_the_portal_home(self):
        # /admin/ on the gateway strips to "/" (PrefixFix); the origin's
        # portal home IS "/" (both shapes render the same home since
        # #97, and post-swap the forward no longer re-prefixes it).
        response = via_gateway(self.client, "/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{ORIGIN}/")

    def test_gateway_static_forwards_to_the_origin_root_shape(self):
        response = via_gateway(self.client, "/static/css/whatever.css")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"{ORIGIN}/static/css/whatever.css",
        )

    def test_no_forward_once_public_url_names_the_gateway_host(self):
        # The PUBLIC_URL flip turns the forward off by itself: the
        # namespace root then serves the portal home on the gateway.
        patcher = mock.patch.dict(
            os.environ, {"PUBLIC_URL": f"https://{GATEWAY_HOST}"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        response = via_gateway(self.client, "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Campus Admin Portal", response.data)

    def test_origin_requests_are_never_forwarded(self):
        # No X-Forwarded-Prefix: origin traffic is served where it
        # landed, whatever Host it carries.
        response = self.client.get("/", headers={"Host": GATEWAY_HOST})
        self.assertEqual(response.status_code, 200)

    def test_forward_stands_down_without_public_url(self):
        # canonical_origin raises when PUBLIC_URL is unset (local dev);
        # the forward fails open and the request is served as-is.
        os.environ.pop("PUBLIC_URL", None)
        response = via_gateway(self.client, "/")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
