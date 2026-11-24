"""apps.admin.authz

Who may do what in the portal — answered by the service that owns the
authority.

The portal is a UI over three services, and each one owns its own
authorization model. Nothing here is a parallel allowlist: this module
asks the owning service about the signed-in user and turns the answer
into the gates the views enforce AND the nav affordances, so the two
cannot disagree.

**campus.auth — the Admin section** (clients, users, sessions, vaults,
credentials, migrations). Management authority is the access-grant
store (#883): a *designated admin* is a user holding a grant row for a
vocabulary whose token also carries that scope — both legs, enforced by
campus.auth on every call (#865). The portal additionally *is* its
deployment's operator client (AUTH_OPERATOR_CLIENT_IDS), so a second
kind of portal user exists: a *platform operator* — a holder of `admin`
on the operator circle (ADMIN_CIRCLE, default `campus`, the campus.api
platform wildcard) — who drives that client and gets the full operator
console. Every admin-section view runs in one of the two lanes
(`admin_lane`): platform operators act as the operator client,
designated admins as themselves, so the wire principal always matches
the authority that admitted them. Portal entry follows the 2026-11-17
ruling (campus#1129 / campus-admin#72): any account holding at least
one grant row may use the portal.

**campus.api — the Resources section.** Authority is circles
(campus docs/circles-access-control.md): the signed-in user's effective
access value per resource family, group-scope and self-scope. Client
principals hold no domain authority there (#854), so the portal never
acts as its own client for campus.api — every call rides the signed-in
user's session and the service decides. `api_access` reads the same
gate-equivalent probe the service's gates answer through.

**campus.audit — the Audit section.** Authorized by API-key scope only;
the portal holds one shared UI key and decides locally who may drive it
(apps.admin.audit.gate).

Everything fails closed with a visible reason: a probe that cannot
answer renders the affordance disabled ("check unavailable") and makes
the gate refuse with 503, never with a guess. The services enforce on
every call regardless — this module is the portal's truth-telling.

Caching: answers are cached process-local for a short TTL, so warm
page renders make no extra upstream calls; changes apply within the
window. Probe failures are never cached.
"""

__all__ = [
    "ACCESS_CONSTANTS",
    "API_FAMILIES",
    "GrantCache",
    "LEVELS",
    "MIGRATION_GRANT_RESOURCES",
    "OPERATOR_CIRCLE",
    "VOCABULARIES",
    "Vocabulary",
    "admin_access",
    "admin_denial",
    "admin_lane",
    "api_access",
    "family_denial",
    "has_grant",
    "has_migrations_grant",
    "has_permission",
    "init_app",
    "migrations_capability",
    "migrations_grant_reason",
    "reset_caches",
    "scope_held",
    "section_membership",
    "vocabulary_denial",
    "vocabulary_states",
]

import contextlib
import logging
import time
import typing
import urllib.parse

import flask
from campus.common import env
from campus.flask_campus import circles
from campus_python.auth.v1.logins import LOGIN_STORAGE_KEY

from apps.admin.audit import gate as audit_gate

logger = logging.getLogger(__name__)

# How long a probe answer may be reused across requests.
PROBE_TTL_SECONDS = 60.0

# Circles and the portal's own client record are near-static
# registration data (docs/circles-access-control.md); a rename or a cap
# edit may take this long to show.
REGISTRY_TTL_SECONDS = 300.0

# Distinguishes a cache miss from a cached False / None.
_MISS = object()

# The migrations vocabulary (#1168) has no vocabulary-level rows —
# every row names its service (resource_id ∈ auth|api|audit) — so a
# resource_id-less grants.check can never match one (campus-admin#86).
MIGRATION_GRANT_RESOURCES = ("auth", "api", "audit")

# The campus.api platform-admin wildcard (campus#1216): `campus:admin`
# confers admin on every resource family, and the circle the portal's
# bootstrap seeds its platform admins into.
OPERATOR_CIRCLE = "campus"

# Management levels, low to high — campus.auth's scope algebra is
# monotonic within a vocabulary (campus/auth/scopes.py::grants).
LEVELS = ("read", "mod", "write", "admin")

# The campus.api access-value layout (ratified 2026-11-23):
# `[0 | G-c G-m G-r | S-c S-m S-r | 0]` — the group field at bit base
# 4, the self field at bit base 1. Mirrors
# campus/api/resources/circle.py::has_permission; the constants are
# part of the ratified contract.
_PERMISSION_SHIFTS = {"create": 2, "modify": 1, "read": 0}

# The five ratified access-value constants (campus#1216). An edge value
# MUST be one of them — campus.api validates at write time — and they
# are ordered: admin > editor > viewer > self > guest.
ACCESS_CONSTANTS: dict[str, int] = {
    "admin": 0b01111110,    # 126: create/modify/read, group and own
    "editor": 0b00110110,   # 54: modify/read, group and own
    "viewer": 0b00010010,   # 18: read, group and own
    "self": 0b00001110,     # 14: own resources only: create/modify/read
    "guest": 0b00000010,    # 2: own resources only: read
}


class Vocabulary(typing.NamedTuple):
    """A campus.auth management vocabulary the Admin section surfaces.

    scope: the token-scope prefix (`<scope>:<level>`).
    resource: the grant store's resource_type.
    levels: the levels a grant row can hold.
    """

    scope: str
    resource: str
    levels: tuple[str, ...]
    label: str


# The Admin section's resource views, keyed by the sidebar's resource
# id. `clients:admin` and the sessions/credentials/migrations grant
# administrations are operator-only by construction upstream (rows are
# rejected as operator-equivalent or inert), so designated admins top
# out at `write` on clients.
VOCABULARIES: dict[str, Vocabulary] = {
    "clients": Vocabulary("clients", "clients", ("read", "write", "admin"),
                          "Clients"),
    "users": Vocabulary("users", "users", ("read", "mod", "write", "admin"),
                        "Users"),
    "sessions": Vocabulary("sessions", "sessions", ("read", "write"),
                           "Sessions"),
    "credentials": Vocabulary("credentials", "credentials", ("read", "write"),
                              "Credentials"),
    # Vault rows are per label (resource_id names the label).
    "vaults": Vocabulary("vaults", "vault", ("read", "write", "admin"),
                         "Vaults"),
    # Migrations rows are per service DB (resource_id ∈ auth|api|audit).
    "migrations": Vocabulary("migrations", "migrations",
                             ("read", "write", "admin"), "Migrations"),
}

# The campus.api resource families the Resources section manages —
# each is a grant circle of the same name.
API_FAMILIES = (
    "circles", "venues", "timetable", "assignments", "submissions",
    "subjcombi", "files", "hostedapps",
)

_NO_ACCESS = {"create": False, "modify": False, "read": False}
_FULL_ACCESS = {"create": True, "modify": True, "read": True}


class GrantCache:
    """Process-local TTL cache of grant-store check answers.

    Keys are (grantee_id, resource_type, level, resource_id); values
    are booleans. Expired entries are dropped lazily on read, like
    flask_campus.circles.MembersCache. Probe failures are NOT cached —
    they retry on the next request so a blip never sticks for the TTL.
    """

    def __init__(self, ttl_seconds: float = PROBE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str, str, str], tuple[float, bool]] = {}

    def get(self, grantee_id: str, resource_type: str, level: str,
            resource_id: str = "") -> bool | object:
        entry = self._entries.get(
            (grantee_id, resource_type, level, resource_id))
        if entry is None:
            return _MISS
        expires_at, answer = entry
        if time.monotonic() >= expires_at:
            self._entries.pop((grantee_id, resource_type, level, resource_id),
                              None)
            return _MISS
        return answer

    def put(self, grantee_id: str, resource_type: str, level: str,
            resource_id: str, answer: bool) -> None:
        self._entries[(grantee_id, resource_type, level, resource_id)] = (
            time.monotonic() + self.ttl_seconds, answer,
        )


class TtlCache:
    """Process-local TTL cache of probe answers of any shape.

    Same lazy-expiry semantics as GrantCache; a cached value may be
    None (a definitive negative answer), which is distinct from a miss.
    """

    def __init__(self, ttl_seconds: float = PROBE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[typing.Hashable, tuple[float, typing.Any]] = {}

    def get(self, key: typing.Hashable) -> typing.Any:
        entry = self._entries.get(key)
        if entry is None:
            return _MISS
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return _MISS
        return value

    def put(self, key: typing.Hashable, value: typing.Any) -> None:
        self._entries[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self) -> None:
        self._entries.clear()


_grant_cache = GrantCache()
_operator_cache = TtlCache()
_rows_cache = TtlCache()
_api_cache = TtlCache()
_cap_cache = TtlCache(REGISTRY_TTL_SECONDS)
# user_id -> (expires_at, scopes, login-session id)
_scopes_cache: dict[str, tuple[float, list[str], str]] = {}
_SCOPES_TTL_SECONDS = PROBE_TTL_SECONDS


def reset_caches() -> None:
    """Drop every cached probe answer (tests; also handy after seeding)."""
    for cache in (_grant_cache, _operator_cache, _rows_cache, _api_cache,
                  _cap_cache):
        cache._entries.clear()
    _scopes_cache.clear()


def _signed_in_user_id() -> str:
    user = getattr(flask.g, "user", None)
    return str(getattr(user, "id", "") or "") if user else ""


def has_permission(value: int, scope: str, permission: str) -> bool:
    """Whether an access value carries `permission` in its `scope` field.

    `c⇒m⇒r` holds within a field, so a create check answers only the
    create bit (the service gate's own reading).
    """
    base = 4 if scope == "group" else 1
    return bool(value & (1 << (_PERMISSION_SHIFTS[permission] + base)))


def _fields(value: int) -> dict[str, bool]:
    return {
        permission: has_permission(value, "group", permission)
        for permission in ("create", "modify", "read")
    }


def scope_held(scopes: typing.Iterable[str] | None, scope: str,
               level: str) -> bool:
    """Whether a token's scopes cover `<scope>:<level>`.

    Mirrors campus.auth's scope algebra (campus/auth/scopes.py::grants):
    monotonic within a vocabulary, exact on the vocabulary name.
    """
    if not scopes:
        return False
    required = LEVELS.index(level)
    for held in scopes:
        resource, sep, held_level = str(held).partition(":")
        if (sep and resource == scope and held_level in LEVELS
                and LEVELS.index(held_level) >= required):
            return True
    return False


def has_grant(campus, user_id: str, *, resource: str, level: str,
              resource_id: str = "") -> bool | None:
    """Whether the user's grant row covers a management level.

    The grant store's check route accepts the deployment operator —
    which the portal's own client is — so the portal can answer this
    for any signed-in user without their token. `resource_id` scopes
    the check to a store-row instance (vault label, or the migrations
    vocabulary's per-service subsets); an empty resource_id checks the
    vocabulary level, which for migrations matches nothing (#1168).
    Returns None when the store could not be reached (fail-closed
    callers render the affordance disabled with "check unavailable");
    the failure is not cached.
    """
    cached = _grant_cache.get(user_id, resource, level, resource_id)
    if cached is not _MISS:
        return typing.cast(bool, cached)
    try:
        answer = bool(campus.auth.grants.check(
            grantee_type="user",
            grantee_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            level=level,
        ))
    except Exception as err:  # noqa: BLE001 - affordance probe only
        flask.current_app.logger.warning(
            "Grant check for %s on %s:%s[%s] failed: %s",
            user_id, resource, level, resource_id, err,
        )
        return None
    _grant_cache.put(user_id, resource, level, resource_id, answer)
    return answer


def has_migrations_grant(campus, user_id: str, *, level: str = "read",
                         ) -> bool | None:
    """Whether any per-service migrations subset covers the level.

    The migrations vocabulary (#1168) has no vocabulary-level rows —
    each row names its service — so the designation probe asks the
    auth/api/audit subsets and passes when any answers True. Returns
    None only when no subset answered True and at least one check
    could not be reached (fail-closed); answers cache per subset, so a
    newly seeded row lights up within one TTL.
    """
    answers = [
        has_grant(campus, user_id, resource="migrations", level=level,
                  resource_id=service)
        for service in MIGRATION_GRANT_RESOURCES
    ]
    if any(answer is True for answer in answers):
        return True
    if any(answer is None for answer in answers):
        return None
    return False


def operator_access_value(campus, circle: str, user_id: str) -> int | None:
    """The signed-in user's resolved access value in the operator circle.

    None when they are not a member. The probe is the server's shared
    membership resolver (campus#1202) — the gate's own question — run as
    the signed-in user, never with the portal's client credentials.

    Raises:
        circles.CirclesUnavailableError: If the circles backend (or the
            user token flow backing the check) fails to answer.
    """
    cached = _operator_cache.get((circle, user_id))
    if cached is not _MISS:
        return typing.cast(int | None, cached)
    try:
        with campus.with_user_session() as client:
            matches = [
                c for c in client.api.circles.list() if c.name == circle
            ]
            if not matches:
                logger.warning(
                    "No circle named '%s' is configured: this deployment "
                    "has no platform operators (the campus.api bootstrap "
                    "seeds '%s')", circle, OPERATOR_CIRCLE,
                )
                value = None
            else:
                resolution = client.api.circles[
                    matches[0].id
                ].members.resolve(user_id)
                value = (
                    resolution.get("access_value")
                    if isinstance(resolution, dict) else None
                )
    except circles.CirclesUnavailableError:
        raise
    except Exception as err:  # noqa: BLE001 - probe only
        logger.error("Operator circle check failed: %s", err)
        raise circles.CirclesUnavailableError(
            f"Could not answer membership in '{circle}': {err}"
        ) from err
    if not isinstance(value, int) or isinstance(value, bool):
        value = None
    _operator_cache.put((circle, user_id), value)
    return value


def is_platform_operator(campus, circle: str, user_id: str) -> bool | None:
    """Whether the user holds `admin` on the operator circle.

    Only a value carrying group-create (the `admin` constant) is the
    platform-operator grant; a lesser membership does not qualify. None
    when the circles backend could not answer.
    """
    try:
        value = operator_access_value(campus, circle, user_id)
    except circles.CirclesUnavailableError as err:
        flask.current_app.logger.warning(
            "Operator circle unavailable: %s", err,
        )
        return None
    return value is not None and has_permission(value, "group", "create")


def grant_rows(campus, user_id: str) -> list[dict] | None:
    """The user's rows in the campus.auth access-grant store.

    Read with the portal's operator client — the store's list route is
    operator-readable, so the portal can answer for any signed-in user
    without their token. None when the store could not be reached.
    """
    cached = _rows_cache.get(user_id)
    if cached is not _MISS:
        return typing.cast(list[dict] | None, cached)
    try:
        body = campus.auth.grants.list(
            grantee_type="user", grantee_id=user_id,
        )
        rows = [
            row for row in (body or {}).get("grants", [])
            if isinstance(row, dict)
        ]
    except Exception as err:  # noqa: BLE001 - probe only
        flask.current_app.logger.warning(
            "Grant rows for %s could not be read: %s", user_id, err,
        )
        return None
    _rows_cache.put(user_id, rows)
    return rows


def session_scopes(campus) -> list[str] | None:
    """The signed-in session's token scopes (briefly cached), or None.

    Same lane as apps.admin.migrations.client.session_scopes, cached
    per login session so the nav can afford it on every page render.
    A step-up re-auth ends in a NEW login session (flask_campus
    finalize_login), so keying on the login-session id makes the wider
    token visible on the very next request instead of after the TTL.
    """
    user_id = _signed_in_user_id()
    if not user_id:
        return None
    login_id = str(flask.session.get(LOGIN_STORAGE_KEY) or "")
    entry = _scopes_cache.get(user_id)
    now = time.monotonic()
    if entry is not None:
        expires_at, scopes, cached_login = entry
        if now < expires_at and cached_login == login_id:
            return scopes
        _scopes_cache.pop(user_id, None)
    try:
        scopes = list(campus.auth.get_token().scopes or [])
    except Exception:  # noqa: BLE001 - affordance probe only
        return None
    _scopes_cache[user_id] = (now + _SCOPES_TTL_SECONDS, scopes, login_id)
    return scopes


def portal_scope_cap(campus) -> frozenset[str] | None:
    """The portal client's own allowed_scopes cap, or None if unreadable.

    A user token can carry a management scope only if the minting
    client's cap includes it (docs/auth-token-invariants.md A1/A8), so
    the step-up asks only for scopes the cap covers. The portal client
    may read its own record (#881).
    """
    client_id = env.get("CLIENT_ID") or ""
    cached = _cap_cache.get(client_id)
    if cached is not _MISS:
        return typing.cast(frozenset[str] | None, cached)
    try:
        record = campus.auth.clients[client_id].get()
        cap = frozenset(
            str(scope) for scope in (getattr(record, "allowed_scopes", None)
                                     or [])
        )
    except Exception as err:  # noqa: BLE001 - affordance planning only
        flask.current_app.logger.warning(
            "Portal scope cap could not be read: %s", err,
        )
        return None
    _cap_cache.put(client_id, cap)
    return cap


def _granted_level(rows: list[dict], vocabulary: Vocabulary) -> str | None:
    """The highest grant-row level the user holds in a vocabulary."""
    held = [
        LEVELS.index(str(row.get("level")))
        for row in rows
        if row.get("resource_type") == vocabulary.resource
        and str(row.get("level")) in LEVELS
    ]
    return LEVELS[max(held)] if held else None


def vocabulary_states(access: dict) -> dict[str, dict[str, typing.Any]]:
    """Per-vocabulary affordance states for the Admin section.

    Each state says whether the signed-in user may read and write the
    vocabulary's resources and, when not, why — the same answer the
    view gates act on. In the operator lane every vocabulary is fully
    open; in the designated lane the grant row fixes the ceiling and
    the session token's scopes decide whether it is reachable yet.
    """
    states: dict[str, dict[str, typing.Any]] = {}
    user_id = access["user_id"]
    for vocab_id, vocabulary in VOCABULARIES.items():
        if access["lane"] == "operator":
            states[vocab_id] = {
                "granted": "admin", "scope": True, "readable": True,
                "writable": True, "operator_only": True, "reason": None,
                "labels": {},
            }
            continue
        granted = _granted_level(access["grants"] or [], vocabulary)
        scope = (
            None if access["scopes"] is None or granted is None
            else scope_held(access["scopes"], vocabulary.scope, granted)
        )
        if granted is None:
            reason = (
                f"Your account holds no {vocabulary.label.lower()} grant. "
                "Grants are seeded in the Campus access-grant store by an "
                "operator (campus grant grant --grantee-type user "
                f"--grantee-id {user_id} --resource {vocabulary.resource} "
                "--level read)."
            )
        elif scope is None:
            reason = (
                "The session's token scopes could not be read; try again "
                "shortly."
            )
        elif not scope:
            reason = (
                f"This session's token does not carry the "
                f"{vocabulary.scope} scope yet — one re-authentication "
                "enables it."
            )
        else:
            reason = None
        states[vocab_id] = {
            "granted": granted,
            "scope": scope,
            "readable": granted is not None and scope is True,
            "writable": (
                granted is not None and scope is True
                and LEVELS.index(granted) >= LEVELS.index("write")
            ),
            "operator_only": False,
            "reason": reason,
            # Vault rows are per label: which labels this user may open.
            "labels": {
                str(row.get("resource_id")): str(row.get("level"))
                for row in (access["grants"] or [])
                if vocabulary.resource == "vault"
                and row.get("resource_type") == "vault"
            },
        }
    return states


def _plan_stepup(
    states: dict[str, dict[str, typing.Any]], cap: frozenset[str] | None,
) -> dict[str, typing.Any]:
    """Which granted vocabularies a step-up re-auth can enable.

    A step-up is worth offering exactly when a grant row exists and the
    token lacks its scope; it asks for every level up to the granted
    one (campus.auth compares levels monotonically). The portal client's
    cap bounds what may be requested: a vocabulary whose scopes the cap
    does not cover is `blocked` — nothing the user can do, the
    deployment widens the cap.
    """
    scopes: list[str] = []
    blocked: list[str] = []
    for vocab_id, state in states.items():
        if state["granted"] is None or state["scope"] is not False:
            continue
        vocabulary = VOCABULARIES[vocab_id]
        wanted = [
            f"{vocabulary.scope}:{level}" for level in vocabulary.levels
            if LEVELS.index(level) <= LEVELS.index(state["granted"])
        ]
        allowed = [
            scope for scope in wanted if cap is None or scope in cap
        ]
        if allowed:
            scopes.extend(scope for scope in allowed if scope not in scopes)
        else:
            blocked.append(vocab_id)
    return {"scopes": scopes, "blocked": blocked}


def admin_access(campus, operator_circle: str) -> dict[str, typing.Any]:
    """The signed-in user's Admin-section access (memoized per request).

    Answers the 2026-11-17 ruling's gate — a platform operator OR an
    account holding at least one access grant — together with the lane
    each view must run in and the per-vocabulary affordances:

        operator:   True / False (platform operator), None if unknown
        lane:       "operator" | "designated" | None
        grants:     the user's grant rows (designated lane), None if
                    the store could not be reached
        admitted:   the gate's answer
        unavailable: the gate could not answer (fail closed, 503)
        scopes:     the session token's scopes, None if unreadable
        vocabularies, stepup: see vocabulary_states / _plan_stepup
    """
    if hasattr(flask.g, "_admin_access"):
        return typing.cast(dict[str, typing.Any], flask.g._admin_access)

    user_id = _signed_in_user_id()
    operator = is_platform_operator(campus, operator_circle, user_id)
    grants = None if operator else grant_rows(campus, user_id)
    scopes = session_scopes(campus)
    if operator:
        lane = "operator"
    elif grants:
        lane = "designated"
    else:
        lane = None
    access: dict[str, typing.Any] = {
        "user_id": user_id,
        "operator": operator,
        "lane": lane,
        "grants": grants,
        "scopes": scopes,
        "admitted": lane is not None,
        # Nobody admitted and at least one authority could not answer:
        # the gate must not turn an outage into a denial.
        "unavailable": lane is None and (operator is None or grants is None),
    }
    access["vocabularies"] = vocabulary_states(access)
    access["stepup"] = _plan_stepup(
        access["vocabularies"],
        None if lane == "operator" else portal_scope_cap(campus),
    )
    flask.g._admin_access = access
    return access


ADMIN_DENIED_REASON = (
    "The Admin section is for platform operators and designated admins: "
    "hold at least one access grant in the Campus auth grant store, or "
    "admin on the portal's operator circle ({circle})."
)


def admin_denial(campus, operator_circle: str) -> flask.Response | None:
    """The Admin-section gate as a before-request / decorator answer.

    Anonymous sessions are sent to sign-in; a session the authorities
    cannot answer for gets 503; a signed-in user holding neither
    authority gets 403. None admits the request.
    """
    if not getattr(flask.g, "user", None):
        return flask.redirect(
            flask.url_for("auth.login", next=flask.request.path)
        )
    access = admin_access(campus, operator_circle)
    if access["unavailable"]:
        flask.abort(503, description=(
            "Authorization backend unavailable; try again shortly."
        ))
    if not access["admitted"]:
        flask.abort(403, description=ADMIN_DENIED_REASON.format(
            circle=operator_circle,
        ))
    return None


def vocabulary_denial(
    campus, operator_circle: str, vocab_id: str, level: str = "read",
) -> flask.Response | None:
    """Whether the signed-in user may act on one admin vocabulary.

    None admits the request. Otherwise a page explaining the missing
    leg: a missing grant row or a level above it is a 403 (nothing
    self-serves a designation); a grant the token does not carry yet
    renders the step-up affordance (200) when the portal's scope cap
    allows it, or names the cap otherwise.
    """
    access = admin_access(campus, operator_circle)
    state = access["vocabularies"][vocab_id]
    vocabulary = VOCABULARIES[vocab_id]
    if state["readable"] and (
        level == "read" or LEVELS.index(state["granted"]) >= LEVELS.index(level)
    ):
        return None

    granted = state["granted"]
    if granted is None:
        reason, status = state["reason"], 403
    elif LEVELS.index(granted) < LEVELS.index(level):
        if vocab_id == "clients" and level == "admin":
            reason = (
                "Registering, deleting and rotating clients and setting "
                "their scope caps and vault access is reserved for the "
                "deployment operator — a shared client secret is the only "
                "principal upstream accepts here (campus#883 decision 4). "
                "Platform operators (admin on the "
                f"'{operator_circle}' circle) act as the operator."
            )
        else:
            reason = (
                f"Your {vocabulary.label.lower()} grant covers "
                f"{granted}, not {level}. An operator raises the level in "
                "the Campus access-grant store."
            )
        status = 403
    else:
        status = 200
        if state["scope"] is None:
            reason = state["reason"]
        elif vocab_id in access["stepup"]["blocked"]:
            reason = (
                f"Your {vocabulary.label.lower()} grant is in place, but "
                "this deployment's portal client does not allow the "
                f"{vocabulary.scope} scopes (allowed_scopes cap). Ask the "
                "deployment owner to widen the cap."
            )
        else:
            reason = state["reason"]
    return flask.make_response(flask.render_template(
        "admin_access.html",
        active=vocab_id,
        vocabulary=vocabulary,
        state=state,
        reason=reason,
        stepup=(
            access["stepup"]["scopes"]
            if status == 200 and state["scope"] is False
            and vocab_id not in access["stepup"]["blocked"] else None
        ),
    ), status)


@contextlib.contextmanager
def admin_lane(campus, operator_circle: str):
    """Position the campus client for the calling view's admin lane.

    Platform operators keep the portal's operator credentials — the
    operator console. Designated admins act as themselves: the block
    runs inside their campus session, so campus.auth authorizes every
    call against their own grant row and token scopes. Enter it only
    around upstream calls, never around template rendering (the nav
    probes read the grant store with the operator client).
    """
    if admin_access(campus, operator_circle)["lane"] == "operator":
        yield campus
        return
    with campus.with_user_session() as user_client:
        yield user_client


def api_access(
    campus, user_id: str, *, operator: bool,
) -> dict[str, dict[str, bool]] | None:
    """The signed-in user's effective campus.api access per family.

    Each family answers through the service's gate-equivalent probe
    (GET /circles/{id}/access/{member}) — the same resolver its
    enforcement gates call — reduced to group-scope create/modify/read,
    the scope the section's management views act in. Platform operators
    short-circuit to full access, as the service's own resolver does
    for the `campus` wildcard. None when campus.api could not answer.
    """
    if operator:
        return {family: dict(_FULL_ACCESS) for family in API_FAMILIES}
    cached = _api_cache.get(user_id)
    if cached is not _MISS:
        return typing.cast(dict[str, dict[str, bool]] | None, cached)
    member = urllib.parse.quote(user_id, safe="")
    try:
        with campus.with_user_session() as client:
            grant_circles = {
                circle.name: str(circle.id)
                for circle in client.api.circles.list()
            }
            access: dict[str, dict[str, bool]] = {}
            for family in API_FAMILIES:
                circle_id = grant_circles.get(family)
                if circle_id is None:
                    # No grant circle for the family: the service's gate
                    # denies fail-closed, and so does the affordance.
                    access[family] = dict(_NO_ACCESS)
                    continue
                response = client.api.client.get(
                    f"/api/v1/circles/{circle_id}/access/{member}/",
                )
                if getattr(response, "status_code", None) != 200:
                    raise RuntimeError(
                        f"access probe for {family} returned "
                        f"{getattr(response, 'status_code', 'no status')}"
                    )
                access[family] = _fields(int(response.json()["g"]))
    except Exception as err:  # noqa: BLE001 - probe only
        flask.current_app.logger.warning(
            "campus.api access for %s could not be resolved: %s",
            user_id, err,
        )
        return None
    _api_cache.put(user_id, access)
    return access


RESOURCES_DENIED_REASON = (
    "The Resources section acts on campus.api resources as you, and you "
    "hold no campus.api access: it takes a primary-group baseline (staff) "
    "or a membership in a resource circle. Ask a circles administrator."
)


def family_denial(
    campus, operator_circle: str, family: str | None = None, *,
    write: bool = False,
) -> flask.Response | None:
    """The Resources-section gate, optionally for one family's view.

    Anonymous sessions are sent to sign-in. A session campus.api cannot
    answer for gets 503; a user with no readable family gets the
    section's 403. With a `family`, a user without its read — or, for a
    mutation, its group-scope create — gets a 403 naming the family.
    None admits the request.
    """
    if not getattr(flask.g, "user", None):
        return flask.redirect(
            flask.url_for("auth.login", next=flask.request.path)
        )
    user_id = _signed_in_user_id()
    access = api_access(
        campus, user_id,
        operator=admin_access(campus, operator_circle)["lane"] == "operator",
    )
    if access is None:
        flask.abort(503, description=(
            "Authorization backend unavailable; try again shortly."
        ))
    if not any(state["read"] for state in access.values()):
        flask.abort(403, description=RESOURCES_DENIED_REASON)
    state = access.get(family) if family is not None else None
    if state is None:
        return None
    if not state["read"] or (write and not state["create"]):
        flask.abort(403, description=(
            f"Your campus.api access on {family} does not include "
            + ("creating or changing its resources." if write
               else "reading it.")
            + " Access follows your circle memberships' access values."
        ))
    return None


def section_membership(
    campus, operator_circle: str, *, access: dict | None = None,
    api: dict[str, dict[str, bool]] | None = None,
) -> dict[str, dict[str, typing.Any]]:
    """The signed-in user's state for each section pill.

    Returns {"admin" | "audit" | "resources": {"enabled": bool,
    "reason": str | None}} — the same answers the pages' gates use, so
    the nav never offers an affordance a gate refuses. Callers that
    already hold this request's probes pass them in.
    """
    user = getattr(flask.g, "user", None)
    user_id = str(getattr(user, "id", "") or "") if user else ""
    if not user_id:
        # Anonymous: the pills lead to sign-in; nothing to probe.
        return {
            section: {"enabled": True, "reason": None}
            for section in ("admin", "audit", "resources")
        }

    access = access or admin_access(campus, operator_circle)
    if access["unavailable"]:
        admin_state = {"enabled": False,
                       "reason": "Access check unavailable; try again shortly."}
    elif access["admitted"]:
        admin_state = {"enabled": True, "reason": None}
    else:
        admin_state = {
            "enabled": False,
            "reason": ADMIN_DENIED_REASON.format(circle=operator_circle),
        }

    if api is None:
        api = api_access(
            campus, user_id, operator=access["lane"] == "operator",
        )
    if api is None:
        resources_state = {
            "enabled": False,
            "reason": "Access check unavailable; try again shortly.",
        }
    elif any(state["read"] for state in api.values()):
        resources_state = {"enabled": True, "reason": None}
    else:
        resources_state = {"enabled": False, "reason": RESOURCES_DENIED_REASON}

    audit_state = (
        {"enabled": True, "reason": None} if audit_gate.can_view_traces()
        else {
            "enabled": False,
            "reason": (
                "Requires membership in the AUDIT_ADMINS list on this "
                "deployment."
            ),
        }
    )
    return {
        "admin": admin_state,
        "resources": resources_state,
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
            f"--grantee-id {user_id} --resource migrations "
            "--resource-id auth|api|audit --level read)."
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

    The Migrations view always rides the signed-in user's own session
    (the service's gate wants the two legs, and client principals hold
    no migrations authority), so platform operators are probed like
    everyone else here.
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
    grant_ok = has_migrations_grant(campus, user_id)
    reason = migrations_grant_reason(grant_ok, user_id)
    return {
        "scopes": scopes_ok,
        "grant": grant_ok,
        # The link is clickable unless the designation leg blocks it —
        # a missing scope is fixable in-view via the step-up.
        "enabled": grant_ok is True,
        "reason": reason,
    }


def init_app(app: flask.Flask, *, campus, operator_circle: str) -> None:
    """Register the nav-capabilities context processor.

    Every signed-in template sees `nav` (section states, the admin
    lane's per-vocabulary states, the migrations two-leg state),
    `api_access` (per-family campus.api access) and `can_write(vocab)`
    — whether this session may change that Admin-section vocabulary's
    resources — plus `can_administer(vocab)`, whether it holds the
    vocabulary's top level, so templates can withhold the write
    controls. Probes run per request and are cached (per-request memo
    on flask.g so multiple includes in one render share the answer,
    plus the module TTL caches across requests).
    """

    def can_write(vocab_id: str) -> bool:
        if not getattr(flask.g, "user", None):
            return False
        return admin_access(campus, operator_circle)["vocabularies"][vocab_id][
            "writable"
        ]

    def can_administer(vocab_id: str) -> bool:
        """Whether this session holds a vocabulary's top level.

        The actions campus.auth reserves for `admin` — deleting an
        account is the one the portal offers — are withheld below it.
        """
        if not getattr(flask.g, "user", None):
            return False
        state = admin_access(campus, operator_circle)["vocabularies"][vocab_id]
        return state["granted"] == "admin" and state["scope"] is True

    def can(family: str, permission: str = "create") -> bool:
        """Whether campus.api lets the signed-in user act on a family.

        The gate-equivalent access answer the section's view gates use,
        so a template can withhold exactly the controls the service
        would refuse (write affordances need group-scope create).
        """
        if not getattr(flask.g, "user", None):
            return False
        access = api_access(
            campus, _signed_in_user_id(),
            operator=admin_access(campus, operator_circle)["lane"] == "operator",
        )
        return bool((access or {}).get(family, {}).get(permission))

    app.jinja_env.globals["can_write"] = can_write
    app.jinja_env.globals["can_administer"] = can_administer
    app.jinja_env.globals["can"] = can

    @app.context_processor
    def _nav_capabilities() -> dict[str, typing.Any]:
        if not getattr(flask.g, "user", None):
            return {"nav": {}, "api_access": {}}
        if not hasattr(flask.g, "_nav_capabilities"):
            access = admin_access(campus, operator_circle)
            api = api_access(
                campus, access["user_id"],
                operator=access["lane"] == "operator",
            )
            flask.g._nav_capabilities = {
                "sections": section_membership(
                    campus, operator_circle, access=access, api=api,
                ),
                "admin": access,
                "migrations": migrations_capability(campus),
            }
            flask.g._api_access = api or {}
        return {
            "nav": flask.g._nav_capabilities,
            "api_access": typing.cast(
                dict[str, dict[str, bool]], flask.g._api_access
            ),
            "access_constants": tuple(ACCESS_CONSTANTS.items()),
        }
