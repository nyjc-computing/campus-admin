"""Tests for the Grants view (apps.admin.grants): the campus.auth
access-grant store as a page.

The harness is tests.support.make_admin_app — the real create_app()
with the Campus SDK client swapped for a recording mock — with the
signed-in user a platform operator by default, so the view tests
exercise the operator lane: the whole matrix in one list call, every
resource grantable, actions as the operator client. Who may administer
grants at all, and the designated-admin lane, are covered in
tests/test_auth_model.py (TestGrantAdministration).

File: tests/test_grants_view.py
"""

import re
import unittest
from types import SimpleNamespace
from unittest import mock

from apps.admin import grants
from tests.support import TEST_USER_ID, make_admin_app


def store_row(grantee_type, grantee_id, resource_type, level=None,
              resource_id="", bits=None, row_id="uid-grant-1") -> dict:
    """One access-grant store row, as the API returns it."""
    return {
        "id": row_id,
        "grantee_type": grantee_type,
        "grantee_id": grantee_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "bits": bits,
        "level": level,
        "created_at": "2026-11-01T00:00:00",
    }


class Form(dict):
    """A form mapping with the MultiDict.getlist the parsers use."""

    def getlist(self, name):
        value = self.get(name)
        if isinstance(value, list):
            return value
        return [value] if value else []


def csrf_token(client) -> str:
    """The token the page's action forms carry."""
    body = client.get("/auth/grants").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)


class GrantFormTests(unittest.TestCase):
    """The form parsers and the action reports: what a post may name,
    pinned to the shapes campus.auth's grant routes accept."""

    def test_management_row_names_its_level(self):
        key = grants.parse_grant_form(Form(
            grantee_type="user", grantee_id=" a@nyjc.edu.sg ",
            resource_type="users", level="write", resource_id="",
        ))
        self.assertEqual(key, grants.GrantKey(
            "user", "a@nyjc.edu.sg", "users", "", "write", None,
        ))

    def test_instance_scoped_rows_name_their_instance(self):
        migrations = grants.parse_grant_form(Form(
            grantee_type="user", grantee_id="a", resource_type="migrations",
            resource_id="audit", level="admin",
        ))
        self.assertEqual(migrations.resource_id, "audit")
        vault = grants.parse_grant_form(Form(
            grantee_type="user", grantee_id="a", resource_type="vault",
            resource_id="google.classroom", level="read",
        ))
        self.assertEqual(vault.resource_id, "google.classroom")

        for form, complaint in (
            (Form(grantee_type="user", grantee_id="a",
                  resource_type="migrations", level="read"),
             "must name its service"),
            (Form(grantee_type="user", grantee_id="a",
                  resource_type="migrations", resource_id="db",
                  level="read"), "one of: auth, api, audit"),
            (Form(grantee_type="user", grantee_id="a",
                  resource_type="vault", level="read"),
             "must name its vault label"),
        ):
            with self.subTest(form=form):
                self.assertIn(complaint, grants.parse_grant_form(form))

    def test_vocabulary_level_rows_take_no_instance(self):
        refused = grants.parse_grant_form(Form(
            grantee_type="user", grantee_id="a", resource_type="users",
            resource_id="somewhere", level="read",
        ))
        self.assertIn("not scoped to an instance", refused)

    def test_client_rows_are_vault_bitflags(self):
        key = grants.parse_grant_form(Form(
            grantee_type="client", grantee_id="uid-client-x",
            resource_type="vault", resource_id="deployment",
            bits=["R", "U"],
        ))
        self.assertEqual(key, grants.GrantKey(
            "client", "uid-client-x", "vault", "deployment", None, 5,
        ))
        for form, complaint in (
            # Clients are never designated admins.
            (Form(grantee_type="client", grantee_id="c",
                  resource_type="users", level="read"), "vault labels"),
            (Form(grantee_type="client", grantee_id="c",
                  resource_type="vault", bits=["R"]), "name its vault label"),
            (Form(grantee_type="client", grantee_id="c",
                  resource_type="vault", resource_id="x"),
             "at least one vault permission"),
        ):
            with self.subTest(form=form):
                self.assertIn(complaint, grants.parse_grant_form(form))

    def test_levels_the_grant_routes_refuse_are_not_offered(self):
        # campus/auth/routes/grants.py::_forbid_forbidden_shapes.
        for resource_type, level in (
            ("clients", "admin"), ("sessions", "admin"),
            ("credentials", "admin"), ("timetable", "mod"),
            ("migrations", "mod"), ("users", "owner"),
        ):
            with self.subTest(resource_type=resource_type, level=level):
                form = Form(
                    grantee_type="user", grantee_id="a",
                    resource_type=resource_type, level=level,
                )
                if resource_type in ("vault",):
                    form["resource_id"] = "x"
                self.assertIsInstance(
                    grants.parse_grant_form(form), str,
                )
        users_mod = grants.parse_grant_form(Form(
            grantee_type="user", grantee_id="a", resource_type="users",
            level="mod",
        ))
        self.assertEqual(users_mod.level, "mod")

    def test_grant_form_needs_a_grantee_and_a_resource(self):
        for form in (
            Form(grantee_type="group", grantee_id="a", resource_type="users"),
            Form(grantee_type="user", grantee_id="  ", resource_type="users"),
            Form(grantee_type="user", grantee_id="a", resource_type="nope"),
            Form(grantee_type="user", grantee_id="a", resource_type=""),
        ):
            with self.subTest(form=form):
                self.assertIsInstance(grants.parse_grant_form(form), str)

    def test_revoke_form_reads_the_rows_key(self):
        self.assertEqual(grants.parse_revoke_form(Form(
            grantee_type="user", grantee_id="a", resource_type="users",
            level="write",
        )), grants.GrantKey("user", "a", "users", "", "write", None))
        self.assertEqual(grants.parse_revoke_form(Form(
            grantee_type="client", grantee_id="c", resource_type="vault",
            resource_id="deployment", bits="5",
        )), grants.GrantKey("client", "c", "vault", "deployment", None, 5))
        for form in (
            # Exactly one permission operand.
            Form(grantee_type="user", grantee_id="a", resource_type="users"),
            Form(grantee_type="user", grantee_id="a", resource_type="users",
                 level="write", bits="5"),
            Form(grantee_type="user", grantee_id="a", resource_type="users",
                 level="owner"),
            Form(grantee_type="client", grantee_id="c", resource_type="vault",
                 bits="0"),
            Form(grantee_type="client", grantee_id="c", resource_type="vault",
                 bits="16"),
        ):
            with self.subTest(form=form):
                self.assertIsInstance(grants.parse_revoke_form(form), str)

    def test_grant_report(self):
        store = mock.MagicMock()
        key = grants.GrantKey("user", "a", "users", "", "write", None)
        store.grant.return_value = {"level": "write", "bits": None}
        self.assertEqual(
            grants.grant_report(store, key),
            "Granted users:write to user a.",
        )
        # Levels never lower: the store answers with the higher row.
        store.grant.return_value = {"level": "admin", "bits": None}
        self.assertEqual(
            grants.grant_report(store, key),
            "user a already holds users:admin — a level never lowers. "
            "Revoke it first, then grant write.",
        )
        store.grant.assert_called_with(
            grantee_type="user", grantee_id="a", resource_type="users",
            resource_id="", level="write", bits=None,
        )
        # Bits OR into the mask: the report is the resulting access.
        store.grant.return_value = {"bits": 7, "level": None}
        self.assertEqual(
            grants.grant_report(store, grants.GrantKey(
                "client", "c", "vault", "deployment", None, 5,
            )),
            "Vault access for client c on deployment is now R, C, U.",
        )

    def test_revoke_report(self):
        store = mock.MagicMock()
        level = grants.GrantKey("user", "a", "users", "", "write", None)
        store.revoke.return_value = None
        self.assertEqual(
            grants.revoke_report(store, level),
            "Revoked users:write from user a. The grant is gone.",
        )
        bits = grants.GrantKey("client", "c", "vault", "deployment", None, 4)
        store.revoke.return_value = {"bits": 1, "level": None}
        self.assertEqual(
            grants.revoke_report(store, bits),
            "Revoked vault deployment: U from client c. Remaining: R.",
        )
        store.revoke.return_value = None
        self.assertEqual(
            grants.revoke_report(store, bits),
            "Revoked vault deployment: U from client c. The grant is gone.",
        )


class GrantsViewTestCase(unittest.TestCase):
    """The page and its actions in the operator lane."""

    def setUp(self):
        self.app, self.client, self.campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        self.store = self.campus.auth.grants

    def post(self, **data):
        """POST an action with the page's CSRF token, following the
        redirect back to the list."""
        data["csrf_token"] = csrf_token(self.client)
        return self.client.post("/auth/grants", data=data,
                                follow_redirects=True)

    def test_lists_the_whole_matrix_in_one_call_sorted_by_grantee(self):
        self.store.list.return_value = {"grants": [
            store_row("user", "b@nyjc.edu.sg", "users", level="read"),
            store_row("client", "uid-client-9", "vault", bits=15,
                      resource_id="deployment"),
            "not a row",
            store_row("user", "a@nyjc.edu.sg", "migrations", level="admin",
                      resource_id="api"),
        ]}

        resp = self.client.get("/auth/grants")

        self.assertEqual(resp.status_code, 200)
        self.store.list.assert_called_once_with()
        body = resp.get_data(as_text=True)
        # People first, then integrations; alphabetical within each.
        order = [body.index(text) for text in (
            "a@nyjc.edu.sg", "b@nyjc.edu.sg", "uid-client-9",
        )]
        self.assertEqual(order, sorted(order))
        for text in ("migrations", "api", "grant-level-admin",
                     "grant-level-read", "deployment"):
            self.assertIn(text, body)
        # Table and mobile cards both carry every row.
        self.assertEqual(body.count('<span class="small">b@nyjc.edu.sg</span>'),
                         2)

    def test_only_the_filters_in_use_go_on_the_wire(self):
        # An empty value would be matched literally by the store, so
        # blank filters must not travel.
        self.store.list.return_value = {"grants": []}

        self.client.get(
            "/auth/grants?grantee_type=client&grantee_id=+uid-client-x"
            "&resource_type=&resource_id="
        )

        self.store.list.assert_called_once_with(
            grantee_type="client", grantee_id="uid-client-x",
        )

    def test_filters_return_into_the_form(self):
        self.store.list.return_value = {"grants": []}

        body = self.client.get(
            "/auth/grants?grantee_id=a@x&resource_type=vault"
        ).get_data(as_text=True)

        self.assertIn('name="grantee_id" value="a@x"', body)
        self.assertIn('<option value="vault" selected>', body)
        # The modal opens on the filtered grantee.
        self.assertIn('id="grantGranteeId" name="grantee_id"\n'
                      '                                   value="a@x"', body)

    def test_client_grantees_show_their_registered_name(self):
        self.store.list.return_value = {"grants": [
            store_row("client", "uid-client-9", "vault", bits=1,
                      resource_id="deployment"),
        ]}
        self.campus.auth.clients.list.return_value = [
            SimpleNamespace(id="uid-client-9", name="campus-classroom-dev"),
        ]

        body = self.client.get("/auth/grants").get_data(as_text=True)

        self.assertIn("campus-classroom-dev", body)

    def test_names_are_best_effort(self):
        self.store.list.return_value = {"grants": [
            store_row("client", "uid-client-9", "vault", bits=1,
                      resource_id="deployment"),
        ]}
        self.campus.auth.clients.list.side_effect = RuntimeError("down")

        resp = self.client.get("/auth/grants")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("uid-client-9", resp.get_data(as_text=True))

    def test_each_row_posts_its_key_once(self):
        # The table button, the card button and every per-bit chip of a
        # row submit one hidden form carrying the row's key, instead of
        # each repeating it.
        self.store.list.return_value = {"grants": [
            store_row("user", "a@nyjc.edu.sg", "users", level="write"),
            store_row("client", "uid-client-9", "vault", bits=15,
                      resource_id="deployment"),
            store_row("user", "b@nyjc.edu.sg", "users"),
        ]}

        body = self.client.get("/auth/grants").get_data(as_text=True)

        # Two revocable rows (the one without a permission has no
        # controls), whatever the number of controls on each.
        self.assertEqual(body.count('name="grantee_type" value='), 2)
        self.assertEqual(body.count('<form method="post"'), 3)
        controls = re.findall(r'<button type="submit" form="(revoke-\d+)"'
                              r' name="(\w+)" value="([^"]*)"', body)
        # People first, then integrations: the client's row is third.
        # Table rows in order (a row's chips, then its revoke button),
        # then the phone cards.
        self.assertEqual(controls, [
            ("revoke-1", "level", "write"),
            ("revoke-3", "bits", "1"), ("revoke-3", "bits", "2"),
            ("revoke-3", "bits", "4"), ("revoke-3", "bits", "8"),
            ("revoke-3", "bits", "15"),
            ("revoke-1", "level", "write"), ("revoke-3", "bits", "15"),
        ])
        keys = dict(re.findall(r'id="(revoke-\d+)" hidden>(.*?)</form>',
                               body, flags=re.S))
        self.assertIn('name="grantee_id" value="a@nyjc.edu.sg"', keys["revoke-1"])
        self.assertIn('name="resource_id" value="deployment"', keys["revoke-3"])

    def test_vault_bits_render_as_one_revoke_control_each(self):
        self.store.list.return_value = {"grants": [
            store_row("client", "uid-client-9", "vault", bits=5,
                      resource_id="deployment"),
        ]}

        body = self.client.get("/auth/grants").get_data(as_text=True)

        # Desktop: each held bit is its own control (R=1, U=4). The
        # unheld C and D are plain chips, on the table and on the card.
        self.assertEqual(body.count('name="bits" value="1"'), 1)
        self.assertEqual(body.count('name="bits" value="4"'), 1)
        self.assertEqual(body.count("grant-flag-off"), 4)
        # The whole row's revoke (mask 5) on the table and the card.
        self.assertEqual(body.count('name="bits" value="5"'), 2)
        self.assertIn(
            'data-confirm="Revoke R (read) on vault deployment from '
            'client uid-client-9?"', body,
        )
        self.assertIn(
            'data-confirm="Revoke vault deployment: R, U from client '
            'uid-client-9?"', body,
        )

    def test_management_rows_revoke_their_level(self):
        self.store.list.return_value = {"grants": [
            store_row("user", "a@nyjc.edu.sg", "users", level="write"),
        ]}

        body = self.client.get("/auth/grants").get_data(as_text=True)

        # Table and mobile card.
        self.assertEqual(body.count('name="level" value="write"'), 2)
        self.assertIn('data-confirm="Revoke users:write from user '
                      'a@nyjc.edu.sg?"', body)

    def test_row_without_a_permission_renders_a_dash(self):
        self.store.list.return_value = {"grants": [
            store_row("user", "a@nyjc.edu.sg", "users"),
        ]}

        resp = self.client.get("/auth/grants")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("data-confirm=", body)
        self.assertIn("a@nyjc.edu.sg", body)

    def test_grant_management_row_calls_the_store(self):
        self.store.grant.return_value = {"level": "write", "bits": None}

        resp = self.post(
            action="grant", grantee_type="user", grantee_id="a@nyjc.edu.sg",
            resource_type="users", resource_id="", level="write", bits="",
        )

        self.assertEqual(resp.status_code, 200)
        self.store.grant.assert_called_once_with(
            grantee_type="user", grantee_id="a@nyjc.edu.sg",
            resource_type="users", resource_id="", level="write", bits=None,
        )
        self.assertIn("Granted users:write to user a@nyjc.edu.sg.",
                      resp.get_data(as_text=True))

    def test_grant_client_vault_access(self):
        self.store.grant.return_value = {"bits": 5, "level": None}

        resp = self.post(
            action="grant", grantee_type="client", grantee_id="uid-client-9",
            resource_type="vault", resource_id="deployment",
            bits=["R", "U"], level="",
        )

        self.store.grant.assert_called_once_with(
            grantee_type="client", grantee_id="uid-client-9",
            resource_type="vault", resource_id="deployment", level=None,
            bits=5,
        )
        self.assertIn("is now R, U", resp.get_data(as_text=True))

    def test_grant_above_a_held_level_says_so(self):
        self.store.grant.return_value = {"level": "admin", "bits": None}

        resp = self.post(
            action="grant", grantee_type="user", grantee_id="a@nyjc.edu.sg",
            resource_type="users", resource_id="", level="read",
        )

        self.assertIn("already holds users:admin", resp.get_data(as_text=True))

    def test_revoke_management_row(self):
        self.store.revoke.return_value = None

        resp = self.post(
            action="revoke", grantee_type="user", grantee_id="a@nyjc.edu.sg",
            resource_type="migrations", resource_id="api", level="admin",
        )

        self.store.revoke.assert_called_once_with(
            grantee_type="user", grantee_id="a@nyjc.edu.sg",
            resource_type="migrations", resource_id="api", level="admin",
            bits=None,
        )
        self.assertIn("Revoked migrations api:admin from user a@nyjc.edu.sg. "
                      "The grant is gone.", resp.get_data(as_text=True))

    def test_revoke_one_vault_bit_reports_the_rest(self):
        self.store.revoke.return_value = {"bits": 1, "level": None}

        resp = self.post(
            action="revoke", grantee_type="client", grantee_id="uid-client-9",
            resource_type="vault", resource_id="deployment", bits="4",
        )

        self.assertIn("Remaining: R.", resp.get_data(as_text=True))

    def test_action_returns_to_the_filtered_list(self):
        self.store.grant.return_value = {"level": "read", "bits": None}

        resp = self.client.post("/auth/grants", data={
            "action": "grant", "csrf_token": csrf_token(self.client),
            "grantee_type": "user", "grantee_id": "a@nyjc.edu.sg",
            "resource_type": "users", "level": "read",
            "filters": ("grantee_type=user&resource_id="
                        "&grantee_id=a%40nyjc.edu.sg&other=x"),
        })

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.headers["Location"],
            "/auth/grants?grantee_type=user&grantee_id=a%40nyjc.edu.sg",
        )

    def test_malformed_posts_are_refused_before_any_call(self):
        for data, complaint in (
            ({"action": "grant", "grantee_type": "client",
              "grantee_id": "c", "resource_type": "users", "level": "read"},
             "access to vault labels"),
            ({"action": "grant", "grantee_type": "user", "grantee_id": "a",
              "resource_type": "clients", "level": "admin"},
             "granted at one of: read, write"),
            ({"action": "grant", "grantee_type": "user", "grantee_id": "a",
              "resource_type": "vault", "level": "read"},
             "must name its vault label"),
            ({"action": "revoke", "grantee_type": "user", "grantee_id": "a",
              "resource_type": "users"},
             "names no permission"),
        ):
            with self.subTest(data=data):
                # Each attempt consumes a page render's token.
                resp = self.client.post("/auth/grants", data={
                    **data, "csrf_token": csrf_token(self.client),
                }, follow_redirects=True)
                self.assertEqual(resp.status_code, 200)
                body = resp.get_data(as_text=True)
                self.assertIn(complaint, body)
                self.assertIn('class="alert alert-error', body)
        self.store.grant.assert_not_called()
        self.store.revoke.assert_not_called()

    def test_actions_need_the_page_s_csrf_token(self):
        action = {"action": "revoke", "grantee_type": "user",
                  "grantee_id": "a@nyjc.edu.sg", "resource_type": "users",
                  "level": "write"}
        missing = self.client.post("/auth/grants", data=action)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("form session expired", missing.get_data(as_text=True))

        # A token is single-use: the same one does not carry two posts.
        token = csrf_token(self.client)
        self.client.post("/auth/grants", data={**action, "csrf_token": token})
        again = self.client.post("/auth/grants", data={
            **action, "csrf_token": token,
        })
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.store.revoke.call_count, 1)

    def test_unknown_actions_are_not_routes(self):
        resp = self.client.post("/auth/grants", data={
            "action": "delete", "csrf_token": csrf_token(self.client),
        })

        self.assertEqual(resp.status_code, 404)

    def test_the_store_s_refusal_is_relayed(self):
        self.store.grant.side_effect = RuntimeError(
            "User principals may not administer their own access grants"
        )

        resp = self.post(
            action="grant", grantee_type="user", grantee_id="a",
            resource_type="users", level="read",
        )

        body = resp.get_data(as_text=True)
        self.assertIn("Failed to grant users:read", body)
        self.assertIn("their own access grants", body)

    def test_unreadable_store_renders_the_error(self):
        self.store.list.side_effect = RuntimeError("grants down")

        resp = self.client.get("/auth/grants")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("The grant store could not be read: grants down",
                      resp.get_data(as_text=True))

    def test_empty_store(self):
        self.store.list.return_value = {"grants": []}

        body = self.client.get("/auth/grants").get_data(as_text=True)

        self.assertIn("No grant rows yet", body)
        self.store.list.return_value = {"grants": []}
        filtered = self.client.get("/auth/grants?grantee_id=nobody").get_data(
            as_text=True)
        self.assertIn("No grant rows match these filters", filtered)

    def test_grant_form_offers_every_resource_and_its_levels(self):
        self.store.list.return_value = {"grants": [
            store_row("user", "a@nyjc.edu.sg", "users", level="write"),
        ]}
        body = self.client.get("/auth/grants").get_data(as_text=True)

        self.assertIn('id="newGrantForm"', body)
        self.assertIn('data-bs-target="#newGrantModal"', body)
        for key in grants.authz.GRANT_RESOURCES:
            self.assertIn(
                f'<option value="{key}"', body, f"{key} not offered",
            )
        # The per-resource shapes drive the dependent fields.
        self.assertIn('"levels": ["read", "write", "admin"]', body)
        self.assertIn('"instance": "Service"', body)
        self.assertIn('"choices": ["auth", "api", "audit"]', body)
        self.assertIn('name="bits" value="R"', body)
        # Confirm dialogs never carry page text inside inline JS.
        self.assertNotIn("onsubmit=", body)
        self.assertIn("data-confirm=", body)

    def test_page_is_an_app_shell_view_with_the_grants_entry_active(self):
        body = self.client.get("/auth/grants").get_data(as_text=True)

        self.assertIn('<div class="dashboard-layout dashboard-layout--app">',
                      body)
        self.assertIn('class="dashboard-content"', body)
        self.assertNotIn("2025 Campus Admin Portal", body)
        self.assertEqual(
            re.findall(r'class="sidebar-link active"[^>]*>\s*'
                       r'<span class="sidebar-icon">▶</span> (\w+)', body),
            ["Grants"],
        )
        self.assertNotIn('class="sidebar-link disabled"', body)

    def test_anonymous_is_sent_to_sign_in(self):
        _, client, _, cleanup = make_admin_app(user_id=None)
        self.addCleanup(cleanup)

        resp = client.get("/auth/grants")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login?next=/auth/grants", resp.headers["Location"])

    def test_holding_neither_authority_is_refused(self):
        _, client, campus, cleanup = make_admin_app(members=())
        self.addCleanup(cleanup)

        resp = client.get("/auth/grants")

        self.assertEqual(resp.status_code, 403)
        self.assertIn("access grant", resp.get_data(as_text=True))
        # The section gate's own probe asked whether the user holds any
        # grant; the page itself listed nothing.
        campus.auth.grants.list.assert_called_once_with(
            grantee_type="user", grantee_id=TEST_USER_ID,
        )


if __name__ == "__main__":
    unittest.main()
