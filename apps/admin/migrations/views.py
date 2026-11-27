"""apps.admin.migrations.views

Routes for the Migrations view (#75), in the /admin namespace:

- GET  /admin/migrations                      the page: one section per
                                              service (plan, history,
                                              actions), server-rendered
- GET  /admin/migrations/enable              step-up re-auth: request the
                                              migrations vocabulary for the
                                              session token (no-op when it
                                              already carries it)
- GET  /admin/api/migrations/<service>        JSON status for the page's
                                              polling while an apply is
                                              in flight
- POST /admin/migrations/<service>/apply      start a background apply
                                              (optional staged up_to)
- POST /admin/migrations/<service>/abort      cancel between migrations
- POST /admin/migrations/<service>/rollback   roll back one applied
                                              revision (synchronous)

Gates: pages and mutations use the Admin section's gate (the portal's
own admin_required decorator, passed in by create_app — campus.auth
authority: a platform operator or an account holding an access grant).
The JSON endpoint gates the same authority in fetch-friendly form
(401/403/503 JSON, mirroring the audit UI data endpoints). None of this
is the security boundary: the services enforce the migrations grants
(#1168 two-leg model) on every call — client-side gating is UX.

Action forms carry single-use CSRF tokens (the audit keys page's
convention — these are destructive actions). Every action redirects
back to the page and reports through flash messages, like the rest
of the admin section; failures relay the service's own text.
"""

__all__ = ["create_data_blueprint", "create_page_blueprint"]

import hmac
import logging
import secrets

import campus_python
import flask

from apps.admin import authz

from . import client

logger = logging.getLogger(__name__)

# CSRF session key for the action forms (single-use, like the audit
# keys page's token).
MIGRATIONS_CSRF_KEY = "migrations_csrf"

# How the page names the three services.
SERVICE_LABELS = {
    "auth": "Auth",
    "api": "API",
    "audit": "Audit",
}

# The api/audit migrations gates fail closed until campus.auth
# surfaces token scopes to services (campus#1183) — their sections
# explain the 403 instead of treating it as a view bug. The auth
# service's gate is fully wired (the full two-leg store check), so
# its 403s are genuine authority gaps and speak for themselves.
CAPABILITY_LEG_CAVEAT = (
    "This service's migrations gate cannot verify token scopes yet "
    "(campus#1183): it fails closed until campus.auth surfaces token "
    "scopes to services, so the section stays blocked even for "
    "correctly-granted admins."
)


def _csrf_token() -> str:
    """Issue a CSRF token for the action forms and remember it."""
    token = secrets.token_urlsafe(32)
    flask.session[MIGRATIONS_CSRF_KEY] = token
    return token


def _check_csrf() -> bool:
    """Validate the submitted CSRF token (single-use).

    The comparison runs on bytes: compare_digest() raises TypeError on
    non-ASCII strings, and the submitted token is raw form data.
    """
    issued = flask.session.pop(MIGRATIONS_CSRF_KEY, None)
    submitted = flask.request.form.get("csrf_token", "")
    if not isinstance(issued, str):
        return False
    return hmac.compare_digest(
        issued.encode("utf-8"), str(submitted).encode("utf-8")
    )


def _service_or_404(service: str) -> str:
    """Validate the service path parameter against the runner scopes."""
    if service not in client.SERVICES:
        flask.abort(404, description=(
            f"Unknown migration service {service!r}: expected one of "
            f"{', '.join(client.SERVICES)}."
        ))
    return service


def _csrf_error_page(campus) -> flask.Response:
    """Re-render the page with the stale-form error (status 400)."""
    return flask.make_response(
        flask.render_template(
            "migrations.html",
            sections=[], scopes=None,
            capabilities=dict.fromkeys(client.VOCABULARY_LEVELS, False),
            migrations_grant=_migrations_grant(campus),
            csrf_token=_csrf_token(),
            error=(
                "Your form session expired. Please reload the page and "
                "try again."
            ),
            capability_caveat=CAPABILITY_LEG_CAVEAT,
        ),
        400,
    )


def _migrations_grant(campus) -> bool | None:
    """The signed-in user's migrations designation (fail-closed).

    The two-leg panel's designation leg: probed through the
    operator-readable grant store (apps.admin.authz, briefly cached),
    independent of the token's scopes. None (unknown) renders the
    unavailable state. The migrations vocabulary has no vocabulary-level
    rows (#1168) — the probe passes when any per-service subset
    (auth/api/audit) covers the level.
    """
    from apps.admin.authz import has_migrations_grant

    user = getattr(flask.g, "user", None)
    user_id = str(getattr(user, "id", "") or "") if user else ""
    if not user_id:
        return None
    return has_migrations_grant(campus, user_id)


def _section_view(service: str, result: dict,
                  capabilities: dict[str, bool]) -> dict:
    """One service's page-section view model.

    body is the service's status payload:
    {service, status, plan, history, running} — plan holds the
    pending revisions with their approval metadata (hash, declared
    rollback support, docstring summary), history the full ledger
    (who/when/duration/hash/error per the #746 execution audit).
    """
    body = result.get("body")
    section = {
        "id": service,
        "label": SERVICE_LABELS[service],
        "status_code": result.get("status_code"),
        "error_text": None,
        "capability_caveat": False,
        "plan": [],
        "history": [],
        "running": False,
        "pending_count": 0,
        "failed_count": 0,
        "capabilities": capabilities,
    }
    if result.get("status_code") == 403:
        section["error_text"] = client.service_error_text(
            result, "The migrations gate refused this read (403)."
        )
        # api/audit fail closed until campus#1183 lands; auth 403s are
        # genuine authority gaps and speak for themselves.
        section["capability_caveat"] = service in ("api", "audit")
        return section
    if result.get("error") or result.get("status_code") != 200:
        section["error_text"] = client.service_error_text(
            result,
            f"The migration status is unavailable (the {service} "
            f"service returned {result.get('status_code') or 'no response'}).",
        )
        return section
    if not isinstance(body, dict):
        # A 200 with a non-JSON (or missing) body: render empty rather
        # than crash; the wire layer keeps body None in that case.
        return section
    section["plan"] = list(body.get("plan") or [])
    # Newest-first reads like a log; the ledger's natural order is
    # insertion order (oldest first).
    section["history"] = sorted(
        body.get("history") or [],
        key=lambda row: str(row.get("applied_at") or ""),
        reverse=True,
    )
    section["running"] = bool(body.get("running"))
    section["pending_count"] = len(section["plan"])
    section["failed_count"] = sum(
        1 for row in section["history"] if row.get("status") == "failed"
    )
    return section


def create_page_blueprint(*, campus, page_gate) -> flask.Blueprint:
    """Create the page + actions blueprint (the admin-section gate).

    Args:
        campus: The portal's Campus client (the wire calls ride its
            user session).
        page_gate: The admin-section gate as a before-request callable.

    Returns:
        A Flask blueprint serving the Migrations page and the
        apply/abort/rollback form posts.
    """
    bp = flask.Blueprint("admin_migrations_ui", __name__)
    bp.before_request(page_gate)

    def _page(error: str | None = None, status: int = 200) -> flask.Response:
        """Render the page: every service's status, one round of calls."""
        statuses, scopes = client.fetch_statuses(campus)
        capabilities = client.levels_from_scopes(scopes)
        sections = [
            _section_view(service, statuses.get(service, {}), capabilities)
            for service in client.SERVICES
        ]
        html = flask.render_template(
            "migrations.html",
            sections=sections,
            scopes=scopes,
            capabilities=capabilities,
            migrations_grant=_migrations_grant(campus),
            csrf_token=_csrf_token(),
            error=error,
            capability_caveat=CAPABILITY_LEG_CAVEAT,
        )
        return flask.make_response(html, status)

    def _action_error(result: dict, service: str, what: str,
                      revision: str = "") -> str:
        """The flash text for a refused mutation."""
        suffix = f" of {revision}" if revision else ""
        return client.service_error_text(
            result,
            f"The {service} service refused the {what}{suffix} "
            f"({result['status_code'] or 'no response'}).",
        )

    @bp.get("/auth/migrations")
    def migrations_page():
        """The Migrations view: one section per service DB.

        Renders the plan delta (the human approval artifact), the
        ledger history, and the grant-gated actions; sections whose
        service 403s render the service's own refusal (api/audit add
        the campus#1183 fail-closed caveat).
        """
        return _page()

    @bp.get("/auth/migrations/enable")
    def migrations_enable():
        """Step-up re-auth: put the migrations vocabulary on the
        session token.

        Sign-in deliberately requests only the structural scope
        baseline (apps.admin.LOGIN_SCOPES_BASE) so a deployment's
        allowed_scopes cap lagging a feature vocabulary can never 500
        every sign-in again. This route acquires the migrations
        capability on demand instead: it re-enters the OAuth flow
        requesting exactly the migrations vocabulary — the auth
        service's browser session is warm, so this is a fast redirect
        round trip, and finalize_login lands back on the view.

        Failure is contained here by design: if the portal client's
        cap does not cover the vocabulary, the session-creation call
        raises before any redirect and the page renders the reason
        (the deployment must widen the cap) instead of a 500.
        """
        from campus.common.utils import url as campus_url
        from campus.flask_campus import context

        from apps.admin import MIGRATION_SCOPES

        capabilities = client.levels_from_scopes(client.session_scopes(campus))
        if capabilities["read"]:
            return flask.redirect(
                flask.url_for("admin_migrations_ui.migrations_page")
            )
        flask.session["login_next"] = flask.url_for(
            "admin_migrations_ui.migrations_page"
        )
        try:
            return context.start_login(
                campus,
                target=campus_url.full_url_for("auth.finalize_login"),
                scopes=list(MIGRATION_SCOPES),
            )
        except campus_python.errors.BadRequestError as err:
            # The portal client's allowed_scopes cap does not cover the
            # migrations vocabulary: nothing the user can do here — the
            # deployment widens the cap (README, Migrations view).
            flask.current_app.logger.warning(
                "Migrations step-up rejected (allowed_scopes cap): %s", err,
            )
            flask.flash(
                "Migrations access is not enabled on this deployment's "
                "portal client: its allowed_scopes cap does not include "
                "the migrations vocabulary. Ask the deployment owner to "
                "widen the cap (README, Migrations view).",
                "error",
            )
            return flask.redirect(
                flask.url_for("admin_migrations_ui.migrations_page")
            )

    @bp.post("/auth/migrations/<service>/apply")
    def apply_action(service: str):
        """Start an apply. The optional up_to stages the window."""
        _service_or_404(service)
        if not _check_csrf():
            return _csrf_error_page(campus)
        up_to = (flask.request.form.get("up_to") or "").strip() or None
        result = client.apply_migrations(campus, service, up_to=up_to)
        if result["status_code"] == 202:
            stage = f" (up to {up_to})" if up_to else ""
            flask.flash(
                f"Apply started for {SERVICE_LABELS[service]}{stage}. "
                "Progress lands in the ledger; the view polls while "
                "the run is in flight.",
                "success",
            )
            logger.info(
                "Migrations view started apply for %s%s",
                service, f" up_to={up_to}" if up_to else "",
            )
        else:
            flask.flash(_action_error(result, service, "apply"), "error")
        return flask.redirect(
            flask.url_for("admin_migrations_ui.migrations_page")
        )

    @bp.post("/auth/migrations/<service>/abort")
    def abort_action(service: str):
        """Abort a running apply (takes effect between migrations)."""
        _service_or_404(service)
        if not _check_csrf():
            return _csrf_error_page(campus)
        result = client.abort_apply(campus, service)
        if result["status_code"] == 200:
            flask.flash(
                f"Abort requested for {SERVICE_LABELS[service]}: the "
                "in-flight migration finishes, the rest stay pending.",
                "success",
            )
            logger.info("Migrations view aborted apply for %s", service)
        else:
            flask.flash(_action_error(result, service, "abort"), "error")
        return flask.redirect(
            flask.url_for("admin_migrations_ui.migrations_page")
        )

    @bp.post("/auth/migrations/<service>/rollback")
    def rollback_action(service: str):
        """Roll back one applied revision (synchronous, destructive)."""
        _service_or_404(service)
        if not _check_csrf():
            return _csrf_error_page(campus)
        revision = (flask.request.form.get("revision") or "").strip()
        if not revision:
            return _page("Rollback needs a revision.", 400)
        from_backup = (
            flask.request.form.get("from_backup") or ""
        ).strip() or None
        result = client.rollback_migration(
            campus, service, revision, from_backup=from_backup,
        )
        if result["status_code"] == 200:
            flask.flash(
                f"Rolled back {revision} on {SERVICE_LABELS[service]}: "
                "it is pending again.",
                "success",
            )
            logger.info(
                "Migrations view rolled back %s on %s", revision, service,
            )
        else:
            flask.flash(
                _action_error(result, service, "rollback", revision),
                "error",
            )
        return flask.redirect(
            flask.url_for("admin_migrations_ui.migrations_page")
        )

    return bp


def create_data_blueprint(*, campus, operator_circle: str) -> flask.Blueprint:
    """Create the JSON status blueprint (for the page's polling).

    Args:
        campus: The portal's Campus client, for the authority probes
            behind this blueprint's JSON gate.
        operator_circle: The portal's platform-operator circle — the
            same authority the page gate checks, answered in
            fetch-friendly JSON.

    Returns:
        A Flask blueprint serving GET /admin/api/migrations/<service>.
    """
    bp = flask.Blueprint("admin_migrations_data", __name__)

    def _json_gate() -> tuple[dict, int] | None:
        """The page gate in fetch-friendly form.

        Unauthenticated callers get 401 JSON (browsers redirect pages;
        fetch() callers get 401), a signed-in user without campus.auth
        authority 403, and unreachable authorities 503 — the audit data
        endpoints' convention.
        """
        user = getattr(flask.g, "user", None)
        if user is None:
            return {"error": "Authentication required. Visit /login."}, 401
        access = authz.admin_access(campus, operator_circle)
        if access["unavailable"]:
            return {
                "error": (
                    "Authorization backend unavailable; try again "
                    "shortly."
                ),
            }, 503
        if not access["admitted"]:
            return {"error": authz.ADMIN_DENIED_REASON.format(
                circle=operator_circle,
            )}, 403
        return None

    @bp.get("/auth/migrations/<service>")
    def migrations_status(service: str) -> tuple[dict, int]:
        """One service's status payload for the polling loop.

        Returns:
            JSON: {"service": ..., "running": bool|null, "error":
            str|null, "body": <the GET status payload>|null} — running
            is null when the service did not answer, so the poller
            keeps waiting instead of reloading on a failed poll.
        """
        denied = _json_gate()
        if denied is not None:
            return denied
        _service_or_404(service)
        statuses, _scopes = client.fetch_statuses(campus)
        result = statuses.get(service, {})
        body = result.get("body")
        ok = not result.get("error") and result.get("status_code") == 200
        return {
            "service": service,
            "running": body.get("running") if ok and body else None,
            "error": None if ok else client.service_error_text(
                result,
                f"The migration status is unavailable (the {service} "
                f"service returned {result.get('status_code') or 'no response'}).",
            ),
            "body": body if ok else None,
        }, 200

    return bp
