"""apps.admin.audit.client

HTTP client wiring for the audit web UI hosted by campus-admin.

Every read and mutation this UI performs goes through the audit
service's versioned API (/audit/v1) with the deployment's audit UI API
key — the same surface the CLI and producers use. The key is a server
side secret (AUDIT_UI_API_KEY): it authenticates the *deployment* to
campus.audit, while the gate (apps.admin.audit.gate) decides which
signed-in human may drive it. Scope the key narrowly when minting it:
traces:read + apikeys:read + apikeys:write covers the whole UI; it
must NOT hold traces:write (producing spans is the tracing middleware's
job, done under the separate AUDIT_API_KEY).

The UI key is deliberately separate from the middleware's tracing key:
distinct rate-limit buckets, and a compromise of one surface does not
drag the other along (same reasoning as the old AUDIT_OAUTH_CLIENT_ID
split from ambient credentials, #699).

Test injection: set ``json_client_factory`` to a callable returning a
JsonClient double; ``get_audit_client()`` then wraps it in an
AuditClient without touching the environment.

Upstream errors: the SDK's resource methods raise instead of returning
a 4XX/5XX response. The portal's views branch on status codes, so they
make their calls through ``call_audit``, which turns an error back into
the response it came from.
"""

__all__ = [
    "AuditResponse",
    "call_audit",
    "config_error",
    "get_audit_client",
    "json_client_factory",
]

import ast
import logging
import re
import typing

from campus.common import env
from campus.common.services import get_base_url as get_service_base_url
from campus_python.audit import AuditClient
from campus_python.audit.http import DefaultClient, HttpClientError
from campus_python.audit.http.interface import JsonClient

logger = logging.getLogger(__name__)

# Test seam: a factory for the wire-level client, mirroring the
# json_client_class pattern of campus_python.audit.client and the old web
# gate's AuthClient.
json_client_factory: typing.Callable[[], JsonClient] | None = None


def config_error() -> str | None:
    """Why the audit UI cannot serve requests, or None if configured.

    Both the audit service origin (the env-only service registry) and
    the UI API key must be present; anything else is a deployment
    misconfiguration the gate surfaces as a 503 page.
    """
    api_key = env.get("AUDIT_UI_API_KEY")
    if not api_key:
        return (
            "The audit web UI is not configured: set AUDIT_UI_API_KEY "
            "(an audit API key with the traces:read, apikeys:read and "
            "apikeys:write scopes, minted from the operator key) and "
            "the audit service URL (CAMPUS_AUDIT_URL)."
        )
    return None


def get_audit_client() -> AuditClient:
    """Build an AuditClient for the UI's server-side calls.

    Raises:
        OSError: if AUDIT_UI_API_KEY is unset (see config_error).
    """
    if json_client_factory is not None:
        return AuditClient(http_client=json_client_factory())
    message = config_error()
    if message is not None:
        raise OSError(message)
    return AuditClient(http_client=DefaultClient(
        base_url=get_service_base_url("campus.audit"),
        auth=env.get("AUDIT_UI_API_KEY"),
    ))


# The audit client's raise_for_status builds each error message as
# "<status> <reason>"; a transport failure carries no status.
_STATUS_RE = re.compile(r"^(\d{3})\b")

# The status call_audit uses for a failure the audit service did not
# answer for: a transport failure, or an error whose body is not JSON.
UNANSWERED_STATUS = 502


class AuditResponse(typing.NamedTuple):
    """What the views read from a response to a call that raised.

    Attributes:
        status_code: The upstream status, or 502 when the service did
            not answer for the failure.
        body: The parsed upstream body, or an empty dict.
    """

    status_code: int
    body: typing.Any

    def json(self) -> typing.Any:
        """The parsed body, as a JsonResponse returns it."""
        return self.body


def call_audit(call: typing.Callable[[], typing.Any]) -> typing.Any:
    """Run one audit SDK call and return a response, even when it failed.

    The SDK's resource methods call raise_for_status() before returning,
    so an upstream 4XX/5XX never comes back as a response: it is raised
    as a campus_python.audit.http error, with the parsed body attached
    as an exception note. The portal's views branch on status codes, so
    this turns the error back into the response it came from.

    A transport failure, or an error whose body is not JSON (an HTML
    error page from a proxy), becomes an AuditResponse with status 502
    and an empty body.

    Args:
        call: A zero-argument callable making one SDK call

    Returns:
        The SDK's response on success, otherwise an AuditResponse
    """
    try:
        return call()
    except HttpClientError as error:
        status = _status_of(error)
        if status is None:
            logger.warning("audit service unreachable: %s", error)
            return AuditResponse(UNANSWERED_STATUS, {})
        return AuditResponse(status, _attached_body(error))
    except OSError as error:
        # raise_for_status attaches the body by parsing it. A body that
        # is not JSON fails there, and the parse error is what surfaces
        # (an OSError as well as a ValueError, through requests).
        logger.warning("audit service sent an unreadable error: %s", error)
        return AuditResponse(UNANSWERED_STATUS, {})


def _status_of(error: Exception) -> int | None:
    """The HTTP status an audit client error stands for, if any."""
    match = _STATUS_RE.match(str(error))
    return int(match.group(1)) if match else None


def _attached_body(error: Exception) -> dict:
    """The parsed body raise_for_status attached to an error as a note.

    The note reads "Body: <python literal>". literal_eval reads that
    literal without executing anything. An empty dict when there is no
    such note, it cannot be read, or it is not a dict.
    """
    for note in getattr(error, "__notes__", ()):
        if not isinstance(note, str) or not note.startswith("Body: "):
            continue
        try:
            body = ast.literal_eval(note[len("Body: "):])
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return {}
        return body if isinstance(body, dict) else {}
    return {}
