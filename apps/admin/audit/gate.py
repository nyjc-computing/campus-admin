"""apps.admin.audit.gate

Access gate for the audit web UI hosted by campus-admin.

The audit UI previously lived inside campus.audit, which ran its own
browser OAuth flow (AUDIT_OAUTH_CLIENT_ID/SECRET), an AUDIT_ADMINS
superadmin list, and a development-only API-key sign-in (#915). Merged
into campus-admin, sign-in is this portal's existing campus.auth OAuth
login (flask_campus OAuthLoginManager): a signed-in portal user is
flask.g.user.

What carries over unchanged is the capability model:

- AUDIT_ADMINS: comma-separated campus.auth user ids (emails),
  compared case-insensitively; whitespace around entries is ignored.
  Intended for a handful of accounts (<= 5). Members are campus.audit
  superadmins: they hold both capabilities (view traces, manage keys).
- An unset or empty variable allows no one (fail-closed).
- The list is read per request, so allowlist changes take effect
  without touching the login session.
- Authenticated users not on the list get a 403 page (403 JSON on the
  data endpoints). The landing page and static assets stay reachable
  without superadmin rights.

What does NOT carry over: the API-key sign-in lane (#915) was a
campus.audit development convenience that verified keys against the
in-process apikeys store; this app has no such store. Scoped key
holders use the versioned API directly.

The data itself is reached over HTTP with the deployment's audit UI
API key (apps.admin.audit.client): a key minted from the operator key
with the traces:read + apikeys:read + apikeys:write scopes. The key
never leaves the server, so every session's effective privileges are
the key's scopes, gated per page by AUDIT_ADMINS.
"""

__all__ = [
    "can_manage_keys",
    "can_view_traces",
    "forbidden_page",
    "is_superadmin",
    "require_login_api",
    "require_login_page",
    "require_superadmin_page",
]

import logging

import flask
import werkzeug
from campus.common import env

logger = logging.getLogger(__name__)

# UI endpoints reachable without a login: the landing page and this
# blueprint's static assets (the landing page needs its CSS and JS).
# Everything else under /audit requires a signed-in superadmin.
PUBLIC_UI_ENDPOINTS = frozenset({"audit_ui.index", "audit_ui.static"})


def _admin_emails() -> frozenset[str]:
    """Parse the AUDIT_ADMINS allowlist.

    The variable holds comma-separated campus.auth user ids (emails).
    Entries are trimmed and compared case-insensitively; empty entries
    are ignored. An unset or empty variable allows no one (fail-closed).
    """
    raw = env.get("AUDIT_ADMINS") or ""
    return frozenset(
        entry.strip().lower() for entry in raw.split(",") if entry.strip()
    )


def _session_user_id() -> str | None:
    """The signed-in campus.auth user id, if any."""
    user = getattr(flask.g, "user", None)
    if user is not None and getattr(user, "id", None):
        return str(user.id)
    return None


def is_superadmin() -> bool:
    """Whether the signed-in user is on the AUDIT_ADMINS list.

    An anonymous session is not a superadmin; the page gate redirects
    it to sign in before consulting the list.
    """
    user_id = _session_user_id()
    return user_id is not None and user_id.strip().lower() in _admin_emails()


# The two web-UI capabilities (#928). AUDIT_ADMINS is the superadmin
# list — its members hold both. The deployment's audit UI API key holds
# the wire scopes (traces:read, apikeys:read, apikeys:write); the gate
# decides who may drive it.
def can_view_traces() -> bool:
    """Whether the signed-in session may read traces."""
    return is_superadmin()


def can_manage_keys() -> bool:
    """Whether the signed-in session may manage audit API keys."""
    return is_superadmin()


def _missing_capability_reason() -> str:
    """Explain why the current session cannot use the audit UI."""
    return (
        "Your account is not a campus.audit superadmin. Superadmins "
        "are the accounts on this deployment's AUDIT_ADMINS list; they "
        "may manage API keys and read traces."
    )


def _forbidden_page(reason: str | None = None) -> flask.Response:
    """Render the 403 page for sessions without the capability."""
    identity = _session_user_id() or "unknown"
    logger.warning(
        "Audit UI access denied for %s (not on AUDIT_ADMINS)", identity,
    )
    return flask.Response(
        flask.render_template(
            "audit/forbidden.html",
            identity=identity,
            reason=reason or _missing_capability_reason(),
        ),
        status=403,
        mimetype="text/html",
    )


def forbidden_page(reason: str) -> flask.Response:
    """Render the 403 page for a session missing another capability."""
    return _forbidden_page(reason)


def _config_error_page() -> flask.Response | None:
    """503 page when the audit UI's service credentials are unset.

    The UI proxies every read and mutation through the audit service
    API with AUDIT_UI_API_KEY; without it the pages would render and
    then fail on every fetch, so the gate refuses them up front with
    the remedy in the message (mirrors the old OAuth-gate behaviour).
    """
    from .client import config_error

    message = config_error()
    if message is None:
        return None
    return flask.Response(
        f"<h1>Audit Web UI unavailable</h1><p>{message}</p>",
        status=503,
        mimetype="text/html",
    )


def require_login_page() -> werkzeug.Response | None:
    """Before-request gate for UI page routes.

    Public endpoints (landing page, static assets) pass. A signed-in
    superadmin passes. A signed-in non-superadmin gets the 403 page
    (re-authenticating cannot help; the page says which list to join).
    An anonymous session is sent to sign in, preserving the target
    path. A deployment without audit service credentials gets 503.
    """
    if flask.request.endpoint in PUBLIC_UI_ENDPOINTS:
        return None
    if not getattr(flask.g, "user", None):
        return flask.redirect(
            flask.url_for("auth.login", next=flask.request.path)
        )
    if can_view_traces():
        return None
    return _forbidden_page()


def require_superadmin_page() -> werkzeug.Response | None:
    """Before-request gate for the key-management page.

    Managing keys is a separate capability from reading traces
    (docs/web-ui-requirements.md §5.2), so this gate must not inherit
    the trace-view one: an anonymous session is sent to sign in, a
    session without the key-management capability gets the 403 page.
    """
    if not getattr(flask.g, "user", None):
        return flask.redirect(
            flask.url_for("auth.login", next=flask.request.path)
        )
    if can_manage_keys():
        return None
    return forbidden_page(
        "Your session cannot manage API keys. Managing keys is limited "
        "to campus.audit superadmins (the AUDIT_ADMINS list)."
    )


def require_login_api() -> flask.Response | dict | tuple | None:
    """Before-request gate for the UI data endpoints: 401 JSON when
    not signed in (browsers redirect pages; fetch() callers get 401),
    and 403 JSON for a session without the trace-view capability.
    """
    if getattr(flask.g, "user", None):
        if can_view_traces():
            return None
        identity = _session_user_id() or "unknown"
        logger.warning(
            "Audit UI data access denied for %s (not on AUDIT_ADMINS)",
            identity,
        )
        return {"error": (
            "Access denied: this session cannot read traces. "
            f"{_missing_capability_reason()}"
        )}, 403
    return {"error": "Authentication required. Visit /login."}, 401
