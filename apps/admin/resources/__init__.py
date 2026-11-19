"""apps.admin.resources

Resources section of the admin portal: management views for the
campus.api service's resources (the third portal section alongside
Admin and Audit, at the /resources namespace).

Section layout mirrors the other two: the shared top navbar
(apps/admin/templates/_top_nav.html) carries the section pill, pages
extend this package's app-shell base (sidebar + resource container,
the admin section's resource-view grammar), and access is gated the
same way as the admin section — portal sign-in plus membership in the
admin circle (ADMIN_CIRCLE). campus.api's own authz stays the wall
behind the UI: every wire call rides the portal's operator client.

The sidebar lists the campus.api resources the SDK (campus-python
ApiRoot) exposes: circles, venues, timetable, assignments,
submissions, subjcombi, files and hostedapps.
"""

__all__ = ["init_app"]

import flask
import werkzeug


def create_blueprint(campus, page_gate) -> flask.Blueprint:
    """Create the /resources blueprint.

    Args:
        campus: the portal's campus-python client; route closures make
            their campus.api calls through it.
        page_gate: the gate callable applied to every blueprint route
            via before_request — proceed (None) / redirect / 403 / 503.
            The admin section's circle gate fits exactly:
            admin_required(lambda: None).
    """
    bp = flask.Blueprint(
        "resources",
        __name__,
        url_prefix="/resources",
        template_folder="templates",
    )

    @bp.before_request
    def _gate():
        return page_gate()

    from . import views

    views.register(bp, campus)
    return bp


def init_app(app: flask.Flask, *, campus, page_gate) -> werkzeug.Response | None:
    """Register the resources blueprint on the portal app."""
    app.register_blueprint(create_blueprint(campus, page_gate))
    return None
