"""Unit tests for the audit UI's data endpoints (apps.admin.audit.data).

The endpoints proxy the audit service's versioned API with the
deployment's UI key and relay the JSON, enriching trace summaries and
span trees with client display names. These tests verify the wire
calls (paths, params), the relay shapes the UI's JavaScript expects,
and the error translation.

File: tests/test_audit_data.py
"""

import unittest

from tests.support import StubJsonClient, StubResponse, make_app


class DataProxyTestBase(unittest.TestCase):
    """Shared harness: one app per test, patches restored after."""

    def setUp(self):
        self.json_client = StubJsonClient()
        self.app, self.client, cleanup = make_app(json_client=self.json_client)
        self.addCleanup(cleanup)


class TestListTracesProxy(DataProxyTestBase):
    """GET /audit/api/traces proxies the search endpoint."""

    def test_relay_shape(self):
        """The {traces, cursor} envelope passes through untouched."""
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse({
            "traces": [{"trace_id": "a" * 32, "root_span": {}}],
            "cursor": {"next": "tok", "has_more": True},
        })
        response = self.client.get("/audit/api/traces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["cursor"],
            {"next": "tok", "has_more": True},
        )

    def test_forwards_filters(self):
        self.client.get(
            "/audit/api/traces?path=/auth/v1/token&status=500"
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
        self.client.get("/audit/api/traces")
        _, _, params, _ = self.json_client.calls[0]
        self.assertEqual(params["events"], "0")

    def test_status_must_be_an_integer(self):
        response = self.client.get("/audit/api/traces?status=2x0")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_audit_service_error_is_relayed_with_status(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/search"
        )] = StubResponse(
            {"error": {"code": "FORBIDDEN", "message": "key lacks traces:read"}},
            status_code=403,
        )
        response = self.client.get("/audit/api/traces")
        self.assertEqual(response.status_code, 403)
        body = response.get_json()
        self.assertEqual(body["error"]["message"], "key lacks traces:read")


class TestJourneyProxy(DataProxyTestBase):
    """GET /audit/api/journeys/<id> proxies the journey endpoint."""

    def test_journey_envelope(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/journeys/j-1/"
        )] = StubResponse({
            "journey_id": "j-1",
            "trace_count": 2,
            "traces": [{"trace_id": "a" * 32}, {"trace_id": "b" * 32}],
        })
        response = self.client.get("/audit/api/journeys/j-1")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["journey_id"], "j-1")
        self.assertEqual(body["trace_count"], 2)
        method, path, _, _ = self.json_client.calls[0]
        self.assertEqual(path, "/audit/v1/journeys/j-1/")


class TestTraceTreeProxy(DataProxyTestBase):
    """GET /audit/api/traces/<trace_id> proxies the tree endpoint."""

    def test_tree_envelope(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/traces/" + "a" * 32
        )] = StubResponse({
            "trace_id": "a" * 32,
            "root_span": {"span_id": "b" * 16, "children": []},
        })
        response = self.client.get(f"/audit/api/traces/{'a' * 32}")
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
        response = self.client.get(f"/audit/api/traces/{'e' * 32}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())


class TestSpanProxy(DataProxyTestBase):
    """GET /audit/api/traces/<id>/spans/<sid> proxies span detail."""

    def test_span_passthrough(self):
        self.json_client.handlers[(
            "GET", f"/audit/v1/traces/{'a' * 32}/spans/{'b' * 16}"
        )] = StubResponse({
            "span_id": "b" * 16,
            "request_headers": {"X-Test": "1"},
        })
        response = self.client.get(f"/audit/api/traces/{'a' * 32}/spans/{'b' * 16}")
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
        response = self.client.get("/audit/api/traces")
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
        response = self.client.get(f"/audit/api/traces/{'a' * 32}")
        root = response.get_json()["root_span"]
        self.assertEqual(root["client_name"], "Auth Service")
        # Unresolvable ids stay raw.
        self.assertNotIn("client_name", root["children"][0])


if __name__ == "__main__":
    unittest.main()
