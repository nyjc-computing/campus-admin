"""campus.apps.admin

Campus Admin Portal Application: authentication routes and views.
"""

import os

import campus_python
import flask

from . import authentication


def create_app():
    """Application factory for Campus Admin Portal."""
    app = flask.Flask(__name__)
    
    # Configure Flask secret key from environment
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
    if not app.config['SECRET_KEY']:
        raise ValueError("SECRET_KEY environment variable is required")

    campus = campus_python.Campus(timeout=60)
    app.before_request(campus.auth.push_context)

    app.register_blueprint(authentication.bp)

    @app.before_request
    def debug_session():
        if 'campus_login_id' in flask.session:
            session_id = flask.session['campus_login_id']
            print(f"DEBUG: campus_login_id = {session_id!r}")
            print(f"DEBUG: has trailing slash? {session_id.endswith('/')}")

    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        return flask.render_template("sign_in.html")

    @app.get("/dashboard")
    def dashboard():
        return flask.render_template("dashboard.html")

    return app
