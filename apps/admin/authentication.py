"""campus.apps.admin.authentication

Campus Admin Portal Application: authentication routes and views.
"""

import flask
import werkzeug

import campus_python
from campus import flask_campus

campus = campus_python.Campus(timeout=60)
bp = flask.Blueprint("auth", __name__, url_prefix="/")


@bp.get("/login")
@flask_campus.unpack_request
def authorize_login(next: str) -> werkzeug.Response:
    """Initiate Sign In to NYJC"""
    return campus.auth.authorize(
        target=next or '/'
    )

@bp.post("/login")
@flask_campus.unpack_request
def finalize_login(
        state: str,
        code: str,
        scope: str
) -> werkzeug.Response:
    """Finalize Sign In to NYJC"""
    resp = campus.auth.finalize(state=state, code=code, scope=scope)
    return resp

@bp.get("/logout")
def logout():
    """Sign Out of NYJC"""
    campus.auth.logout()
    resp = flask.redirect(flask.url_for("index"))
    return resp
