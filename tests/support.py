"""Shared test harness for the admin portal's view tests.

Two harnesses:

- make_app: a minimal Flask app hosting the audit blueprints with a
  fake signed-in user and the audit wire client swapped for a
  recording JsonClient double (apps.admin.audit.client).
- make_admin_app: the REAL create_app() with the Campus SDK client
  swapped for a recording MagicMock, so the route closures capture the
  double. The portal asks three services who the signed-in user is
  (apps.admin.authz), and the harness answers each from fakes:
  campus.api circles and the per-family access probe
  (FakeCirclesCollection), the campus.auth grant store
  (auth.grants.list) and the session token's scopes (auth.get_token).
  The migrations view (#75) drives its per-service wire calls through
  the apps.admin.migrations.client.clients_factory seam, separate from
  these.

Usage (in a TestCase):

    self.app, self.client, self.campus, cleanup = make_admin_app()
    self.addCleanup(cleanup)

File: tests/support.py
"""

import json
import os
import re
import typing
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import flask
from campus_python.audit.http import JsonResponse

from apps.admin import audit, authz
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

OPERATOR_CIRCLE_ID = "circle_campus"

# The campus.api access-value group field (ratified layout): create,
# modify and read bits, and the full admin field.
GROUP_CREATE = 0b1000000
GROUP_MODIFY = 0b0100000
GROUP_READ = 0b0010000
GROUP_ADMIN = 0b1110000

# Every management scope a designated admin could be granted.
ALL_MANAGEMENT_SCOPES = (
    "campus.profile", "users:read",
    "clients:read", "clients:write", "clients:admin",
    "users:mod", "users:write", "users:admin",
    "sessions:read", "sessions:write",
    "credentials:read", "credentials:write",
    "vaults:read", "vaults:write", "vaults:admin",
    "migrations:read", "migrations:write", "migrations:admin",
)


def grant(resource_type: str, level: str, resource_id: str = "") -> dict:
    """One access-grant store row, as the API returns it."""
    return {
        "id": f"uid-grant-{resource_type}-{level}-{resource_id or 'x'}",
        "grantee_type": "user",
        "grantee_id": TEST_USER_ID,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "level": level,
        "bits": None,
    }


class FakeCirclesCollection:
    """The api.circles surface the gates and the Resources views use.

    A real Mock for the views' own calls (list, new, and per-circle
    MagicMocks), plus the circles the portal's gates resolve: the
    operator circle with its members and their access values, and one
    grant circle per campus.api family.
    """

    def __init__(
        self,
        circle_name: str = authz.OPERATOR_CIRCLE,
        members: typing.Mapping[str, int] | typing.Sequence[str] = (TEST_USER_ID,),
        families: typing.Sequence[str] = authz.API_FAMILIES,
    ):
        if not isinstance(members, typing.Mapping):
            # A bare sequence means "admin members of the operator circle".
            members = dict.fromkeys(members, authz.ACCESS_CONSTANTS["admin"])
        self._circles: dict[str, dict] = {}
        if circle_name:
            self._circles[OPERATOR_CIRCLE_ID] = {
                "id": OPERATOR_CIRCLE_ID, "name": circle_name,
                "tag": "group", "members": dict(members),
            }
        for family in families:
            circle_id = f"circle_{family}"
            self._circles[circle_id] = {
                "id": circle_id, "name": family,
                "tag": "resource", "members": {},
            }
        self._items: dict[str, mock.MagicMock] = {}
        self.list = mock.Mock(side_effect=self._list)
        self.new = mock.MagicMock()

    def _list(self):
        return [
            SimpleNamespace(
                id=circle["id"], name=circle["name"], tag=circle["tag"],
                description="", created_at=None,
            )
            for circle in self._circles.values()
        ]

    def __getitem__(self, circle_id):
        if circle_id not in self._items:
            item = mock.MagicMock()
            item.members = mock.MagicMock()
            members = self._circles.get(circle_id, {}).get("members", {})

            def resolve(member_id, **_ignored):
                """The shared-resolver probe (campus#1202): resolution
                dict, None when unreachable."""
                if member_id not in members:
                    return None
                return {
                    "member_id": member_id,
                    "access_value": members[member_id],
                    "direct": True,
                    "via": [],
                }

            item.members.resolve.side_effect = resolve
            self._items[circle_id] = item
        return self._items[circle_id]


def make_access_probe(
    circles: FakeCirclesCollection, family_access: typing.Mapping[str, int] | None,
):
    """A stand-in for campus.api's gate-equivalent access probe.

    `family_access` maps a family name to the group-scope field a
    NON-operator resolves to on it (default: nothing). A platform
    operator short-circuits to the wildcard answer, as the service's
    own resolver does.
    """
    family_access = dict(family_access or {})
    pattern = re.compile(r"/api/v1/circles/([^/]+)/access/([^/]+)/")

    def get(path, query=None):  # noqa: ARG001 - stub shape
        match = pattern.fullmatch(path)
        if match is None:
            raise AssertionError(f"unexpected raw campus.api call: {path}")
        circle_id, member_id = match.group(1), urllib.parse.unquote(match.group(2))
        circle = circles._circles.get(circle_id)
        if circle is None:
            return SimpleNamespace(status_code=409, json=lambda: {})
        operator = circles._circles.get(OPERATOR_CIRCLE_ID, {})
        held = operator.get("members", {}).get(member_id, 0)
        if authz.has_permission(held, "group", "create"):
            body = {"wildcard": True, "g": GROUP_ADMIN, "s": 0b1110}
        else:
            body = {
                "wildcard": False,
                "g": family_access.get(circle["name"], 0), "s": 0,
            }
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"circle_id": circle_id, "member_id": member_id, **body},
        )

    return get


class StubResponse:
    """JsonResponse double with a canned body and status.

    ``raise_for_status`` is the SDK's real implementation, so a stub
    carrying an error status raises the same typed error the audit
    client resources raise in production, with the body attached as an
    exception note.
    """

    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else {}
        self.status_code = status_code
        self.headers = {}

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def client_error(self) -> bool:
        return 400 <= self.status_code < 500

    def server_error(self) -> bool:
        return 500 <= self.status_code < 600

    def raise_for_status(self):
        # The campus.audit client resources call this before returning.
        JsonResponse.raise_for_status(self)


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

    def get(self, path, query=None):  # query: sdk#180 kwarg name
        return self._respond("GET", path, query)

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

    # The audit lists are read per request; a key-admins list left in
    # the environment by another test would change who may manage keys.
    for var in ("AUDIT_KEY_ADMINS",):
        os.environ.pop(var, None)

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
        for stopper in stoppers:
            stopper()

    return app, client, cleanup


def make_admin_app(
    *,
    circle_name: str | None = authz.OPERATOR_CIRCLE,
    members: tuple[str, ...] = (TEST_USER_ID,),
    user_id: str | None = TEST_USER_ID,
    scopes: typing.Sequence[str] | None = TEST_MIGRATIONS_SCOPES,
    wire_clients: dict | None = None,
    get_token_error: Exception | None = None,
    grants: typing.Sequence[dict] | None = None,
    family_access: typing.Mapping[str, int] | None = None,
    scope_cap: typing.Sequence[str] | None = None,
    admin_circle_env: str | None = None,
) -> tuple[flask.Flask, typing.Any, mock.MagicMock, typing.Callable[[], None]]:
    """Build the real create_app() with a mocked Campus SDK client.

    Who the signed-in user is, per service:

    - circle_name / members: the portal's operator circle and its
      `admin` members. The default makes the test user a platform
      operator (the operator console); pass members=() for someone who
      is not, or circle_name=None for a deployment without the circle.
    - grants: the user's rows in the campus.auth access-grant store
      (tests.grant builds one). A non-operator holding at least one is
      a designated admin.
    - scopes: what campus.auth.get_token() reports as the signed-in
      session's token scopes (the capability leg the views render
      from); None leaves the mock returning a plain MagicMock, whose
      scopes fail the read and render fail-closed.
    - get_token_error: when set, get_token raises it (the session
      token cannot be read at all).
    - family_access: {family: group-scope field} a NON-operator
      resolves to on each campus.api family (support.GROUP_*); absent
      families resolve to nothing.
    - scope_cap: the portal client's allowed_scopes (defaults to every
      management scope).
    - admin_circle_env: sets the ADMIN_CIRCLE env override.
    - wire_clients: {service: JsonClient double} installed on the
      apps.admin.migrations.client.clients_factory seam — the
      per-service stubs the view's wire calls hit.

    with_user_session() yields the campus mock itself, so view calls
    made inside the signed-in user's session are recorded on the same
    mock the operator lane uses.

    Returns (app, test client, campus mock, cleanup).
    """
    stoppers: list[typing.Callable[[], None]] = []
    authz.reset_caches()
    stoppers.append(authz.reset_caches)

    campus_mock = mock.MagicMock()
    campus_mock.auth.sessions.has_session.return_value = False
    campus_mock.auth.logins.has_session.return_value = False

    circles_collection = FakeCirclesCollection(circle_name, members)
    campus_mock.api.circles = circles_collection
    campus_mock.api.client.get.side_effect = make_access_probe(
        circles_collection, family_access,
    )
    campus_mock.with_user_session.return_value.__enter__.return_value = (
        campus_mock
    )
    campus_mock.auth.grants.list.return_value = {"grants": list(grants or [])}
    campus_mock.auth.clients.__getitem__.return_value.get.return_value = (
        SimpleNamespace(
            allowed_scopes=list(
                ALL_MANAGEMENT_SCOPES if scope_cap is None else scope_cap
            ),
        )
    )

    if get_token_error is not None:
        campus_mock.auth.get_token.side_effect = get_token_error
    elif scopes is not None:
        campus_mock.auth.get_token.return_value = SimpleNamespace(
            scopes=list(scopes),
        )

    env_vars = {"SECRET_KEY": "test-secret"}
    if admin_circle_env is not None:
        env_vars["ADMIN_CIRCLE"] = admin_circle_env

    class _StubCampus:
        # Class-shaped Campus double: flask_campus 0.2.5+ (#40) runs
        # isinstance(campus_client, campus_python.Campus), so the patch
        # must be a real class — __new__ hands back the shared mock, and
        # the manager's duck-typed branch routes the framework's own
        # calls through it too.

        def __new__(cls, *_args, **_kwargs):
            return campus_mock

    patchers = [
        mock.patch.dict(os.environ, env_vars, clear=False),
        mock.patch("campus_python.Campus", _StubCampus),
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
        for stopper in stoppers:
            stopper()

    return app, app.test_client(), campus_mock, cleanup
