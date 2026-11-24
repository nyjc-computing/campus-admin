"""apps.admin.csrf

Single-use CSRF tokens for the portal's destructive form posts.

A page render issues a token (`issue`) and the Flask session remembers
it under the caller's key; the form posts it back and the view spends
it (`check`). The token is spent by the first post that carries it:
every action redirects to a fresh render with a fresh token, so a page
whose controls share one token (a list with an action per row) still
works, and a captured form cannot be replayed. A post without the
token is refused — a forged request on another site cannot read one
off the page.
"""

__all__ = ["check", "issue"]

import hmac
import secrets

import flask


def issue(key: str) -> str:
    """Issue a token for the forms on this page render; remember it."""
    token = secrets.token_urlsafe(32)
    flask.session[key] = token
    return token


def check(key: str) -> bool:
    """Whether the submitted token is the session's remembered one.

    Single-use: the remembered token is dropped whichever way the
    comparison goes. It runs on bytes — compare_digest() raises
    TypeError on non-ASCII strings, and the submitted token is raw form
    data.
    """
    issued = flask.session.pop(key, None)
    submitted = flask.request.form.get("csrf_token", "")
    if not isinstance(issued, str):
        return False
    return hmac.compare_digest(
        issued.encode("utf-8"), str(submitted).encode("utf-8")
    )
