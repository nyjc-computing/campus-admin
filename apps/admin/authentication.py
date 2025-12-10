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
    # Build authorization URL directly to preserve browser user-agent
    # Don't use campus_python SDK here as it makes server-side request
    auth_base_url = campus.auth.base_url
    authorize_path = campus.auth.make_path("campus/authorize")
    authorize_url = f"{auth_base_url}{authorize_path}?target={next or '/'}"
    
    return flask.redirect(authorize_url)

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
    resp = flask.redirect(flask.url_for(".index"))
    return resp
