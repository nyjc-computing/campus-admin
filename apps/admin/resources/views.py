"""apps.admin.resources.views

Routes for the resources section (/resources): management views over
the campus.api service's resources via the SDK's ApiRoot.

Page map:

- GET  /resources/                        → redirect to circles
- GET  /resources/circles                 circle list (+ create)
- POST /resources/circles                 action=create
- GET  /resources/circles/<circle_id>     circle detail: members, update, delete
- POST /resources/circles/<circle_id>/<action>   update | add_member |
    set_member | remove_member
- POST /resources/circles/<circle_id>/delete
- GET/POST /resources/venues              list/create; action ∈ create |
    update | delete
- GET  /resources/timetable               pointer state + metadata list
- POST /resources/timetable/pointers      action ∈ point_current |
    schedule_next | cancel_next
- POST /resources/timetable/<id>/delete   delete a timetable version
- GET/POST /resources/assignments         list (created_by filter);
    action ∈ create | delete
- GET  /resources/submissions             read-only list with filters
- GET  /resources/subjcombi               mappings + pending records
- POST /resources/subjcombi/set           set one user's mapping
- POST /resources/subjcombi/pending/<record_id>/cancel
- GET  /resources/files                   by-slug list (prefix filter)
- GET  /resources/files/content/<path:slug>   download
- POST /resources/files/upload            upsert by slug (multipart)
- POST /resources/files/delete            delete by slug
- GET  /resources/hostedapps              by-slug list (prefix filter)
- GET  /resources/hostedapps/content/<slug>   download
- POST /resources/hostedapps/publish      upsert by slug (multipart)
- POST /resources/hostedapps/delete       delete by slug

Every wire call rides the portal's operator client; errors are
flashed, never raised into the page (the admin section's grammar).
"""

__all__ = ["register"]

import json
import re

import flask
import werkzeug

# The hostedapp slug grammar (campus.model.hostedapp.SLUG_PATTERN):
# references (document questions, pickers) break on slug changes, so
# the publisher surface validates up front instead of exposing the
# upstream 422.
HOSTEDAPP_SLUG_RE = re.compile(r"^[a-z0-9._-]+$")


def _flash_error(exc: Exception, message: str) -> None:
    flask.current_app.logger.error("%s: %s", message, exc)
    flask.flash(f"{message}: {exc}", "error")


def register(bp: flask.Blueprint, campus) -> None:
    """Attach the resources-section routes, closing over the client."""

    @bp.get("/")
    def index() -> werkzeug.Response:
        """Route the section root to its first resource view (the
        audit section's pattern: /audit/ hops to the trace list)."""
        return flask.redirect(flask.url_for("resources.circles"))

    # ----------------------------------------------------------- circles

    @bp.get("/circles")
    def circles() -> str:
        circles_data = []
        error_msg = None
        try:
            circles_data = campus.api.circles.list()
        except Exception as e:
            _flash_error(e, "Failed to list circles")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/circles.html", circles=circles_data, error=error_msg
        )

    @bp.post("/circles")
    def circles_create() -> werkzeug.Response:
        name = (flask.request.form.get("name") or "").strip()
        tag = (flask.request.form.get("tag") or "").strip()
        description = (flask.request.form.get("description") or "").strip()

        if not name or not tag:
            flask.flash("Name and tag are required", "error")
        else:
            try:
                circle = campus.api.circles.new(
                    name=name, tag=tag, description=description
                )
                flask.flash(f"Successfully created circle {circle.name}", "success")
            except Exception as e:
                _flash_error(e, "Failed to create circle")
        return flask.redirect(flask.url_for("resources.circles"))

    @bp.get("/circles/<circle_id>")
    def circle(circle_id: str) -> flask.Response | str:
        try:
            record = campus.api.circles[circle_id].get()
        except Exception as e:
            _flash_error(e, f"Failed to read circle {circle_id}")
            return flask.redirect(flask.url_for("resources.circles"))

        # {member_id: access_value}; a failed members read must not
        # blank the whole page — the header still renders.
        members = None
        try:
            members = campus.api.circles[circle_id].members.list()
        except Exception as e:
            _flash_error(e, f"Failed to list members of {circle_id}")

        return flask.render_template(
            "resources/circle.html", circle=record, members=members
        )

    @bp.post("/circles/<circle_id>/<action>")
    def circle_action(circle_id: str, action: str) -> werkzeug.Response:
        detail_url = flask.url_for("resources.circle", circle_id=circle_id)
        members = campus.api.circles[circle_id].members

        if action == "update":
            name = (flask.request.form.get("name") or "").strip()
            description = (flask.request.form.get("description") or "").strip()
            if not name:
                # The server requires name on every circle PATCH (422).
                flask.flash("Name is required", "error")
            else:
                try:
                    campus.api.circles[circle_id].update(
                        name=name,
                        description=description or None,
                    )
                    flask.flash(
                        f"Successfully updated circle {circle_id}", "success"
                    )
                except Exception as e:
                    _flash_error(e, f"Failed to update circle {circle_id}")

        elif action in ("add_member", "set_member"):
            member_id = (flask.request.form.get("member_id") or "").strip()
            raw_access = (flask.request.form.get("access_value") or "").strip()
            try:
                access_value = int(raw_access)
            except ValueError:
                access_value = -1
            if not member_id or access_value < 0:
                flask.flash(
                    "Member ID and a non-negative access value are required",
                    "error",
                )
            else:
                try:
                    if action == "add_member":
                        # add() re-errors on an existing member; set()
                        # is the upsert lane.
                        members.add(member_id, access_value)
                    else:
                        members.set(member_id, access_value)
                    verb = "added to" if action == "add_member" else "set in"
                    flask.flash(
                        f"Successfully {verb} circle {circle_id}: "
                        f"{member_id} (access {access_value})",
                        "success",
                    )
                except Exception as e:
                    _flash_error(
                        e, f"Failed to {action.replace('_', ' ')} in circle {circle_id}"
                    )

        elif action == "remove_member":
            member_id = (flask.request.form.get("member_id") or "").strip()
            if not member_id:
                flask.flash("Member ID is required", "error")
            else:
                try:
                    members.remove(member_id)
                    flask.flash(
                        f"Successfully removed {member_id} from circle "
                        f"{circle_id}",
                        "success",
                    )
                except Exception as e:
                    _flash_error(
                        e, f"Failed to remove member from circle {circle_id}"
                    )

        else:
            flask.flash("Unknown action", "error")

        return flask.redirect(detail_url)

    @bp.post("/circles/<circle_id>/delete")
    def circle_delete(circle_id: str) -> werkzeug.Response:
        try:
            campus.api.circles[circle_id].delete()
            flask.flash(f"Successfully deleted circle {circle_id}", "success")
        except Exception as e:
            _flash_error(e, f"Failed to delete circle {circle_id}")
            return flask.redirect(
                flask.url_for("resources.circle", circle_id=circle_id)
            )
        return flask.redirect(flask.url_for("resources.circles"))

    # ------------------------------------------------------------ venues

    @bp.route("/venues", methods=["GET", "POST"])
    def venues():
        error_msg = None

        if flask.request.method == "POST":
            action = flask.request.form.get("action")
            venue_id = flask.request.form.get("venue_id")

            if action == "create":
                metadata: dict = {}
                name = (flask.request.form.get("name") or "").strip()
                capacity = (flask.request.form.get("capacity") or "").strip()
                location = (flask.request.form.get("location") or "").strip()
                if name:
                    metadata["name"] = name
                if capacity:
                    try:
                        metadata["capacity"] = int(capacity)
                    except ValueError:
                        metadata["capacity"] = capacity
                if location:
                    metadata["location"] = location
                try:
                    venue = campus.api.venues.create(
                        metadata=metadata or None,
                        notes=flask.request.form.get("notes") or None,
                    )
                    flask.flash(
                        f"Successfully created venue {venue.id}", "success"
                    )
                except Exception as e:
                    _flash_error(e, "Failed to create venue")
            elif action == "update" and venue_id:
                # metadata is fluid upstream, so the edit form carries
                # it as JSON: empty leaves it unchanged, anything JSON
                # replaces the whole dict. Invalid JSON fails the whole
                # update — writes nothing (fail-closed, like the flash
                # says).
                raw_metadata = flask.request.form.get("metadata")
                metadata_update = None
                if raw_metadata is not None and raw_metadata.strip():
                    try:
                        parsed = json.loads(raw_metadata)
                        if not isinstance(parsed, dict):
                            raise ValueError("metadata must be a JSON object")
                        metadata_update = parsed
                    except (json.JSONDecodeError, ValueError) as e:
                        flask.flash(f"Metadata must be valid JSON: {e}", "error")
                        return flask.redirect(flask.url_for("resources.venues"))
                try:
                    # The notes textarea always submits — prefilled with
                    # the venue's notes, so an untouched save resubmits
                    # the same value (a harmless no-op write) and empty
                    # means clear (same grammar as the clients page's
                    # redirect_uris textarea).
                    notes = flask.request.form.get("notes")
                    campus.api.venues[venue_id].update(
                        metadata=metadata_update,
                        notes=notes or None,
                        clear_notes=not (notes or "").strip(),
                    )
                    flask.flash(
                        f"Successfully updated venue {venue_id}", "success"
                    )
                except Exception as e:
                    _flash_error(e, f"Failed to update venue {venue_id}")
            elif action == "delete" and venue_id:
                try:
                    campus.api.venues[venue_id].delete()
                    flask.flash(
                        f"Successfully deleted venue {venue_id}", "success"
                    )
                except Exception as e:
                    _flash_error(e, f"Failed to delete venue {venue_id}")
            else:
                flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("resources.venues"))

        # GET request - venue list.
        venues_data = []
        try:
            venues_data = campus.api.venues.list()
        except Exception as e:
            _flash_error(e, "Failed to list venues")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/venues.html", venues=venues_data, error=error_msg
        )

    # ---------------------------------------------------------- timetable

    @bp.route("/timetable", methods=["GET"])
    def timetable():
        pointers = None
        metadata_records = []
        error_msg = None
        try:
            pointers = campus.api.timetable.view()
            metadata_records = campus.api.timetable.list_metadata()
        except Exception as e:
            _flash_error(e, "Failed to read timetables")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/timetable.html",
            pointers=pointers,
            timetables=metadata_records,
            error=error_msg,
        )

    @bp.post("/timetable/pointers")
    def timetable_pointers() -> werkzeug.Response:
        action = flask.request.form.get("action")
        timetable_id = (flask.request.form.get("timetable_id") or "").strip()

        try:
            if action == "point_current":
                if not timetable_id:
                    flask.flash("Timetable ID is required", "error")
                else:
                    campus.api.timetable.patch(current=timetable_id)
                    flask.flash(
                        f"Current timetable is now {timetable_id}", "success"
                    )
            elif action == "schedule_next":
                if not timetable_id:
                    flask.flash("Timetable ID is required", "error")
                else:
                    effective_from = (
                        flask.request.form.get("effective_from") or ""
                    ).strip() or None
                    campus.api.timetable.patch(
                        next=timetable_id, effective_from=effective_from
                    )
                    when = f", effective {effective_from}" if effective_from else ""
                    flask.flash(
                        f"Scheduled {timetable_id} as the next timetable"
                        f"{when}",
                        "success",
                    )
            elif action == "cancel_next":
                campus.api.timetable.cancel_next()
                flask.flash("Pending switch cancelled", "success")
            else:
                flask.flash("Unknown action", "error")
        except Exception as e:
            _flash_error(e, "Failed to update timetable pointers")

        return flask.redirect(flask.url_for("resources.timetable"))

    @bp.post("/timetable/<timetable_id>/delete")
    def timetable_delete(timetable_id: str) -> werkzeug.Response:
        try:
            campus.api.timetable[timetable_id].delete()
            flask.flash(
                f"Successfully deleted timetable {timetable_id}", "success"
            )
        except Exception as e:
            # The API refuses the version a pointer names with a 409 —
            # move or cancel the pointer first.
            _flash_error(e, f"Failed to delete timetable {timetable_id}")
        return flask.redirect(flask.url_for("resources.timetable"))

    # -------------------------------------------------------- assignments

    @bp.route("/assignments", methods=["GET", "POST"])
    def assignments():
        error_msg = None
        created_by = flask.request.args.get("created_by", "").strip()

        if flask.request.method == "POST":
            action = flask.request.form.get("action")

            if action == "create":
                title = (flask.request.form.get("title") or "").strip()
                description = (
                    flask.request.form.get("description") or ""
                ).strip() or None
                if not title:
                    flask.flash("Title is required", "error")
                else:
                    try:
                        assignment = campus.api.assignments.new(
                            title=title, description=description
                        )
                        flask.flash(
                            "Successfully created assignment "
                            f"{assignment.title}",
                            "success",
                        )
                    except Exception as e:
                        _flash_error(e, "Failed to create assignment")
            elif action == "delete":
                assignment_id = flask.request.form.get("assignment_id")
                if not assignment_id:
                    flask.flash("Assignment ID is required", "error")
                else:
                    try:
                        campus.api.assignments[assignment_id].delete()
                        flask.flash(
                            "Successfully deleted assignment "
                            f"{assignment_id}",
                            "success",
                        )
                    except Exception as e:
                        _flash_error(
                            e, f"Failed to delete assignment {assignment_id}"
                        )
            else:
                flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("resources.assignments"))

        # GET request - assignment list (created_by filter).
        assignments_data = []
        try:
            assignments_data = campus.api.assignments.list(
                created_by=created_by or None
            )
        except Exception as e:
            _flash_error(e, "Failed to list assignments")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/assignments.html",
            assignments=assignments_data,
            created_by=created_by,
            error=error_msg,
        )

    # -------------------------------------------------------- submissions

    @bp.get("/submissions")
    def submissions() -> str:
        filters = {
            "assignment_id": flask.request.args.get("assignment_id", "").strip(),
            "student_id": flask.request.args.get("student_id", "").strip(),
            "course_id": flask.request.args.get("course_id", "").strip(),
        }
        submissions_data = []
        error_msg = None
        try:
            submissions_data = campus.api.submissions.list(
                assignment_id=filters["assignment_id"] or None,
                student_id=filters["student_id"] or None,
                course_id=filters["course_id"] or None,
            )
        except Exception as e:
            _flash_error(e, "Failed to list submissions")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/submissions.html",
            submissions=submissions_data,
            filters=filters,
            error=error_msg,
        )

    # ---------------------------------------------------------- subjcombi

    @bp.route("/subjcombi", methods=["GET", "POST"])
    def subjcombi():
        if flask.request.method == "POST":
            user_id = (flask.request.form.get("user_id") or "").strip()
            lessongroup_id = (
                flask.request.form.get("lessongroup_id") or ""
            ).strip()
            effective_from = (
                flask.request.form.get("effective_from") or ""
            ).strip() or None
            reason = (flask.request.form.get("reason") or "").strip() or None

            if not user_id or not lessongroup_id:
                flask.flash(
                    "User ID and lessongroup are required", "error"
                )
            else:
                try:
                    campus.api.subjcombi.set(
                        user_id,
                        lessongroup_id,
                        effective_from=effective_from,
                        reason=reason,
                    )
                    flask.flash(
                        f"Successfully mapped {user_id} to {lessongroup_id}"
                        + (f" effective {effective_from}" if effective_from else ""),
                        "success",
                    )
                except Exception as e:
                    _flash_error(e, f"Failed to set mapping for {user_id}")

            return flask.redirect(flask.url_for("resources.subjcombi"))

        # GET request - effective mappings + pending scheduled changes.
        mappings = []
        pending = []
        error_msg = None
        try:
            mappings = campus.api.subjcombi.list()
            pending = campus.api.subjcombi.pending()
        except Exception as e:
            _flash_error(e, "Failed to read subjcombi mappings")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/subjcombi.html",
            mappings=mappings,
            pending=pending,
            error=error_msg,
        )

    @bp.post("/subjcombi/pending/<record_id>/cancel")
    def subjcombi_cancel(record_id: str) -> werkzeug.Response:
        try:
            campus.api.subjcombi.cancel(record_id)
            flask.flash(f"Successfully cancelled pending change {record_id}",
                        "success")
        except Exception as e:
            # 409 once the record is effective — it can no longer be
            # cancelled.
            _flash_error(e, f"Failed to cancel pending change {record_id}")
        return flask.redirect(flask.url_for("resources.subjcombi"))

    # -------------------------------------------------------------- files

    @bp.get("/files")
    def files() -> str:
        prefix = flask.request.args.get("prefix", "").strip()
        records = []
        error_msg = None
        try:
            records = campus.api.files.list(prefix=prefix or None)
        except Exception as e:
            _flash_error(e, "Failed to list files")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/files.html",
            files=records,
            prefix=prefix,
            error=error_msg,
        )

    @bp.get("/files/content/<path:slug>")
    def file_content(slug: str) -> flask.Response:
        try:
            meta = campus.api.files.get_meta(slug)
            data = campus.api.files.get_content(slug)
        except Exception as e:
            _flash_error(e, f"Failed to read file {slug}")
            return flask.redirect(flask.url_for("resources.files"))

        content_type = meta.get("content_type") or "application/octet-stream"
        name = meta.get("name") or slug.rsplit("/", 1)[-1]
        return flask.Response(
            data,
            mimetype=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"'
            },
        )

    @bp.post("/files/upload")
    def file_upload() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        name = (flask.request.form.get("name") or "").strip() or None
        content_type = (
            flask.request.form.get("content_type") or ""
        ).strip() or None
        upload = flask.request.files.get("file")

        if not slug or upload is None or not upload.filename:
            flask.flash("Slug and a file are required", "error")
        else:
            try:
                campus.api.files.put_by_slug(
                    slug,
                    upload.read(),
                    content_type=content_type,
                    name=name,
                )
                flask.flash(f"Successfully stored file {slug}", "success")
            except Exception as e:
                _flash_error(e, f"Failed to store file {slug}")
        return flask.redirect(flask.url_for("resources.files"))

    @bp.post("/files/delete")
    def file_delete() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        if not slug:
            flask.flash("Slug is required", "error")
        else:
            try:
                campus.api.files.delete_by_slug(slug)
                flask.flash(f"Successfully deleted file {slug}", "success")
            except Exception as e:
                _flash_error(e, f"Failed to delete file {slug}")
        return flask.redirect(flask.url_for("resources.files"))

    # --------------------------------------------------------- hostedapps

    @bp.get("/hostedapps")
    def hostedapps() -> str:
        prefix = flask.request.args.get("prefix", "").strip()
        apps = []
        error_msg = None
        try:
            apps = campus.api.hostedapps.list(prefix=prefix or None)
        except Exception as e:
            _flash_error(e, "Failed to list hosted apps")
            error_msg = f"API call failed: {e}"

        return flask.render_template(
            "resources/hostedapps.html",
            apps=apps,
            prefix=prefix,
            error=error_msg,
        )

    @bp.get("/hostedapps/content/<slug>")
    def hostedapp_content(slug: str) -> flask.Response:
        try:
            meta = campus.api.hostedapps.get_meta(slug)
            data = campus.api.hostedapps.get_content(slug)
        except Exception as e:
            _flash_error(e, f"Failed to read hosted app {slug}")
            return flask.redirect(flask.url_for("resources.hostedapps"))

        content_type = meta.get("content_type") or "text/html"
        name = meta.get("name") or slug
        return flask.Response(
            data,
            mimetype=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}.html"'
            },
        )

    @bp.post("/hostedapps/publish")
    def hostedapp_publish() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        name = (flask.request.form.get("name") or "").strip() or None
        upload = flask.request.files.get("file")

        if not slug or upload is None or not upload.filename:
            flask.flash("Slug and an HTML file are required", "error")
        elif not HOSTEDAPP_SLUG_RE.match(slug):
            flask.flash(
                "Slug must be lowercase letters, digits, dots, underscores "
                "or hyphens (no slashes)",
                "error",
            )
        else:
            try:
                campus.api.hostedapps.put_by_slug(
                    slug,
                    upload.read(),
                    # Hosted-app content defaults to text/html upstream
                    # when absent.
                    content_type=None,
                    name=name,
                )
                flask.flash(f"Successfully published {slug}", "success")
            except Exception as e:
                _flash_error(e, f"Failed to publish {slug}")
        return flask.redirect(flask.url_for("resources.hostedapps"))

    @bp.post("/hostedapps/delete")
    def hostedapp_delete() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        if not slug:
            flask.flash("Slug is required", "error")
        else:
            try:
                campus.api.hostedapps.delete_by_slug(slug)
                flask.flash(f"Successfully deleted hosted app {slug}",
                            "success")
            except Exception as e:
                _flash_error(e, f"Failed to delete hosted app {slug}")
        return flask.redirect(flask.url_for("resources.hostedapps"))
