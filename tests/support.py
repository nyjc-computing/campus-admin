"""Shared test harness for the admin portal's view tests.

Two harnesses:

- make_app: a minimal Flask app hosting the audit blueprints with a
  fake signed-in user and the audit wire client swapped for a
  recording JsonClient double (apps.admin.audit.client).
- make_admin_app: the REAL create_app() with the Campus SDK client
  swapped for a recording MagicMock (the test_admin_resources
  pattern), for views gated by the admin circle — the migrations
  view (#75) drives its per-service wire calls through the
  apps.admin.migrations.client.clients_factory seam.

Usage (in a TestCase):

    self.app, self.client, self.campus, cleanup = make_admin_app()
    self.addCleanup(cleanup)

File: tests/support.py
"""

import json
import os
import typing
from types import SimpleNamespace
from unittest import mock

import flask

from apps.admin import audit
from apps.admin.audit import client as audit_client

TEST_USER_ID = "staff@nyjc.edu.sg"

# The migrations vocabulary the tests' session tokens carry by
# default (read < write < admin).
TEST_MIGRATIONS_SCOPES = (
    "campus.profile",
    "migrations:read",
    "migrations:write",
    "migrations:admin",
)

ADMIN_CIRCLE_ID = "circle_admin01"


class FakeCirclesCollection:
    """The api.circles surface the admin gate's membership check uses."""

    def __init__(self, circle_name: str, members: tuple[str, ...]):
        self._circle_name = circle_name
        self._members = dict.fromkeys(members, 15)

    def list(self):
        return [SimpleNamespace(id=ADMIN_CIRCLE_ID, name=self._circle_name)]

    def __getitem__(self, circle_id):
        assert circle_id == ADMIN_CIRCLE_ID
        collection = self

        class FakeMembers:
            def get(self, member_id):
                """The membership probe: access value, None when absent."""
                return collection._members.get(member_id)

        return SimpleNamespace(id=circle_id, members=FakeMembers())


class StubResponse:
    """JsonResponse double with a canned body and status."""

    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else {}
        self.status_code = status_code

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def raise_for_status(self):
        # The campus.audit client resources call this before returning;
        # stubs carry non-error statuses only.
        return None


class StubJsonClient:
    """JsonClient double: routes (method, path-prefix) to canned responses.

    handlers maps (method, path_prefix) -> StubResponse; the first
    matching prefix wins. Unmatched calls return 200 {} and are still
    recorded, so tests can assert on paths and params.
    """

    def __init__(self, handlers=None):
        self.handlers = {
            (method, path): response
            for (method, path), response in (handlers or {}).items()
        }
        self.calls: list[tuple[str, str, dict | None, object]] = []

    def _respond(self, method, path, params=None, json=None):
        self.calls.append((method, path, params, json))
        for (route_method, route_path), response in self.handlers.items():
            if method == route_method and path.startswith(route_path):
                return response
        return StubResponse()

    def get(self, path, params=None):
        return self._respond("GET", path, params)

    def post(self, path, json=None):
        return self._respond("POST", path, None, json)

    def put(self, path, json=None):
        return self._respond("PUT", path, None, json)

    def delete(self, path):
        return self._respond("DELETE", path)

    def patch(self, path, json=None):
        return self._respond("PATCH", path, None, json)


def make_app(
        *,
        user_id: str | None = TEST_USER_ID,
        admins: str | None = TEST_USER_ID,
        json_client: StubJsonClient | None = None,
) -> tuple[flask.Flask, typing.Any, typing.Callable[[], None]]:
    """Build a test app hosting the audit blueprints.

    Args:
        user_id: the signed-in user's campus.auth id (None = anonymous).
        admins: the AUDIT_ADMINS allowlist to install for the test
            (None leaves the environment untouched).
        json_client: the wire double for the audit service (None keeps
            any factory already installed).

    Returns:
        (app, test client, cleanup) — call cleanup() (e.g. via
        addCleanup) to restore the environment and client factory.
    """
    stoppers: list[typing.Callable[[], None]] = []

    # The app is named after the real package so its static folder
    # (apps/admin/static, shared by both sections since the CSS
    # unification) resolves exactly as in create_app.
    app = flask.Flask("apps.admin", static_folder="static", static_url_path="/static")
    app.secret_key = "test-secret"

    if user_id is not None:
        def _inject_user():
            flask.g.user = SimpleNamespace(id=user_id, email=user_id)

        app.before_request(_inject_user)

    # Satisfies url_for("auth.login", next=...) in the gate's
    # anonymous-redirect branch; the real login blueprint belongs to
    # OAuthLoginManager, which these unit tests do not install.
    auth_bp = flask.Blueprint("auth", __name__)

    @auth_bp.get("/login")
    def login():
        return "login page"

    app.register_blueprint(auth_bp)

    if admins is not None:
        patcher = mock.patch.dict("os.environ", {"AUDIT_ADMINS": admins})
        patcher.start()
        stoppers.append(patcher.stop)
    else:
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        stoppers.append(patcher.stop)

    # Keep the client-name lookup failing fast in every environment
    # (the real lookup is a campus.auth HTTP call): tests either assert
    # raw ids or stub get_client_names directly.
    os.environ.pop("CLIENT_ID", None)
    os.environ.pop("CLIENT_SECRET", None)

    audit.init_app(app)
    client = app.test_client()

    if json_client is not None:
        restore = audit_client.json_client_factory
        audit_client.json_client_factory = lambda: json_client
        stoppers.append(lambda: setattr(audit_client, "json_client_factory", restore))

    def cleanup():
        for stop in stoppers:
            stop()

    return app, client, cleanup


def make_admin_app(
        *,
        circle_name: str = "campus-admin",
        members: tuple[str, ...] = (TEST_USER_ID,),
        user_id: str | None = TEST_USER_ID,
        scopes: typing.Sequence[str] = TEST_MIGRATIONS_SCOPES,
        wire_clients: dict | None = None,
        get_token_error: Exception | None = None,
) -> tuple[flask.Flask, typing.Any, mock.MagicMock, typing.Callable[[], None]]:
    """Build the real create_app() with a mocked Campus SDK client.

    The harness for admin-circle-gated views (the migrations view,
    #75): the same pattern as test_admin_resources.make_app, plus

    - scopes: what campus.auth.get_token() reports as the signed-in
      session's token scopes (the capability leg the view renders
      from); None leaves the mock returning a plain MagicMock, whose
      scopes fail the view's read and render fail-closed;
    - get_token_error: when set, get_token raises it (the session
      token cannot be read at all);
    - wire_clients: {service: JsonClient double} installed on the
      apps.admin.migrations.client.clients_factory seam — the
      per-service stubs the view's wire calls hit. The circles gate
      still rides the mocked with_user_session (FakeCirclesCollection
      above), separate from the wire stubs.

    Returns (app, test client, campus mock, cleanup).
    """
    stoppers: list[typing.Callable[[], None]] = []

    campus_mock = mock.MagicMock()
    campus_mock.auth.sessions.has_session.return_value = False
    campus_mock.auth.logins.has_session.return_value = False

    circles_collection = FakeCirclesCollection(circle_name, tuple(members))
    session_cm = campus_mock.with_user_session.return_value
    session_cm.__enter__.return_value = SimpleNamespace(
        api=SimpleNamespace(circles=circles_collection),
    )

    if get_token_error is not None:
        campus_mock.auth.get_token.side_effect = get_token_error
    elif scopes is not None:
        campus_mock.auth.get_token.return_value = SimpleNamespace(
            scopes=list(scopes),
        )

    env_vars = {"SECRET_KEY": "test-secret"}
    patchers = [
        mock.patch.dict(os.environ, env_vars, clear=False),
        mock.patch("campus_python.Campus", return_value=campus_mock),
    ]
    for patcher in patchers:
        patcher.start()
        stoppers.append(patcher.stop)

    from apps.admin import create_app
    from apps.admin.migrations import client as migrations_client

    app = create_app()

    # Runs after the login manager's push_context hook (registration
    # order), restoring the signed-in admin it cleared.
    if user_id is not None:
        @app.before_request
        def _inject_user():
            flask.g.user = SimpleNamespace(id=user_id, email=user_id)

    if wire_clients is not None:
        restore_factory = migrations_client.clients_factory
        migrations_client.clients_factory = lambda: wire_clients
        stoppers.append(
            lambda: setattr(migrations_client, "clients_factory", restore_factory)
        )

    def cleanup():
        for stop in stoppers:
            stop()

    return app, app.test_client(), campus_mock, cleanup
