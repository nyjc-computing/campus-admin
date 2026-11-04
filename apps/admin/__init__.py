"""campus.apps.admin

Campus Admin Portal Application: authentication routes and views.
"""

import json
import os
from datetime import datetime
from functools import wraps

import campus_python
import flask
from campus import flask_campus
from campus.common import env


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


def create_app():
    """Application factory for Campus Admin Portal."""
    app = flask.Flask(__name__, static_folder="static", static_url_path="/static")

    # Trace producer (campus#816): record admin requests as audit spans,
    # with SDK calls landing as child spans. Opt-in via AUDIT_API_KEY +
    # AUDIT_TRACING_ENABLED; see campus docs/audit-tracing.md. Read once
    # at app creation — flipping the flag is a redeploy.
    if env.get_flag("AUDIT_TRACING_ENABLED", False):
        from campus.audit.middleware import init_app as init_audit_tracing

        init_audit_tracing(app)

    # Action journeys (campus#828): one user-initiated episode — the
    # page load that began it, its XHRs and form posts, and the
    # server-to-server calls they spawn — groups as one journey in the
    # audit UI. Navigations mint; the sliding campus_action_journey
    # cookie carries the episode across same-origin requests.
    from campus.audit.middleware import init_journeys

    init_journeys(app)

    campus = campus_python.Campus(timeout=60)

    # Configure Flask secret key from environment
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

    # The admin-section allowlist moved to AUTH_ADMINS (mirroring
    # AUDIT_ADMINS); flag deployments still on the legacy name.
    if not os.environ.get("AUTH_ADMINS") and os.environ.get("ADMINS"):
        app.logger.warning(
            "ADMINS is deprecated: rename it to AUTH_ADMINS (the admin "
            "section gate reads AUTH_ADMINS first, ADMINS as a fallback)"
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

    # Admin required decorator. The gate reads AUTH_ADMINS — the name
    # mirrors AUDIT_ADMINS, the audit section's allowlist. The
    # pre-rename ADMINS name still works when AUTH_ADMINS is unset or
    # empty (create_app logs a startup warning so the rename gets
    # finished); both are read per request, so list changes apply
    # without a redeploy.
    def admin_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            admins = (
                os.environ.get("AUTH_ADMINS") or os.environ.get("ADMINS", "")
            ).split(";")
            if not hasattr(flask.g, "user") or flask.g.user.id not in admins:
                flask.abort(403)
            return f(*args, **kwargs)

        return decorated_function

    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus, default_endpoint="index"
    )
    login_manager.init_app(app)

    # Audit web UI (migrated from campus.audit.web): the trace viewer
    # and key-management pages under /audit, gated by this portal's
    # login plus the AUDIT_ADMINS superadmin list, with data proxied
    # to the audit service's versioned API.
    from apps.admin import audit

    audit.init_app(app)

    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        return flask.render_template("sign_in.html")

    @app.get("/admin/dashboard")
    @login_manager.login_required
    def dashboard(**_):
        return flask.render_template("dashboard.html")

    @app.route("/admin/clients", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    def clients():
        clients_data = []
        error_msg = None

        if flask.request.method == "POST":
            # Handle form submission for creating, updating, or deleting client
            action = flask.request.form.get("action")
            client_id = flask.request.form.get("client_id")
            delete = flask.request.form.get("delete")

            if action == "create":
                # Handle create
                name = flask.request.form.get("name")
                description = flask.request.form.get("description")

                if not name or not description:
                    flask.flash("Name and description are required", "error")
                else:
                    try:
                        # Call the create API using campus_python
                        new_client = campus.auth.clients.new(
                            name=name,
                            description=description,
                            is_public=flask.request.form.get("is_public") == "on",
                            redirect_uris=form_redirect_uris(
                                flask.request.form.get("redirect_uris")
                            ),
                        )
                        flask.flash(
                            f"Successfully created client {new_client.id}", "success"
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
                        campus.auth.clients[client_id].delete()
                        flask.flash(
                            f"Successfully deleted client {client_id}", "success"
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
                            update_data["redirect_uris"] = form_redirect_uris(raw_uris)

                        if update_data:
                            # Call the update API
                            campus.auth.clients[client_id].update(**update_data)
                            flask.flash(
                                f"Successfully updated client {client_id}", "success"
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
        try:
            clients_data = campus.auth.clients.list()
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

    @app.route("/admin/clients/<client_id>/<action>", methods=["POST"])
    @login_manager.login_required
    @admin_required
    def client_action(client_id, action):
        if action == "revoke":
            label = flask.request.form.get("label")
            if not label:
                flask.flash("Label is required", "error")
            else:
                try:
                    campus.auth.clients[client_id].access.revoke(label, 15)
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
                    campus.auth.clients[client_id].access.grant(label, permissions)
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
                    campus.auth.clients[client_id].access.update(label, permissions)
                    flask.flash(
                        f"Successfully updated permissions for {label}", "success"
                    )
                except Exception as e:
                    flask.current_app.logger.error(
                        f"Failed to update permissions for {label}: {str(e)}"
                    )
                    flask.flash(f"Failed to update permissions: {str(e)}", "error")
        else:
            flask.flash("Unknown action", "error")

        return flask.redirect(flask.url_for("clients"))

    @app.route("/admin/clients/<client_id>/revoke_secret", methods=["POST"])
    @login_manager.login_required
    @admin_required
    def revoke_secret(client_id):
        try:
            new_secret = campus.auth.clients[client_id].revoke()
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

    @app.route("/admin/users", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    def users():
        users_data = []
        error_msg = None

        if flask.request.method == "POST":
            # Handle form submission for creating user
            action = flask.request.form.get("action")

            if action == "create":
                # Handle create
                email = flask.request.form.get("email")
                name = flask.request.form.get("name")

                if not email or not name:
                    flask.flash("Email and name are required", "error")
                else:
                    try:
                        # Call the create API using campus_python
                        new_user = campus.auth.users.new(email=email, name=name)
                        flask.flash(
                            f"Successfully created user {new_user.email}", "success"
                        )
                    except Exception as e:
                        flask.current_app.logger.error(
                            f"Failed to create user: {str(e)}"
                        )
                        flask.flash(f"Failed to create user: {str(e)}", "error")

            # Redirect back to GET request to prevent form resubmission
            return flask.redirect(flask.url_for("users"))

        # GET request - display users list
        try:
            users_data = campus.auth.users.list()
        except Exception as e:
            flask.current_app.logger.error(f"Failed to list users: {str(e)}")
            error_msg = f"API call failed: {e}"

        return flask.render_template("users.html", users=users_data, error=error_msg)

    @app.route("/admin/sessions", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
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

            if action == "revoke":
                provider = flask.request.form.get("provider") or "campus"
                session_id = flask.request.form.get("session_id")

                if not session_id:
                    flask.flash("Session ID is required", "error")
                else:
                    try:
                        # The admin portal holds the operator principal, so
                        # this delete is an admin revoke upstream
                        # (campus#1055): the service emits
                        # campus.sessions.revoke, not the flow's finalize.
                        campus.auth.sessions[provider][session_id].finalize()
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
                    swept = campus.auth.sessions.sweep()
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
        try:
            records = campus.auth.sessions.list(
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

    @app.route("/admin/vaults", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    def vaults():
        error_msg = None

        if flask.request.method == "POST":
            action = flask.request.form.get("action")
            label = flask.request.form.get("label")
            key = flask.request.form.get("key")

            if not label or not key:
                flask.flash("Label and key are required", "error")
            elif action == "set":
                value = flask.request.form.get("value")
                if value is None:
                    flask.flash("Value is required", "error")
                else:
                    try:
                        campus.auth.vaults[label][key] = value
                        flask.flash(
                            f"Successfully set {key} in vault {label}", "success"
                        )
                    except Exception as e:
                        flask.current_app.logger.error(
                            f"Failed to set {key} in vault {label}: {str(e)}"
                        )
                        flask.flash(f"Failed to set key: {str(e)}", "error")
            elif action == "delete_key":
                try:
                    del campus.auth.vaults[label][key]
                    flask.flash(
                        f"Successfully deleted {key} from vault {label}", "success"
                    )
                except KeyError:
                    flask.flash(f"Key {key} not found in vault {label}", "error")
                except Exception as e:
                    flask.current_app.logger.error(
                        f"Failed to delete {key} from vault {label}: {str(e)}"
                    )
                    flask.flash(f"Failed to delete key: {str(e)}", "error")
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
        try:
            labels = campus.auth.vaults.labels()
            if selected and selected not in labels:
                flask.flash(f"No vault labeled {selected}", "warning")
                selected = ""
            if selected:
                vault = campus.auth.vaults[selected]
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

    @app.route("/admin/credentials", methods=["GET", "POST"])
    @login_manager.login_required
    @admin_required
    def credentials():
        # The credentials API serves campus-provider records only
        # (invariant B1): upstream provider tokens are custody-only.
        provider = "campus"
        credentials_data = []
        error_msg = None

        if flask.request.method == "POST":
            if flask.request.form.get("action") == "disconnect":
                user_id = flask.request.form.get("user_id")
                client_id = flask.request.form.get("client_id")

                if not user_id:
                    flask.flash("User ID is required", "error")
                else:
                    try:
                        campus.auth.credentials[provider][user_id].delete(
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
        try:
            records = campus.auth.credentials[provider].list()
            credentials_data = [
                {
                    "id": getattr(record, "id", None),
                    "client_id": getattr(record, "client_id", None),
                    "user_id": getattr(record, "user_id", None),
                    "token_type": getattr(record.token, "token_type", None)
                    if getattr(record, "token", None) is not None else None,
                    "expires_at": getattr(record.token, "expires_at", None)
                    if getattr(record, "token", None) is not None else None,
                    "scope": getattr(record.token, "scope", None)
                    if getattr(record, "token", None) is not None else None,
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

    # Pre-namespace URLs (bookmarks, old next= targets, saved forms):
    # 308 so the method and body survive the move (client/user actions
    # are POSTs). The clients action routes carry their path segments.
    @app.get("/dashboard")
    def legacy_dashboard():
        return flask.redirect(flask.url_for("dashboard"), code=308)

    @app.route("/clients", methods=["GET", "POST"])
    def legacy_clients():
        return flask.redirect(flask.url_for("clients"), code=308)

    @app.route("/clients/<client_id>/<action>", methods=["POST"])
    def legacy_client_action(client_id, action):
        return flask.redirect(
            flask.url_for("client_action", client_id=client_id, action=action),
            code=308,
        )

    @app.route("/clients/<client_id>/revoke_secret", methods=["POST"])
    def legacy_revoke_secret(client_id):
        return flask.redirect(
            flask.url_for("revoke_secret", client_id=client_id), code=308
        )

    @app.route("/users", methods=["GET", "POST"])
    def legacy_users():
        return flask.redirect(flask.url_for("users"), code=308)

    @app.route("/sessions", methods=["GET", "POST"])
    def legacy_sessions():
        return flask.redirect(flask.url_for("sessions"), code=308)

    @app.route("/vaults", methods=["GET", "POST"])
    def legacy_vaults():
        return flask.redirect(flask.url_for("vaults"), code=308)

    @app.route("/credentials", methods=["GET", "POST"])
    def legacy_credentials():
        return flask.redirect(flask.url_for("credentials"), code=308)

    return app
