"""Shared test harness for the audit web UI tests.

Builds a minimal Flask app hosting the audit blueprints with:

- a fake signed-in user (SimpleNamespace with an id) injected via
  before_request — the real login flow (flask_campus OAuthLoginManager)
  is portal-level behaviour already covered by the CI smoke job;
- a stub auth blueprint satisfying url_for("auth.login") for the
  anonymous-redirect branch;
- the audit wire client swapped for a recording JsonClient double via
  apps.admin.audit.client.json_client_factory;
- AUDIT_ADMINS installed via patch.dict for the duration of the test.

Usage (in a TestCase):

    self.app, self.json_client, self.wire = make_app()
    self.addCleanup(self.wire)

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

    app = flask.Flask(__name__)
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
