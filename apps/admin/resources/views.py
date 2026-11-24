"""apps.admin.resources.views

Routes for the resources section (/resources): management views over
the campus.api service's resources via the SDK's ApiRoot.

Page map:

- GET  /resources/                        → redirect to the first family
                                             the user may read
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

Every wire call rides the signed-in user's own campus session: campus.api
authorizes by circles and denies client principals on gated routes
(#854), so the portal never acts as its own client here. Each view is
gated on the user's effective access for its family — reading needs the
family's group-scope read, a mutation its group-scope create — and the
templates render the write affordances from the same answer. Errors are
flashed, never raised into the page (the admin section's grammar).
"""

__all__ = ["register"]

import functools
import json
import re

import flask
import werkzeug

from apps.admin import authz

# The hostedapp slug grammar (campus.model.hostedapp.SLUG_PATTERN):
# references (document questions, pickers) break on slug changes, so
# the publisher surface validates up front instead of exposing the
# upstream 422.
HOSTEDAPP_SLUG_RE = re.compile(r"^[a-z0-9._-]+$")


def _flash_error(exc: Exception, message: str) -> None:
    flask.current_app.logger.error("%s: %s", message, exc)
    flask.flash(f"{message}: {exc}", "error")


def register(bp: flask.Blueprint, campus, operator_circle: str) -> None:
    """Attach the resources-section routes, closing over the client."""

    def family(name: str | None = None, *, write: bool = False):
        """Gate a view on the signed-in user's campus.api access.

        `name` is the resource family (a campus.api grant circle); None
        gates on the section alone (any readable family).
        """
        def decorator(view):
            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                return authz.family_denial(
                    campus, operator_circle, name, write=write,
                ) or view(*args, **kwargs)
            return wrapped
        return decorator

    def load(fn):
        """Run fn inside the signed-in user's campus session.

        Returns (fn's result, None), or (None, the exception) — callers
        relay failures to the page. One session per call: batch a
        view's requests inside fn rather than calling load twice.
        """
        try:
            with campus.with_user_session() as user:
                return fn(user), None
        except Exception as exc:  # noqa: BLE001 - relayed to the page
            return None, exc

    def list_error(message: str, exc: Exception) -> str:
        """A failed list read: flashed, and returned for the error box."""
        _flash_error(exc, message)
        return f"API call failed: {exc}"

    def api_access() -> dict[str, dict[str, bool]]:
        access = authz.admin_access(campus, operator_circle)
        return authz.api_access(
            campus, access["user_id"],
            operator=access["lane"] == "operator",
        ) or {}

    @bp.get("/")
    @family()
    def index() -> werkzeug.Response:
        """Route the section root to the first resource view the user
        may read (the audit section's pattern: /audit/ hops to the
        trace list)."""
        access = api_access()
        first = next(
            (name for name in authz.API_FAMILIES
             if access.get(name, {}).get("read")),
            "circles",
        )
        return flask.redirect(flask.url_for(f"resources.{first}"))

    # ----------------------------------------------------------- circles

    @bp.get("/circles")
    @family("circles")
    def circles() -> str:
        circles_data, error = load(lambda user: user.api.circles.list())
        error_msg = (
            list_error("Failed to list circles", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/circles.html", circles=circles_data or [], error=error_msg
        )

    @bp.post("/circles")
    @family("circles", write=True)
    def circles_create() -> werkzeug.Response:
        name = (flask.request.form.get("name") or "").strip()
        tag = (flask.request.form.get("tag") or "").strip()
        description = (flask.request.form.get("description") or "").strip()

        if not name or not tag:
            flask.flash("Name and tag are required", "error")
        else:
            circle, error = load(lambda user: user.api.circles.new(
                name=name, tag=tag, description=description
            ))
            if error is not None:
                _flash_error(error, "Failed to create circle")
            else:
                flask.flash(
                    f"Successfully created circle {circle.name}", "success"
                )
        return flask.redirect(flask.url_for("resources.circles"))

    @bp.get("/circles/<circle_id>")
    @family("circles")
    def circle(circle_id: str) -> flask.Response | str:
        def read(user):
            record = user.api.circles[circle_id].get()
            try:
                return record, user.api.circles[circle_id].members.list()
            except Exception as exc:  # noqa: BLE001 - flashed below
                return record, exc

        # A failed members read must not blank the whole page — the
        # header still renders.
        loaded, error = load(read)
        if error is not None:
            _flash_error(error, f"Failed to read circle {circle_id}")
            return flask.redirect(flask.url_for("resources.circles"))
        record, members = loaded
        if isinstance(members, Exception):
            _flash_error(members, f"Failed to list members of {circle_id}")
            members = None

        return flask.render_template(
            "resources/circle.html", circle=record, members=members
        )

    @bp.post("/circles/<circle_id>/<action>")
    @family("circles", write=True)
    def circle_action(circle_id: str, action: str) -> werkzeug.Response:
        detail_url = flask.url_for("resources.circle", circle_id=circle_id)

        if action == "update":
            name = (flask.request.form.get("name") or "").strip()
            description = (flask.request.form.get("description") or "").strip()
            if not name:
                # The server requires name on every circle PATCH (422).
                flask.flash("Name is required", "error")
            else:
                _, error = load(lambda user: user.api.circles[circle_id].update(
                    name=name,
                    description=description or None,
                ))
                if error is not None:
                    _flash_error(error, f"Failed to update circle {circle_id}")
                else:
                    flask.flash(
                        f"Successfully updated circle {circle_id}", "success"
                    )

        elif action in ("add_member", "set_member"):
            member_id = (flask.request.form.get("member_id") or "").strip()
            raw_access = (flask.request.form.get("access_value") or "").strip()
            try:
                access_value = int(raw_access)
            except ValueError:
                access_value = -1
            # campus.api accepts only the five ratified constants at
            # write time (campus#1216); say so here instead of relaying
            # the 422.
            if not member_id or access_value not in authz.ACCESS_CONSTANTS.values():
                flask.flash(
                    "Member ID and one of the five access values are "
                    "required (admin 126, editor 54, viewer 18, self 14, "
                    "guest 2)",
                    "error",
                )
            else:
                def add(user):
                    # add() re-errors on an existing member; set() is the
                    # upsert lane.
                    if action == "add_member":
                        return user.api.circles[circle_id].members.add(
                            member_id, access_value
                        )
                    return user.api.circles[circle_id].members.set(
                        member_id, access_value
                    )

                _, error = load(add)
                if error is not None:
                    _flash_error(
                        error,
                        f"Failed to {action.replace('_', ' ')} in circle {circle_id}",
                    )
                else:
                    verb = "added to" if action == "add_member" else "set in"
                    flask.flash(
                        f"Successfully {verb} circle {circle_id}: "
                        f"{member_id} (access {access_value})",
                        "success",
                    )

        elif action == "remove_member":
            member_id = (flask.request.form.get("member_id") or "").strip()
            if not member_id:
                flask.flash("Member ID is required", "error")
            else:
                _, error = load(
                    lambda user: user.api.circles[circle_id].members.remove(member_id)
                )
                if error is not None:
                    _flash_error(
                        error, f"Failed to remove member from circle {circle_id}"
                    )
                else:
                    flask.flash(
                        f"Successfully removed {member_id} from circle "
                        f"{circle_id}",
                        "success",
                    )

        else:
            flask.flash("Unknown action", "error")

        return flask.redirect(detail_url)

    @bp.post("/circles/<circle_id>/delete")
    @family("circles", write=True)
    def circle_delete(circle_id: str) -> werkzeug.Response:
        _, error = load(lambda user: user.api.circles[circle_id].delete())
        if error is not None:
            _flash_error(error, f"Failed to delete circle {circle_id}")
            return flask.redirect(
                flask.url_for("resources.circle", circle_id=circle_id)
            )
        flask.flash(f"Successfully deleted circle {circle_id}", "success")
        return flask.redirect(flask.url_for("resources.circles"))

    # ------------------------------------------------------------ venues

    @bp.route("/venues", methods=["GET", "POST"])
    @family("venues")
    def venues():
        error_msg = None

        if flask.request.method == "POST":
            denied = authz.family_denial(
                campus, operator_circle, "venues", write=True,
            )
            if denied is not None:
                return denied

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
                venue, error = load(lambda user: user.api.venues.create(
                    metadata=metadata or None,
                    notes=flask.request.form.get("notes") or None,
                ))
                if error is not None:
                    _flash_error(error, "Failed to create venue")
                else:
                    flask.flash(
                        f"Successfully created venue {venue.id}", "success"
                    )
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
                notes = flask.request.form.get("notes")
                _, error = load(lambda user: user.api.venues[venue_id].update(
                    # The notes textarea always submits — prefilled with
                    # the venue's notes, so an untouched save resubmits
                    # the same value (a harmless no-op write) and empty
                    # means clear (same grammar as the clients page's
                    # redirect_uris textarea).
                    metadata=metadata_update,
                    notes=notes or None,
                    clear_notes=not (notes or "").strip(),
                ))
                if error is not None:
                    _flash_error(error, f"Failed to update venue {venue_id}")
                else:
                    flask.flash(
                        f"Successfully updated venue {venue_id}", "success"
                    )
            elif action == "delete" and venue_id:
                _, error = load(
                    lambda user: user.api.venues[venue_id].delete()
                )
                if error is not None:
                    _flash_error(error, f"Failed to delete venue {venue_id}")
                else:
                    flask.flash(
                        f"Successfully deleted venue {venue_id}", "success"
                    )
            else:
                flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("resources.venues"))

        # GET request - venue list.
        venues_data, error = load(lambda user: user.api.venues.list())
        if error is not None:
            error_msg = list_error("Failed to list venues", error)

        return flask.render_template(
            "resources/venues.html", venues=venues_data or [], error=error_msg
        )

    # ---------------------------------------------------------- timetable

    @bp.get("/timetable")
    @family("timetable")
    def timetable():
        def read(user):
            return user.api.timetable.view(), user.api.timetable.list_metadata()

        loaded, error = load(read)
        pointers, timetables = loaded if error is None else (None, [])
        error_msg = (
            list_error("Failed to read timetables", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/timetable.html",
            pointers=pointers,
            timetables=timetables,
            error=error_msg,
        )

    @bp.post("/timetable/pointers")
    @family("timetable", write=True)
    def timetable_pointers() -> werkzeug.Response:
        action = flask.request.form.get("action")
        timetable_id = (flask.request.form.get("timetable_id") or "").strip()

        def run(call) -> bool:
            """Run the pointer call, flashing a failure."""
            _, error = load(call)
            if error is not None:
                _flash_error(error, "Failed to update timetable pointers")
            return error is None

        if action == "point_current":
            if not timetable_id:
                flask.flash("Timetable ID is required", "error")
            elif run(lambda user: user.api.timetable.patch(current=timetable_id)):
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
                if run(lambda user: user.api.timetable.patch(
                    next=timetable_id, effective_from=effective_from
                )):
                    when = f", effective {effective_from}" if effective_from else ""
                    flask.flash(
                        f"Scheduled {timetable_id} as the next timetable"
                        f"{when}",
                        "success",
                    )
        elif action == "cancel_next":
            if run(lambda user: user.api.timetable.cancel_next()):
                flask.flash("Pending switch cancelled", "success")
        else:
            flask.flash("Unknown action", "error")

        return flask.redirect(flask.url_for("resources.timetable"))

    @bp.post("/timetable/<timetable_id>/delete")
    @family("timetable", write=True)
    def timetable_delete(timetable_id: str) -> werkzeug.Response:
        _, error = load(lambda user: user.api.timetable[timetable_id].delete())
        if error is not None:
            # The API refuses the version a pointer names with a 409 —
            # move or cancel the pointer first.
            _flash_error(error, f"Failed to delete timetable {timetable_id}")
        else:
            flask.flash(
                f"Successfully deleted timetable {timetable_id}", "success"
            )
        return flask.redirect(flask.url_for("resources.timetable"))

    # -------------------------------------------------------- assignments

    @bp.route("/assignments", methods=["GET", "POST"])
    @family("assignments")
    def assignments():
        error_msg = None
        created_by = flask.request.args.get("created_by", "").strip()

        if flask.request.method == "POST":
            denied = authz.family_denial(
                campus, operator_circle, "assignments", write=True,
            )
            if denied is not None:
                return denied

            action = flask.request.form.get("action")

            if action == "create":
                title = (flask.request.form.get("title") or "").strip()
                description = (
                    flask.request.form.get("description") or ""
                ).strip() or None
                if not title:
                    flask.flash("Title is required", "error")
                else:
                    assignment, error = load(lambda user: user.api.assignments.new(
                        title=title, description=description
                    ))
                    if error is not None:
                        _flash_error(error, "Failed to create assignment")
                    else:
                        flask.flash(
                            "Successfully created assignment "
                            f"{assignment.title}",
                            "success",
                        )
            elif action == "delete":
                assignment_id = flask.request.form.get("assignment_id")
                if not assignment_id:
                    flask.flash("Assignment ID is required", "error")
                else:
                    _, error = load(
                        lambda user: user.api.assignments[assignment_id].delete()
                    )
                    if error is not None:
                        _flash_error(
                            error, f"Failed to delete assignment {assignment_id}"
                        )
                    else:
                        flask.flash(
                            "Successfully deleted assignment "
                            f"{assignment_id}",
                            "success",
                        )
            else:
                flask.flash("Unknown action", "error")

            return flask.redirect(flask.url_for("resources.assignments"))

        # GET request - assignment list (created_by filter).
        assignments_data, error = load(
            lambda user: user.api.assignments.list(created_by=created_by or None)
        )
        if error is not None:
            error_msg = list_error("Failed to list assignments", error)

        return flask.render_template(
            "resources/assignments.html",
            assignments=assignments_data or [],
            created_by=created_by,
            error=error_msg,
        )

    # -------------------------------------------------------- submissions

    @bp.get("/submissions")
    @family("submissions")
    def submissions() -> str:
        filters = {
            "assignment_id": flask.request.args.get("assignment_id", "").strip(),
            "student_id": flask.request.args.get("student_id", "").strip(),
            "course_id": flask.request.args.get("course_id", "").strip(),
        }
        submissions_data, error = load(lambda user: user.api.submissions.list(
            assignment_id=filters["assignment_id"] or None,
            student_id=filters["student_id"] or None,
            course_id=filters["course_id"] or None,
        ))
        error_msg = (
            list_error("Failed to list submissions", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/submissions.html",
            submissions=submissions_data or [],
            filters=filters,
            error=error_msg,
        )

    # ---------------------------------------------------------- subjcombi

    @bp.route("/subjcombi", methods=["GET", "POST"])
    @family("subjcombi")
    def subjcombi():
        if flask.request.method == "POST":
            denied = authz.family_denial(
                campus, operator_circle, "subjcombi", write=True,
            )
            if denied is not None:
                return denied

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
                _, error = load(lambda api_user: api_user.api.subjcombi.set(
                    user_id,
                    lessongroup_id,
                    effective_from=effective_from,
                    reason=reason,
                ))
                if error is not None:
                    _flash_error(error, f"Failed to set mapping for {user_id}")
                else:
                    flask.flash(
                        f"Successfully mapped {user_id} to {lessongroup_id}"
                        + (f" effective {effective_from}" if effective_from else ""),
                        "success",
                    )

            return flask.redirect(flask.url_for("resources.subjcombi"))

        # GET request - effective mappings + pending scheduled changes.
        def read(user):
            return user.api.subjcombi.list(), user.api.subjcombi.pending()

        loaded, error = load(read)
        mappings, pending = loaded if error is None else ([], [])
        error_msg = (
            list_error("Failed to read subjcombi mappings", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/subjcombi.html",
            mappings=mappings,
            pending=pending,
            error=error_msg,
        )

    @bp.post("/subjcombi/pending/<record_id>/cancel")
    @family("subjcombi", write=True)
    def subjcombi_cancel(record_id: str) -> werkzeug.Response:
        _, error = load(lambda user: user.api.subjcombi.cancel(record_id))
        if error is not None:
            # 409 once the record is effective — it can no longer be
            # cancelled.
            _flash_error(error, f"Failed to cancel pending change {record_id}")
        else:
            flask.flash(f"Successfully cancelled pending change {record_id}",
                        "success")
        return flask.redirect(flask.url_for("resources.subjcombi"))

    # -------------------------------------------------------------- files

    @bp.get("/files")
    @family("files")
    def files() -> str:
        prefix = flask.request.args.get("prefix", "").strip()
        records, error = load(lambda user: user.api.files.list(prefix=prefix or None))
        error_msg = (
            list_error("Failed to list files", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/files.html",
            files=records or [],
            prefix=prefix,
            error=error_msg,
        )

    @bp.get("/files/content/<path:slug>")
    @family("files")
    def file_content(slug: str) -> flask.Response:
        def read(user):
            return user.api.files.get_meta(slug), user.api.files.get_content(slug)

        loaded, error = load(read)
        if error is not None:
            _flash_error(error, f"Failed to read file {slug}")
            return flask.redirect(flask.url_for("resources.files"))

        meta, data = loaded
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
    @family("files", write=True)
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
            content = upload.read()
            _, error = load(lambda user: user.api.files.put_by_slug(
                slug,
                content,
                content_type=content_type,
                name=name,
            ))
            if error is not None:
                _flash_error(error, f"Failed to store file {slug}")
            else:
                flask.flash(f"Successfully stored file {slug}", "success")
        return flask.redirect(flask.url_for("resources.files"))

    @bp.post("/files/delete")
    @family("files", write=True)
    def file_delete() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        if not slug:
            flask.flash("Slug is required", "error")
        else:
            _, error = load(lambda user: user.api.files.delete_by_slug(slug))
            if error is not None:
                _flash_error(error, f"Failed to delete file {slug}")
            else:
                flask.flash(f"Successfully deleted file {slug}", "success")
        return flask.redirect(flask.url_for("resources.files"))

    # --------------------------------------------------------- hostedapps

    @bp.get("/hostedapps")
    @family("hostedapps")
    def hostedapps() -> str:
        prefix = flask.request.args.get("prefix", "").strip()
        apps, error = load(lambda user: user.api.hostedapps.list(prefix=prefix or None))
        error_msg = (
            list_error("Failed to list hosted apps", error)
            if error is not None else None
        )

        return flask.render_template(
            "resources/hostedapps.html",
            apps=apps or [],
            prefix=prefix,
            error=error_msg,
        )

    @bp.get("/hostedapps/content/<slug>")
    @family("hostedapps")
    def hostedapp_content(slug: str) -> flask.Response:
        def read(user):
            return (
                user.api.hostedapps.get_meta(slug),
                user.api.hostedapps.get_content(slug),
            )

        loaded, error = load(read)
        if error is not None:
            _flash_error(error, f"Failed to read hosted app {slug}")
            return flask.redirect(flask.url_for("resources.hostedapps"))

        meta, data = loaded
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
    @family("hostedapps", write=True)
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
            content = upload.read()
            _, error = load(lambda user: user.api.hostedapps.put_by_slug(
                slug,
                content,
                # Hosted-app content defaults to text/html upstream
                # when absent.
                content_type=None,
                name=name,
            ))
            if error is not None:
                _flash_error(error, f"Failed to publish {slug}")
            else:
                flask.flash(f"Successfully published {slug}", "success")
        return flask.redirect(flask.url_for("resources.hostedapps"))

    @bp.post("/hostedapps/delete")
    @family("hostedapps", write=True)
    def hostedapp_delete() -> werkzeug.Response:
        slug = (flask.request.form.get("slug") or "").strip()
        if not slug:
            flask.flash("Slug is required", "error")
        else:
            _, error = load(lambda user: user.api.hostedapps.delete_by_slug(slug))
            if error is not None:
                _flash_error(error, f"Failed to delete hosted app {slug}")
            else:
                flask.flash(f"Successfully deleted hosted app {slug}",
                            "success")
        return flask.redirect(flask.url_for("resources.hostedapps"))
