"""Unit tests for the audit UI's key-management page
(apps.admin.audit.keys).

The page proxies /audit/v1/apikeys with the deployment's UI key:
mint, list, re-scope, rotate, revoke. These tests verify the CSRF
gate, the wire calls, the one-time plaintext relay, and the error
handling (unknown key, audit-service refusal).

File: tests/test_audit_keys.py
"""

import unittest

from tests.support import StubJsonClient, StubResponse, make_app


class KeysPageTestBase(unittest.TestCase):
    """Shared harness: one app per test, patches restored after."""

    def setUp(self):
        self.json_client = StubJsonClient()
        self.app, self.client, cleanup = make_app(json_client=self.json_client)
        self.addCleanup(cleanup)

    def _csrf(self) -> str:
        """Fetch a fresh CSRF token from the page."""
        page = self.client.get("/audit/keys").get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        start = page.index(marker) + len(marker)
        return page[start:page.index('"', start)]

    def _wire_call(self, method: str, path_prefix: str):
        """Find a recorded wire call by method and path prefix.

        Page renders also hit the API (the key list), so index-by-zero
        is wrong for mutation tests.
        """
        return next(
            call for call in self.json_client.calls
            if call[0] == method and call[1].startswith(path_prefix)
        )

    def _post(self, path: str, **form):
        form["csrf_token"] = self._csrf()
        return self.client.post(path, data=form)


class TestKeysPage(KeysPageTestBase):
    """The page renders the key list from the audit service."""

    def test_page_lists_keys(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/apikeys/"
        )] = StubResponse({"api_keys": [{
            "id": "uid-apikey-1",
            "name": "Viewer key",
            "owner_id": "alice@nyjc.edu.sg",
            "scopes": ["traces:read"],
            "expires_at": None,
            "revoked_at": None,
            "last_used": None,
            "rate_limit": None,
        }], "count": 1})
        page = self.client.get("/audit/keys").get_data(as_text=True)
        self.assertIn("Viewer key", page)
        self.assertIn("alice@nyjc.edu.sg", page)
        self.assertIn("traces:read", page)
        method, path, params, _ = self.json_client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/audit/v1/apikeys/")
        self.assertEqual(params, {"active_only": "false", "limit": 200})

    def test_list_failure_shows_error(self):
        self.json_client.handlers[(
            "GET", "/audit/v1/apikeys/"
        )] = StubResponse(
            {"error": {"message": "key lacks apikeys:read"}}, status_code=403,
        )
        page = self.client.get("/audit/keys").get_data(as_text=True)
        self.assertIn("key lacks apikeys:read", page)

    def test_page_is_no_store(self):
        response = self.client.get("/audit/keys")
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class TestCsrf(KeysPageTestBase):
    """Form posts need a single-use CSRF token."""

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post("/audit/keys/new", data={
            "name": "k", "owner_id": "alice@nyjc.edu.sg", "scopes": "traces:read",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("form session expired", response.get_data(as_text=True))

    def test_post_with_stale_csrf_is_rejected(self):
        token = self._csrf()
        # Consume the token with a rejected (missing-field) post…
        self.client.post("/audit/keys/new", data={
            "csrf_token": token, "name": "", "owner_id": "",
        })
        # …then replay it: the token was popped, so it no longer works.
        response = self.client.post("/audit/keys/new", data={
            "csrf_token": token, "name": "k",
            "owner_id": "alice@nyjc.edu.sg", "scopes": "traces:read",
        })
        self.assertEqual(response.status_code, 400)


class TestMint(KeysPageTestBase):
    """POST /audit/keys/new proxies the mint call."""

    def test_mint_relayed_and_plaintext_shown_once(self):
        self.json_client.handlers[(
            "POST", "/audit/v1/apikeys/"
        )] = StubResponse({
            "id": "uid-apikey-new",
            "api_key": "audit_v1_PLAINTEXT",
            "name": "Viewer key",
            "scopes": ["traces:read"],
        }, status_code=201)
        response = self._post(
            "/audit/keys/new",
            name="Viewer key",
            owner_id="alice@nyjc.edu.sg",
            scopes="traces:read",
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("audit_v1_PLAINTEXT", page)
        _, _, _, body = self._wire_call("POST", "/audit/v1/apikeys/")
        self.assertEqual(body, {
            "name": "Viewer key",
            "owner_id": "alice@nyjc.edu.sg",
            "scopes": ["traces:read"],
        })
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_mint_requires_name_owner_and_scopes(self):
        response = self._post("/audit/keys/new", name="", owner_id="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Give the key a name", response.get_data(as_text=True))

        response = self._post(
            "/audit/keys/new", name="k", owner_id="alice@nyjc.edu.sg"
        )
        self.assertIn("role, or at least one scope", response.get_data(as_text=True))

    def test_mint_rejects_role_and_scopes_together(self):
        response = self._post(
            "/audit/keys/new", name="k", owner_id="alice@nyjc.edu.sg",
            role="traces-viewer", scopes="traces:read",
        )
        self.assertIn("not both", response.get_data(as_text=True))

    def test_mint_rejects_unknown_role(self):
        response = self._post(
            "/audit/keys/new", name="k", owner_id="alice@nyjc.edu.sg",
            role="not-a-role",
        )
        self.assertEqual(response.status_code, 400)

    def test_mint_rejects_bad_expiry(self):
        response = self._post(
            "/audit/keys/new", name="k", owner_id="alice@nyjc.edu.sg",
            scopes="traces:read", expires_at="not-a-date",
        )
        self.assertIn("ISO 8601", response.get_data(as_text=True))

    def test_mint_validation_error_from_api_is_shown(self):
        self.json_client.handlers[(
            "POST", "/audit/v1/apikeys/"
        )] = StubResponse(
            {"error": {"message": "One or more fields are invalid"}},
            status_code=422,
        )
        response = self._post(
            "/audit/keys/new", name="k", owner_id="alice@nyjc.edu.sg",
            scopes="traces:read",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "One or more fields are invalid", response.get_data(as_text=True)
        )


class TestMutations(KeysPageTestBase):
    """Re-scope, rotate, and revoke proxy their API calls."""

    def test_scopes_updated(self):
        self.json_client.handlers[(
            "PATCH", "/audit/v1/apikeys/uid-apikey-1/"
        )] = StubResponse({"id": "uid-apikey-1", "scopes": ["traces:read"]})
        response = self._post(
            "/audit/keys/uid-apikey-1/scopes", scopes="traces:read"
        )
        self.assertIn(
            "Scopes updated for key uid-apikey-1",
            response.get_data(as_text=True),
        )
        _, _, _, body = self._wire_call(
            "PATCH", "/audit/v1/apikeys/uid-apikey-1/"
        )
        self.assertEqual(body, {"scopes": ["traces:read"]})

    def test_scopes_empty_rejected(self):
        response = self._post("/audit/keys/uid-apikey-1/scopes", scopes="")
        self.assertIn(
            "at least one scope", response.get_data(as_text=True)
        )

    def test_rotate_shows_new_plaintext(self):
        self.json_client.handlers[(
            "POST", "/audit/v1/apikeys/uid-apikey-1/regenerate"
        )] = StubResponse({"key": "audit_v1_ROTATED"})
        response = self._post("/audit/keys/uid-apikey-1/rotate")
        self.assertIn("audit_v1_ROTATED", response.get_data(as_text=True))
        self.assertIn("rotated", response.get_data(as_text=True))

    def test_revoke(self):
        self.json_client.handlers[(
            "DELETE", "/audit/v1/apikeys/uid-apikey-1/"
        )] = StubResponse({}, status_code=204)
        response = self._post("/audit/keys/uid-apikey-1/revoke")
        self.assertIn("revoked", response.get_data(as_text=True))

    def test_unknown_key_404s_to_form_error(self):
        self.json_client.handlers[(
            "DELETE", "/audit/v1/apikeys/uid-apikey-ghost/"
        )] = StubResponse(
            {"error": {"code": "NOT_FOUND", "message": "API key not found"}},
            status_code=404,
        )
        response = self._post("/audit/keys/uid-apikey-ghost/revoke")
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "No key with id uid-apikey-ghost", response.get_data(as_text=True)
        )

    def test_forbidden_mutation_explains_scope_gap(self):
        self.json_client.handlers[(
            "DELETE", "/audit/v1/apikeys/uid-apikey-1/"
        )] = StubResponse(
            {"error": {"code": "FORBIDDEN", "message": "forbidden"}},
            status_code=403,
        )
        response = self._post("/audit/keys/uid-apikey-1/revoke")
        self.assertIn("apikeys:write", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
