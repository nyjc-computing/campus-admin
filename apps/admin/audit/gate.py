"""apps.admin.audit.gate

Access gate for the audit web UI hosted by campus-admin.

campus.audit authorizes by API-key scope only and never authenticates
a person (campus/audit/__init__.py::_authenticate_audit_api_key). This
portal therefore does two distinct jobs, and keeps them distinct:

- **Authentication** is the portal's own campus.auth OAuth login
  (flask_campus OAuthLoginManager): a signed-in portal user is
  flask.g.user. campus.audit never sees that identity.
- **Service authorization** is the deployment's audit UI API key
  (apps.admin.audit.client), a key minted from the operator key with
  the traces:read + apikeys:read + apikeys:write scopes. The key never
  leaves the server, so what the service allows every session is the
  key's scopes.

Because one shared key serves the whole UI, the portal needs its own
answer to "who may drive it". That answer is two deployment lists of
campus.auth user ids (emails), compared case-insensitively with
whitespace ignored, read per request, and fail-closed when unset or
empty (access policy: campus docs/environment-access-policy.md, the
2026-11-17 ruling on campus#1129):

- AUDIT_ADMINS — who may read traces. On production this is the
  admin list; students may be added to it on the **development**
  deployment only.
- AUDIT_KEY_ADMINS — the audit superadmins, who may additionally manage
  API keys. Key management never opens to students. Unset, it falls
  back to AUDIT_ADMINS, so a deployment whose AUDIT_ADMINS is already
  the superadmin list needs no second variable; set it explicitly
  wherever AUDIT_ADMINS includes non-superadmins. A list that is set
  but names no one allows no one.

Authenticated users without the capability get a 403 page (403 JSON on
the data endpoints). The section root and static assets stay reachable
without signing in.

What does NOT carry over from the old in-service UI: the API-key
sign-in lane (#915) was a campus.audit development convenience that
verified keys against the in-process apikeys store; this app has no
such store. Scoped key holders use the versioned API directly.
"""

__all__ = [
    "can_manage_keys",
    "can_view_traces",
    "forbidden_page",
    "is_superadmin",
    "key_admins_variable",
    "require_login_api",
    "require_login_page",
    "require_superadmin_page",
]

import logging

import flask
import werkzeug
from campus.common import env

logger = logging.getLogger(__name__)

# UI endpoints reachable without a login: the section-root redirect
# (anonymous /audit/ hops to /audit/traces, whose gate then sends the
# browser to sign in with next=/audit/traces) and this blueprint's
# static assets. Everything else under /audit requires a signed-in
# trace viewer.
PUBLIC_UI_ENDPOINTS = frozenset({"audit_ui.index", "audit_ui.static"})


def _allowlist(varname: str) -> frozenset[str] | None:
    """Parse a comma-separated allowlist, or None when unset or empty."""
    raw = env.get(varname) or ""
    entries = frozenset(
        entry.strip().lower() for entry in raw.split(",") if entry.strip()
    )
    return entries or None


def _session_user_id() -> str | None:
    """The signed-in campus.auth user id, if any."""
    user = getattr(flask.g, "user", None)
    if user is not None and getattr(user, "id", None):
        return str(user.id)
    return None


def _listed(varname: str) -> bool:
    user_id = _session_user_id()
    if user_id is None:
        return False
    return user_id.strip().lower() in (_allowlist(varname) or frozenset())


def key_admins_variable() -> str:
    """The deployment variable that currently names the key admins."""
    return "AUDIT_KEY_ADMINS" if env.get("AUDIT_KEY_ADMINS") else "AUDIT_ADMINS"


def can_view_traces() -> bool:
    """Whether the signed-in session may read traces (AUDIT_ADMINS)."""
    return _listed("AUDIT_ADMINS")


def can_manage_keys() -> bool:
    """Whether the signed-in session may manage audit API keys.

    The audit superadmins: AUDIT_KEY_ADMINS where it is set, otherwise
    the AUDIT_ADMINS list. A list that is set but names no one allows
    no one — an emptied list is a decision, not an omission.
    """
    if env.get("AUDIT_KEY_ADMINS"):
        return _listed("AUDIT_KEY_ADMINS")
    return can_view_traces()


def is_superadmin() -> bool:
    """Whether the signed-in user is an audit superadmin (may manage keys).

    An anonymous session is not a superadmin; the page gate redirects
    it to sign in before consulting the list.
    """
    return can_manage_keys()


def _missing_capability_reason() -> str:
    """Explain why the current session cannot use the audit UI."""
    return (
        "Your account is not on this deployment's AUDIT_ADMINS list. "
        "Accounts on it may read traces in the audit web UI."
    )


def _forbidden_page(reason: str | None = None) -> flask.Response:
    """Render the 403 page for sessions without the capability."""
    identity = _session_user_id() or "unknown"
    logger.warning(
        "Audit UI access denied for %s (%s)", identity, reason or "no capability",
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

    Public endpoints (section root, static assets) pass. A signed-in
    trace viewer passes. A signed-in account that is not one gets the
    403 page (re-authenticating cannot help; the page says which list
    to join). An anonymous session is sent to sign in, preserving the
    target path. A deployment without audit service credentials gets
    503.
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

    Managing keys is a separate capability from reading traces (the
    2026-11-17 ruling: key management is superadmin-only on every
    environment, while trace reading may open to students on
    development), so this gate must not inherit the trace-view one: an
    anonymous session is sent to sign in, a session without the
    key-management capability gets the 403 page.
    """
    if not getattr(flask.g, "user", None):
        return flask.redirect(
            flask.url_for("auth.login", next=flask.request.path)
        )
    if can_manage_keys():
        return None
    return forbidden_page(
        "Your session cannot manage API keys. Managing keys is limited "
        "to audit superadmins: accounts on the "
        f"{key_admins_variable()} list."
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
