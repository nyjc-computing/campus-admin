"""Unit tests for the audit UI's data endpoints (apps.admin.audit.data).

The endpoints proxy the audit service's versioned API with the
deployment's UI key and relay the JSON, enriching trace summaries and
span trees with client display names. These tests verify the wire
calls (paths, params), the relay shapes the UI's JavaScript expects,
and the error translation.

File: tests/test_audit_data.py
"""

import unittest

from campus_python.audit.http import NetworkError
from requests.exceptions import JSONDecodeError

from tests.support import StubJsonClient, StubResponse, make_app


class DataProxyTestBase(unittest.TestCase):
    """Shared harness: one app per test, patches restored after."""

    def setUp(self):
        self.json_client = StubJsonClient()
        self.app, self.client, cleanup = make_app(json_client=self.json_client)
        self.addCleanup(cleanup)


class TestListTracesProxy(DataProxyTestBase):
    """GET /admin/audit/api/traces proxies the search endpoint."""

    def test_relay_shape(self):
        """The {traces, cursor} envelope passes through untouched."""
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse({
            "traces": [{"trace_id": "a" * 32, "root_span": {}}],
            "cursor": {"next": "tok", "has_more": True},
        })
        response = self.client.get("/admin/audit/api/traces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["cursor"],
            {"next": "tok", "has_more": True},
        )

    def test_forwards_filters(self):
        self.client.get(
            "/admin/audit/api/traces?path=/auth/v1/token&status=500"
            "&journey_id=j-1&since=2026-01-01T00:00:00%2B00:00"
            "&limit=25&cursor=tok&events=1"
        )
        method, path, params, _ = self.json_client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/audit/v1/traces/search")
        self.assertEqual(params, {
            "limit": 25,
            "path": "/auth/v1/token",
            "status": 500,
            "journey_id": "j-1",
            "since": "2026-01-01T00:00:00+00:00",
            "cursor": "tok",
            "events": "1",
        })

    def test_events_off_by_default(self):
        """The viewer lists request traces only (#941)."""
        self.client.get("/admin/audit/api/traces")
        _, _, params, _ = self.json_client.calls[0]
        self.assertEqual(params["events"], "0")

    def test_status_must_be_an_integer(self):
        response = self.client.get("/admin/audit/api/traces?status=2x0")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_audit_service_error_is_relayed_with_status(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse(
            {"error": {"code": "FORBIDDEN", "message": "key lacks traces:read"}},
            status_code=403,
        )
        response = self.client.get("/admin/audit/api/traces")
        self.assertEqual(response.status_code, 403)
        body = response.get_json()
        self.assertEqual(body["error"]["message"], "key lacks traces:read")


class TestJourneyProxy(DataProxyTestBase):
    """GET /admin/audit/api/journeys/<id> proxies the journey endpoint."""

    def test_journey_envelope(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/journeys/j-1/"
        )] = StubResponse({
            "journey_id": "j-1",
            "trace_count": 2,
            "traces": [{"trace_id": "a" * 32}, {"trace_id": "b" * 32}],
        })
        response = self.client.get("/admin/audit/api/journeys/j-1")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["journey_id"], "j-1")
        self.assertEqual(body["trace_count"], 2)
        method, path, _, _ = self.json_client.calls[0]
        self.assertEqual(path, "/audit/v1/journeys/j-1/")


class TestTraceTreeProxy(DataProxyTestBase):
    """GET /admin/audit/api/traces/<trace_id> proxies the tree endpoint."""

    def test_tree_envelope(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/" + "a" * 32
        )] = StubResponse({
            "trace_id": "a" * 32,
            "root_span": {"span_id": "b" * 16, "children": []},
        })
        response = self.client.get(f"/admin/audit/api/traces/{'a' * 32}")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["root_span"]["span_id"], "b" * 16)

    def test_unknown_trace_404s_with_error_body(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/" + "e" * 32
        )] = StubResponse(
            {"error": {"code": "NOT_FOUND", "message": "Trace not found"}},
            status_code=404,
        )
        response = self.client.get(f"/admin/audit/api/traces/{'e' * 32}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())


class TestSpanProxy(DataProxyTestBase):
    """GET /admin/audit/api/traces/<id>/spans/<sid> proxies span detail."""

    def test_span_passthrough(self):
        self.json_client.handlers[(
            "GET", f"/audit/v1/traces/{'a' * 32}/spans/{'b' * 16}"
        )] = StubResponse({
            "span_id": "b" * 16,
            "request_headers": {"X-Test": "1"},
        })
        response = self.client.get(
            f"/admin/audit/api/traces/{'a' * 32}/spans/{'b' * 16}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["request_headers"], {"X-Test": "1"}
        )


class TestClientNameEnrichment(DataProxyTestBase):
    """Client ids resolve to display names via the cached lookup."""

    def setUp(self):
        super().setUp()
        from apps.admin.audit import clientnames

        self.clientnames = clientnames
        # Deterministic enrichment: stub the campus.auth lookup.
        self._restore = clientnames.get_client_names
        clientnames.get_client_names = (
            lambda _ids: {"uid-client-1": "Auth Service"}
        )

    def tearDown(self):
        self.clientnames.get_client_names = self._restore

    def test_summary_root_gets_client_name(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse({
            "traces": [{
                "trace_id": "a" * 32,
                "root_span": {"client_id": "uid-client-1"},
            }],
            "cursor": {"next": None, "has_more": False},
        })
        response = self.client.get("/admin/audit/api/traces")
        root = response.get_json()["traces"][0]["root_span"]
        self.assertEqual(root["client_name"], "Auth Service")

    def test_tree_children_get_client_name(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/" + "a" * 32
        )] = StubResponse({
            "trace_id": "a" * 32,
            "root_span": {
                "client_id": "uid-client-1",
                "children": [{"client_id": "uid-client-unknown"}],
            },
        })
        response = self.client.get(f"/admin/audit/api/traces/{'a' * 32}")
        root = response.get_json()["root_span"]
        self.assertEqual(root["client_name"], "Auth Service")
        # Unresolvable ids stay raw.
        self.assertNotIn("client_name", root["children"][0])


class TestUpstreamErrors(DataProxyTestBase):
    """Upstream errors reach the browser with the audit service's status.

    The audit client resources call raise_for_status() before returning,
    so an error arrives here as a typed exception and never as a
    response. Before the proxy handled that, every one of them surfaced
    as an HTTP 500.
    """

    SEARCH = ("GET", "/audit/v1/traces/search")

    def _list(self):
        return self.client.get("/admin/audit/api/traces")

    def test_client_errors_keep_the_upstream_status(self):
        """Each 4XX the client raises is relayed with its status and error."""
        cases = (
            (401, {"error": "UNAUTHORIZED",
                   "error_description": "Invalid API key"}),
            (403, {"error": {"code": "FORBIDDEN",
                             "message": "key lacks traces:read"}}),
            (404, {"error": {"code": "NOT_FOUND",
                             "message": "Trace not found"}}),
            (409, {"error": "CONFLICT"}),
            (400, {"error": "INVALID_REQUEST"}),
            (422, {"error": "INVALID_REQUEST"}),
            (429, {"error": {"code": "RATE_LIMITED",
                             "message": "Rate limit exceeded"}}),
        )
        for status, envelope in cases:
            with self.subTest(status=status):
                self.json_client.handlers[self.SEARCH] = StubResponse(
                    envelope, status_code=status,
                )
                response = self._list()
                self.assertEqual(response.status_code, status)
                self.assertEqual(
                    response.get_json(), {"error": envelope["error"]},
                )

    def test_4xx_without_an_error_value_gets_a_generic_message(self):
        self.json_client.handlers[self.SEARCH] = StubResponse(
            {"detail": "nothing usable"}, status_code=404,
        )
        response = self._list()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json(), {"error": "audit service returned 404"},
        )

    def test_5xx_is_a_generic_502(self):
        """The body of an upstream 5XX is not ours to show."""
        self.json_client.handlers[self.SEARCH] = StubResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": "private detail"}},
            status_code=503,
        )
        response = self._list()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": "The audit service is unavailable."},
        )
        self.assertNotIn("private detail", response.get_data(as_text=True))

    def test_non_json_error_body_is_a_502(self):
        """A proxy's HTML error page: the client cannot attach the body, so
        the parse error is what surfaces."""
        class NotJson(StubResponse):
            def json(self):
                raise JSONDecodeError("Expecting value", "<html>", 0)

        self.json_client.handlers[self.SEARCH] = NotJson(status_code=502)
        response = self._list()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": "The audit service is unavailable."},
        )

    def test_transport_failure_is_a_502(self):
        class Unreachable(StubResponse):
            def raise_for_status(self):
                raise NetworkError("connection refused")

        self.json_client.handlers[self.SEARCH] = Unreachable()
        response = self._list()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": "The audit service is unavailable."},
        )

    def test_every_proxy_route_relays_upstream_errors(self):
        trace, span = "e" * 32, "f" * 16
        routes = (
            ("/admin/audit/api/traces", "/audit/v1/traces/search"),
            ("/admin/audit/api/journeys/journey-1", "/audit/v1/journeys/journey-1/"),
            (f"/admin/audit/api/traces/{trace}", f"/audit/v1/traces/{trace}"),
            (f"/admin/audit/api/traces/{trace}/spans/{span}",
             f"/audit/v1/traces/{trace}/spans/{span}"),
        )
        for url, upstream in routes:
            with self.subTest(url=url):
                self.json_client.handlers = {
                    ("GET", upstream): StubResponse(
                        {"error": "FORBIDDEN"}, status_code=403,
                    ),
                }
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json(), {"error": "FORBIDDEN"})


if __name__ == "__main__":
    unittest.main()
