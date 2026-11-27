"""apps.admin.resources

Resources section of the admin portal: management views for the
campus.api service's resources (the third portal section alongside
Admin and Audit, at the /resources namespace).

Section layout mirrors the other two: the shared top navbar
(apps/admin/templates/_top_nav.html) carries the section pill, pages
extend this package's app-shell base (sidebar + resource container,
the admin section's resource-view grammar).

Authority is campus.api's alone — circles. Every wire call rides the
signed-in user's own campus session (campus.api denies client
principals on gated routes, #854, so the section never acts as the
portal's own client), and each view gates on the user's effective
access for its family, read from the service's gate-equivalent probe
(apps.admin.authz.api_access). Users manage API resources according to
their scoped permissions: a staff baseline reads every family, and the
write affordances follow each family's access value.

The sidebar lists the campus.api resources the SDK (campus-python
ApiRoot) exposes: circles, venues, timetable, assignments,
submissions, subjcombi, files and hostedapps.
"""

__all__ = ["init_app"]

import flask


def create_blueprint(campus, operator_circle: str) -> flask.Blueprint:
    """Create the /resources blueprint.

    Args:
        campus: the portal's campus-python client; route closures make
            their campus.api calls through it, inside the signed-in
            user's session.
        operator_circle: the portal's platform-operator circle —
            platform operators short-circuit to full campus.api access,
            like the service's own `campus` wildcard.
    """
    bp = flask.Blueprint(
        "resources",
        __name__,
        url_prefix="/api",
        template_folder="templates",
    )

    from . import views

    views.register(bp, campus, operator_circle)
    return bp


def init_app(app: flask.Flask, *, campus, operator_circle: str) -> None:
    """Register the resources blueprint on the portal app."""
    app.register_blueprint(create_blueprint(campus, operator_circle))
