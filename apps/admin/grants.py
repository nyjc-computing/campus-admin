"""apps.admin.grants

The Grants view (/admin/grants): the campus.auth access-grant store —
the "who can administer what" matrix (#883) — for both grantee kinds:

- a user's rows designate them to manage a resource (users, clients,
  sessions, credentials, timetable, or one service's migrations) or one
  vault label, at a level (read < mod < write < admin);
- a client's rows are its access to vault labels, as CRUD bitflags —
  what the Clients page's Access modal shows.

**grant** creates a row or raises it (a management level never lowers:
revoke first, then grant again); **revoke** removes a row or, for a
client's vault row, only the bits named. campus.auth administers the
store behind its own gate on every call (campus
campus/auth/routes/grants.py): the operator may do anything, a user
holding <resource>:admin (grant row AND token scope) may administer
that resource alone and never their own rows, and it refuses the shapes
the form does not offer (an admin-level clients, sessions or
credentials row, a label-less vault row, a migrations row naming no
service). The view runs in the caller's admin lane like every other
Admin-section page (apps.admin.authz) and admits exactly who
campus.auth would (authz.grant_admin_denial).

Action forms carry single-use CSRF tokens (the migrations view's
convention — these actions confer and remove authority). Every action
redirects back to the filtered list; failures relay the service's own
text through flash messages.
"""

__all__ = ["init_app"]

import hmac
import logging
import secrets
import typing
import urllib.parse

import flask

from apps.admin import authz

logger = logging.getLogger(__name__)

# CSRF session key for the action forms (single-use, like the
# migrations view's).
GRANTS_CSRF_KEY = "grants_csrf"

# The list filters, each read from a field (query or form) of the same
# name.
FILTERS = ("grantee_type", "grantee_id", "resource_type", "resource_id")

GRANTEE_TYPES = ("user", "client")

# A client's vault access is a CRUD bitflag mask (campus
# campus/model/client.py::ClientAccess): READ, CREATE, UPDATE, DELETE.
VAULT_RESOURCE = "vault"
VAULT_BITS: tuple[tuple[str, int], ...] = (
    ("R", 1), ("C", 2), ("U", 4), ("D", 8),
)
VAULT_ALL_BITS = sum(bit for _, bit in VAULT_BITS)
VAULT_FLAG_MEANING = {"R": "read", "C": "create", "U": "update", "D": "delete"}


class GrantKey(typing.NamedTuple):
    """What a grant row is keyed by, and the permission it carries.

    Exactly one of `level` (a management row) and `bits` (a client's
    vault row) is set.
    """

    grantee_type: str
    grantee_id: str
    resource_type: str
    resource_id: str = ""
    level: str | None = None
    bits: int | None = None

    def sdk_args(self) -> dict[str, typing.Any]:
        """The keyword arguments the SDK's Grants.grant / .revoke take
        (both drop None fields and read resource_id "" as no instance)."""
        return self._asdict()


def _flags(mask: int | None) -> str:
    """A vault bitflag mask as its flag letters, in R C U D order."""
    return ", ".join(
        flag for flag, bit in VAULT_BITS if mask and bit & int(mask)
    )


def _name(key: GrantKey) -> str:
    """The resource a row names, instance included."""
    if key.resource_id:
        return f"{key.resource_type} {key.resource_id}"
    return key.resource_type


def describe(key: GrantKey) -> str:
    """A grant as campus-cli prints it: `users:write`, `migrations
    api:admin`, `vault google.classroom: R, C`."""
    if key.bits is not None:
        return f"{_name(key)}: {_flags(key.bits)}"
    return f"{_name(key)}:{key.level}"


def _key_fields(
    form: typing.Any,
) -> tuple[str, str, str, str] | str:
    """The key a form post names — (grantee_type, grantee_id,
    resource_type, resource_id) — or the sentence saying it names
    nothing."""
    grantee_type = (form.get("grantee_type") or "").strip()
    grantee_id = (form.get("grantee_id") or "").strip()
    resource_type = (form.get("resource_type") or "").strip()
    if grantee_type not in GRANTEE_TYPES:
        return "The grantee must be a user or a client."
    if not grantee_id:
        return "A grantee is required: a user's email or a client's id."
    if not resource_type:
        return "A resource is required."
    return (
        grantee_type, grantee_id, resource_type,
        (form.get("resource_id") or "").strip(),
    )


def parse_grant_form(form: typing.Any) -> GrantKey | str:
    """The grant a **grant** post names, or the sentence refusing it.

    Mirrors the row shapes campus.auth's grant routes accept
    (authz.GRANT_RESOURCES); the store re-checks every one.
    """
    key = _key_fields(form)
    if isinstance(key, str):
        return key
    grantee_type, grantee_id, resource_type, resource_id = key
    if grantee_type == "client":
        # Clients are never designated admins (#854/#865): their grants
        # are vault access, as bitflags.
        if resource_type != VAULT_RESOURCE:
            return (
                "A client's grants are its access to vault labels: the "
                "resource must be vault."
            )
        if not resource_id:
            return "A vault grant must name its vault label."
        bits = 0
        for flag, bit in VAULT_BITS:
            if flag in form.getlist("bits"):
                bits |= bit
        if not bits:
            return "Choose at least one vault permission (R, C, U or D)."
        return GrantKey(
            grantee_type, grantee_id, resource_type, resource_id, bits=bits
        )
    resource = authz.GRANT_RESOURCES.get(resource_type)
    if resource is None:
        return f"Unknown resource {resource_type!r}."
    if resource.instance is None:
        if resource_id:
            return (
                f"{resource.label} grants are not scoped to an instance: "
                "leave it empty."
            )
    elif not resource_id:
        return (
            f"A {resource.label.lower()} grant must name its "
            f"{resource.instance.lower()}."
        )
    elif resource.choices and resource_id not in resource.choices:
        return (
            f"A {resource.label.lower()} grant names one of: "
            f"{', '.join(resource.choices)}."
        )
    level = (form.get("level") or "").strip()
    if level not in resource.levels:
        return (
            f"A {resource.label.lower()} grant is granted at one of: "
            f"{', '.join(resource.levels)}."
        )
    return GrantKey(
        grantee_type, grantee_id, resource_type, resource_id, level=level
    )


def parse_revoke_form(form: typing.Any) -> GrantKey | str:
    """The grant a **revoke** post names, or the sentence refusing it.

    The row's key comes from the page's own hidden fields, so a row of
    a resource this portal does not model can still be revoked; only
    the permission operand is checked here.
    """
    key = _key_fields(form)
    if isinstance(key, str):
        return key
    grantee_type, grantee_id, resource_type, resource_id = key
    level = (form.get("level") or "").strip()
    raw_bits = (form.get("bits") or "").strip()
    if level and not raw_bits:
        if level not in authz.LEVELS:
            return f"Unknown level {level!r}."
        return GrantKey(
            grantee_type, grantee_id, resource_type, resource_id, level=level
        )
    if raw_bits and not level:
        try:
            bits = int(raw_bits)
        except ValueError:
            bits = 0
        if not 0 < bits <= VAULT_ALL_BITS:
            return f"Unknown vault permissions {raw_bits!r}."
        return GrantKey(
            grantee_type, grantee_id, resource_type, resource_id, bits=bits
        )
    return "The revoke names no permission."


def grant_report(store, key: GrantKey) -> str:
    """Apply a grant through the SDK's Grants resource; return the
    sentence reporting what happened."""
    row = store.grant(**key.sdk_args())
    if not isinstance(row, dict):
        row = {}
    who = f"{key.grantee_type} {key.grantee_id}"
    if key.bits is not None:
        try:
            held = int(row.get("bits") or 0)
        except (TypeError, ValueError):
            held = key.bits
        return (
            f"Vault access for {who} on {key.resource_id} is now "
            f"{_flags(held)}."
        )
    held = str(row.get("level") or "")
    if (
        held != key.level
        and held in authz.LEVELS
        and key.level in authz.LEVELS
        and authz.LEVELS.index(held) > authz.LEVELS.index(key.level)
    ):
        return (
            f"{who} already holds {_name(key)}:{held} — a level never "
            f"lowers. Revoke it first, then grant {key.level}."
        )
    return f"Granted {describe(key)} to {who}."


def revoke_report(store, key: GrantKey) -> str:
    """Apply a revoke through the SDK's Grants resource; return the
    sentence reporting what happened."""
    remaining = store.revoke(**key.sdk_args())
    who = f"{key.grantee_type} {key.grantee_id}"
    report = f"Revoked {describe(key)} from {who}."
    if key.bits is None or not isinstance(remaining, dict):
        return f"{report} The grant is gone."
    try:
        left = int(remaining.get("bits") or 0)
    except (TypeError, ValueError):
        left = 0
    if not left:
        return f"{report} The grant is gone."
    return f"{report} Remaining: {_flags(left)}."


def _csrf_token() -> str:
    """Issue a CSRF token for the action forms and remember it."""
    token = secrets.token_urlsafe(32)
    flask.session[GRANTS_CSRF_KEY] = token
    return token


def _check_csrf() -> bool:
    """Validate the submitted CSRF token (single-use).

    The comparison runs on bytes: compare_digest() raises TypeError on
    non-ASCII strings, and the submitted token is raw form data.
    """
    issued = flask.session.pop(GRANTS_CSRF_KEY, None)
    submitted = flask.request.form.get("csrf_token", "")
    if not isinstance(issued, str):
        return False
    return hmac.compare_digest(
        issued.encode("utf-8"), str(submitted).encode("utf-8")
    )


def _filters(source: typing.Any) -> dict[str, str]:
    """The list filters as the page uses them: stripped, blank as ""."""
    return {
        name: (source.get(name) or "").strip() for name in FILTERS
    }


def _query_string(filters: typing.Mapping[str, str]) -> str:
    """The non-blank filters as a query string ("" when there are none)."""
    return urllib.parse.urlencode({
        name: value for name, value in filters.items() if value
    })


def _row(raw: typing.Any, names: typing.Mapping[str, str],
         own_id: str | None) -> dict | None:
    """One store row as the page renders it; None for a malformed row."""
    if not isinstance(raw, dict):
        return None
    bits = raw.get("bits")
    try:
        bits = None if bits is None else int(bits)
    except (TypeError, ValueError):
        bits = None
    grantee_type = str(raw.get("grantee_type") or "")
    grantee_id = str(raw.get("grantee_id") or "")
    resource_type = str(raw.get("resource_type") or "")
    resource_id = str(raw.get("resource_id") or "")
    level = raw.get("level")
    level = str(level) if level else None
    key = GrantKey(grantee_type, grantee_id, resource_type, resource_id)
    return {
        "id": str(raw.get("id") or ""),
        "grantee_type": grantee_type,
        "grantee_id": grantee_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "level": level,
        "bits": bits,
        # Each vault flag with whether this row holds it (Jinja has no
        # bitwise operators, so the mask is read here).
        "flags": [
            {"flag": flag, "bit": bit, "meaning": VAULT_FLAG_MEANING[flag],
             "held": bool(bits and bit & bits)}
            for flag, bit in VAULT_BITS
        ],
        # What the row grants, for the confirm dialog: `users:write`,
        # `vault label: R, C`.
        "description": describe(
            key._replace(bits=bits) if bits else key._replace(level=level)
        ) if bits or level else "",
        "created_at": raw.get("created_at"),
        "name": names.get(grantee_id, ""),
        # campus.auth does not let a user administer their own rows
        # (#883); the page withholds the controls for them.
        "own": grantee_type == "user" and grantee_id == own_id,
        # People first (the designated admins), then integrations.
        "sort_key": (
            grantee_type != "user", grantee_id, resource_type, resource_id,
        ),
    }


def create_blueprint(*, campus, operator_circle: str,
                     page_gate) -> flask.Blueprint:
    """Create the Grants page + actions blueprint (admin-section gate).

    Args:
        campus: The portal's Campus client; the wire calls ride the
            caller's admin lane.
        operator_circle: The portal's platform-operator circle — the
            lane and the grant-administration gate both resolve
            through it.
        page_gate: The Admin section's gate as a before-request
            callable (create_app derives it from the portal's own
            admin_required decorator, so this view is gated like the
            other /admin resource views).

    Returns:
        A Flask blueprint serving the Grants page and the grant/revoke
        form posts.
    """
    bp = flask.Blueprint("admin_grants_ui", __name__)
    bp.before_request(page_gate)

    def _own_id(access: dict) -> str | None:
        """Whose own rows this session may not change: the signed-in
        user's in the designated lane, nobody's in the operator lane
        (the wire principal there is the operator client)."""
        return access["user_id"] if access["lane"] == "designated" else None

    def _client_names(lane, access: dict) -> dict[str, str]:
        """{client_id: name} for the grantee column, best-effort.

        Operator lane only — a designated admin holds no clients read
        to resolve names with.
        """
        if access["lane"] != "operator":
            return {}
        try:
            return {
                str(record.id): str(record.name)
                for record in lane.auth.clients.list()
                if getattr(record, "name", None)
            }
        except Exception as err:  # noqa: BLE001 - names are display only
            logger.warning(
                "Client names for the Grants page could not be read: %s",
                err,
            )
            return {}

    def _list_url(filters: typing.Mapping[str, str]) -> str:
        query = _query_string(filters)
        return flask.url_for("admin_grants_ui.grants_page") + (
            f"?{query}" if query else ""
        )

    def _page(filters: typing.Mapping[str, str], *, error: str | None = None,
              status: int = 200) -> flask.Response:
        """Render the list: the store's rows within the filters."""
        access = authz.admin_access(campus, operator_circle)
        administrable = access["grant_admin"]["vocabularies"]
        off_limits = None
        rows: list[dict] = []
        names: dict[str, str] = {}
        if (
            filters["resource_type"]
            and filters["resource_type"] not in administrable
        ):
            # The service would refuse the read; say so rather than ask.
            off_limits = filters["resource_type"]
        else:
            # Only the filters in use go on the wire: the store matches
            # an empty value literally instead of reading it as "any".
            wanted = {
                name: value for name, value in filters.items() if value
            }
            # The operator reads the whole matrix in one call; a
            # designated admin's unfiltered list is refused upstream, so
            # they list each resource they administer.
            if "resource_type" in wanted or access["lane"] == "operator":
                queries = [wanted]
            else:
                queries = [
                    {**wanted, "resource_type": target}
                    for target in administrable
                ]
            with authz.admin_lane(campus, operator_circle) as lane:
                names = _client_names(lane, access)
                for query in queries:
                    try:
                        body = lane.auth.grants.list(**query)
                    except Exception as err:  # noqa: BLE001 - render the refusal
                        logger.error("Grant rows could not be listed: %s", err)
                        error = error or (
                            f"The grant store could not be read: {err}"
                        )
                        continue
                    rows.extend(
                        row for row in (
                            _row(raw, names, _own_id(access))
                            for raw in (body or {}).get("grants") or []
                        ) if row is not None
                    )
        rows.sort(key=lambda row: row["sort_key"])
        return flask.make_response(flask.render_template(
            "grants.html",
            rows=rows,
            filters=filters,
            administrable=administrable,
            # The operator resolved client names; they double as the
            # suggestions on the Grantee filter.
            client_names=names,
            # A designated admin's page says what it covers; their form
            # offers no client grantees (those are vault rows).
            designated=access["lane"] == "designated",
            off_limits=off_limits,
            vault_flags=[
                (flag, VAULT_FLAG_MEANING[flag]) for flag, _ in VAULT_BITS
            ],
            resource_forms={
                key: {
                    "levels": list(resource.levels),
                    "instance": resource.instance,
                    "choices": list(resource.choices),
                }
                for key, resource in authz.GRANT_RESOURCES.items()
                if key in administrable
            },
            csrf_token=_csrf_token(),
            error=error,
            query=_query_string(filters),
        ), status)

    @bp.get("/admin/grants")
    def grants_page():
        """The Grants list: the store's rows within the filters."""
        denied = authz.grant_admin_denial(campus, operator_circle)
        if denied is not None:
            return denied
        return _page(_filters(flask.request.args))

    @bp.post("/admin/grants")
    def grants_action():
        """Grant or revoke: one store row per post."""
        denied = authz.grant_admin_denial(campus, operator_circle)
        if denied is not None:
            return denied
        # The acting form carries the filters it was posted from.
        carried = urllib.parse.parse_qs(
            flask.request.form.get("filters") or ""
        )
        filters = {
            name: (carried.get(name) or [""])[0].strip() for name in FILTERS
        }
        if not _check_csrf():
            return _page(
                filters,
                error=(
                    "Your form session expired. Please reload the page "
                    "and try again."
                ),
                status=400,
            )

        action = flask.request.form.get("action")
        if action == "grant":
            key = parse_grant_form(flask.request.form)
        elif action == "revoke":
            key = parse_revoke_form(flask.request.form)
        else:
            flask.abort(404)

        access = authz.admin_access(campus, operator_circle)
        administrable = access["grant_admin"]["vocabularies"]
        if isinstance(key, str):
            flask.flash(key, "error")
        elif key.resource_type not in administrable:
            flask.abort(403, description=(
                f"Administering {key.resource_type} grants is reserved "
                "for the deployment operator."
                + (
                    f" You administer: {', '.join(administrable)}."
                    if administrable else ""
                )
            ))
        elif key.grantee_type == "user" and key.grantee_id == _own_id(access):
            flask.flash(
                "You cannot change your own access grants (campus#883): "
                "ask another admin or a platform operator.",
                "error",
            )
        else:
            with authz.admin_lane(campus, operator_circle) as lane:
                try:
                    report = (
                        grant_report if action == "grant" else revoke_report
                    )(lane.auth.grants, key)
                except Exception as err:  # noqa: BLE001 - relay the refusal
                    logger.error(
                        "Grants view: %s %s failed: %s",
                        action, describe(key), err,
                    )
                    flask.flash(
                        f"Failed to {action} {describe(key)}: {err}", "error",
                    )
                else:
                    flask.flash(report, "success")
        return flask.redirect(_list_url(filters))

    return bp


def init_app(app: flask.Flask, *, campus, operator_circle: str,
             page_gate) -> None:
    """Register the Grants view blueprint on the portal app.

    Args:
        app: The portal Flask app.
        campus: The portal's Campus client; the view's wire calls ride
            the caller's admin lane.
        operator_circle: The portal's platform-operator circle — the
            lane and the grant-administration gate both resolve through
            it.
        page_gate: The Admin section's gate as a before-request
            callable (create_app derives it from the portal's own
            admin_required decorator, so this view is gated like the
            other /admin resource views).
    """
    app.register_blueprint(create_blueprint(
        campus=campus, operator_circle=operator_circle, page_gate=page_gate,
    ))
