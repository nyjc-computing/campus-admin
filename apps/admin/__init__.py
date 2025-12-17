"""campus.apps.admin

Campus Admin Portal Application: authentication routes and views.
"""

import os

import campus_python
import flask
from campus import flask_campus


def create_app():
    """Application factory for Campus Admin Portal."""
    app = flask.Flask(__name__, static_folder='static', static_url_path='/static')
    campus = campus_python.Campus(timeout=60)
    
    # Configure Flask secret key from environment
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
    if not app.config['SECRET_KEY']:
        raise ValueError("SECRET_KEY environment variable is required")

    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus,
        default_endpoint="index"
    )
    login_manager.init_app(app)


    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        return flask.render_template("sign_in.html")

    @app.get("/dashboard")
    def dashboard():
        return flask.render_template("dashboard.html")

    @app.get("/clients")
    def clients():
        return flask.render_template("clients.html")

    return app
