"""apps.admin.migrations.client

Per-service wire calls for the Migrations view (#75).

The migration management surface (migrations/routes.py in the campus
suite, campus#1169) is mounted on EACH app service behind its own
versioned namespace, gated by the migrations grant vocabulary
(campus#1168):

    GET  /auth/v1/admin/migrations   (same shape on api, audit)
    POST /{svc}/v1/admin/migrations/apply      migrations:write
    POST /{svc}/v1/admin/migrations/abort      migrations:write
    POST /{svc}/v1/admin/migrations/rollback   migrations:admin

The gate is the #865/#883 two-leg model: a grant row designating the
user for migrations:<level> on that service AND the matching scope on
the caller's bearer. Client principals hold no migrations authority
(#854), so every call rides the SIGNED-IN admin's campus bearer via
campus.with_user_session() — the login session's token route
(campus#963/#964) re-issues the session user's grant token with its
grant-record scopes, never wider (A2). Those same scopes are what the
view renders affordances from (levels_from_scopes); the API enforces
regardless — client-side gating is UX, not security.

The audit service is the exception to the bearer lane: it
authenticates its versioned API with audit API keys, not campus
bearers, so its leg goes out with the deployment's audit UI key
(AUDIT_UI_API_KEY — the /audit section's credential, see
apps.admin.audit.client). Its migrations gate fails closed today
(audit keys carry no user principal, and no service sees token scopes
until campus#1183 lands); the view renders that as a caveat, not a
view bug.

Test injection: set ``clients_factory`` to a callable returning a
{service: JsonClient-double} dict; the call helpers then use those
stubs without touching the environment or the user session.
"""

__all__ = [
    "SERVICES",
    "STATUS_PATHS",
    "VOCABULARY_LEVELS",
    "clients_factory",
    "levels_from_scopes",
    "abort_apply",
    "apply_migrations",
    "fetch_statuses",
    "rollback_migration",
    "session_scopes",
]

import contextlib
import typing

from campus.common import env
from campus.common.services import get_base_url as get_service_base_url
from campus_python.json_client import CampusRequest

# The per-service sections, in page order. These are the runner
# scopes (#1104) — each service's ledger lives in its own DB.
SERVICES = ("auth", "api", "audit")

# Where each service mounts the shared migrations blueprint.
STATUS_PATHS = {
    "auth": "/auth/v1/admin/migrations",
    "api": "/api/v1/admin/migrations",
    "audit": "/audit/v1/admin/migrations",
}

# The migrations vocabulary levels (#1168), low to high. A held level
# covers every level at or below it (the same grants() semantics the
# service gates apply), so the token's highest migrations:<level>
# scope decides every affordance up to it.
VOCABULARY_LEVELS = ("read", "write", "admin")

# Test seam: a callable returning {service: JsonClient}; when set,
# the call helpers issue their requests against those stubs instead
# of opening the signed-in user's campus session.
clients_factory: typing.Callable[[], dict] | None = None


def levels_from_scopes(scopes: typing.Iterable[str] | None) -> dict[str, bool]:
    """The migrations levels a token's scopes cover.

    Args:
        scopes: The token's scope list (OAuthToken.scopes), or None
            when the session token could not be read (render with
            every affordance off — fail closed).

    Returns:
        {"read": bool, "write": bool, "admin": bool}: which levels
        the token covers. Levels are hierarchical — admin covers
        write covers read — mirroring the service gates' grants()
        semantics. Scopes outside the migrations vocabulary, and
        levels that are not vocabulary levels, are ignored.
    """
    if not scopes:
        return dict.fromkeys(VOCABULARY_LEVELS, False)
    held: list[int] = []
    for scope in scopes:
        resource, _, level = str(scope).partition(":")
        if resource == "migrations" and level in VOCABULARY_LEVELS:
            held.append(VOCABULARY_LEVELS.index(level))
    highest = max(held, default=-1)
    return {
        level: index <= highest
        for index, level in enumerate(VOCABULARY_LEVELS)
    }


def session_scopes(campus) -> list[str] | None:
    """The signed-in session's token scopes, or None when unavailable.

    One call to the login-session token route (the same lane
    with_user_session mints through — the auth service reuses the
    live grant token, so this is not a second issuance). None means
    the scopes could not be read; the view renders fail-closed.
    """
    try:
        return list(campus.auth.get_token().scopes or [])
    except Exception:  # noqa: BLE001 - scopes are a UX affordance only
        return None


def _error_text(exc: Exception) -> str:
    """A one-line message for a transport-level failure."""
    return f"{type(exc).__name__}: {exc}"


def _safe_json(response) -> dict | None:
    """Parse a service response body, tolerating non-JSON bodies."""
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict):
            return body
    return None


def _call(client, method: str, path: str,
          json_body: dict | None = None) -> dict:
    """One wire call, shaped {status_code, body, error}.

    Transport failures (SDK ServerError on network problems) come
    back as error text with status 0; service responses keep their
    status code and parsed body (None when not JSON) with error None
    — HTTP-level failures are the service's answer, not a transport
    error, and carry envelopes the view relays verbatim.
    """
    try:
        if method == "POST":
            response = client.post(path, json=json_body)
        else:
            response = client.get(path)
    except Exception as exc:  # noqa: BLE001 - relayed to the section
        return {"status_code": 0, "body": None, "error": _error_text(exc)}
    return {
        "status_code": response.status_code,
        "body": _safe_json(response),
        "error": None,
    }


@contextlib.contextmanager
def _service_clients(campus):
    """The per-service JSON clients for one batch of calls.

    Production opens the signed-in user's campus session: the bearer
    lands on the shared auth and api clients for the block (restored
    on exit), and the audit leg goes out with the deployment's audit
    UI key on its own client. Tests swap the whole batch via
    clients_factory.
    """
    if clients_factory is not None:
        yield clients_factory()
        return
    with campus.with_user_session() as user_client:
        audit_client = CampusRequest(
            base_url=get_service_base_url("campus.audit"), mode="device",
        )
        api_key = env.get("AUDIT_UI_API_KEY")
        if api_key:
            audit_client.set_bearer_authorization(api_key)
        yield {
            "auth": user_client.auth.client,
            "api": user_client.api.client,
            "audit": audit_client,
        }


def fetch_statuses(campus) -> tuple[dict[str, dict], list[str] | None]:
    """The migration status payload of every service, plus the
    session token's scopes.

    Returns:
        ({service: {status_code, body, error}}, scopes). A failure to
        open the user session at all (expired login, unreachable auth
        service) yields the same error for every service and None
        scopes; per-service failures (403 caveats, network) fill only
        that service's entry. Datetimes in the bodies are ISO strings.
    """
    scopes = session_scopes(campus)
    results: dict[str, dict] = {}
    try:
        with _service_clients(campus) as clients:
            for service in SERVICES:
                results[service] = _call(
                    clients[service], "GET", STATUS_PATHS[service],
                )
    except Exception as exc:  # noqa: BLE001 - the page renders it
        message = _error_text(exc)
        for service in SERVICES:
            results.setdefault(service, {
                "status_code": 0, "body": None, "error": message,
            })
    return results, scopes


def apply_migrations(
        campus, service: str, up_to: str | None = None,
) -> dict:
    """POST apply for one service; 202 starts the background run.

    The optional up_to stages the window: only revisions at or below
    it run (the service validates the revision, 400 when unknown).
    """
    body = {"up_to": up_to} if up_to else None
    return _mutate(campus, service, "/apply", body)


def abort_apply(campus, service: str) -> dict:
    """POST abort for one service; 409 when nothing is running."""
    return _mutate(campus, service, "/abort")


def rollback_migration(
        campus, service: str, revision: str,
        from_backup: str | None = None,
) -> dict:
    """POST rollback of one applied revision (synchronous).

    from_backup names the backup envelope to restore for
    backup-declared migrations (a local path or a bucket:// URI);
    the service refuses (400) when a backup rollback lacks it and
    409s undeclared, hash-guarded or conflicting rollbacks.
    """
    body = {"revision": revision}
    if from_backup:
        body["from_backup"] = from_backup
    return _mutate(campus, service, "/rollback", body)


def _mutate(campus, service: str, action_path: str,
            json_body: dict | None = None) -> dict:
    """One mutation call against one service."""
    with _service_clients(campus) as clients:
        return _call(
            clients[service], "POST", STATUS_PATHS[service] + action_path,
            json_body,
        )


def service_error_text(result: dict, fallback: str) -> str:
    """A human-readable line from one _call-shaped result.

    Transport failures carry error text; service errors carry an
    envelope the keys page also handles ({"error": {"message": ...}}
    or {"error": "<string>"}); anything else falls back.
    """
    if result.get("error"):
        return str(result["error"])
    body = result.get("body")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    return fallback
