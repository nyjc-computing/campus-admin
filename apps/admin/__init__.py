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
    campus = campus_python.Campus(timeout=60)

    # Configure Flask secret key from environment
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

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

    # Admin required decorator
    def admin_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            admins = os.environ.get("ADMINS", "").split(";")
            if not hasattr(flask.g, "user") or flask.g.user.id not in admins:
                flask.abort(403)
            return f(*args, **kwargs)

        return decorated_function

    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus, default_endpoint="index"
    )
    login_manager.init_app(app)

    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        return flask.render_template("sign_in.html")

    @app.get("/dashboard")
    @login_manager.login_required
    def dashboard(**_):
        return flask.render_template("dashboard.html")

    @app.route("/clients", methods=["GET", "POST"])
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

    @app.route("/clients/<client_id>/<action>", methods=["POST"])
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

    @app.route("/clients/<client_id>/revoke_secret", methods=["POST"])
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

    @app.route("/users", methods=["GET", "POST"])
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

    return app
