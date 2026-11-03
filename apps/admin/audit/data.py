"""apps.admin.audit.data

Data endpoints for the audit web UI hosted by campus-admin.

The browser cannot call the audit service's authenticated /audit/v1
API directly (it has no API key, and must never hold one), so these
routes proxy the same data server-side: each endpoint forwards its
query to the audit service over HTTP with the deployment's audit UI
API key (apps.admin.audit.client) and relays the JSON response. This
replaces the in-process resources-layer reads the UI did when it lived
inside campus.audit — the response shapes are identical, so the UI's
JavaScript is unchanged.

Gated by the trace-view capability (apps.admin.audit.gate):
unauthenticated callers get 401, and a signed-in non-superadmin gets
403. Client-id display names are attached here (clientnames), exactly
as the in-process endpoints did.
"""

__all__ = ["create_blueprint"]

import logging

import flask
from campus.audit.http import NetworkError

from . import clientnames
from .client import get_audit_client

logger = logging.getLogger(__name__)

# Query values that mean "yes" for a boolean-ish param.
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def _include_events() -> bool:
    """Whether the caller asked for audit-event traces (#941).

    Off by default: the viewer's list is for debugging real request
    flows, and the event trail is noise there. The toggle in the filter
    bar sets ?events=1; the proxy forwards it as the versioned API's
    events=true|false search parameter.
    """
    value = (flask.request.args.get("events") or "").strip().lower()
    return value in _TRUTHY


def _optional(name: str) -> str | None:
    """A query parameter or None if absent/blank."""
    value = flask.request.args.get(name)
    return value if value else None


def _page_size() -> int:
    """The limit query parameter, clamped by the audit service."""
    return int(flask.request.args.get("limit") or 0) or 50


def _relay(response) -> tuple[dict, int]:
    """Relay an audit API response to the browser.

    2xx bodies pass through with their status code (204 keeps its
    empty body). Error envelopes pass through as {"error": ...} with
    the audit service's status code; a body the proxy cannot parse
    becomes a generic message. Connection failures become 502.
    """
    if 200 <= response.status_code < 300:
        if response.status_code == 204:
            return {}, 204
        return response.json(), response.status_code
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("error"):
        return {"error": body["error"]}, response.status_code
    return (
        {"error": f"audit service returned {response.status_code}"},
        response.status_code,
    )


def _relay_or_502(response) -> tuple[dict, int]:
    """_relay, mapping audit-service connection failures to 502."""
    try:
        return _relay(response)
    except NetworkError as e:
        logger.warning("audit service unreachable: %s", e)
        return {"error": "The audit service is unavailable."}, 502


def create_blueprint() -> flask.Blueprint:
    """Create a Flask blueprint for UI data endpoints.

    Returns:
        A Flask blueprint serving JSON data for the UI's JavaScript
    """
    bp = flask.Blueprint(
        'audit_data',
        __name__,
        url_prefix='/audit/api',
    )

    @bp.route('/traces')
    def list_traces() -> tuple[dict, int]:
        """List traces for the UI trace table.

        Query params (all optional):
            path: filter by endpoint path
            status: filter by HTTP status code
            journey_id: filter by login-journey tag (#803)
            since: ISO 8601 timestamp
            until: ISO 8601 timestamp
            limit: page size, clamped by the audit service
            cursor: opaque token from a previous page's cursor.next
            events: include audit-event traces (#941). Off by default:
                the viewer lists request traces, and the event trail
                (audit.apikeys.*, audit.ui.auth.*, …) is noise for
                someone debugging a real flow.

        Returns:
            JSON: {"traces": [...], "cursor": {"next": ..., "has_more": ...}}
        """
        status = flask.request.args.get("status")
        status_int: int | None
        if status is None or status == "":
            status_int = None
        else:
            try:
                status_int = int(status)
            except (TypeError, ValueError):
                return (
                    {"error": f"status must be an integer, got {status!r}"},
                    400,
                )
        client = get_audit_client()
        response = client.traces.search(
            path=_optional("path"),
            status=status_int,
            journey_id=_optional("journey_id"),
            since=_optional("since"),
            until=_optional("until"),
            limit=_page_size(),
            cursor=_optional("cursor"),
            events=_include_events(),
        )
        body, status_code = _relay_or_502(response)
        if status_code == 200:
            body["traces"] = clientnames.enrich_summaries(body.get("traces", []))
        return body, status_code

    @bp.route('/journeys/<journey_id>')
    def get_journey(journey_id: str) -> tuple[dict, int]:
        """List the member traces of one login journey (#803).

        Serves the journey view: every trace whose spans carry the
        journey tag, oldest first, so the flow reads top to bottom.
        """
        client = get_audit_client()
        response = client.journeys[journey_id].get()
        body, status_code = _relay_or_502(response)
        if status_code == 200:
            body["traces"] = clientnames.enrich_summaries(body.get("traces", []))
        return body, status_code

    @bp.route('/traces/<trace_id>')
    def get_trace(trace_id: str) -> tuple[dict, int]:
        """Get a single trace tree for the UI detail page.

        Mirrors the versioned API's response shape:
        {"trace_id": ..., "root_span": <tree with nested children>}.
        """
        client = get_audit_client()
        response = client.traces[trace_id].get_tree()
        body, status_code = _relay_or_502(response)
        if status_code == 200:
            body["root_span"] = clientnames.enrich_tree(body.get("root_span", {}))
        return body, status_code

    @bp.route('/traces/<trace_id>/spans/<span_id>')
    def get_span(trace_id: str, span_id: str) -> tuple[dict, int]:
        """Get a single span (with headers/bodies) for the UI drawer."""
        client = get_audit_client()
        response = client.traces[trace_id].spans[span_id].get()
        return _relay_or_502(response)

    return bp
