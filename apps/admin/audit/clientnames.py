"""apps.admin.audit.clientnames

Client-id -> display-name resolution for the audit web UI.

Spans carry raw client ids (uid-client-…); the UI shows each client's
registered name instead. Names come from campus.auth's clients API,
fetched in one call with this portal's own OAuth credentials and
cached in-process — client records are near-static, and a TTL bounds
staleness after a rename. (Ported from campus.audit.web.clientnames:
same cache and fallback behaviour, different credential source — this
app's ambient CLIENT_ID/CLIENT_SECRET instead of the audit
deployment's OAuth gate credentials.)

Resolution is best-effort by design: the UI must keep rendering when
campus.auth is unreachable or credentials are missing, so lookups
never raise — unresolvable ids simply stay raw.
"""

__all__ = ["get_client_names", "enrich_summaries", "enrich_tree"]

import logging
import threading
import time
import typing

from campus.common.services import get_base_url as get_service_base_url

logger = logging.getLogger(__name__)

# Client records change rarely (registration-time); 5 minutes bounds
# how long a rename stays invisible rather than how often we pay the
# lookup.
_CACHE_TTL_SECONDS = 300.0

_lock = threading.Lock()
_names: dict[str, str] = {}
# None = never fetched. Not 0.0: time.monotonic() is seconds-since-boot
# on Linux, and a fresh CI/VM host can legitimately read < TTL.
_fetched_at: float | None = None


def _fetch_client_names() -> dict[str, str]:
    """Fetch {client_id: name} from campus.auth's clients API.

    Authenticates as this portal (its CLIENT_ID/CLIENT_SECRET, the
    same credentials the login flow and the clients page use).
    Raises on any failure — the caller falls back to the cache or raw
    ids.
    """
    from campus.common import env
    from campus_python.json_client import CampusRequest

    client_id = env.get("CLIENT_ID")
    client_secret = env.get("CLIENT_SECRET")
    if not (client_id and client_secret):
        raise LookupError(
            "no client credentials for name lookup (set CLIENT_ID/SECRET)"
        )

    client = CampusRequest(
        base_url=get_service_base_url("campus.auth"),
        mode="device",
        timeout=5,
    )
    client.set_basic_authorization(client_id, client_secret)
    response = client.get("/auth/v1/clients/")
    if not response.ok():
        raise LookupError(
            f"clients API returned {response.status_code}"
        )
    clients = response.json().get("clients", [])
    return {
        entry["id"]: entry.get("name") or entry["id"]
        for entry in clients
        if entry.get("id")
    }


def get_client_names(client_ids: typing.Iterable[str]) -> dict[str, str]:
    """Resolve the given client ids to display names (cached).

    Fetches the full id->name map on first use and every
    _CACHE_TTL_SECONDS after; a failed refresh keeps serving the
    previous map. Ids with no known name are omitted from the result —
    callers fall back to the raw id. Never raises.
    """
    global _names, _fetched_at
    wanted = {cid for cid in client_ids if cid}
    if not wanted:
        return {}
    with _lock:
        if _fetched_at is None or (
            time.monotonic() - _fetched_at
        ) >= _CACHE_TTL_SECONDS:
            try:
                _names = _fetch_client_names()
                _fetched_at = time.monotonic()
            except Exception as e:
                logger.warning("client name lookup failed: %s", e)
    return {cid: _names[cid] for cid in wanted if cid in _names}


def _attach_name(span: dict, names: dict[str, str]) -> None:
    """Set span["client_name"] when the span's client_id resolves."""
    name = names.get(span.get("client_id") or "")
    if name:
        span["client_name"] = name


def _collect_client_ids(spans: typing.Iterable[dict]) -> set[str]:
    """All client_ids in a span tree (spans + nested children)."""
    ids: set[str] = set()
    for span in spans:
        if span.get("client_id"):
            ids.add(span["client_id"])
        ids |= _collect_client_ids(span.get("children") or [])
    return ids


def enrich_summaries(traces: list[dict]) -> list[dict]:
    """Attach client names to the root spans of trace summaries.

    Mutates and returns the trace dicts for inline use in the data
    endpoints.
    """
    roots = [t.get("root_span") or {} for t in traces]
    names = get_client_names(
        root["client_id"] for root in roots if root.get("client_id")
    )
    for root in roots:
        _attach_name(root, names)
    return traces


def enrich_tree(span: dict) -> dict:
    """Attach client names to every span of a trace tree."""
    names = get_client_names(_collect_client_ids([span]))

    def _walk(node: dict) -> None:
        _attach_name(node, names)
        for child in node.get("children") or []:
            _walk(child)

    _walk(span)
    return span
