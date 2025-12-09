"""campus.apps.admin.authentication

Campus Admin Portal Application: authentication routes and views.
"""

from campus.common import flask as campus_flask
import campus_python
import flask
import werkzeug

campus = campus_python.Campus(timeout=60)
bp = flask.Blueprint("auth", __name__, url_prefix="/")


@bp.get("/login")
@campus_flask.unpack_request
def authorize_login(next: str) -> werkzeug.Response:
    """Initiate Sign In to NYJC"""
    resp = campus.auth.authorize(
        redirect_uri=flask.url_for(".finalize_login"),
        target=next or "/"
    )
    return resp

@bp.post("/login")
@campus_flask.unpack_request
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
