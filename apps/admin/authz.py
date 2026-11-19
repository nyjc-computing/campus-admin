"""apps.admin.authz

Per-user capability probes behind the portal's affordances.

The portal renders navigation states from what each signed-in user can
actually do, instead of hard-failing at click time:

- **Section pills** (Admin / Audit / Resources in the shared top
  navbar): enabled for members, disabled with a tooltip reason for
  signed-in non-members. The probes are the same ones the pages' gates
  use — circle membership for the admin and resources sections (the
  domain access control), the AUDIT_ADMINS superadmin list for audit —
  so the nav can never claim an affordance the gate would refuse.
- **Migrations affordances**: the two-leg model means a token scope
  (capability, acquired via the step-up re-auth) AND a grant row
  (designation, seeded via the campus grant store) must both hold.
  Tokens carry whatever /login or the step-up requested — the auth
  service does not narrow issuance by designation — so grant state is
  probed separately through the operator-readable grants.check route
  and cached briefly.

Everything is fail-closed with a visible reason: a probe that cannot
answer (network, backend error) renders the affordance disabled with
"check unavailable" rather than guessing. None of this is the security
boundary — the gates and the services enforce on every call; this
module is UX truth-telling.

Caching: membership and grant answers are cached process-local for a
short TTL (mirroring flask_campus.circles.MembersCache semantics), so
warm page renders make no extra upstream calls. Changes apply within
the TTL window.
"""

__all__ = [
    "GrantCache",
    "circle_member",
    "has_grant",
    "init_app",
    "migrations_capability",
    "section_membership",
]

import time
import typing

import flask
from campus.flask_campus import circles

from apps.admin.audit import gate as audit_gate

# How long a probe answer may be reused across requests.
PROBE_TTL_SECONDS = 60.0

# Distinguishes a cache miss from a cached False.
_MISS = object()


class GrantCache:
    """Process-local TTL cache of grant-store check answers.

    Keys are (grantee_id, resource_type, level); values are booleans.
    Expired entries are dropped lazily on read, like
    flask_campus.circles.MembersCache. Probe failures are NOT cached —
    they retry on the next request so a blip never sticks for the TTL.
    """

    def __init__(self, ttl_seconds: float = PROBE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str, str], tuple[float, bool]] = {}

    def get(self, grantee_id: str, resource_type: str, level: str) -> bool | object:
        entry = self._entries.get((grantee_id, resource_type, level))
        if entry is None:
            return _MISS
        expires_at, answer = entry
        if time.monotonic() >= expires_at:
            self._entries.pop((grantee_id, resource_type, level), None)
            return _MISS
        return answer

    def put(self, grantee_id: str, resource_type: str, level: str,
            answer: bool) -> None:
        self._entries[(grantee_id, resource_type, level)] = (
            time.monotonic() + self.ttl_seconds, answer,
        )


_grant_cache = GrantCache()
_circle_cache = circles.MembersCache()
_scopes_cache: dict[str, tuple[float, list[str]]] = {}
_SCOPES_TTL_SECONDS = PROBE_TTL_SECONDS


def has_grant(campus, user_id: str, *, resource: str, level: str,
              ) -> bool | None:
    """Whether the user's grant row covers a management level.

    The grant store's check route accepts the deployment operator —
    which the portal's own client is — so the portal can answer this
    for any signed-in user without their token. Returns None when the
    store could not be reached (fail-closed callers render the
    affordance disabled with "check unavailable"); the failure is not
    cached.
    """
    cached = _grant_cache.get(user_id, resource, level)
    if cached is not _MISS:
        return typing.cast(bool, cached)
    try:
        answer = bool(campus.auth.grants.check(
            grantee_type="user",
            grantee_id=user_id,
            resource_type=resource,
            level=level,
        ))
    except Exception as err:  # noqa: BLE001 - affordance probe only
        flask.current_app.logger.warning(
            "Grant check for %s on %s:%s failed: %s",
            user_id, resource, level, err,
        )
        return None
    _grant_cache.put(user_id, resource, level, answer)
    return answer


def circle_member(campus, circle: str, user_id: str) -> bool | None:
    """Whether the user is a member of a named circle, or None when the
    circles backend could not answer (the gate's semantics, without the
    abort — the nav degrades instead)."""
    try:
        return circles.is_member(campus, _circle_cache, circle, user_id)
    except circles.CirclesUnavailableError as err:
        flask.current_app.logger.warning(
            "Circle membership unavailable for nav: %s", err,
        )
        return None


def session_scopes(campus) -> list[str] | None:
    """The signed-in session's token scopes (briefly cached), or None.

    Same lane as apps.admin.migrations.client.session_scopes, cached
    per user so the nav can afford it on every page render. A step-up
    re-auth mints a new token; the cache lag (one TTL) only affects the
    nav affordance, never the views (they read fresh scopes).
    """
    user = getattr(flask.g, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return None
    entry = _scopes_cache.get(user_id)
    now = time.monotonic()
    if entry is not None:
        expires_at, scopes = entry
        if now < expires_at:
            return scopes
        _scopes_cache.pop(user_id, None)
    try:
        scopes = list(campus.auth.get_token().scopes or [])
    except Exception:  # noqa: BLE001 - affordance probe only
        return None
    _scopes_cache[user_id] = (now + _SCOPES_TTL_SECONDS, scopes)
    return scopes


def section_membership(
        campus, admin_circle: str,
) -> dict[str, dict[str, typing.Any]]:
    """The signed-in user's membership state for each section pill.

    Returns {"admin" | "audit" | "resources": {"enabled": bool,
    "reason": str | None}}. admin and resources share the admin
    section's circle gate, so one probe answers both.
    """
    user = getattr(flask.g, "user", None)
    user_id = str(getattr(user, "id", "") or "") if user else ""
    if not user_id:
        # Anonymous: the pills lead to sign-in; nothing to probe.
        return {
            section: {"enabled": True, "reason": None}
            for section in ("admin", "audit", "resources")
        }

    admin = circle_member(campus, admin_circle, user_id)
    admin_circle_reason = (
        f"Requires membership in the '{admin_circle}' circle (ADMIN_CIRCLE)."
    )
    probe_reason = "Membership check unavailable; try again shortly."
    admin_state = (
        {"enabled": True, "reason": None} if admin
        else {
            "enabled": False,
            "reason": admin_circle_reason if admin is False else probe_reason,
        }
    )
    audit_ok = audit_gate.is_superadmin()
    audit_state = (
        {"enabled": True, "reason": None} if audit_ok
        else {
            "enabled": False,
            "reason": (
                "Requires membership in the AUDIT_ADMINS superadmin list."
            ),
        }
    )
    return {
        "admin": admin_state,
        "resources": admin_state,
        "audit": audit_state,
    }


def migrations_grant_reason(grant_ok: bool | None, user_id: str) -> str | None:
    """Why migrations affordances are disabled on the designation leg.

    None when the grant leg is satisfied (or unknown user); a user-facing
    sentence otherwise — the templates and the view share this text.
    """
    if grant_ok is False:
        return (
            "Your account holds no migrations grant. Grants are seeded "
            "via the campus grant store "
            "(campus grant grant --grantee-type user "
            f"--grantee-id {user_id} --resource migrations --level read)."
        )
    if grant_ok is None:
        return (
            "The grant store could not be reached, so migrations "
            "availability is unknown. Try again shortly."
        )
    return None


def migrations_capability(campus) -> dict[str, typing.Any]:
    """The signed-in user's migrations state: both two-leg legs.

    Returns {"scopes": bool | None, "grant": bool | None, "enabled":
    bool, "reason": str | None}. The sidebar link (and the view's
    panel) disables on the grant leg — a missing designation cannot be
    self-served, while a missing scope is what the step-up re-auth
    fixes, so the link stays clickable in that state and the view
    offers the one-click enable.
    """
    user = getattr(flask.g, "user", None)
    user_id = str(getattr(user, "id", "") or "") if user else ""
    if not user_id:
        return {"scopes": None, "grant": None, "enabled": True,
                "reason": None}

    scopes = session_scopes(campus)
    scopes_ok = (
        None if scopes is None
        else any(str(s).startswith("migrations:") for s in scopes)
    )
    grant_ok = has_grant(campus, user_id, resource="migrations", level="read")
    reason = migrations_grant_reason(grant_ok, user_id)
    return {
        "scopes": scopes_ok,
        "grant": grant_ok,
        # The link is clickable unless the designation leg blocks it —
        # a missing scope is fixable in-view via the step-up.
        "enabled": grant_ok is True,
        "reason": reason,
    }


def init_app(app: flask.Flask, *, campus, admin_circle: str) -> None:
    """Register the nav-capabilities context processor.

    Every template sees `nav`; probes run per request and are cached
    (per-request memo on flask.g so multiple includes in one render
    share the answer, plus the module TTL caches across requests).
    """

    @app.context_processor
    def _nav_capabilities() -> dict[str, typing.Any]:
        if not getattr(flask.g, "user", None):
            return {"nav": {}}
        if not hasattr(flask.g, "_nav_capabilities"):
            flask.g._nav_capabilities = {
                "sections": section_membership(campus, admin_circle),
                "migrations": migrations_capability(campus),
            }
        return {"nav": flask.g._nav_capabilities}
