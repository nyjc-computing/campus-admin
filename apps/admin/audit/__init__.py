"""apps.admin.audit

The audit web UI, hosted by campus-admin (migrated from
campus.audit.web, which read the audit service's resources layer
in-process).

campus.audit authorizes by API-key scope only and never authenticates
a person (docs/auth-model.md): the portal signs the session in through
its own campus.auth OAuth login and holds one shared UI key, so it
decides locally who may drive that key.

Layout:

- gate: who may drive the UI key, over the portal's OAuth login
  session — the deployment's AUDIT_ADMINS list for trace viewing, its
  audit superadmins (AUDIT_KEY_ADMINS, falling back to AUDIT_ADMINS)
  for key management
- client: the audit service HTTP client (server-side AUDIT_UI_API_KEY)
- ui: HTML pages (landing, trace list, trace detail, journey redirect)
- data: /audit/api/* JSON endpoints proxying /audit/v1 for the JS
- keys: the key-management page, proxying /audit/v1/apikeys
- clientnames: client-id display-name enrichment (campus.auth lookup)
"""

__all__ = ["init_app"]

import flask


def init_app(app: flask.Flask) -> None:
    """Register the audit UI blueprints on the portal app."""
    from . import data, gate, keys, ui

    # Pages, gated by the trace-view capability; the landing page and
    # this blueprint's static assets stay public.
    ui_blueprint = ui.create_blueprint()
    ui_blueprint.before_request(gate.require_login_page)
    app.register_blueprint(ui_blueprint)

    # Data endpoints (proxied to the audit service's versioned API),
    # gated by the same session; fetch() callers get 401 JSON instead
    # of a redirect.
    data_blueprint = data.create_blueprint()
    data_blueprint.before_request(gate.require_login_api)
    app.register_blueprint(data_blueprint)

    # The key-management page (#927) has its own gate: managing keys
    # is a separate capability from reading traces (audit superadmins
    # only, on every environment), so it must not inherit the
    # trace-view gate.
    app.register_blueprint(keys.create_blueprint())
