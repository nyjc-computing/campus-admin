"""Tests for the /resources section (campus.api resource management).

Builds the real create_app() with the Campus SDK client swapped for a
recording MagicMock (the test_admin_resources pattern, via
tests.support.make_admin_app), so the route closures capture the
double. The section rides the same admin-circle gate as the admin
section's views: anonymous sessions redirect to sign-in, signed-in
non-members get the app-level 403 page naming the circle.
"""

import io
import json
import unittest
from types import SimpleNamespace

from tests.support import TEST_USER_ID, make_admin_app


def venue(id="v1", name="Hall", notes="book via dean"):
    return SimpleNamespace(
        id=id,
        metadata={"name": name, "capacity": 100, "location": "Block D"},
        notes=notes,
        updated_at=None,
        created_at=None,
    )


class ResourcesGateTest(unittest.TestCase):
    """The section gate: sign-in + admin-circle membership."""

    def test_anonymous_redirects_to_sign_in(self):
        app, client, _campus, cleanup = make_admin_app(user_id=None)
        self.addCleanup(cleanup)
        resp = client.get("/resources/circles")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertIn("/resources/circles", resp.headers["Location"])

    def test_non_member_gets_403_naming_the_circle(self):
        app, client, _campus, cleanup = make_admin_app(
            members=("someone-else@nyjc.edu.sg",)
        )
        self.addCleanup(cleanup)
        resp = client.get("/resources/circles")
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"resources", resp.data)
        self.assertIn(b"campus-admin", resp.data)

    def test_section_root_redirects_to_circles(self):
        app, client, _campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        resp = client.get("/resources/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/resources/circles")

    def test_sidebar_lists_every_campus_api_resource(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.circles.list.return_value = []
        resp = client.get("/resources/circles")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'data-section="resources"', resp.data)
        for res in (
            "circles", "venues", "timetable", "assignments",
            "submissions", "subjcombi", "files", "hostedapps",
        ):
            self.assertIn(
                f"/resources/{res}".encode(), resp.data, res,
            )
        # The shared top navbar carries the new section pill, active.
        self.assertIn(b">Resources</a>", resp.data)


class ResourcesLayoutTest(unittest.TestCase):
    """Structural layout assertions (the PR #54 regression class):
    every resources page renders inside the app-shell wrapper with the
    sidebar and exactly one active entry."""

    ACTIVE_BY_PATH = {
        "/resources/circles": "circles",
        "/resources/venues": "venues",
        "/resources/timetable": "timetable",
        "/resources/assignments": "assignments",
        "/resources/submissions": "submissions",
        "/resources/subjcombi": "subjcombi",
        "/resources/files": "files",
        "/resources/hostedapps": "hostedapps",
    }

    def test_every_page_renders_app_shell_with_sidebar(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.circles.list.return_value = []
        campus.api.venues.list.return_value = []
        campus.api.timetable.view.return_value = {}
        campus.api.timetable.list_metadata.return_value = []
        campus.api.assignments.list.return_value = []
        campus.api.submissions.list.return_value = []
        campus.api.subjcombi.list.return_value = []
        campus.api.subjcombi.pending.return_value = []
        campus.api.files.list.return_value = []
        campus.api.hostedapps.list.return_value = []

        for path in self.ACTIVE_BY_PATH:
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            body = resp.get_data(as_text=True)
            self.assertIn(
                '<div class="dashboard-layout dashboard-layout--app">',
                body, path,
            )
            self.assertIn('class="dashboard-content"', body, path)
            self.assertIn('class="resource-container"', body, path)
            for res in self.ACTIVE_BY_PATH.values():
                self.assertIn(f"/resources/{res}", body, f"{res} on {path}")
            # Exactly one active sidebar entry: this page's resource.
            self.assertIn('class="sidebar-link active"', body, path)
            self.assertIn(
                '<span class="sidebar-icon">▶</span> ',
                body, path,
            )
            self.assertNotIn('class="sidebar-link disabled"', body, path)
            self.assertIn('data-section="resources"', body, path)
            self.assertIn(">Campus Resources</a>", body, path)
            self.assertIn(
                'class="section-link active" href="/resources/"', body, path
            )

    def test_circle_detail_renders_same_shell(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        circle = SimpleNamespace(
            id="c1", name="campus-admin", tag="bootstrap",
            description="", created_at=None,
        )
        campus.api.circles["c1"].get.return_value = circle
        campus.api.circles["c1"].members.list.return_value = {}
        resp = client.get("/resources/circles/c1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn(
            '<div class="dashboard-layout dashboard-layout--app">', body
        )
        self.assertIn('class="sidebar-link active"', body)


class CirclesViewTest(unittest.TestCase):
    def test_list_renders_circles(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.circles.list.return_value = [
            SimpleNamespace(
                id="c1", name="campus-admin", tag="bootstrap",
                description="portal gate", created_at=None,
            ),
        ]
        resp = client.get("/resources/circles")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"campus-admin", resp.data)
        self.assertIn(b"bootstrap", resp.data)
        campus.api.circles.list.assert_called_once_with()

    def test_create_circle(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.circles.new.return_value = SimpleNamespace(name="role.x")
        resp = client.post(
            "/resources/circles",
            data={"name": "role.x", "tag": "role", "description": "test"},
        )
        self.assertEqual(resp.status_code, 302)
        campus.api.circles.new.assert_called_once_with(
            name="role.x", tag="role", description="test"
        )

    def test_create_circle_requires_name_and_tag(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post("/resources/circles", data={"name": "only-name"})
        campus.api.circles.new.assert_not_called()

    def test_detail_renders_members(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        circle = SimpleNamespace(
            id="c1", name="campus-admin", tag="bootstrap",
            description="", created_at=None,
        )
        campus.api.circles["c1"].get.return_value = circle
        campus.api.circles["c1"].members.list.return_value = {
            TEST_USER_ID: 15,
        }
        resp = client.get("/resources/circles/c1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(TEST_USER_ID.encode(), resp.data)
        self.assertIn(b"15", resp.data)

    def test_add_and_set_member(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/circles/c1/add_member",
            data={"member_id": "new@nyjc.edu.sg", "access_value": "15"},
        )
        campus.api.circles["c1"].members.add.assert_called_once_with(
            "new@nyjc.edu.sg", 15
        )
        client.post(
            "/resources/circles/c1/set_member",
            data={"member_id": "new@nyjc.edu.sg", "access_value": "5"},
        )
        campus.api.circles["c1"].members.set.assert_called_once_with(
            "new@nyjc.edu.sg", 5
        )

    def test_add_member_rejects_bad_access_value(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/circles/c1/add_member",
            data={"member_id": "x@nyjc.edu.sg", "access_value": "abc"},
        )
        campus.api.circles["c1"].members.add.assert_not_called()

    def test_remove_member(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/circles/c1/remove_member",
            data={"member_id": "old@nyjc.edu.sg"},
        )
        campus.api.circles["c1"].members.remove.assert_called_once_with(
            "old@nyjc.edu.sg"
        )

    def test_update_and_delete_circle(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/circles/c1/update",
            data={"name": "renamed", "description": "d"},
        )
        campus.api.circles["c1"].update.assert_called_once_with(
            name="renamed", description="d"
        )
        # A PATCH without a name would 422 upstream — refuse client-side.
        client.post(
            "/resources/circles/c1/update", data={"description": "d"}
        )
        campus.api.circles["c1"].update.assert_called_once()

        client.post("/resources/circles/c1/delete")
        campus.api.circles["c1"].delete.assert_called_once_with()


class VenuesViewTest(unittest.TestCase):
    def test_list_renders_venues(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.venues.list.return_value = [venue()]
        resp = client.get("/resources/venues")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Hall", resp.data)
        self.assertIn(b"Block D", resp.data)

    def test_create_venue(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.venues.create.return_value = SimpleNamespace(id="v9")
        client.post(
            "/resources/venues",
            data={"action": "create", "name": "Hall", "capacity": "100",
                  "location": "Block D", "notes": "n"},
        )
        campus.api.venues.create.assert_called_once_with(
            metadata={"name": "Hall", "capacity": 100, "location": "Block D"},
            notes="n",
        )

    def test_update_venue_parses_metadata_json(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/venues",
            data={"action": "update", "venue_id": "v1",
                  "metadata": json.dumps({"name": "Renamed"}),
                  "notes": ""},
        )
        campus.api.venues["v1"].update.assert_called_once_with(
            metadata={"name": "Renamed"}, notes=None, clear_notes=True
        )

    def test_update_venue_invalid_json_writes_nothing(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/venues",
            data={"action": "update", "venue_id": "v1",
                  "metadata": "{not json", "notes": "kept"},
        )
        campus.api.venues["v1"].update.assert_not_called()

    def test_delete_venue(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/venues",
            data={"action": "delete", "venue_id": "v1"},
        )
        campus.api.venues["v1"].delete.assert_called_once_with()


class TimetableViewTest(unittest.TestCase):
    def test_renders_pointers_and_versions(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.timetable.view.return_value = {
            "current": "tt1", "next": "tt2", "effective_from": None,
        }
        campus.api.timetable.list_metadata.return_value = [
            SimpleNamespace(
                id="tt1", filename="2026.xml", label="2026 Term 1",
                start_date=None, end_date=None,
            ),
        ]
        resp = client.get("/resources/timetable")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"tt1", resp.data)
        self.assertIn(b"2026 Term 1", resp.data)
        self.assertIn(b"cancel pending", resp.data)

    def test_point_current(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/timetable/pointers",
            data={"action": "point_current", "timetable_id": "tt9"},
        )
        campus.api.timetable.patch.assert_called_once_with(current="tt9")

    def test_schedule_next_with_date(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/timetable/pointers",
            data={"action": "schedule_next", "timetable_id": "tt9",
                  "effective_from": "2027-01-04"},
        )
        campus.api.timetable.patch.assert_called_once_with(
            next="tt9", effective_from="2027-01-04"
        )

    def test_cancel_next(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/timetable/pointers", data={"action": "cancel_next"}
        )
        campus.api.timetable.cancel_next.assert_called_once_with()

    def test_delete_version(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post("/resources/timetable/tt9/delete")
        campus.api.timetable["tt9"].delete.assert_called_once_with()


class AssignmentsViewTest(unittest.TestCase):
    def test_list_renders_assignments(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.assignments.list.return_value = [
            SimpleNamespace(
                id="a1", title="Quiz 1", description="ch 1-3",
                created_by="teacher@nyjc.edu.sg", created_at=None,
            ),
        ]
        resp = client.get("/resources/assignments")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Quiz 1", resp.data)
        campus.api.assignments.list.assert_called_once_with(created_by=None)

    def test_list_filter_created_by(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.assignments.list.return_value = []
        client.get("/resources/assignments?created_by=teacher@nyjc.edu.sg")
        campus.api.assignments.list.assert_called_once_with(
            created_by="teacher@nyjc.edu.sg"
        )

    def test_create_and_delete(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.assignments.new.return_value = SimpleNamespace(title="Q")
        client.post(
            "/resources/assignments",
            data={"action": "create", "title": "Q", "description": "d"},
        )
        campus.api.assignments.new.assert_called_once_with(
            title="Q", description="d"
        )
        client.post(
            "/resources/assignments",
            data={"action": "delete", "assignment_id": "a1"},
        )
        campus.api.assignments["a1"].delete.assert_called_once_with()


class SubmissionsViewTest(unittest.TestCase):
    def test_list_with_filters(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.submissions.list.return_value = [
            SimpleNamespace(
                id="s1", assignment_id="a1", student_id="student@nyjc.edu.sg",
                course_id="gc-1", responses=[1], feedback=[], submitted_at=None,
            ),
        ]
        resp = client.get(
            "/resources/submissions?assignment_id=a1&student_id=&course_id="
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"s1", resp.data)
        campus.api.submissions.list.assert_called_once_with(
            assignment_id="a1", student_id=None, course_id=None
        )


class SubjcombiViewTest(unittest.TestCase):
    def test_renders_mappings_and_pending(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.subjcombi.list.return_value = [
            {"user_id": "student@nyjc.edu.sg", "lessongroup_id": "2601-H1/P"},
        ]
        campus.api.subjcombi.pending.return_value = [
            {"id": "rec1", "user_id": "student@nyjc.edu.sg",
             "lessongroup_id": "2602-H2/C", "effective_from": None,
             "reason": "subject swap"},
        ]
        resp = client.get("/resources/subjcombi")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"2601-H1/P", resp.data)
        self.assertIn(b"2602-H2/C", resp.data)
        self.assertIn(b"subject swap", resp.data)

    def test_set_mapping(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/subjcombi",
            data={"user_id": "student@nyjc.edu.sg",
                  "lessongroup_id": "2601-H1/P",
                  "effective_from": "2027-01-04", "reason": "admin fix"},
        )
        campus.api.subjcombi.set.assert_called_once_with(
            "student@nyjc.edu.sg", "2601-H1/P",
            effective_from="2027-01-04", reason="admin fix",
        )

    def test_cancel_pending(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post("/resources/subjcombi/pending/rec1/cancel")
        campus.api.subjcombi.cancel.assert_called_once_with("rec1")


class FilesViewTest(unittest.TestCase):
    def test_list_renders_records(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.files.list.return_value = [
            {"slug": "apps/quest-dom", "name": "Quest DOM",
             "content_type": "text/plain", "size": 3},
        ]
        resp = client.get("/resources/files")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"apps/quest-dom", resp.data)
        campus.api.files.list.assert_called_once_with(prefix=None)

    def test_list_prefix_filter(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.files.list.return_value = []
        client.get("/resources/files?prefix=apps/")
        campus.api.files.list.assert_called_once_with(prefix="apps/")

    def test_download(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.files.get_meta.return_value = {
            "slug": "apps/x", "name": "X", "content_type": "text/plain",
        }
        campus.api.files.get_content.return_value = b"abc"
        resp = client.get("/resources/files/content/apps/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"abc")
        self.assertIn("text/plain", resp.headers["Content-Type"])

    def test_upload(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        resp = client.post(
            "/resources/files/upload",
            data={"slug": "apps/x", "name": "X", "content_type": "text/plain",
                  "file": (io.BytesIO(b"hello"), "x.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        campus.api.files.put_by_slug.assert_called_once_with(
            "apps/x", b"hello", content_type="text/plain", name="X"
        )

    def test_upload_requires_slug_and_file(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/files/upload",
            data={"slug": "apps/x"},
            content_type="multipart/form-data",
        )
        campus.api.files.put_by_slug.assert_not_called()

    def test_delete(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post("/resources/files/delete", data={"slug": "apps/x"})
        campus.api.files.delete_by_slug.assert_called_once_with("apps/x")


class HostedAppsViewTest(unittest.TestCase):
    def test_list_renders_apps(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.hostedapps.list.return_value = [
            SimpleNamespace(
                slug="quiz-submission", name="Quiz", status="published",
                content_type="text/html", size=42, updated_at=None,
                created_at=None, id="h1", content_hash="0" * 64,
            ),
        ]
        resp = client.get("/resources/hostedapps")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"quiz-submission", resp.data)
        self.assertIn(b"published", resp.data)
        campus.api.hostedapps.list.assert_called_once_with(prefix=None)

    def test_publish(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        resp = client.post(
            "/resources/hostedapps/publish",
            data={"slug": "quiz-submission", "name": "Quiz",
                  "file": (io.BytesIO(b"<p>hi</p>"), "app.html")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        campus.api.hostedapps.put_by_slug.assert_called_once_with(
            "quiz-submission", b"<p>hi</p>", content_type=None, name="Quiz"
        )

    def test_publish_rejects_invalid_slug(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post(
            "/resources/hostedapps/publish",
            data={"slug": "Has Space", "name": "",
                  "file": (io.BytesIO(b"<p>hi</p>"), "app.html")},
            content_type="multipart/form-data",
        )
        campus.api.hostedapps.put_by_slug.assert_not_called()

    def test_download(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        campus.api.hostedapps.get_meta.return_value = {
            "slug": "quiz", "content_type": "text/html",
        }
        campus.api.hostedapps.get_content.return_value = b"<p>hi</p>"
        resp = client.get("/resources/hostedapps/content/quiz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"<p>hi</p>")

    def test_delete(self):
        app, client, campus, cleanup = make_admin_app()
        self.addCleanup(cleanup)
        client.post("/resources/hostedapps/delete", data={"slug": "quiz"})
        campus.api.hostedapps.delete_by_slug.assert_called_once_with("quiz")


if __name__ == "__main__":
    unittest.main()
