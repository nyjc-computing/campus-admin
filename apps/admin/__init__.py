"""campus.apps.admin

Campus Admin Portal Application: authentication routes and views.

Each section is a UI over one service and takes its authority from that
service alone (apps.admin.authz): the Admin section from campus.auth's
access-grant store, the Resources section from campus.api's circles, the
Audit section from campus.audit's API key. Sign-in is the portal's own
campus.auth OAuth login.
"""

import functools
import json
import os
from datetime import datetime

import campus_python
import flask
from campus import flask_campus
from campus.common import env

# The scopes /login requests for every signed-in user: the STRUCTURAL
# baseline only — campus.profile is the OAuth floor, and users:read is
# what flask_campus's context hook needs to re-fetch the signed-in
# user's record on cache misses (its #879 embedded-user fast path
# hides the need until the fallback runs; without the scope the portal
# 500s on every request once the context cache expires). Feature
# vocabularies are deliberately NOT here: /login validates the whole
# request against the portal client's allowed_scopes cap all-or-nothing,
# so a cap that lagged a newly added feature scope 500ed every
# sign-in (the Nov 2026 outage). Feature capability is acquired
# per-session through the step-up re-auth instead (the /admin/enable
# route and the migrations view), whose failure renders as a disabled
# affordance with the reason — never a portal-wide 500. The cap must
# cover this baseline before deploying (README, Access Model).
LOGIN_SCOPES_BASE = [
    "campus.profile",
    "users:read",
]

# The migrations view's step-up requests the whole vocabulary (its
# actions span the three levels); the allowed_scopes cap must include
# it before the step-up can succeed, and a missing entry surfaces in
# the UI. The general /admin/enable step-up asks per granted level
# instead.
MIGRATION_SCOPES = [
    "migrations:read",
    "migrations:write",
    "migrations:admin",
]

# Session key of the single-use CSRF token the Users page's delete
# form carries (apps.admin.csrf).
USERS_CSRF_KEY = "users_csrf"

# The per-client actions the Clients page's vault-access forms post
# (clients/<id>/<action>). Anything else under that path is not a
# route.
CLIENT_ACTIONS = ("grant", "revoke", "update_permissions")


def _split_uri_literal(inner: str) -> list[str]:
    """Split a Postgres array-literal body on commas, honouring quotes."""
    items: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    for ch in inner:
        if escape:
            buf.append(ch)
            escape = False
        elif in_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_quotes = False
            else:
                buf.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf).strip())
    return [item for item in items if item]


def parse_uri_list(value) -> list[str]:
    """Normalise redirect_uris as serialised by the auth API to a list.

    The auth service stores redirect_uris in a TEXT column, so a read can
    return a Postgres array literal string ('{a,b}'), a JSON array string
    ('["a"]'), an empty string, or a real list depending on the last write
    path.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(uri) for uri in value]
    text = str(value).strip()
    if not text:
        return []
    if text[0] == "[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [str(uri) for uri in parsed] if isinstance(parsed, list) else []
    if text[0] == "{":
        return _split_uri_literal(text[1:-1])
    return [text]


def form_redirect_uris(raw: str | None) -> list[str]:
    """Parse the redirect_uris textarea: one URI per line, blanks dropped."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def split_scopes(value) -> list[str]:
    """Normalise a token's scope to a list: one entry per scope.

    The token model exposes the granted scopes both as `scope` (the
    RFC 6749 space-delimited string) and `scopes` (the list); a read
    may deliver either.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(scope) for scope in value]


def create_app():
    """Application factory for Campus Admin Portal."""
    app = flask.Flask(__name__, static_folder="static", static_url_path="/static")

    # Trace producer (campus#816): record admin requests as audit spans,
    # with SDK calls landing as child spans. Opt-in via AUDIT_API_KEY +
    # AUDIT_TRACING_ENABLED; see campus docs/audit-tracing.md. Read once
    # at app creation — flipping the flag is a redeploy.
    if env.get_flag("AUDIT_TRACING_ENABLED", False):
        from campus.flask_campus.audit import init_tracing as init_audit_tracing

        init_audit_tracing(app)

    # Action journeys (campus#828): one user-initiated episode — the
    # page load that began it, its XHRs and form posts, and the
    # server-to-server calls they spawn — groups as one journey in the
    # audit UI. Navigations mint; the sliding campus_action_journey
    # cookie carries the episode across same-origin requests.
    from campus.flask_campus.audit import init_journeys

    init_journeys(app)

    campus = campus_python.Campus(timeout=60)

    # Configure Flask secret key from environment
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

    # Fleet session cookie scheme (campus docs "Session Cookie Naming",
    # campus#1290): a distinct name so this portal's session neither
    # clobbers — nor is clobbered by — the other campus apps' sessions
    # on the shared gateway hostname (gateway umbrella campus#1295;
    # opt-in tracked as campus-admin#95).
    app.config["SESSION_COOKIE_NAME"] = "campus_admin_session"

    from apps.admin import authz, csrf
    from apps.admin.audit import gate as audit_gate

    # Platform-operator circle: holders of `admin` on this circle drive
    # the portal as its deployment's operator client (the operator
    # console). Defaults to the campus.api bootstrap's `campus`
    # wildcard (campus#1216); ADMIN_CIRCLE re-points it without a code
    # change. Entry to the Admin section itself is campus.auth
    # authority — a grant row, or this operator status (apps.admin.authz).
    operator_circle = os.environ.get("ADMIN_CIRCLE") or authz.OPERATOR_CIRCLE

    # Startup gate summary: names each section's authority and its
    # configuration so a misdeployment surfaces at deploy time, not as
    # 403s. The audit lists are counted (never listed) like before.
    def _gate_entries(varname, sep):
        return [e for e in os.environ.get(varname, "").split(sep) if e.strip()]

    audit_n = len(_gate_entries("AUDIT_ADMINS", ","))
    key_admins_var = audit_gate.key_admins_variable()
    key_admins_n = len(_gate_entries(key_admins_var, ","))

    def _fmt(n):
        return "1 entry" if n == 1 else f"{n} entries"

    gate_log = app.logger.warning if audit_n == 0 else app.logger.info
    gate_log(
        "access gates: admin section = campus.auth grants + admin on "
        "circle '%s' (ADMIN_CIRCLE, platform operators); resources "
        "section = campus.api circles as the signed-in user; audit "
        "section = AUDIT_ADMINS (%s), key management = %s (%s)",
        operator_circle,
        _fmt(audit_n),
        key_admins_var,
        _fmt(key_admins_n),
    )

    # Static asset cache-busting: each deploy gets fresh asset URLs
    # (?v=<commit sha>) so browsers can keep caching for 12h across
    # deploys without ever serving stale stylesheets or scripts.
    # Railway injects RAILWAY_GIT_COMMIT_SHA on git deploys.
    app.config["ASSET_VERSION"] = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "dev"
    )

    # Cache static assets for 12h instead of Flask's default no-cache: the
    # files are small and unfingerprinted, and every revalidation round trip
    # goes through the full app stack (push_context runs before_request even
    # for /static).
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 12 * 3600

    # Add jinja filters
    # Jinja timestamp filter
    def timestamp(dt, format="%Y-%m-%d %H:%M:%S"):
        if dt is None:
            return "N/A"
        if isinstance(dt, str):
            # Try to parse ISO string
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return dt
        if isinstance(dt, datetime):
            return dt.strftime(format)
        return str(dt)

    app.jinja_env.filters["timestamp"] = timestamp

    # Permission bitmask filter
    def has_permission(permissions, perm):
        perm_bits = {"R": 1, "C": 2, "U": 4, "D": 8}
        return permissions & perm_bits[perm] != 0

    app.jinja_env.filters["has_perm"] = has_permission

    # Self-diagnosing 403: name the signed-in identity and the authority
    # the section refused on (each gate passes its reason through the
    # abort description), so the page says what to change instead of
    # staging a diagnosis session. (The audit gate renders its own
    # forbidden page directly; this handler serves the admin and
    # resources sections' gates.)
    @app.errorhandler(403)
    def forbidden(error):
        section = (
            "resources" if flask.request.path.startswith("/admin/api")
            else "admin"
        )
        user = getattr(flask.g, "user", None)
        return (
            flask.render_template(
                "403.html",
                section=section,
                reason=getattr(error, "description", None),
                user_email=getattr(user, "email", None) or getattr(user, "id", None),
            ),
            403,
        )

    # The auth blueprint mounts at root: behind campus-gateway the
    # 0.2.8 PrefixFix middleware (fc#50, installed by init_app below)
    # moves the gateway's X-Forwarded-Prefix (/admin) into SCRIPT_NAME,
    # so the blueprint answers /admin/login, /admin/finalize_login and
    # /admin/logout there while the origin keeps /login etc. — the
    # timetable pattern. login_prefix (flask-campus#47) is superseded
    # for gateway deployments and must not be combined with the header
    # mount (fc#50 do-not-combine). (Gateway umbrella campus#1295;
    # opt-in tracked as campus-admin#95.)
    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus, default_endpoint="index",
        login_scopes=LOGIN_SCOPES_BASE,
    )
    login_manager.init_app(app)

    # The Admin section's gate and per-vocabulary gates (apps.admin.authz
    # answers both): signed-in, and either a platform operator or an
    # account holding at least one campus.auth access grant. AUTH_ADMINS
    # / ADMINS are not read — they gated nothing since the circles gate
    # replaced them and are obsolete.
    def admin_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            return authz.admin_denial(campus, operator_circle) or view(
                *args, **kwargs
            )
        return wrapped

    def require_vocabulary(vocab_id, level="read"):
        def decorator(view):
            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                return authz.vocabulary_denial(
                    campus, operator_circle, vocab_id, level,
                ) or view(*args, **kwargs)
            return wrapped
        return decorator

    # Capability probes behind the portal's affordances (apps.admin.authz):
    # the nav pills and the sidebar links render each signed-in user's
    # real access state — section authority, per-vocabulary grants and
    # scopes, per-family campus.api access — disabled with a reason
    # instead of hard-failing on click.
    authz.init_app(app, campus=campus, operator_circle=operator_circle)

    # Audit web UI (migrated from campus.audit.web): the trace viewer
    # and key-management pages under /audit, gated by this portal's
    # login plus the deployment's AUDIT_ADMINS list (key management:
    # audit superadmins), with data proxied to the audit service's
    # versioned API under its own API key.
    from apps.admin import audit

    audit.init_app(app)

    # Migrations view (campus-admin#75): per-service plan/history and
    # apply/abort/rollback over each service's migration management
    # routes, behind the Admin section's gate. The migrations grants
    # themselves (#1168 two-leg model) are enforced by the services on
    # every call; every wire call rides the signed-in user's campus
    # session — the only lane the services accept for them.
    from apps.admin import migrations

    migrations.init_app(
        app, campus=campus, operator_circle=operator_circle,
        # The admin_required decorator applied to a no-op is exactly
        # its gate: proceed (None) / redirect / 403 / 503.
        page_gate=admin_required(lambda: None),
    )

    # Grants view: the campus.auth access-grant store ("who may
    # administer what") for user and client grantees, behind the Admin
    # section's gate. campus.auth administers the store on every call;
    # the view admits exactly who it would and runs in the caller's
    # lane.
    from apps.admin import grants

    grants.init_app(
        app, campus=campus, operator_circle=operator_circle,
        page_gate=admin_required(lambda: None),
    )

    # Resources section (campus.api resources): the third portal
    # section, at the /resources namespace — management views over the
    # campus.api service's resources (circles, venues, timetable,
    # assignments, submissions, subjcombi, files, hostedapps) via the
    # SDK's ApiRoot. Every wire call rides the signed-in user's own
    # session (campus.api denies client principals on gated routes,
    # #854) and campus.api's authz is the wall behind the UI; each view
    # gates on the user's effective access for its family.
    from apps.admin import resources

    resources.init_app(app, campus=campus, operator_circle=operator_circle)

    @app.get("/")
    def index():
        # The root namespace belongs to the gateway (campus-admin#97):
        # the portal lives under /admin, so old bookmarks land there.
        return flask.redirect(flask.url_for("portal_home"))

    @app.get("/admin")
    @app.get("/admin/")
    def portal_home():
        # The portal home under the gateway-dispatchable /admin
        # namespace (campus-admin#97 — bare / itself 404s on the
        # shared host, so the home page had to move here).
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        # The old sign-in page folded into the namespaced login route
        # when the auth blueprint moved under /admin (#95): the page's
        # only affordance was the login link.
        return flask.redirect(flask.url_for("auth.login"))

    # Old auth-ops paths → their /admin/auth remap (campus-admin#97),
    # kept as redirects through the transition.
    @app.get("/admin/dashboard")
    def dashboard_redirect():
        return flask.redirect(flask.url_for("dashboard"))

    @app.get("/admin/clients")
    @app.get("/admin/clients/<path:rest>")
    def clients_redirect(rest: str | None = None):
        return flask.redirect("/admin/auth/clients" + (f"/{rest}" if rest else ""))

    @app.get("/admin/users")
    def users_redirect():
        return flask.redirect(flask.url_for("users"))

    @app.get("/admin/sessions")
    def sessions_redirect():
        return flask.redirect(flask.url_for("sessions"))

    @app.get("/admin/vaults")
    def vaults_redirect():
        return flask.redirect(flask.url_for("vaults"))

    @app.get("/admin/credentials")
    def credentials_redirect():
        return flask.redirect(flask.url_for("credentials"))

    @app.get("/admin/enable")
    def admin_enable_redirect():
        return flask.redirect(flask.url_for("admin_enable"))

    @app.get("/admin/migrations")
    @app.get("/admin/migrations/<path:rest>")
    def migrations_redirect(rest: str | None = None):
        return flask.redirect(
            "/admin/auth/migrations" + (f"/{rest}" if rest else "")
        )

    @app.get("/admin/grants")
    @app.get("/admin/grants/<path:rest>")
    def grants_redirect(rest: str | None = None):
        return flask.redirect("/admin/auth/grants" + (f"/{rest}" if rest else ""))

    # Old section roots → their remapped namespaces (#97). /audit/*
    # reaches admin only from old bookmarks: the gateway routes
    # /audit/* to campus.audit, whose ui_redirect blueprint now points
    # here via AUDIT_UI_URL.
    @app.get("/audit")
    @app.get("/audit/<path:rest>")
    def audit_redirect(rest: str | None = None):
        return flask.redirect("/admin/audit" + (f"/{rest}" if rest else ""))

    @app.get("/resources")
    @app.get("/resources/<path:rest>")
    def resources_redirect(rest: str | None = None):
        return flask.redirect("/admin/api" + (f"/{rest}" if rest else ""))

    @app.get("/admin/auth/dashboard")
    @login_manager.login_required
    def dashboard(**_):
        return flask.render_template("dashboard.html")

    @app.route("/admin/auth/clients", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("clients")
    def clients():
        clients_data = []
        error_msg = None

        if flask.request.method == "POST":
            # Handle form submission for creating, updating, or deleting client
            action = flask.request.form.get("action")
            client_id = flask.request.form.get("client_id")
            delete = flask.request.form.get("delete")

            # Record edits need clients:write; registering and deleting
            # are operator-level upstream (clients:admin).
            denied = authz.vocabulary_denial(
                campus, operator_circle, "clients",
                "admin" if action == "create" or delete else "write",
            )
            if denied is not None:
                return denied

            with authz.admin_lane(campus, operator_circle) as lane:
                if action == "create":
                    # Handle create
                    name = flask.request.form.get("name")
                    description = flask.request.form.get("description")

                    if not name or not description:
                        flask.flash("Name and description are required", "error")
                    else:
                        try:
                            # Call the create API using campus_python
                            new_client = lane.auth.clients.new(
                                name=name,
                                description=description,
                                is_public=flask.request.form.get("is_public") == "on",
                                redirect_uris=form_redirect_uris(
                                    flask.request.form.get("redirect_uris")
                                ),
                            )
                            flask.flash(
                                f"Successfully created client {new_client.id}",
                                "success",
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to create client: {str(e)}"
                            )
                            flask.flash(f"Failed to create client: {str(e)}", "error")
                elif delete:
                    # Handle delete
                    if not client_id:
                        flask.flash("Client ID is required", "error")
                    else:
                        try:
                            # Call the delete API
                            lane.auth.clients[client_id].delete()
                            flask.flash(
                                f"Successfully deleted client {client_id}",
                                "success",
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to delete client {client_id}: {str(e)}"
                            )
                            flask.flash(f"Failed to delete client: {str(e)}", "error")
                else:
                    # Handle update
                    name = flask.request.form.get("name")
                    description = flask.request.form.get("description")

                    if not client_id:
                        flask.flash("Client ID is required", "error")
                    else:
                        try:
                            # Prepare update data
                            update_data = {}
                            if name:
                                update_data["name"] = name
                            if description:
                                update_data["description"] = description
                            raw_uris = flask.request.form.get("redirect_uris")
                            if raw_uris is not None:
                                # The textarea is always submitted, so an empty
                                # value clears the registered URIs
                                update_data["redirect_uris"] = (
                                    form_redirect_uris(raw_uris)
                                )

                            if update_data:
                                # Call the update API
                                lane.auth.clients[client_id].update(**update_data)
                                flask.flash(
                                    f"Successfully updated client {client_id}",
                                "success",
                                )
                            else:
                                flask.flash("No fields to update", "warning")
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to update client {client_id}: {str(e)}"
                            )
                            flask.flash(f"Failed to update client: {str(e)}", "error")

            # Redirect back to GET request to prevent form resubmission
            return flask.redirect(flask.url_for("clients"))

        # GET request - display clients list
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                clients_data = lane.auth.clients.list()
            except Exception as e:
                flask.current_app.logger.error(f"Failed to list clients: {str(e)}")
                error_msg = f"API call failed: {e}"

        uris_map = {
            client.id: parse_uri_list(getattr(client, "redirect_uris", None))
            for client in clients_data
        }
        empty_uris = sum(1 for uris in uris_map.values() if not uris)

        return flask.render_template(
            "clients.html",
            clients=clients_data,
            uris_map=uris_map,
            empty_uris=empty_uris,
            error=error_msg,
        )

    def _client_action(lane, client_id, action):
        if action == "revoke":
            label = flask.request.form.get("label")
            if not label:
                flask.flash("Label is required", "error")
            else:
                try:
                    lane.auth.clients[client_id].access.revoke(label, 15)
                    flask.flash(
                        f"Successfully revoked permissions for {label}", "success"
                    )
                except Exception as e:
                    flask.current_app.logger.error(
                        f"Failed to revoke permissions for {label}: {str(e)}"
                    )
                    flask.flash(f"Failed to revoke permissions: {str(e)}", "error")
        elif action == "grant":
            label = flask.request.form.get("label")
            permissions_list = flask.request.form.getlist("permissions")
            permissions = 0
            for p in permissions_list:
                if p == "R":
                    permissions |= 1
                elif p == "C":
                    permissions |= 2
                elif p == "U":
                    permissions |= 4
                elif p == "D":
                    permissions |= 8
            if not label:
                flask.flash("Label is required", "error")
            else:
                try:
                    lane.auth.clients[client_id].access.grant(label, permissions)
                    flask.flash(
                        f"Successfully granted permissions for {label}", "success"
                    )
                except Exception as e:
                    flask.current_app.logger.error(
                        f"Failed to grant permissions for {label}: {str(e)}"
                    )
                    flask.flash(f"Failed to grant permissions: {str(e)}", "error")
        elif action == "update_permissions":
            label = flask.request.form.get("label")
            permissions_list = flask.request.form.getlist("permissions")
            permissions = 0
            for p in permissions_list:
                if p == "R":
                    permissions |= 1
                elif p == "C":
                    permissions |= 2
                elif p == "U":
                    permissions |= 4
                elif p == "D":
                    permissions |= 8
            if not label:
                flask.flash("Label is required", "error")
            else:
                try:
                    lane.auth.clients[client_id].access.update(label, permissions)
                    flask.flash(
                        f"Successfully updated permissions for {label}", "success"
                    )
                except Exception as e:
                    flask.current_app.logger.error(
                        f"Failed to update permissions for {label}: {str(e)}"
                    )
                    flask.flash(f"Failed to update permissions: {str(e)}", "error")

        return flask.redirect(flask.url_for("clients"))

    @app.route("/admin/auth/clients/<client_id>/<action>", methods=["POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("clients", "admin")
    def client_action(client_id, action):
        # Every action on this route is operator-level upstream: the
        # vault-access grants are clients:admin. Nothing else is a
        # route here — the client record's scope caps are edited with
        # campus-cli, not through this page.
        if action not in CLIENT_ACTIONS:
            flask.abort(404)
        with authz.admin_lane(campus, operator_circle) as lane:
            return _client_action(lane, client_id, action)

    @app.route("/admin/auth/clients/<client_id>/revoke_secret", methods=["POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("clients", "admin")
    def revoke_secret(client_id):
        # Rotating another client's secret is operator-level upstream.
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                new_secret = lane.auth.clients[client_id].revoke()
                flask.flash(
                    "Client secret revoked. New secret (copy it now — it will "
                    f"not be shown again): {new_secret}",
                    "success",
                )
            except Exception as e:
                flask.current_app.logger.error(
                    f"Failed to revoke client secret for {client_id}: {str(e)}"
                )
                flask.flash(f"Failed to revoke client secret: {str(e)}", "error")
        return flask.redirect(flask.url_for("clients"))

    @app.route("/admin/auth/users", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("users")
    def users():
        users_data = []
        error_msg = None

        if flask.request.method == "POST":
            # Handle form submission for creating user
            action = flask.request.form.get("action")

            if action == "create":
                denied = authz.vocabulary_denial(
                    campus, operator_circle, "users", "write",
                )
                if denied is not None:
                    return denied

                email = flask.request.form.get("email")
                name = flask.request.form.get("name")

                if not email or not name:
                    flask.flash("Email and name are required", "error")
                else:
                    with authz.admin_lane(campus, operator_circle) as lane:
                        try:
                            # Call the create API using campus_python
                            new_user = lane.auth.users.new(email=email, name=name)
                            flask.flash(
                                f"Successfully created user {new_user.email}", "success"
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to create user: {str(e)}"
                            )
                            flask.flash(f"Failed to create user: {str(e)}", "error")

            elif action == "delete":
                # Deleting an account is users:admin upstream.
                denied = authz.vocabulary_denial(
                    campus, operator_circle, "users", "admin",
                )
                if denied is not None:
                    return denied

                user_id = (flask.request.form.get("user_id") or "").strip()
                typed = (flask.request.form.get("confirm") or "").strip()
                if not csrf.check(USERS_CSRF_KEY):
                    flask.flash(
                        "Your form session expired. Please reload the page "
                        "and try again.", "error",
                    )
                elif not user_id:
                    flask.flash("User ID is required", "error")
                elif typed.casefold() != user_id.casefold():
                    # The dialog's typed confirmation, enforced here so
                    # the mechanism is not only on the page.
                    flask.flash(
                        "The confirmation did not match the account's "
                        "email, so nothing was deleted.", "error",
                    )
                elif user_id.casefold() == str(flask.g.user.id).casefold():
                    # The portal re-reads the signed-in user's record on
                    # cache misses; deleting it breaks this session.
                    flask.flash(
                        "You cannot delete the account you are signed in "
                        "with.", "error",
                    )
                else:
                    with authz.admin_lane(campus, operator_circle) as lane:
                        try:
                            lane.auth.users[user_id].delete()
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to delete user {user_id}: {str(e)}"
                            )
                            flask.flash(
                                f"Failed to delete user {user_id}: {str(e)}",
                                "error",
                            )
                        else:
                            flask.flash(
                                f"Deleted user {user_id}. Signing in to this "
                                "deployment again re-creates the account.",
                                "success",
                            )

            # Redirect back to GET request to prevent form resubmission
            return flask.redirect(flask.url_for("users"))

        # GET request - display users list
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                users_data = lane.auth.users.list()
            except Exception as e:
                flask.current_app.logger.error(f"Failed to list users: {str(e)}")
                error_msg = f"API call failed: {e}"

        return flask.render_template(
            "users.html", users=users_data, error=error_msg,
            csrf_token=csrf.issue(USERS_CSRF_KEY),
        )

    @app.route("/admin/auth/sessions", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("sessions")
    def sessions():
        sessions_data = []
        error_msg = None
        filters = {
            "provider": flask.request.args.get("provider", "").strip(),
            "client_id": flask.request.args.get("client_id", "").strip(),
            "user_id": flask.request.args.get("user_id", "").strip(),
        }

        if flask.request.method == "POST":
            action = flask.request.form.get("action")

            denied = authz.vocabulary_denial(
                campus, operator_circle, "sessions", "write",
            )
            if denied is not None:
                return denied

            with authz.admin_lane(campus, operator_circle) as lane:
                if action == "revoke":
                    provider = flask.request.form.get("provider") or "campus"
                    session_id = flask.request.form.get("session_id")

                    if not session_id:
                        flask.flash("Session ID is required", "error")
                    else:
                        try:
                            # In the operator lane this delete is an admin
                            # revoke upstream (campus#1055): the service
                            # emits campus.sessions.revoke, not the flow's
                            # finalize. The designated lane needs the
                            # sessions:write grant and scope; the service
                            # checks both.
                            lane.auth.sessions[provider][session_id].finalize()
                            flask.flash(
                                f"Successfully revoked session {session_id}", "success"
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to revoke session {session_id}: {str(e)}"
                            )
                            flask.flash(f"Failed to revoke session: {str(e)}", "error")
                elif action == "sweep":
                    try:
                        swept = lane.auth.sessions.sweep()
                        flask.flash(
                            f"Successfully swept {swept} expired session(s)", "success"
                        )
                    except Exception as e:
                        flask.current_app.logger.error(
                            f"Failed to sweep sessions: {str(e)}"
                        )
                        flask.flash(f"Failed to sweep sessions: {str(e)}", "error")
                else:
                    flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("sessions"))

        # GET request - display live sessions (metadata records only).
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                records = lane.auth.sessions.list(
                    # None lists every provider; the SDK defaults to "campus".
                    provider=filters["provider"] or None,
                    client_id=filters["client_id"] or None,
                    user_id=filters["user_id"] or None,
                    limit=500,
                )
                sessions_data = [
                    {
                        "id": getattr(record, "id", None),
                        "provider": getattr(record, "provider", None) or "campus",
                        "client_id": getattr(record, "client_id", None),
                        "user_id": getattr(record, "user_id", None),
                        "created_at": getattr(record, "created_at", None),
                        "expires_at": getattr(record, "expires_at", None),
                        "scopes": getattr(record, "scopes", None) or [],
                    }
                    for record in records
                ]
            except Exception as e:
                flask.current_app.logger.error(f"Failed to list sessions: {str(e)}")
                error_msg = f"API call failed: {e}"

        return flask.render_template(
            "sessions.html", sessions=sessions_data, filters=filters, error=error_msg
        )

    @app.route("/admin/auth/vaults", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("vaults")
    def vaults():
        error_msg = None
        vault_state = authz.admin_access(
            campus, operator_circle
        )["vocabularies"]["vaults"]

        def label_denied(label):
            """Whether a designated admin's vault rows leave out `label`.

            Vault grants are per label; the operator holds every label.
            """
            return (
                not vault_state["operator_only"]
                and label not in vault_state["labels"]
            )

        if flask.request.method == "POST":
            action = flask.request.form.get("action")
            label = (flask.request.form.get("label") or "").strip()
            key = (flask.request.form.get("key") or "").strip()

            if not label or not key:
                flask.flash("Label and key are required", "error")
            elif label_denied(label):
                flask.flash(
                    f"Your vault grants do not include {label}", "error",
                )
            else:
                denied = authz.vocabulary_denial(
                    campus, operator_circle, "vaults", "write",
                )
                if denied is not None:
                    return denied

                with authz.admin_lane(campus, operator_circle) as lane:
                    if action == "set":
                        value = flask.request.form.get("value")
                        if value is None:
                            flask.flash("Value is required", "error")
                        else:
                            # The new-vault modal flags first writes. The auth
                            # API is create-on-write — a label exists only once
                            # it has a key — and brand-new labels are invisible
                            # to non-operator clients until granted, so check
                            # existence here (not on every set) and hint after.
                            is_new_label = False
                            if flask.request.form.get("new_vault"):
                                try:
                                    is_new_label = (
                                        label not in lane.auth.vaults.labels()
                                    )
                                except Exception:
                                    is_new_label = False
                            try:
                                lane.auth.vaults[label][key] = value
                                flask.flash(
                                    f"Successfully set {key} in vault {label}",
                                    "success",
                                )
                                if is_new_label:
                                    flask.flash(
                                        f"Created vault {label}. Non-operator clients "
                                        "can't see it until granted access via the "
                                        "Clients page.",
                                        "warning",
                                    )
                            except Exception as e:
                                flask.current_app.logger.error(
                                    f"Failed to set {key} in vault {label}: {str(e)}"
                                )
                                flask.flash(f"Failed to set key: {str(e)}", "error")
                    elif action == "delete_key":
                        try:
                            del lane.auth.vaults[label][key]
                            flask.flash(
                                f"Successfully deleted {key} from vault {label}",
                                "success",
                            )
                        except KeyError:
                            flask.flash(
                                f"Key {key} not found in vault {label}", "error"
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to delete {key} in vault {label}: {str(e)}"
                            )
                            flask.flash(f"Failed to delete key: {str(e)}", "error")
                    elif action in ("add_scope", "remove_scope"):
                        # SCOPES-style keys hold a whitespace-separated token
                        # list; add/remove rewrites the value via plain set.
                        scope = (flask.request.form.get("scope") or "").strip()
                        if not scope:
                            flask.flash("Scope is required", "error")
                        else:
                            try:
                                vault = lane.auth.vaults[label]
                                scopes = (vault[key] or "").split()
                                if action == "add_scope":
                                    if scope in scopes:
                                        flask.flash(
                                            f"Scope already in {key}: {scope}",
                                            "warning",
                                        )
                                    else:
                                        scopes.append(scope)
                                        vault[key] = " ".join(scopes)
                                        flask.flash(
                                            f"Scope added to {key} in vault {label}",
                                            "success",
                                        )
                                elif scope not in scopes:
                                    flask.flash(
                                        f"Scope not found in {key}: {scope}", "warning"
                                    )
                                else:
                                    scopes.remove(scope)
                                    vault[key] = " ".join(scopes)
                                    flask.flash(
                                        f"Scope removed from {key} in vault {label}",
                                        "success",
                                    )
                            except Exception as e:
                                flask.current_app.logger.error(
                                    f"Failed to update scopes in {key} "
                                    f"(vault {label}): {str(e)}"
                                )
                                flask.flash(
                                    f"Failed to update scopes: {str(e)}", "error"
                                )
                    else:
                        flask.flash("Unknown action", "error")

            return flask.redirect(
                flask.url_for("vaults", label=label) if label
                else flask.url_for("vaults")
            )

        # GET request - vault labels, or one vault's entries via ?label=.
        labels: list[str] = []
        selected = flask.request.args.get("label", "").strip()
        entries = []
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                labels = lane.auth.vaults.labels()
                if selected and label_denied(selected):
                    flask.flash(
                        f"Your vault grants do not include {selected}", "warning"
                    )
                    selected = ""
                if selected and selected not in labels:
                    flask.flash(f"No vault labeled {selected}", "warning")
                    selected = ""
                if selected:
                    vault = lane.auth.vaults[selected]
                    # vault is the SDK's HTTP-backed Vault, not a dict —
                    # keys() is an API call, hence not `for k in vault`.
                    vault_keys = vault.keys()
                    entries = [
                        {"key": key, "value": vault[key]} for key in vault_keys
                    ]
            except Exception as e:
                flask.current_app.logger.error(f"Failed to read vaults: {str(e)}")
                error_msg = f"API call failed: {e}"

        return flask.render_template(
            "vaults.html",
            labels=labels,
            selected=selected,
            entries=entries,
            error=error_msg,
        )

    @app.route("/admin/auth/credentials", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    @require_vocabulary("credentials")
    def credentials():
        # The credentials API serves campus-provider records only
        # (invariant B1): upstream provider tokens are custody-only.
        provider = "campus"
        credentials_data = []
        error_msg = None

        if flask.request.method == "POST":
            denied = authz.vocabulary_denial(
                campus, operator_circle, "credentials", "write",
            )
            if denied is not None:
                return denied

            if flask.request.form.get("action") == "disconnect":
                user_id = flask.request.form.get("user_id")
                client_id = flask.request.form.get("client_id")

                if not user_id:
                    flask.flash("User ID is required", "error")
                else:
                    with authz.admin_lane(campus, operator_circle) as lane:
                        try:
                            lane.auth.credentials[provider][user_id].delete(
                                client_id=client_id or None
                            )
                            flask.flash(
                                "Successfully disconnected "
                                f"{client_id or 'the calling client'} for {user_id}",
                                "success",
                            )
                        except Exception as e:
                            flask.current_app.logger.error(
                                f"Failed to disconnect {client_id} for "
                                f"{user_id}: {str(e)}"
                            )
                            flask.flash(f"Failed to disconnect: {str(e)}", "error")
            else:
                flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("credentials"))

        # GET request - redacted credential records (token metadata only).
        with authz.admin_lane(campus, operator_circle) as lane:
            try:
                records = lane.auth.credentials[provider].list()
                credentials_data = [
                    {
                        "id": getattr(record, "id", None),
                        "client_id": getattr(record, "client_id", None),
                        "user_id": getattr(record, "user_id", None),
                        "token_type": getattr(record.token, "token_type", None)
                        if getattr(record, "token", None) is not None else None,
                        "expires_at": getattr(record.token, "expires_at", None)
                        if getattr(record, "token", None) is not None else None,
                        "scopes": split_scopes(getattr(record.token, "scopes", None))
                        if getattr(record, "token", None) is not None else [],
                        "created_at": getattr(record, "created_at", None),
                    }
                    for record in records
                ]
            except Exception as e:
                flask.current_app.logger.error(f"Failed to list credentials: {str(e)}")
                error_msg = f"API call failed: {e}"

        return flask.render_template(
            "credentials.html",
            credentials=credentials_data,
            provider=provider,
            error=error_msg,
        )

    @app.get("/admin/auth/enable")
    @login_manager.login_required
    @admin_required
    def admin_enable():
        """Step-up re-auth: put a designated admin's granted scopes on the
        session token.

        Sign-in requests only the structural baseline
        (LOGIN_SCOPES_BASE), so the session of a designated admin does
        not carry the management vocabularies their grants cover. This
        route re-enters the OAuth flow requesting exactly those scopes —
        the auth service's browser session is warm, so it is a fast
        redirect round trip, and finalize_login lands back on the page
        that asked. The auth service unions the scopes into the grant
        record (A4), and the next request reads them afresh (the scope
        cache is keyed on the new login session).

        Failure is contained: a vocabulary the portal client's cap does
        not cover was never asked for (the page says the cap must be
        widened); a rejected session-creation call renders the reason
        instead of a 500.
        """
        from campus.common.utils import url as campus_url
        from campus.flask_campus import context

        next_url = flask.request.args.get("next") or ""
        if not next_url.startswith("/admin") or next_url.startswith("//"):
            next_url = flask.url_for("dashboard")
        access = authz.admin_access(campus, operator_circle)
        scopes = list(access["stepup"]["scopes"])
        if access["lane"] == "operator" or not scopes:
            flask.flash(
                "Nothing to enable: this session already covers your "
                "grants.", "info",
            )
            return flask.redirect(next_url)
        flask.session["login_next"] = next_url
        try:
            return context.start_login(
                campus,
                target=campus_url.full_url_for("auth.finalize_login"),
                scopes=scopes,
            )
        except campus_python.errors.BadRequestError as err:
            # The portal client's allowed_scopes cap does not cover a
            # requested scope: nothing the user can do here — the
            # deployment widens the cap (README, Access Model).
            flask.current_app.logger.warning(
                "Admin step-up rejected (allowed_scopes cap): %s", err,
            )
            flask.flash(
                "Access could not be enabled on this deployment's portal "
                "client: its allowed_scopes cap does not include the "
                "requested scopes. Ask the deployment owner to widen the "
                "cap (README, Access Model).",
                "error",
            )
            return flask.redirect(next_url)

    # The root namespace belongs to the gateway (campus-admin#97): the
    # portal's pages live under /admin/<platform>, and the flask_campus
    # auth blueprint stays at root relying on PrefixFix for the
    # gateway's /admin/* dispatch (see the login manager above) —
    # /login, /finalize_login and /logout on the origin, /admin/login
    # etc. behind the gateway. Legacy root paths beyond those redirect
    # to their remapped homes.
    return app
