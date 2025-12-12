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
def login(
        next: str | None = None,
        code: str | None = None,
        state: str | None = None,
        scope: str | None = None
) -> werkzeug.Response:
    """Handle both login initiation and OAuth callback"""

    # If code/state/scope are present, this is the OAuth callback
    if code and state and scope:
        return finalize_login(state=state, code=code, scope=scope)

    # Otherwise, initiate login
    # Store the final destination in session
    flask.session['login_next'] = next or '/'
    # OAuth callback should come back to GET /login with code/state/scope
    callback_url = flask.url_for('auth.login', _external=True)
    return campus.auth.authorize(target=callback_url)

def finalize_login(
        state: str,
        code: str,
        scope: str
) -> werkzeug.Response:
    """Finalize Sign In to NYJC"""
    # Temporary debug page to verify OAuth flow
    debug_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>OAuth Callback Debug</title>
        <style>
            body {{ font-family: monospace; padding: 20px; background: #f5f5f5; }}
            .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            h1 {{ color: #2c3e50; }}
            .param {{ margin: 15px 0; padding: 10px; background: #ecf0f1; border-radius: 4px; }}
            .label {{ font-weight: bold; color: #34495e; }}
            .value {{ color: #27ae60; word-break: break-all; }}
            .success {{ color: #27ae60; font-weight: bold; }}
            button {{ background: #3498db; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background: #2980b9; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✅ OAuth Callback Received!</h1>
            <p class="success">The OAuth flow successfully completed and returned to the app callback.</p>

            <div class="param">
                <div class="label">State:</div>
                <div class="value">{state}</div>
            </div>

            <div class="param">
                <div class="label">Authorization Code:</div>
                <div class="value">{code}</div>
            </div>

            <div class="param">
                <div class="label">Scope:</div>
                <div class="value">{scope}</div>
            </div>

            <form method="post" action="/login">
                <input type="hidden" name="state" value="{state}">
                <input type="hidden" name="code" value="{code}">
                <input type="hidden" name="scope" value="{scope}">
                <button type="submit">Continue to Complete Login</button>
            </form>
        </div>
    </body>
    </html>
    """
    return flask.Response(debug_html, mimetype='text/html')

    # Original finalize code (commented out for now):
    # resp = campus.auth.finalize(state=state, code=code, scope=scope)
    # return resp

@bp.get("/logout")
def logout():
    """Sign Out of NYJC"""
    campus.auth.logout()
    resp = flask.redirect(flask.url_for("index"))
    return resp
