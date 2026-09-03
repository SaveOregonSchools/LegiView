"""Local Flask interface for LegiView.

Routes validate requests and shape read models only. Source access, parsing,
persistence, and downloading remain in the shared collection services.
"""

from __future__ import annotations

import atexit
from datetime import datetime, timezone
import hmac
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlencode

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for

from .config import AppConfig, SETTING_FIELDS
from .runtime import Runtime, build_runtime
from .services.archive_paths import (
    UnsafeArchivePath,
    resolve_stored_path,
)
from .services.collection_workers import CollectionWorkerManager
from .services.file_types import GENERIC_BINARY_MIME_TYPES, normalize_mime_type, validate_file
from .services.storage import DOCUMENT_KINDS, DOWNLOAD_STATUSES


LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 50
CSRF_SESSION_KEY = "_csrf_token"
DOCUMENT_GROUPS = (
    "public_testimony",
    "legacy_testimony",
    "committee_presentation",
    "floor_letter",
    "committee_document_other",
    "unknown",
)


def create_app(config_overrides: dict | None = None) -> Flask:
    """Create the local UI and start bounded workers after recovery."""

    supplied = dict(config_overrides or {})
    service_overrides = {
        key.lower(): value
        for key, value in supplied.items()
        if key.lower() in AppConfig.__dataclass_fields__
    }
    runtime = build_runtime(overrides=service_overrides, exclusive=True)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("LEGIVIEW_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        START_WORKER=True,
    )
    app.config.update(supplied)

    manager = CollectionWorkerManager(
        runtime.collection,
        worker_count=runtime.config.odata_worker_count,
    )
    state: dict[str, Any] = {"runtime": runtime, "workers": manager}
    app.extensions["legiview"] = state
    if app.config["START_WORKER"]:
        manager.start(enqueue_existing=True)

    def shutdown() -> bool:
        """Quiesce the current worker set before releasing mutation ownership."""

        active_runtime: Runtime = state["runtime"]
        active_workers: CollectionWorkerManager = state["workers"]
        quiesced = active_workers.stop(wait=True, timeout=30)
        if quiesced and active_runtime.instance_lock is not None:
            active_runtime.instance_lock.close()
        return quiesced

    state["shutdown"] = shutdown
    atexit.register(shutdown)

    app.jinja_env.filters["human_bytes"] = _human_bytes
    app.jinja_env.filters["human_datetime"] = _human_datetime
    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def require_csrf_token() -> None:
        if request.method != "POST":
            return
        expected = session.get(CSRF_SESSION_KEY)
        supplied = request.form.get(CSRF_SESSION_KEY)
        if (
            not isinstance(expected, str)
            or not isinstance(supplied, str)
            or not hmac.compare_digest(expected, supplied)
        ):
            abort(400, description="The form security token is missing or invalid.")

    def current() -> tuple[Runtime, CollectionWorkerManager]:
        extension = app.extensions["legiview"]
        return extension["runtime"], extension["workers"]

    @app.context_processor
    def common_context() -> dict[str, Any]:
        return {"year": datetime.now().year}

    @app.get("/")
    def home():
        active, _ = current()
        raw_stats = active.storage.archive_stats()
        stats = {
            "sessions": raw_stats.get("sessions_stored", 0),
            "bills": raw_stats.get("bills_stored", 0),
            "sponsors": raw_stats.get("sponsors_stored", 0),
            "documents_discovered": raw_stats.get("documents_discovered", 0),
            "documents_downloaded": raw_stats.get("documents_downloaded", 0),
            "download_failures": raw_stats.get("download_failures", 0),
            "archive_bytes": _human_bytes(raw_stats.get("archive_bytes", 0)),
            "last_completed_collection": _human_datetime(raw_stats.get("last_completed_collection")),
        }
        recent = [_present_run(row) for row in active.collection.runs.list_runs(8)]
        return render_template("home.html", stats=stats, recent_runs=recent)

    @app.route("/collect/bill", methods=["GET", "POST"])
    def collect_bill():
        active, workers = current()
        selected = request.values.get("session_key", "").strip().upper()
        bill_value = request.values.get("bill_id", "").strip().upper()
        if request.method == "POST":
            try:
                run_id = active.collection.create_collect_bill_run(selected, bill_value)
            except (TypeError, ValueError) as exc:
                flash(str(exc), "error")
            else:
                workers.enqueue(run_id)
                flash(f"Collection run #{run_id} was queued.", "success")
                return redirect(url_for("run_detail", run_id=run_id), code=303)
        recent = [
            _present_run(row)
            for row in active.collection.runs.list_runs(30)
            if row.get("run_type") == "collect_bill"
        ][:8]
        return render_template(
            "collect_bill.html",
            sessions=_session_options(active),
            selected_session=selected,
            bill_id=bill_value,
            recent_runs=recent,
        )

    @app.route("/collect/session", methods=["GET", "POST"])
    def collect_session():
        active, workers = current()
        selected = request.values.get("session_key", "").strip().upper()
        max_bills_text = request.values.get("max_bills", "").strip()
        if request.method == "POST":
            try:
                max_bills = int(max_bills_text) if max_bills_text else None
                run_id = active.collection.create_collect_session_run(selected, max_bills=max_bills)
            except (TypeError, ValueError) as exc:
                flash(str(exc), "error")
            else:
                workers.enqueue(run_id)
                flash(f"Session collection run #{run_id} was queued.", "success")
                return redirect(url_for("run_detail", run_id=run_id), code=303)
        recent = [
            _present_run(row)
            for row in active.collection.runs.list_runs(30)
            if row.get("run_type") == "collect_session"
        ][:8]
        return render_template(
            "collect_session.html",
            sessions=_session_options(active),
            selected_session=selected,
            max_bills=max_bills_text,
            recent_runs=recent,
        )

    @app.get("/bills")
    def bills():
        active, _ = current()
        filters = SimpleNamespace(
            session=request.args.get("session", "").strip().upper(),
            chamber=request.args.get("chamber", "").strip(),
            q=request.args.get("q", "").strip(),
            enacted=request.args.get("enacted", "").strip(),
            sort=request.args.get("sort", "bill").strip(),
        )
        page = _page_number(request.args.get("page"))
        sort = "last_synced" if filters.sort == "last_sync" else filters.sort
        if sort not in {"bill", "title", "chapter"}:
            sort = "bill"
            filters.sort = "bill"
        enacted = True if filters.enacted == "enacted" else False if filters.enacted == "not_enacted" else None
        query_args = dict(
            session_key=filters.session or None,
            chamber=filters.chamber or None,
            query=filters.q or None,
            enacted=enacted,
            sort=sort,
            descending=sort == "last_synced",
        )
        rows = active.storage.list_bills(
            **query_args,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        for row in rows:
            if row.get("sponsor_summary"):
                row["sponsor_summary"] = ", ".join(
                    part.strip()
                    for part in str(row["sponsor_summary"]).split(",")
                    if part.strip()
                )
        return render_template(
            "bills.html",
            filters=filters,
            session_options=_session_options(active),
            rows=rows,
            pagination=_pagination(page, has_next, request.args),
        )

    @app.get("/bills/<int:bill_id>")
    def bill_detail(bill_id: int):
        active, _ = current()
        bill = active.storage.get_bill_by_id(bill_id)
        if bill is None:
            abort(404)
        sponsors = [_present_sponsor(row) for row in active.storage.list_bill_sponsors(bill_id)]
        documents = [_present_document(row) for row in active.storage.list_bill_documents(bill_id)]
        for document in documents:
            if _registered_local_file(active, document) is not None:
                document["local_action_url"] = url_for("document_file", document_id=document["id"])
        groups: dict[str, list[dict[str, Any]]] = {}
        for document in documents:
            groups.setdefault(str(document.get("document_kind") or "unknown"), []).append(document)
        with active.database.connection() as connection:
            agenda_items = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT a.*, m.committee_code, m.committee_name, m.meeting_date
                    FROM committee_agenda_items a
                    LEFT JOIN committee_meetings m ON m.id=a.committee_meeting_id
                    WHERE a.bill_id=? ORDER BY COALESCE(m.meeting_date,''), a.agenda_order, a.id
                    """,
                    (bill_id,),
                ).fetchall()
            ]
        return render_template(
            "bill_detail.html",
            bill=bill,
            chief_sponsors=[
                row
                for row in sponsors
                if row.get("normalized_category") == "chief"
                and row.get("sponsor_kind") in {"legislator", "committee"}
            ],
            regular_sponsors=[
                row
                for row in sponsors
                if row.get("normalized_category") == "regular"
                and row.get("sponsor_kind") in {"legislator", "committee"}
            ],
            filing_notices=[
                row
                for row in sponsors
                if row.get("sponsor_kind") == "other"
                and row.get("pre_session_filed_message")
            ],
            unmapped_sponsors=[
                row
                for row in sponsors
                if not (
                    (
                        row.get("normalized_category") == "chief"
                        and row.get("sponsor_kind") in {"legislator", "committee"}
                    )
                    or (
                        row.get("normalized_category") == "regular"
                        and row.get("sponsor_kind") in {"legislator", "committee"}
                    )
                    or (
                        row.get("sponsor_kind") == "other"
                        and row.get("pre_session_filed_message")
                    )
                )
            ],
            agenda_items=agenda_items,
            document_groups=groups,
        )

    @app.get("/documents")
    def documents():
        active, _ = current()
        filters = SimpleNamespace(
            session=request.args.get("session", "").strip().upper(),
            bill=request.args.get("bill", "").replace(" ", "").strip().upper(),
            kind=request.args.get("kind", "").strip(),
            committee=request.args.get("committee", "").strip(),
            submitter=request.args.get("submitter", "").strip(),
            position=request.args.get("position", "").strip(),
            download_status=request.args.get("download_status", "").strip(),
            failed_only=request.args.get("failed_only") == "1",
        )
        page = _page_number(request.args.get("page"))
        rows = active.storage.list_documents(
            session_key=filters.session or None,
            bill_id_compact=filters.bill or None,
            document_kind=filters.kind or None,
            committee=filters.committee or None,
            submitter=filters.submitter or None,
            testimony_position=filters.position or None,
            download_status=filters.download_status or None,
            failed_only=filters.failed_only,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
        has_next = len(rows) > PAGE_SIZE
        presented = [_present_document(row) for row in rows[:PAGE_SIZE]]
        return render_template(
            "documents.html",
            filters=filters,
            session_options=_session_options(active),
            document_kinds=sorted(DOCUMENT_KINDS),
            download_statuses=sorted(DOWNLOAD_STATUSES),
            rows=presented,
            pagination=_pagination(page, has_next, request.args),
        )

    @app.get("/documents/<int:document_id>")
    def document_detail(document_id: int):
        active, _ = current()
        document = active.storage.get_document(document_id)
        if document is None:
            abort(404)
        presented = _present_document(document)
        if _registered_local_file(active, document) is not None:
            presented["local_action_url"] = url_for("document_file", document_id=document_id)
        bill = active.storage.get_bill_by_id(int(document["bill_id"]))
        return render_template(
            "document_detail.html",
            document=presented,
            bill=bill,
            versions=active.storage.list_document_versions(document_id),
        )

    @app.get("/documents/<int:document_id>/file")
    def document_file(document_id: int):
        active, _ = current()
        document = active.storage.get_document(document_id)
        if document is None:
            abort(404)
        path = _registered_local_file(active, document)
        if path is None:
            abort(404)
        response = send_file(path, conditional=True, as_attachment=False, download_name=path.name)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "sandbox"
        return response

    @app.get("/runs")
    def runs():
        active, _ = current()
        filters = SimpleNamespace(
            run_type=request.args.get("run_type", "").strip(),
            status=request.args.get("status", "").strip(),
            scope=request.args.get("scope", "").strip().casefold(),
        )
        all_rows = active.collection.runs.list_runs(1000)
        selected = []
        for row in all_rows:
            presented = _present_run(row)
            if filters.run_type and row.get("run_type") != filters.run_type:
                continue
            if filters.status and row.get("status") != filters.status:
                continue
            if filters.scope and filters.scope not in presented["scope_display"].casefold():
                continue
            selected.append(presented)
        page = _page_number(request.args.get("page"))
        start = (page - 1) * PAGE_SIZE
        rows = selected[start : start + PAGE_SIZE]
        return render_template(
            "runs.html",
            filters=filters,
            rows=rows,
            pagination=_pagination(page, start + PAGE_SIZE < len(selected), request.args, total=len(selected)),
        )

    @app.get("/runs/<int:run_id>")
    def run_detail(run_id: int):
        active, _ = current()
        run = active.collection.runs.get_run(run_id)
        if run is None:
            abort(404)
        items = active.collection.runs.run_items(run_id)
        stages = [_present_stage(item) for item in items if item.get("item_type") == "stage"]
        work_items = [_present_run_item(active, item) for item in items if item.get("item_type") != "stage"]
        presented = _present_run(run)
        return render_template(
            "run_detail.html",
            run=presented,
            stages=stages,
            items=work_items,
            errors=active.collection.runs.errors(run_id),
            config_snapshot=_json_dict(run.get("config_snapshot_json")),
            elapsed=_elapsed(run),
        )

    @app.post("/runs/<int:run_id>/cancel")
    def cancel_run(run_id: int):
        active, _ = current()
        if active.collection.runs.cancel(run_id):
            flash(f"Run #{run_id} was canceled.", "success")
        else:
            flash(f"Run #{run_id} is not cancelable.", "warning")
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.post("/runs/<int:run_id>/resume")
    def resume_run(run_id: int):
        active, workers = current()
        if active.collection.runs.requeue(run_id):
            workers.enqueue(run_id)
            flash(f"Run #{run_id} was queued to resume.", "success")
        else:
            flash(f"Run #{run_id} is not resumable.", "warning")
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.route("/retry-failures", methods=["GET", "POST"])
    def retry_failures():
        active, workers = current()
        run_id_text = request.values.get("run_id", "").strip()
        source_run_id = int(run_id_text) if run_id_text.isdigit() else None
        filters = SimpleNamespace(
            run_id=source_run_id,
            session=request.values.get("session", "").strip().upper(),
            bill=request.values.get("bill", "").replace(" ", "").strip().upper(),
        )
        candidates = active.storage.list_documents_for_retry(
            run_id=source_run_id,
            include_terminal=True,
            limit=100_000,
        )
        candidates = [
            row
            for row in candidates
            if (not filters.session or row.get("session_key") == filters.session)
            and (not filters.bill or row.get("bill_id_compact") == filters.bill)
        ]
        if request.method == "POST":
            selected_ids = [int(value) for value in request.form.getlist("document_ids") if value.isdigit()]
            if request.form.get("action") == "all":
                selected_ids = [int(row["id"]) for row in candidates]
            allowed = {int(row["id"]) for row in candidates}
            selected_ids = list(dict.fromkeys(value for value in selected_ids if value in allowed))
            if not selected_ids:
                flash("Select at least one retryable document.", "error")
            else:
                run_id = active.collection.runs.create_run(
                    "retry_failures",
                    session_key=filters.session or None,
                    bill_id_compact=filters.bill or None,
                    scope={"source_run_id": source_run_id, "document_ids": selected_ids},
                    config_snapshot=active.config.snapshot(),
                )
                workers.enqueue(run_id)
                flash(f"Retry run #{run_id} was queued for {len(selected_ids)} document(s).", "success")
                return redirect(url_for("run_detail", run_id=run_id), code=303)
        presented = [_present_document(row) for row in candidates]
        stats = {
            "retryable_failures": sum(row.get("download_status") == "failed_retryable" for row in candidates),
            "terminal_failures": _document_status_count(active, "failed_terminal"),
            "interrupted": sum(row.get("download_status") == "interrupted" for row in candidates),
            "paused_low_space": sum(row.get("download_status") == "paused_low_space" for row in candidates),
        }
        return render_template(
            "retry_failures.html",
            stats=stats,
            filters=filters,
            rows=presented,
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        active, _ = current()
        if request.method == "POST":
            submitted = {key: request.form.get(key, "").strip() for key in SETTING_FIELDS}
            try:
                validated = active.config.with_settings(submitted)
                archive_root = validated.archive_root.expanduser().resolve(strict=False)
                if archive_root == Path(archive_root.anchor):
                    raise ValueError("Archive root cannot be a filesystem root.")
                if archive_root.exists() and not archive_root.is_dir():
                    raise ValueError("Archive root points to an existing non-directory path.")
                archive_root.mkdir(parents=True, exist_ok=True)
            except (OSError, TypeError, ValueError) as exc:
                flash(f"Settings were not saved: {exc}", "error")
            else:
                for key in SETTING_FIELDS:
                    value = validated.snapshot()[key]
                    active.storage.set_setting(key, value, updated_by="web")
                # Never swap archive roots or worker graphs while this process
                # is alive. A paused/canceled run can still have an HTTP read
                # winding down, so hot replacement could let two runtimes write
                # concurrently. Every run already retains its config snapshot.
                flash(
                    "Settings saved. Restart LegiView to apply them; each run retains its recorded configuration snapshot for audit.",
                    "success",
                )
                return redirect(url_for("settings"), code=303)
        return render_template("settings.html", settings=active.config.snapshot())

    @app.get("/help")
    def help_page():
        return render_template("help.html")

    @app.get("/health")
    def health():
        active, workers = current()
        return {
            "status": "ok",
            "schema_version": active.database.schema_version(),
            "workers": workers.snapshot(),
        }

    return app


def _session_options(runtime: Runtime) -> list[dict[str, Any]]:
    rows = runtime.storage.list_sessions()
    known = {str(row["session_key"]).upper() for row in rows}
    # These are the two sessions explicitly validated in the Phase 1 source
    # spike. The form also allows typing any official session key.
    for key, name in (
        ("2026R1", "2026 Regular Session"),
        ("2014R1", "2014 Regular Session"),
    ):
        if key not in known:
            rows.append({"session_key": key, "session_name": name})
    return rows


def _present_document(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["position"] = row.get("testimony_position")
    result["bill_pk"] = row.get("bill_id")
    result.setdefault("bill_id_display", _display_bill(row.get("bill_id_compact")))
    return result


def _present_sponsor(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["display_name"] = row.get("resolved_display_name")
    return result


def _present_run(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    scope = _json_dict(row.get("requested_scope_json"))
    session = row.get("requested_session_key") or scope.get("session_key")
    bill = row.get("requested_bill_id_compact") or scope.get("bill_id_compact")
    source_run = scope.get("source_run_id")
    if session and bill:
        display = f"{session} / {_display_bill(bill)}"
    elif session:
        display = str(session)
    elif source_run:
        display = f"Failures from run #{source_run}"
    else:
        display = "Selected failed documents" if row.get("run_type") == "retry_failures" else "—"
    result.update(
        scope_display=display,
        requested_scope=display,
        session_key=session,
        bill_id_compact=bill,
        bill_id_display=_display_bill(bill),
        max_bills=scope.get("max_bills"),
        bills_planned=row.get("bills_total"),
        items_total=row.get("bills_total"),
        items_completed=row.get("bills_completed"),
        errors=row.get("error_count"),
        skipped_count=row.get("documents_skipped"),
        cancel_requested=row.get("status") == "canceled",
    )
    return result


def _present_stage(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["name"] = item.get("stage") or item.get("item_key")
    result["label"] = result["name"]
    result["detail"] = item.get("current_activity")
    result["message"] = item.get("current_activity")
    return result


def _present_run_item(runtime: Runtime, item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["message"] = item.get("current_activity")
    if item.get("document_id"):
        document = runtime.storage.get_document(int(item["document_id"]))
        if document:
            result["document_title"] = document.get("title")
            result["source_document_id"] = document.get("source_id")
            result["label"] = document.get("title") or f"Document {document.get('source_id')}"
    elif item.get("bill_id"):
        bill = runtime.storage.get_bill_by_id(int(item["bill_id"]))
        if bill:
            result["bill_id_display"] = bill.get("bill_id_display")
            result["label"] = bill.get("bill_id_display")
    else:
        result["label"] = item.get("item_key")
    return result


def _registered_local_file(runtime: Runtime, document: Mapping[str, Any]) -> Path | None:
    relative = document.get("local_relative_path")
    filename = document.get("local_filename")
    mime_type = normalize_mime_type(document.get("mime_type"))
    digest = str(document.get("sha256") or "").strip().casefold()
    downloaded_bytes = document.get("downloaded_bytes")
    if (
        not relative
        or document.get("download_status") != "downloaded"
        or document.get("validation_status") != "valid"
        or not filename
        or not mime_type
        or not digest
        or downloaded_bytes is None
        or isinstance(downloaded_bytes, bool)
    ):
        return None
    try:
        expected_bytes = int(downloaded_bytes)
        if expected_bytes < 0:
            return None
        raw_relative = str(relative).replace("\\", "/")
        pure_relative = PurePosixPath(raw_relative)
        if pure_relative.name != str(filename):
            return None
        root = Path(runtime.config.archive_root).expanduser().resolve(strict=False)
        path = resolve_stored_path(root, raw_relative)
        if _path_contains_link_or_reparse_point(root, pure_relative.parts):
            return None
        validation = validate_file(
            path,
            mime_type,
            expected_bytes,
            expected_mime_type=mime_type,
            expected_sha256=digest,
            logical_filename=str(filename),
        )
    except (OSError, TypeError, ValueError, UnsafeArchivePath):
        return None
    detected_mime = normalize_mime_type(validation.detection.mime_type)
    if not validation.valid:
        return None
    if mime_type not in GENERIC_BINARY_MIME_TYPES and detected_mime != mime_type:
        return None
    if _path_contains_link_or_reparse_point(root, pure_relative.parts):
        return None
    return path


def _path_contains_link_or_reparse_point(root: Path, parts: tuple[str, ...]) -> bool:
    """Fail closed if any lexical archive component is a link or reparse point."""

    candidates = (root, *(root.joinpath(*parts[:index]) for index in range(1, len(parts) + 1)))
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
            return True
    return False


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _document_status_count(runtime: Runtime, status: str) -> int:
    with runtime.database.connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM documents WHERE download_status=?", (status,)).fetchone()[0])


def _pagination(
    page: int,
    has_next: bool,
    args: Mapping[str, Any],
    *,
    total: int | None = None,
) -> SimpleNamespace:
    values = {key: value for key, value in args.items() if key != "page"}
    previous_url = "?" + urlencode({**values, "page": page - 1}) if page > 1 else None
    next_url = "?" + urlencode({**values, "page": page + 1}) if has_next else None
    pages = math.ceil(total / PAGE_SIZE) if total is not None and total else None
    return SimpleNamespace(
        page=page,
        total=total,
        pages=pages,
        prev_url=previous_url,
        previous_url=previous_url,
        next_url=next_url,
    )


def _page_number(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _display_bill(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).replace(" ", "").upper()
    return f"{text[:2]} {text[2:]}" if len(text) > 2 else text


def _human_bytes(value: Any) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _human_datetime(value: Any) -> str | None:
    if not value:
        return None
    return str(value).replace("T", " ").replace("Z", " UTC")


def _elapsed(run: Mapping[str, Any]) -> str | None:
    if not run.get("started_at"):
        return None
    try:
        start = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
        end_value = run.get("finished_at")
        end = datetime.fromisoformat(str(end_value).replace("Z", "+00:00")) if end_value else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return None
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes:d}m {seconds:02d}s"


__all__ = ["create_app"]
