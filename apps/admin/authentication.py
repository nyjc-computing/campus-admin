"""campus.apps.admin.authentication

Campus Admin Portal Application: authentication routes and views.
"""

import flask
import werkzeug

import campus_python
from campus import flask_campus

campus = campus_python.Campus(timeout=60)
bp = flask.Blueprint("auth", __name__, url_prefix="/")


def _is_safe_redirect(url: str) -> bool:
    """Ensure URL is safe for redirect (prevents open redirect attacks)."""
    # Only allow relative URLs starting with /
    # Reject protocol-relative URLs (//)
    return url.startswith('/') and not url.startswith('//')


@bp.get("/login")
@flask_campus.unpack_request
def login(next: str | None = None) -> werkzeug.Response:
    """Initiate OAuth login flow.

    Args:
        next: Destination URL to redirect to after successful login

    Returns:
        Redirect to Campus OAuth authorization endpoint
    """
    # Validate and store destination to prevent open redirect attacks
    if next and not _is_safe_redirect(next):
        next = '/'
    flask.session['login_next'] = next or '/'

    # Use /finalize-login as the OAuth callback
    callback_url = flask.url_for('auth.finalize_login', _external=True)
    return campus.auth.authorize(target=callback_url)


@bp.get("/finalize-login")
@flask_campus.unpack_request
def finalize_login(
        code: str,
        state: str,
        scope: str
) -> werkzeug.Response:
    """Finalize Sign In to NYJC

    This calls campus.auth.finalize() which:
    1. Validates the auth session
    2. Exchanges authorization code for access token
    3. Stores credentials automatically via token endpoint
    4. Creates login session (30-day expiry)
    5. Redirects to the callback target (which is /login)

    We then redirect to the actual destination from login_next.
    """
    # Complete the OAuth flow (creates login session)
    campus.auth.finalize(state=state, code=code, scope=scope)

    # Redirect to the original destination
    next_url = flask.session.pop('login_next', '/')
    return flask.redirect(next_url)

@bp.get("/logout")
def logout():
    """Sign Out of NYJC"""
    campus.auth.logout()
    resp = flask.redirect(flask.url_for("index"))
    return resp

@bp.get("/token/debug")
def debug_token():
    """Debug endpoint to view current user's token.

    REMOVE THIS IN PRODUCTION - exposes sensitive token information.
    Use this for testing to verify:
    - Token was created during OAuth flow
    - Token is accessible with valid login session
    - Token persists across sessions
    """
    try:
        token = campus.auth.get_token()
        return flask.jsonify({
            "access_token": token.id[:20] + "...",  # Truncate for safety
            "expires_at": str(token.expires_at),
            "scopes": token.scopes,
            "has_refresh_token": token.refresh_token is not None,
        })
    except Exception as e:
        return flask.jsonify({"error": str(e)}), 401
