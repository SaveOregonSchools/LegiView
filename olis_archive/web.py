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
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)

from .config import (
    AppConfig,
    DEFAULT_ALLOWED_DOWNLOAD_HOSTS,
    ODATA_BASE_URL,
    SETTING_FIELDS,
)
from .deployment import (
    WebDeploymentConfig,
    apply_proxy_fix,
    boolean_value,
    normalize_url_prefix,
    parse_trusted_hosts,
    validate_production_web_config,
)
from .runtime import Runtime, build_runtime
from .services.archive_paths import (
    UnsafeArchivePath,
    resolve_stored_path,
    validate_archive_root_candidate,
)
from .services.collection_workers import CollectionWorkerManager
from .services.archive_queries import ArchiveQueries
from .services.csv_exports import stream_query_csv
from .services.file_types import GENERIC_BINARY_MIME_TYPES, normalize_mime_type, validate_file
from .services.historical_sources import require_supported_session_key
from .services.storage import DOCUMENT_KINDS, DOWNLOAD_STATUSES
from .services.source_mapping import InvalidBillId, normalize_bill_id


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
OFFICIAL_SOURCE_HOSTS = frozenset(
    {
        *DEFAULT_ALLOWED_DOWNLOAD_HOSTS,
        str(urlsplit(ODATA_BASE_URL).hostname or "").casefold(),
    }
)


def create_app(config_overrides: dict | None = None) -> Flask:
    """Create the local UI and start bounded workers after recovery."""

    supplied = dict(config_overrides or {})
    service_overrides = {
        key.lower(): value
        for key, value in supplied.items()
        if key.lower() in AppConfig.__dataclass_fields__
    }

    # Loading AppConfig first also loads the optional .env without exposing the
    # Flask secret to collection snapshots. Resolve and validate proxy security
    # before acquiring the exclusive database/archive mutation lock.
    environment_config = AppConfig.from_env()
    environment_web = WebDeploymentConfig.from_env()
    url_prefix = normalize_url_prefix(
        supplied.get("LEGIVIEW_URL_PREFIX", environment_web.url_prefix)
    )
    trust_proxy_raw = supplied.get(
        "LEGIVIEW_TRUST_PROXY", environment_web.trust_proxy
    )
    trust_proxy = boolean_value(
        trust_proxy_raw, name="LEGIVIEW_TRUST_PROXY"
    )
    trusted_hosts = parse_trusted_hosts(
        supplied.get(
            "LEGIVIEW_TRUSTED_HOSTS",
            supplied.get("TRUSTED_HOSTS", environment_web.trusted_hosts),
        )
    )
    deployment_secret = (
        supplied.get("LEGIVIEW_SECRET_KEY")
        or supplied.get("SECRET_KEY")
        or environment_web.secret_key
    )
    secret_text = str(deployment_secret or "").strip()
    secret_configured = (
        len(secret_text) >= 32
        and secret_text != "replace-with-a-persistent-random-value"
    )
    requested_bind_host = str(
        service_overrides.get("host", environment_config.host)
    )
    validate_production_web_config(
        {
            "LEGIVIEW_TRUST_PROXY": trust_proxy,
            "LEGIVIEW_SECRET_KEY_CONFIGURED": secret_configured,
            "LEGIVIEW_TRUSTED_HOSTS_CONFIGURED": bool(trusted_hosts),
            "LEGIVIEW_BIND_HOST": requested_bind_host,
        }
    )
    web_deployment = WebDeploymentConfig(
        url_prefix=url_prefix,
        trust_proxy=trust_proxy,
        trusted_hosts=trusted_hosts,
        secret_key=deployment_secret,
        session_cookie_secure=boolean_value(
            supplied.get(
                "LEGIVIEW_SESSION_COOKIE_SECURE",
                supplied.get(
                    "SESSION_COOKIE_SECURE", environment_web.session_cookie_secure
                ),
            ),
            name="LEGIVIEW_SESSION_COOKIE_SECURE",
        ),
    )

    runtime = build_runtime(
        config=environment_config,
        overrides=service_overrides,
        exclusive=True,
    )

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=web_deployment.secret_key or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        START_WORKER=True,
    )
    app.config.update(web_deployment.flask_config(bind_host=runtime.config.host))
    app.config.update(supplied)
    # Deployment-critical values are normalized and applied after generic
    # Flask test overrides so cookie/prefix behavior cannot drift internally.
    app.config.update(
        SECRET_KEY=web_deployment.secret_key or app.config["SECRET_KEY"],
        APPLICATION_ROOT=url_prefix,
        SESSION_COOKIE_NAME="legiview_session",
        SESSION_COOKIE_PATH=url_prefix,
        SESSION_COOKIE_SECURE=web_deployment.session_cookie_secure,
        TRUSTED_HOSTS=web_deployment.flask_config(
            bind_host=runtime.config.host
        )["TRUSTED_HOSTS"],
        LEGIVIEW_TRUST_PROXY=trust_proxy,
        LEGIVIEW_URL_PREFIX=url_prefix,
        LEGIVIEW_SECRET_KEY_CONFIGURED=secret_configured,
    )
    apply_proxy_fix(app, enabled=trust_proxy)

    # A durable run owns its configured internal concurrency limits.  The outer
    # dispatcher stays singular so independently queued runs cannot multiply
    # those limits or race shared document state.
    manager = CollectionWorkerManager(runtime.collection)
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
    app.jinja_env.filters["official_source_url"] = _official_source_url
    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def require_expected_proxy_prefix() -> None:
        if not app.config["LEGIVIEW_TRUST_PROXY"]:
            return
        expected = str(app.config["LEGIVIEW_URL_PREFIX"])
        forwarded = request.headers.get("X-Forwarded-Prefix")
        # ProxyFix has already copied this trusted header verbatim into
        # SCRIPT_NAME.  Validate the canonical spelling exactly: normalizing
        # here would allow values such as ``//legiview//`` to pass while they
        # still produce scheme-relative links and redirects.
        actual = request.script_root
        if forwarded != expected or actual != expected:
            abort(
                400,
                description=(
                    "The trusted reverse proxy did not supply the configured "
                    "LegiView URL prefix."
                ),
            )

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
        return {"year": datetime.now().year, "app_base_url": url_for("home")}

    @app.get("/")
    def home():
        active, _ = current()
        queries = ArchiveQueries(active.database)
        raw_stats = queries.dashboard_stats()
        stats = {
            "sessions": raw_stats.get("sessions_in_scope", 0),
            "sessions_complete": raw_stats.get("sessions_inventory_complete", 0),
            "bills": raw_stats.get("bills", 0),
            "documents_discovered": raw_stats.get("documents_discovered", 0),
            "public_testimony": raw_stats.get("public_testimony", 0),
            "presentation_legacy": raw_stats.get("presentation_legacy", 0),
            "floor_letters": raw_stats.get("floor_letters", 0),
            "documents_downloaded": raw_stats.get("documents_downloaded", 0),
            "download_failures": raw_stats.get("download_failures", 0),
            "archive_bytes": _human_bytes(raw_stats.get("archive_bytes", 0)),
            "known_remote_bytes": _human_bytes(raw_stats.get("known_remote_bytes", 0)),
            "known_size_documents": raw_stats.get("known_size_documents", 0),
            "unknown_size_documents": raw_stats.get("unknown_size_documents", 0),
            "disk_free": _human_bytes(_disk_free(active.config.archive_root)),
            "last_historical_inventory": _human_datetime(
                raw_stats.get("last_historical_inventory")
            ),
        }
        recent = [
            _present_run(row, queries=queries)
            for row in active.collection.runs.list_runs(8)
        ]
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

    @app.route("/inventory-backfill", methods=["GET", "POST"])
    def inventory_backfill():
        """Resolve and snapshot an explicit historical inventory scope."""

        active, workers = current()
        queries = ArchiveQueries(active.database)
        source_error: str | None = None
        # Once a full catalogue has been persisted, older official sessions
        # remain visible here with an explicit unsupported annotation. Other
        # archive/status views continue to use only the validated boundary.
        sessions = queries.session_choices(include_unsupported=True)

        action = request.form.get("action", "start") if request.method == "POST" else None
        posted_exact = [
            value.strip().upper()
            for value in request.values.getlist("session_keys")
            if value.strip()
        ]
        supplied_mode = request.values.get("scope_mode")
        # Preserve compatibility for an older form/bookmark that posts only
        # exact checkboxes. New UI submissions always state their mode.
        scope_mode = str(supplied_mode or ("exact" if posted_exact else "range")).strip()
        from_session = request.values.get("from_session", "").strip().upper()
        to_session = request.values.get("to_session", "").strip().upper()
        selected_values: list[str] = []

        # Ordinary GET requests remain offline. Source discovery is an
        # intentional, CSRF-protected POST action; starting a run validates and
        # freezes its selection from the same authoritative snapshot.
        if request.method == "POST" and action == "resolve":
            try:
                resolved = active.collection.historical_session_scope()
                sessions = _official_session_rows(
                    resolved,
                    queries.session_state_map(resolved.all_session_keys),
                )
                if scope_mode not in {"range", "exact"}:
                    raise ValueError(f"Unsupported session-scope mode {scope_mode!r}")
                if scope_mode == "range":
                    from_session = from_session or resolved.boundary_key
                    to_session = to_session or resolved.supported_session_keys[-1]
                    selected_values = list(
                        resolved.selected_range(from_session, to_session).session_keys
                    )
                else:
                    selected_values = list(
                        resolved.selected(posted_exact or resolved.session_keys).session_keys
                    )
            except Exception as exc:  # Source failures must be visible, not fatal to Flask.
                LOGGER.warning("Unable to resolve official historical session scope", exc_info=True)
                source_error = str(exc)

        if request.method == "GET":
            supported_newest_first = [
                str(row["session_key"])
                for row in sessions
                if bool(row.get("supported", True))
            ]
            selected_values = list(reversed(supported_newest_first))
            if supported_newest_first:
                from_session = (
                    "2014R1"
                    if "2014R1" in supported_newest_first
                    else supported_newest_first[-1]
                )
                to_session = supported_newest_first[0]

        if request.method == "POST" and action != "resolve":
            probe_remote_sizes = request.form.get("probe_remote_sizes") == "1"
            force_full = request.form.get("force_full") == "1"
            try:
                # Resolve once. Both submitted endpoints, inclusive expansion,
                # catalogue persistence, and frozen run scope use this same
                # immutable official snapshot.
                resolved = active.collection.historical_session_scope()
                sessions = _official_session_rows(
                    resolved,
                    queries.session_state_map(resolved.all_session_keys),
                )
                if scope_mode == "range":
                    from_session = from_session or resolved.boundary_key
                    to_session = to_session or resolved.supported_session_keys[-1]
                    selected_scope = resolved.selected_range(from_session, to_session)
                elif scope_mode == "exact":
                    selected_scope = resolved.selected(posted_exact)
                else:
                    raise ValueError(f"Unsupported session-scope mode {scope_mode!r}")
                selected_values = list(selected_scope.session_keys)
                run_id = active.collection.create_inventory_backfill_run(
                    probe_remote_sizes=probe_remote_sizes,
                    force_full=force_full,
                    resolved_scope=selected_scope,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                flash(f"Inventory Backfill was not started: {exc}", "error")
            else:
                workers.enqueue(run_id)
                flash(
                    f"Inventory Backfill run #{run_id} was queued. Payload download was not started.",
                    "success",
                )
                return redirect(url_for("run_detail", run_id=run_id), code=303)

        return render_template(
            "inventory_backfill.html",
            sessions=sessions,
            selected_sessions=set(selected_values),
            scope_mode=scope_mode,
            from_session=from_session,
            to_session=to_session,
            source_error=source_error,
            paths={
                "project_root": active.config.project_root,
                "archive_root": active.config.archive_root,
                "disk_free": _human_bytes(_disk_free(active.config.archive_root)),
            },
            pacing={
                "odata_workers": active.config.odata_worker_count,
                "html_concurrency": active.config.html_request_concurrency,
                "delay": active.config.inter_request_delay,
            },
        )

    @app.route("/download-archive", methods=["GET", "POST"])
    def download_archive():
        """Preview and explicitly start bounded archive download work."""

        active, workers = current()
        queries = ArchiveQueries(active.database)
        sessions = queries.session_choices(inventoried_only=True)
        all_session_keys = [str(row["session_key"]) for row in sessions]
        selected_values = [
            value.strip().upper()
            for value in request.values.getlist("session_keys")
            if value.strip().upper() in set(all_session_keys)
        ]
        if request.method == "GET" and "preview" not in request.args:
            selected_values = all_session_keys
        selected_kinds = [
            value.strip()
            for value in request.values.getlist("document_kinds")
            if value.strip() in DOCUMENT_KINDS
        ]
        mode = request.values.get("eligibility", "missing_pending").strip()
        retryable_only = mode == "retryable"
        preview_error: str | None = None
        if selected_values:
            try:
                source_preflight = active.collection.download_archive_preflight(
                    selected_values,
                    document_kinds=selected_kinds or None,
                    retryable_failures_only=retryable_only,
                    missing_pending_only=not retryable_only,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                preview_error = str(exc)
                source_preflight = None
        else:
            source_preflight = None
        if source_preflight is None:
            disk_free = _disk_free(active.config.archive_root)
            floor_bytes = int(active.config.minimum_free_space_bytes or 0)
            preflight = SimpleNamespace(
                documents_in_scope=0,
                recorded_downloaded=0,
                pending_missing=0,
                retryable_failures=0,
                terminal_or_non_downloadable=0,
                eligible_documents=0,
                known_pending_bytes=0,
                unknown_size_pending=0,
            )
            blocked = bool(preview_error)
        else:
            disk_free = int(source_preflight.free_bytes)
            floor_bytes = int(source_preflight.minimum_free_space_bytes)
            preflight = SimpleNamespace(
                documents_in_scope=source_preflight.documents_in_scope,
                recorded_downloaded=source_preflight.already_downloaded,
                pending_missing=source_preflight.pending_or_missing,
                retryable_failures=source_preflight.retryable_failures,
                terminal_or_non_downloadable=source_preflight.terminal_or_non_downloadable,
                eligible_documents=source_preflight.pending_or_missing,
                known_pending_bytes=source_preflight.known_pending_bytes,
                unknown_size_pending=source_preflight.unknown_size_pending,
            )
            blocked = not source_preflight.known_bytes_fit
        usable_bytes = max(0, disk_free - floor_bytes)
        block_reason = preview_error
        if blocked and block_reason is None:
            block_reason = (
                f"Known pending payloads require {_human_bytes(preflight.known_pending_bytes)}, "
                f"but only {_human_bytes(usable_bytes)} is available above the configured floor."
            )

        if request.method == "POST" and request.form.get("action") != "preview":
            if not selected_values:
                flash("Select at least one inventoried session.", "error")
            elif blocked:
                flash(f"Download Archive was not started: {block_reason}", "error")
            elif preflight.documents_in_scope < 1 or (
                retryable_only and preflight.eligible_documents < 1
            ):
                flash("No documents match the selected archive scope.", "error")
            else:
                try:
                    run_id = active.collection.create_download_archive_run(
                        selected_values,
                        document_kinds=selected_kinds or None,
                        retryable_failures_only=retryable_only,
                        missing_pending_only=not retryable_only,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    flash(f"Download Archive was not started: {exc}", "error")
                else:
                    workers.enqueue(run_id)
                    flash(f"Download Archive run #{run_id} was queued.", "success")
                    return redirect(url_for("run_detail", run_id=run_id), code=303)

        return render_template(
            "download_archive.html",
            sessions=sessions,
            selected_sessions=set(selected_values),
            document_kinds=sorted(DOCUMENT_KINDS),
            selected_kinds=set(selected_kinds),
            eligibility=mode,
            preflight=preflight,
            disk={
                "free_bytes": disk_free,
                "free": _human_bytes(disk_free),
                "floor_bytes": floor_bytes,
                "floor": _human_bytes(floor_bytes),
                "floor_gb": active.config.minimum_free_space_gb,
                "usable": _human_bytes(usable_bytes),
            },
            archive_root=active.config.archive_root,
            worker_count=active.config.download_worker_count,
            blocked=blocked,
            block_reason=block_reason,
            preview_error=preview_error,
        )

    @app.get("/session-status")
    def session_status():
        active, _ = current()
        filters = SimpleNamespace(
            session=request.args.get("session", "").strip().upper(),
            inventory_status=request.args.get("inventory_status", "").strip(),
        )
        page = _page_number(request.args.get("page"))
        result = ArchiveQueries(active.database).session_status(
            session_key=filters.session or None,
            inventory_status=filters.inventory_status or None,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        return render_template(
            "session_status.html",
            filters=filters,
            rows=result.rows,
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
        )

    @app.get("/operations")
    def operations():
        active, _ = current()
        view = request.args.get("view", "errors").strip()
        if view not in {"errors", "anomalies"}:
            view = "errors"
        filters = _operation_request_filters(request.args, view=view)
        page = _page_number(request.args.get("page"))
        result = ArchiveQueries(active.database).operations(
            view=view,
            **_operation_query_filters(filters),
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        return render_template(
            "operations.html",
            filters=filters,
            rows=result.rows,
            document_kinds=sorted(DOCUMENT_KINDS),
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
        )

    @app.get("/bills")
    def bills():
        active, _ = current()
        filters = SimpleNamespace(
            session=request.args.get("session", "").strip().upper(),
            chamber=request.args.get("chamber", "").strip(),
            q=request.args.get("q", "").strip(),
            sponsor=request.args.get("sponsor", "").strip(),
            enacted=request.args.get("enacted", "").strip(),
            sort=request.args.get("sort", "bill").strip(),
        )
        page = _page_number(request.args.get("page"))
        sort = "last_synced" if filters.sort == "last_sync" else filters.sort
        if sort not in {"bill", "title", "chapter", "last_synced", "documents"}:
            sort = "bill"
            filters.sort = "bill"
        enacted = True if filters.enacted == "enacted" else False if filters.enacted == "not_enacted" else None
        query_args = dict(
            session_key=filters.session or None,
            chamber=filters.chamber or None,
            query=filters.q or None,
            sponsor=filters.sponsor or None,
            enacted=enacted,
            sort=sort,
            descending=sort == "last_synced",
        )
        result = ArchiveQueries(active.database).bills(
            **query_args,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        rows = list(result.rows)
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
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
        )

    @app.get("/bills/<int:bill_id>")
    def bill_detail(bill_id: int):
        active, _ = current()
        bill = active.storage.get_bill_by_id(bill_id)
        if bill is None:
            abort(404)
        sponsors = [_present_sponsor(row) for row in active.storage.list_bill_sponsors(bill_id)]
        document_page_number = _page_number(request.args.get("document_page"))
        document_page = ArchiveQueries(active.database).documents(
            session_key=str(bill["session_key"]),
            bill_id_compact=str(bill["bill_id_compact"]),
            limit=PAGE_SIZE,
            offset=(document_page_number - 1) * PAGE_SIZE,
        )
        documents = [_present_document(row) for row in document_page.rows]
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
            document_pagination=_pagination(
                document_page_number,
                document_page_number * PAGE_SIZE < document_page.total,
                request.args,
                total=document_page.total,
                page_key="document_page",
            ),
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
            organization=request.args.get("organization", "").strip(),
            position=request.args.get("position", "").strip(),
            download_status=request.args.get("download_status", "").strip(),
            source_presence=request.args.get("source_presence", "").strip(),
            displayed_in_olis=request.args.get("displayed_in_olis", "").strip(),
            failed_only=request.args.get("failed_only") == "1",
        )
        page = _page_number(request.args.get("page"))
        result = ArchiveQueries(active.database).documents(
            session_key=filters.session or None,
            bill_id_compact=filters.bill or None,
            document_kind=filters.kind or None,
            committee=filters.committee or None,
            submitter=filters.submitter or None,
            organization=filters.organization or None,
            testimony_position=filters.position or None,
            download_status=filters.download_status or None,
            source_presence=filters.source_presence or None,
            displayed_in_olis=filters.displayed_in_olis or "any",
            failed_only=filters.failed_only,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        presented = [_present_document(row) for row in result.rows]
        return render_template(
            "documents.html",
            filters=filters,
            session_options=_session_options(active),
            document_kinds=sorted(DOCUMENT_KINDS),
            download_statuses=sorted(DOWNLOAD_STATUSES),
            rows=presented,
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
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
            probe=active.storage.get_document_probe(document_id),
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
        queries = ArchiveQueries(active.database)
        filters = SimpleNamespace(
            run_type=request.args.get("run_type", "").strip(),
            status=request.args.get("status", "").strip(),
            scope=request.args.get("scope", "").strip().casefold(),
        )
        page = _page_number(request.args.get("page"))
        result = queries.runs(
            run_type=filters.run_type or None,
            status=filters.status or None,
            scope=filters.scope or None,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        rows = [_present_run(row, queries=queries) for row in result.rows]
        return render_template(
            "runs.html",
            filters=filters,
            rows=rows,
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
        )

    @app.route("/runs/<int:run_id>", methods=["GET", "POST"])
    def run_detail(run_id: int):
        active, _ = current()
        run = active.collection.runs.get_run(run_id)
        if run is None:
            abort(404)
        if request.method == "POST":
            try:
                refresh_seconds = int(request.form.get("refresh_seconds", ""))
                if not 0 <= refresh_seconds <= 900:
                    raise ValueError
            except ValueError:
                flash("Refresh rate must be a whole number from 0 to 900 seconds.", "error")
            else:
                session["run_refresh_seconds"] = refresh_seconds
            return redirect(url_for(
                "run_detail", run_id=run_id,
                item_page=_page_number(request.form.get("item_page")),
                error_page=_page_number(request.form.get("error_page")),
            ), code=303)
        queries = ArchiveQueries(active.database)
        item_page_number = _page_number(request.args.get("item_page"))
        error_page_number = _page_number(request.args.get("error_page"))
        item_page = queries.run_items(
            run_id,
            limit=PAGE_SIZE,
            offset=(item_page_number - 1) * PAGE_SIZE,
        )
        error_page = queries.run_errors(
            run_id,
            limit=PAGE_SIZE,
            offset=(error_page_number - 1) * PAGE_SIZE,
        )
        stages = [_present_stage(item) for item in queries.run_stages(run_id)]
        work_items = [_present_run_item(active, item) for item in item_page.rows]
        presented = _present_run(run, queries=queries)
        # Keep the summary count identical to the Operations view linked from
        # this page: anomalies qualify when this run first or most recently
        # observed them.
        presented["anomaly_count"] = queries.operations(
            view="anomalies",
            run_id=run_id,
            limit=1,
            offset=0,
        ).total
        disk_free = _disk_free(active.config.archive_root)
        return render_template(
            "run_detail.html",
            run=presented,
            stages=stages,
            items=work_items,
            errors=error_page.rows,
            item_pagination=_pagination(
                item_page_number,
                item_page_number * PAGE_SIZE < item_page.total,
                request.args,
                total=item_page.total,
                page_key="item_page",
            ),
            error_pagination=_pagination(
                error_page_number,
                error_page_number * PAGE_SIZE < error_page.total,
                request.args,
                total=error_page.total,
                page_key="error_page",
            ),
            disk={
                "free": _human_bytes(disk_free),
                "floor": _human_bytes(active.config.minimum_free_space_bytes),
                "floor_gb": active.config.minimum_free_space_gb,
            },
            config_snapshot=_json_dict(run.get("config_snapshot_json")),
            elapsed=_elapsed(run),
            refresh_seconds=session.get("run_refresh_seconds", 15),
        )

    @app.post("/runs/<int:run_id>/pause")
    def pause_run(run_id: int):
        active, _ = current()
        if active.collection.runs.pause(run_id, "Paused by operator"):
            flash(
                f"Run #{run_id} is pausing. Active transfers will wind down safely.",
                "success",
            )
        else:
            flash(f"Run #{run_id} is not currently running.", "warning")
        return redirect(url_for("run_detail", run_id=run_id), code=303)

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
        try:
            requeued = active.collection.requeue_run(run_id)
        except ValueError as exc:
            flash(f"Run #{run_id} cannot be resumed: {exc}", "error")
            return redirect(url_for("run_detail", run_id=run_id), code=303)
        if requeued:
            workers.enqueue(run_id)
            flash(f"Run #{run_id} was queued to resume.", "success")
        else:
            flash(f"Run #{run_id} is not resumable.", "warning")
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.route("/retry-failures", methods=["GET", "POST"])
    def retry_failures():
        active, workers = current()
        run_id_text = request.values.get("run_id", "").strip()
        source_run_id = (
            int(run_id_text)
            if run_id_text.isdigit()
            else -1
            if run_id_text
            else None
        )
        filters = SimpleNamespace(
            run_id=run_id_text,
            session=request.values.get("session", "").strip().upper(),
            bill=request.values.get("bill", "").replace(" ", "").strip().upper(),
        )
        page = _page_number(request.values.get("page"))
        result = ArchiveQueries(active.database).retry_documents(
            run_id=source_run_id,
            session_key=filters.session or None,
            bill_id_compact=filters.bill or None,
            include_terminal=True,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        candidates = list(result.rows)
        if request.method == "POST":
            if request.form.get("action") == "all":
                try:
                    run_id, matching_count = active.collection.create_retry_matching_run(
                        source_run_id=source_run_id,
                        session_key=filters.session or None,
                        bill_id_compact=filters.bill or None,
                    )
                except (TypeError, ValueError) as exc:
                    flash(f"Matching retries were not queued: {exc}", "error")
                else:
                    workers.enqueue(run_id)
                    flash(
                        f"Retry run #{run_id} was queued for all "
                        f"{matching_count} matching document(s).",
                        "success",
                    )
                    return redirect(url_for("run_detail", run_id=run_id), code=303)
            else:
                selected_ids = [
                    int(value)
                    for value in request.form.getlist("document_ids")
                    if value.isdigit()
                ]
                allowed = {int(row["id"]) for row in candidates}
                selected_ids = list(
                    dict.fromkeys(value for value in selected_ids if value in allowed)
                )
                if not selected_ids:
                    flash("Select at least one document to retry.", "error")
                else:
                    try:
                        run_id = active.collection.create_retry_selected_run(
                            selected_ids,
                            source_run_id=source_run_id,
                        )
                    except (TypeError, ValueError) as exc:
                        flash(f"Selected retries were not queued: {exc}", "error")
                    else:
                        workers.enqueue(run_id)
                        flash(
                            f"Retry run #{run_id} was queued for "
                            f"{len(selected_ids)} document(s).",
                            "success",
                        )
                        return redirect(
                            url_for("run_detail", run_id=run_id), code=303
                        )
        presented = [_present_document(row) for row in candidates]
        stats = {
            "retryable_failures": _document_status_count(active, "failed_retryable"),
            "terminal_failures": _document_status_count(active, "failed_terminal"),
            "interrupted": _document_status_count(active, "interrupted"),
            "paused_low_space": _document_status_count(active, "paused_low_space"),
        }
        return render_template(
            "retry_failures.html",
            stats=stats,
            filters=filters,
            rows=presented,
            pagination=_pagination(
                page,
                page * PAGE_SIZE < result.total,
                request.args,
                total=result.total,
            ),
        )

    @app.get("/exports/sessions.csv")
    def export_sessions():
        active, _ = current()
        sql, params = ArchiveQueries(active.database).session_export_query()
        return _csv_response(active, sql, params, "legiview-session-inventory.csv")

    @app.get("/exports/documents.csv")
    def export_documents():
        active, _ = current()
        filters = {
            "session": request.args.get("session", "").strip().upper(),
            "bill": request.args.get("bill", "").replace(" ", "").strip().upper(),
            "kind": request.args.get("kind", "").strip(),
            "committee": request.args.get("committee", "").strip(),
            "submitter": request.args.get("submitter", "").strip(),
            "organization": request.args.get("organization", "").strip(),
            "position": request.args.get("position", "").strip(),
            "download_status": request.args.get("download_status", "").strip(),
            "source_presence": request.args.get("source_presence", "").strip(),
            "displayed_in_olis": request.args.get("displayed_in_olis", "").strip() or "any",
            "failed_only": request.args.get("failed_only") == "1",
        }
        sql, params = ArchiveQueries(active.database).document_export_query(filters)
        return _csv_response(active, sql, params, "legiview-document-inventory.csv")

    @app.get("/exports/operations.csv")
    def export_operations():
        active, _ = current()
        view = request.args.get("view", "errors").strip()
        if view not in {"all", "errors", "anomalies"}:
            abort(400, description="Unknown operations export view.")
        filters = _operation_request_filters(request.args, view=view)
        sql, params = ArchiveQueries(active.database).operations_export_query(
            view=view,
            **_operation_query_filters(filters),
        )
        return _csv_response(active, sql, params, "legiview-failures-anomalies.csv")

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        active, _ = current()
        if request.method == "POST":
            submitted = {key: request.form.get(key, "").strip() for key in SETTING_FIELDS}
            try:
                validated = active.config.with_settings(submitted)
                # Settings validation is deliberately read-only. The next
                # locked startup creates the ownership marker only after the
                # configured path has been accepted as a dedicated archive.
                validate_archive_root_candidate(validated.archive_root)
            except (OSError, TypeError, ValueError) as exc:
                flash(f"Settings were not saved: {exc}", "error")
            else:
                snapshot = validated.snapshot()
                for key in SETTING_FIELDS:
                    # Archive paths saved by the UI remain relative and
                    # portable when that is what the operator entered. The
                    # runtime resolves them against the bootstrap project root.
                    value = (
                        validated.archive_root_configured
                        if key == "archive_root"
                        else snapshot[key]
                    )
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
    rows: list[dict[str, Any]] = []
    for row in runtime.storage.list_sessions():
        try:
            require_supported_session_key(str(row.get("session_key") or ""))
        except ValueError:
            continue
        rows.append(row)
    known = {str(row["session_key"]).upper() for row in rows}
    # These are the two sessions explicitly validated in the source spikes.
    # Typed values still pass the same central 2014+ guard before run creation.
    for key, name in (
        ("2026R1", "2026 Regular Session"),
        ("2014R1", "2014 Regular Session"),
    ):
        if key not in known:
            rows.append({"session_key": key, "session_name": name})
    return rows


def _official_session_rows(
    scope: Any,
    stored: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    supported_keys = set(scope.supported_session_keys)
    boundary = scope.boundary_session
    for session_record in scope.sessions:
        supported = session_record.session_key in supported_keys
        row = {
            "session_key": session_record.session_key,
            "session_name": session_record.session_name,
            "begin_date": session_record.begin_date,
            "end_date": session_record.end_date,
            "inventory_status": "not_started",
        }
        row.update(stored.get(session_record.session_key, {}))
        # Archive state comes from SQLite, but the Resolve action must present
        # the catalogue metadata just returned by the official source.  A
        # previously inventoried row may otherwise hide a corrected name or
        # date and make the discovery UI look stale.
        row.update(
            {
                "session_key": session_record.session_key,
                "session_name": session_record.session_name,
                "begin_date": session_record.begin_date,
                "end_date": session_record.end_date,
            }
        )
        row["supported"] = supported
        row["support_reason"] = (
            None
            if supported
            else session_record.compatibility_issue
            or (
                f"Begins before the validated {scope.boundary_key} boundary "
                f"({boundary.begin_date})"
            )
        )
        rows.append(row)
    # Both the table and range dropdowns present the newest official session
    # first, while SessionScope itself retains ascending run chronology.
    rows.reverse()
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


def _present_run(
    row: Mapping[str, Any], *, queries: ArchiveQueries | None = None
) -> dict[str, Any]:
    result = dict(row)
    scope = _json_dict(row.get("requested_scope_json"))
    if row.get("run_type") == "download_archive" and queries is not None:
        result.update(queries.download_run_counts(int(row["id"])))
        preflight = scope.get("preflight") or {}
        selected = int(preflight.get("pending_or_missing") or 0)
        if not scope.get("retryable_failures_only"):
            selected += int(preflight.get("already_downloaded") or 0)
        result["documents_discovered"] = max(selected, result["documents_recorded"])
        row = result
    session = row.get("requested_session_key") or scope.get("session_key")
    bill = row.get("requested_bill_id_compact") or scope.get("bill_id_compact")
    source_run = scope.get("source_run_id")
    retry_match = scope.get("retry_match")
    session_keys = scope.get("session_keys")
    if isinstance(retry_match, dict):
        count = retry_match.get("matching_count")
        display = (
            f"All {count} matching failed documents"
            if count
            else "All matching failed documents"
        )
    elif isinstance(session_keys, list) and session_keys:
        if len(session_keys) == 1:
            display = str(session_keys[0])
        else:
            display = f"{len(session_keys)} sessions ({session_keys[0]} through {session_keys[-1]})"
        kinds = scope.get("document_kinds")
        if row.get("run_type") == "download_archive" and kinds:
            display += " / " + ", ".join(
                str(kind).replace("_", " ") for kind in kinds
            )
    elif session and bill:
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
        items_total=row.get("sessions_total") or row.get("bills_total"),
        items_completed=(
            row.get("sessions_completed")
            if row.get("run_type") in {"inventory_backfill", "download_archive"}
            else row.get("bills_completed")
        ),
        errors=row.get("error_count"),
        skipped_count=row.get("documents_skipped"),
        cancel_requested=row.get("status") == "canceled",
        scope_cutoff_at=row.get("scope_cutoff_at") or scope.get("scope_cutoff_at"),
        probe_remote_sizes=scope.get("probe_remote_sizes", False),
        force_full=scope.get("force_full", False),
        session_keys=session_keys if isinstance(session_keys, list) else [],
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
    details = _json_dict(item.get("details_json"))
    counts = details.get("counts")
    if not isinstance(counts, dict):
        download_counts = details.get("download_counts")
        counts = download_counts if isinstance(download_counts, dict) else {}
    preferred_counts = (
        ("bills", "bills"),
        ("documents", "documents"),
        ("sponsors", "sponsors"),
        ("committees", "committees"),
        ("committee_documents", "committee documents"),
        ("public_testimony", "public testimony"),
        ("floor_letters", "floor letters"),
        ("olis_pages_checked", "OLIS pages checked"),
        ("olis_pages_failed", "OLIS pages failed"),
        ("olis_pages_anomalous", "OLIS anomalies"),
        ("probes_known", "known-size probes"),
        ("probes_unknown", "unknown-size probes"),
        ("probes_failed", "probe failures"),
        ("completed", "downloaded"),
        ("skipped", "skipped"),
        ("failed_retryable", "retryable failures"),
        ("failed_terminal", "terminal failures"),
    )
    result["detail_summary"] = ", ".join(
        f"{counts[key]} {label}" for key, label in preferred_counts if key in counts
    )
    result["inventory_status"] = details.get("inventory_status")
    if item.get("document_id"):
        if item.get("document_title") or item.get("source_document_id"):
            result["label"] = item.get("document_title") or f"Document {item.get('source_document_id')}"
        else:
            document = runtime.storage.get_document(int(item["document_id"]))
            if document:
                result["document_title"] = document.get("title")
                result["source_document_id"] = document.get("source_id")
                result["label"] = document.get("title") or f"Document {document.get('source_id')}"
    elif item.get("bill_id"):
        if item.get("bill_id_display"):
            result["label"] = item.get("bill_id_display")
        else:
            bill = runtime.storage.get_bill_by_id(int(item["bill_id"]))
            if bill:
                result["bill_id_display"] = bill.get("bill_id_display")
                result["label"] = bill.get("bill_id_display")
    elif item.get("session_key"):
        result["label"] = item.get("session_key")
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


def _official_source_url(value: Any) -> str | None:
    """Return a clickable official URL only after strict origin validation."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in OFFICIAL_SOURCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return candidate


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _document_status_count(runtime: Runtime, status: str) -> int:
    with runtime.database.connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM documents WHERE download_status=?", (status,)).fetchone()[0])


def _operation_request_filters(
    args: Mapping[str, Any], *, view: str
) -> SimpleNamespace:
    run_text = str(args.get("run", "")).strip()
    return SimpleNamespace(
        view=view,
        run_id=int(run_text) if run_text.isdigit() else None,
        session=str(args.get("session", "")).strip().upper(),
        bill=str(args.get("bill", "")).replace(" ", "").strip().upper(),
        stage=str(args.get("stage", "")).strip(),
        kind=str(args.get("kind", "")).strip(),
        retryable=str(args.get("retryable", "")).strip(),
        error_class=str(args.get("error_class", "")).strip(),
        anomaly_type=str(args.get("anomaly_type", "")).strip(),
        severity=str(args.get("severity", "")).strip(),
        include_resolved=str(args.get("include_resolved", "")) == "1",
    )


def _operation_query_filters(filters: SimpleNamespace) -> dict[str, Any]:
    return {
        "run_id": filters.run_id,
        "session_key": filters.session or None,
        "bill_id_compact": filters.bill or None,
        "stage_or_entity": filters.stage or None,
        "document_kind": filters.kind or None,
        "retryable": (
            True if filters.retryable == "yes"
            else False if filters.retryable == "no"
            else None
        ),
        "error_class": filters.error_class or None,
        "anomaly_type": filters.anomaly_type or None,
        "severity": filters.severity or None,
        "unresolved_only": not filters.include_resolved,
    }


def _csv_response(
    runtime: Runtime,
    sql: str,
    params: list[Any],
    filename: str,
) -> Response:
    response = Response(
        stream_with_context(stream_query_csv(runtime.database, sql, params)),
        mimetype="text/csv",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _disk_free(path: str | Path) -> int:
    try:
        return int(shutil.disk_usage(Path(path)).free)
    except OSError:
        return 0


def _pagination(
    page: int,
    has_next: bool,
    args: Mapping[str, Any],
    *,
    total: int | None = None,
    page_key: str = "page",
) -> SimpleNamespace:
    endpoint = request.endpoint
    if endpoint is None:  # Defensive: every current caller runs in a Flask route.
        raise RuntimeError("Pagination requires an active Flask endpoint")
    route_values = dict(request.view_args or {})

    # Resolve only trusted route values through url_for. Query-string keys are
    # encoded separately so names such as a route parameter, ``_external``, or
    # ``_method`` remain ordinary query data and cannot change URL generation.
    if callable(getattr(args, "lists", None)):
        query_pairs = [
            (str(key), str(value))
            for key, values in args.lists()
            if key != page_key
            for value in values
        ]
    else:
        query_pairs = [
            (str(key), str(value))
            for key, value in args.items()
            if key != page_key
        ]
    base_url = url_for(endpoint, **route_values)

    def page_url(target_page: int) -> str:
        query = urlencode([*query_pairs, (page_key, str(target_page))])
        return f"{base_url}?{query}"

    previous_url = page_url(page - 1) if page > 1 else None
    next_url = page_url(page + 1) if has_next else None
    pages = math.ceil(total / PAGE_SIZE) if total is not None and total else None
    return SimpleNamespace(
        page=page,
        total=total,
        pages=pages,
        prev_url=previous_url,
        previous_url=previous_url,
        next_url=next_url,
        first_url=page_url(1) if page > 1 else None,
        last_url=page_url(pages) if pages and page < pages else None,
    )


def _page_number(value: str | None) -> int:
    try:
        return min(1_000_000, max(1, int(value or 1)))
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
    try:
        return normalize_bill_id(text)[3]
    except InvalidBillId:
        # Historical run/error rows may retain an invalid source identity for
        # diagnosis; display it verbatim rather than mis-splitting the prefix.
        return text


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
