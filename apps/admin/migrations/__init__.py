"""apps.admin.migrations

The Migrations admin view (#75): per-service plan/history and the
apply/abort/rollback actions over the campus suite's migration
management routes (migrations/routes.py, campus#1169) — the ratified
production migration loop (campus#1172), "somebody at the wheel".

Layout:

- client: the per-service wire calls. The surface is gated by the
  migrations grant vocabulary (campus#1168) under the #865/#883
  two-leg model — a designated user's grant row AND the matching
  scope on the caller's bearer — so every call rides the signed-in
  admin's campus session (campus.with_user_session()), and the same
  session token's scopes are what the view renders affordances from.
- views: the /admin/migrations page, the /admin/api/migrations/<svc>
  JSON endpoint the page's polling uses, and the apply/abort/rollback
  form posts — all gated by the admin section's circle (the same gate
  as the other /admin resource views; the API enforces the migrations
  grants regardless, so the client-side gating is UX, not security).

The api/audit services' capability legs fail CLOSED until campus.auth
surfaces token scopes to services (campus#1183): their sections render
that caveat instead of treating the 403 as a view bug.
"""

__all__ = ["init_app"]

import flask


def init_app(app: flask.Flask, *, campus, page_gate, admin_circle) -> None:
    """Register the Migrations view blueprints on the portal app.

    Args:
        app: The portal Flask app.
        campus: The portal's campus_python.Campus client; the view's
            wire calls ride its user session.
        page_gate: The admin-section gate as a before-request callable
            (create_app derives it from the portal's own
            admin_required decorator, so this view is gated by the
            same circle as the other /admin resource views).
        admin_circle: That gate's circle name — the JSON polling
            endpoint answers with it in fetch-friendly form.
    """
    from . import views

    app.register_blueprint(views.create_page_blueprint(
        campus=campus, page_gate=page_gate,
    ))
    app.register_blueprint(views.create_data_blueprint(
        campus=campus, admin_circle=admin_circle,
    ))
