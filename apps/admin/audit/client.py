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
"""

__all__ = ["config_error", "get_audit_client", "json_client_factory"]

import logging
import typing

from campus.audit.client import AuditClient
from campus.audit.http import DefaultClient
from campus.audit.http.interface import JsonClient
from campus.common import env
from campus.common.services import get_base_url as get_service_base_url

logger = logging.getLogger(__name__)

# Test seam: a factory for the wire-level client, mirroring the
# json_client_class pattern of campus.audit.client and the old web
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
