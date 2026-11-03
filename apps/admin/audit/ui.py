"""apps.admin.audit.ui

UI routes for the audit web UI hosted by campus-admin — HTML pages for
browsing traces (migrated from campus.audit.web.ui; the pages, paths
and JavaScript are unchanged, only the host and login moved).

Page map:

- GET /audit/ - landing page (public: no login required)
- GET /audit/traces - trace list; ?journey_id=<id> switches it to the
  group-by-journey view (#803)
- GET /audit/traces/<trace_id> - trace detail
- GET /audit/journeys/<journey_id> - redirect to the journey view
  (keeps earlier journey URLs working)

Sign-in is the portal's campus.auth OAuth login (flask_campus
OAuthLoginManager): gated pages redirect anonymous browsers to
/login?next=<path>, and capability is the AUDIT_ADMINS superadmin list
(apps.admin.audit.gate). The old audit service's own OAuth flow
(/audit/login, /audit/callback, /audit/logout) and the development
API-key sign-in (#915) do not exist here.
"""

__all__ = ["create_blueprint"]

import flask
import werkzeug

from . import gate


def create_blueprint() -> flask.Blueprint:
    """Create a Flask blueprint for UI routes.

    Returns:
        A Flask blueprint with UI route handlers
    """
    bp = flask.Blueprint(
        'audit_ui',
        __name__,
        url_prefix='/audit',
        template_folder='templates',
        static_folder='static'
    )

    # The shared header (audit/base.html) offers the Keys nav link only
    # to sessions that may manage keys — the same capability the keys
    # page gates on (apps.admin.audit.keys). Registered app-wide so
    # every audit template sees it; anonymous sessions hold nothing, so
    # public pages simply don't show the link.
    @bp.app_context_processor
    def _nav_capabilities() -> dict[str, bool]:
        return {"can_manage_keys": gate.can_manage_keys()}

    @bp.route('/')
    def index() -> str:
        """Render the landing page.

        The landing page is the public entry point for the Audit Web
        UI: it introduces the service and links to the gated trace
        list and the portal's login flow. No trace data is shown here.
        """
        return flask.render_template('audit/index.html')

    @bp.route('/traces')
    def traces() -> str:
        """Render the trace list page (login required via the gate).

        Shows the filter bar and trace table; data is loaded client-side
        from the UI data endpoints.
        """
        return flask.render_template('audit/traces.html')

    @bp.route('/traces/<trace_id>')
    def trace(trace_id: str) -> str:
        """Render the trace detail page.

        Shows trace metadata, a span waterfall, and a span details
        drawer; data is loaded client-side from the UI data endpoints.
        """
        return flask.render_template(
            'audit/trace.html', trace_id=trace_id
        )

    @bp.route('/journeys/<journey_id>')
    def journey(journey_id: str) -> werkzeug.Response:
        """Redirect journey-page links to the merged traces view (#803).

        The journey view is /audit/traces?journey_id=<id>: the same
        trace list switching to group-by-journey. This route keeps any
        previously shared journey URLs working.
        """
        return flask.redirect(
            flask.url_for('audit_ui.traces', journey_id=journey_id)
        )

    return bp
