"""apps.admin.audit.keys

Key-management page for the audit web UI hosted by campus-admin (#927).

Reading traces and managing keys are separate capabilities
(docs/web-ui-requirements.md §5.2): this page is gated on the
key-management capability (a campus.audit superadmin session, see
apps.admin.audit.gate) and must not inherit the trace-view gate.

Where the old in-campus UI verified and mutated keys against the
in-process resources layer, every action here is a call to the audit
service's versioned API (/audit/v1/apikeys) with the deployment's
audit UI API key — the same surface the CLI uses. Consequences:

- The plaintext of a minted or rotated key appears once, in the API
  response relayed to the page that minted it, and is never stored:
  not in the session, not in a log line. Every response carrying a
  plaintext is Cache-Control: no-store.
- Key-management audit events (audit.apikeys.new/update/revoke/
  regenerate) are emitted by the audit API itself, attributed to the
  UI key — the trail records that the admin portal acted, not which
  human was driving it. (The old page attributed events to the
  session; that identity lives only in this app's session now.)
- The wire privileges are the UI key's scopes (apikeys:read to list,
  apikeys:write to mutate); a key without them surfaces as form
  errors, not silent failures.
"""

__all__ = ["create_blueprint"]

import hmac
import logging
import secrets
import typing
from datetime import datetime

import flask
from campus.audit import roles
from campus.common import schema

from . import gate
from .client import get_audit_client

logger = logging.getLogger(__name__)

# CSRF token for the page's forms.
KEYS_CSRF_KEY = "audit_keys_csrf"

# How many keys the page lists. The API's list route is paginated; the
# page shows the most recent keys, and says so when the cap is hit.
KEY_LIST_LIMIT = 200

# The roles the page offers, with what each grants (#918). A role is a
# named bundle of scopes, so the issuer picks a privilege rather than
# assembling one — and the raw scope list stays available for anything
# the catalogue does not cover.
ROLE_HELP = (
    ("traces-viewer", "read traces in the browser and query them via the API"),
    ("traces-searcher", "as above, plus the API's search scope"),
    ("traces-producer", "as above, plus ingesting spans (a producer's role)"),
    ("key-operator", "list and manage API keys"),
)


def _csrf_token() -> str:
    """Issue a CSRF token for the page's forms and remember it."""
    token = secrets.token_urlsafe(32)
    flask.session[KEYS_CSRF_KEY] = token
    return token


def _check_csrf() -> bool:
    """Validate the submitted CSRF token (single-use).

    The comparison runs on bytes: compare_digest() raises TypeError on
    non-ASCII strings, and the submitted token is raw form data.
    """
    issued = flask.session.pop(KEYS_CSRF_KEY, None)
    submitted = flask.request.form.get('csrf_token', '')
    if not isinstance(issued, str):
        return False
    return hmac.compare_digest(
        issued.encode('utf-8'), str(submitted).encode('utf-8')
    )


def _error_message(body: typing.Any, fallback: str) -> str:
    """Pull a human-readable message out of an audit API error body.

    The audit service's envelope is {"error": {"message": ...}};
    the relay may also produce {"error": "<string>"}.
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    return fallback


def _key_rows() -> tuple[list[dict[str, typing.Any]], str | None]:
    """The keys the page lists, newest first, or an error message.

    The audit service returns key records (never key_hash, never the
    plaintext); revoked and expired keys are included so the page can
    show — and revoke — the full set.
    """
    client = get_audit_client()
    response = client.apikeys.list(active_only=False, limit=KEY_LIST_LIMIT)
    if response.status_code != 200:
        return [], _error_message(
            _safe_json(response),
            f"The key list is unavailable (audit service returned "
            f"{response.status_code}).",
        )
    records = _safe_json(response).get("api_keys", [])
    rows = []
    for record in records:
        rows.append({
            "id": str(record.get("id", "")),
            "name": str(record.get("name", "")),
            "owner_id": str(record.get("owner_id", "")),
            "scopes": list(record.get("scopes") or []),
            "created_at": str(record.get("created_at", "")),
            "expires_at": str(record.get("expires_at") or ""),
            "revoked_at": str(record.get("revoked_at") or ""),
            "last_used": str(record.get("last_used") or ""),
            "rate_limit": record.get("rate_limit"),
            "revoked": bool(record.get("revoked_at")),
        })
    return rows, None


def _safe_json(response) -> dict:
    """Parse an audit API response body, tolerating non-JSON bodies."""
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _render_page(
        *,
        status: int = 200,
        minted: str | None = None,
        notice: str | None = None,
        error: str | None = None,
) -> flask.Response:
    """Render the key-management page.

    A response carrying a plaintext (minted or rotated) is marked
    no-store, so it does not linger in a browser cache or a proxy.
    """
    keys, list_error = _key_rows()
    if list_error and error is None:
        error = list_error
    html = flask.render_template(
        'audit/keys.html',
        keys=keys,
        csrf_token=_csrf_token(),
        minted=minted,
        notice=notice,
        error=error,
        roles=roles.list_roles(),
        role_help=ROLE_HELP,
        key_limit=KEY_LIST_LIMIT,
    )
    response = flask.make_response(html, status)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _form_error(message: str) -> flask.Response:
    """Re-render the page with an error and a fresh CSRF token."""
    return _render_page(status=400, error=message)


def _parse_scopes(raw: str) -> list[str]:
    """Parse a comma-separated scope list."""
    return [scope.strip() for scope in raw.split(',') if scope.strip()]


def _parse_expires_at(raw: str) -> schema.DateTime | None:
    """Validate an ISO 8601 expiry, returning None for "no expiry".

    The value is checked here because the API validates it again — a
    malformed date is a form error, not a server round trip.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(
            "The expiry must be an ISO 8601 date-time (e.g. "
            "2026-12-31T23:59:59+00:00)."
        ) from None
    return schema.DateTime(raw)


def _mutation_error(response, not_found: str) -> str:
    """Turn a failed key mutation into a form error message."""
    if response.status_code == 404:
        return not_found
    if response.status_code == 403:
        return (
            "The audit service refused the action: this deployment's "
            "audit UI API key lacks the apikeys:write scope."
        )
    return _error_message(
        _safe_json(response),
        f"The audit service refused the action ({response.status_code}).",
    )


def create_blueprint() -> flask.Blueprint:
    """Create the blueprint for the key-management page.

    Returns:
        A Flask blueprint serving the key-management page
    """
    bp = flask.Blueprint(
        'audit_keys',
        __name__,
        url_prefix='/audit/keys',
    )
    bp.before_request(gate.require_superadmin_page)

    @bp.get('')
    def index() -> flask.Response:
        """The key-management page: the key list and the mint form."""
        return _render_page()

    @bp.post('/new')
    def new() -> flask.Response:
        """Mint a key. The plaintext is shown once, here, and nowhere
        else — the audit service keeps only its hash."""
        if not _check_csrf():
            return _form_error(
                "Your form session expired. Please reload the page and "
                "try again."
            )
        name = (flask.request.form.get('name') or '').strip()
        owner_id = (flask.request.form.get('owner_id') or '').strip()
        role = (flask.request.form.get('role') or '').strip()
        scopes = _parse_scopes(flask.request.form.get('scopes') or '')
        if not name:
            return _form_error("Give the key a name.")
        if not owner_id:
            return _form_error(
                "Give the key an owner — the email of the person it is "
                "for. It is a display label only, never resolved."
            )
        if role and scopes:
            return _form_error(
                "Give either a role or a list of scopes, not both."
            )
        if role:
            try:
                roles.resolve_role(role)
            except roles.UnknownRoleError as e:
                return _form_error(str(e))
        elif not scopes:
            return _form_error(
                "Give the key a role, or at least one scope."
            )
        rate_limit_raw = (flask.request.form.get('rate_limit') or '').strip()
        rate_limit: int | None = None
        if rate_limit_raw:
            try:
                rate_limit = int(rate_limit_raw)
            except ValueError:
                return _form_error(
                    "The rate limit must be a whole number of requests "
                    "per minute."
                )
        try:
            expires_at = _parse_expires_at(
                flask.request.form.get('expires_at') or ''
            )
        except ValueError as e:
            return _form_error(str(e))
        client = get_audit_client()
        response = client.apikeys.new(
            name=name,
            owner_id=owner_id,
            scopes=scopes or None,
            role=role or None,
            rate_limit=rate_limit,
            expires_at=str(expires_at) if expires_at is not None else None,
        )
        if response.status_code != 201:
            return _form_error(_mutation_error(
                response, "The audit service refused the mint."
            ))
        record = _safe_json(response)
        plaintext = str(record.get("api_key") or "")
        logger.info(
            "Audit UI minted key %s (owner %s)",
            record.get("id"), owner_id,
        )
        return _render_page(
            minted=plaintext,
            notice=(
                f"Key '{name}' created. Copy the key below now — it is "
                "shown only here, and the server keeps only its hash."
            ),
        )

    @bp.post('/<key_id>/scopes')
    def update_scopes(key_id: str) -> flask.Response:
        """Change a key's scopes."""
        if not _check_csrf():
            return _form_error(
                "Your form session expired. Please reload the page and "
                "try again."
            )
        scopes = _parse_scopes(flask.request.form.get('scopes') or '')
        if not scopes:
            return _form_error("Give the key at least one scope.")
        client = get_audit_client()
        response = client.apikeys[key_id].update(scopes=scopes)
        if response.status_code != 200:
            return _form_error(_mutation_error(
                response, f"No key with id {key_id}."
            ))
        logger.info("Audit UI re-scoped key %s", key_id)
        return _render_page(
            notice=f"Scopes updated for key {key_id}."
        )

    @bp.post('/<key_id>/rotate')
    def rotate(key_id: str) -> flask.Response:
        """Rotate a key: a new plaintext, the same key id. The old key
        stops working immediately."""
        if not _check_csrf():
            return _form_error(
                "Your form session expired. Please reload the page and "
                "try again."
            )
        client = get_audit_client()
        response = client.apikeys[key_id].regenerate()
        if response.status_code != 200:
            return _form_error(_mutation_error(
                response, f"No key with id {key_id}."
            ))
        plaintext = str(_safe_json(response).get("key") or "")
        logger.info("Audit UI rotated key %s", key_id)
        return _render_page(
            minted=plaintext,
            notice=(
                f"Key {key_id} rotated. The previous key no longer works. "
                "Copy the new key now — it is shown only here."
            ),
        )

    @bp.post('/<key_id>/revoke')
    def revoke(key_id: str) -> flask.Response:
        """Revoke a key. Revoking is the only way to withdraw access
        a holder was given."""
        if not _check_csrf():
            return _form_error(
                "Your form session expired. Please reload the page and "
                "try again."
            )
        client = get_audit_client()
        response = client.apikeys[key_id].revoke()
        if response.status_code not in (200, 204):
            return _form_error(_mutation_error(
                response, f"No key with id {key_id}."
            ))
        logger.info("Audit UI revoked key %s", key_id)
        return _render_page(
            notice=(
                f"Key {key_id} revoked. It no longer authenticates "
                "anything, for anyone."
            ),
        )

    return bp
