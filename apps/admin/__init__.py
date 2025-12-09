"""campus.apps.admin

Campus Admin Portal Application: authentication routes and views.
"""

import campus_python
import flask

from . import authentication


def create_app():
    """Application factory for Campus Admin Portal."""
    app = flask.Flask(__name__)

    campus = campus_python.Campus(timeout=60)
    app.before_request(campus.auth.push_context)

    app.register_blueprint(authentication.bp)

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
