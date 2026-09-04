"""Command-line interface backed by the same collection service as Flask."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .runtime import InstanceAlreadyRunning, build_runtime
from .services.odata import ODataError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m olis_archive",
        description="LegiView legislative archive",
    )
    parser.add_argument("--verbose", action="store_true", help="show informational application logs")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="start the local Flask interface")
    serve.add_argument("--host", help="override the configured bind host")
    serve.add_argument("--port", type=int, help="override the configured port")

    bill = commands.add_parser("collect-bill", help="collect and archive one HB or SB")
    bill.add_argument("session_key")
    bill.add_argument("bill_id")

    session = commands.add_parser("collect-session", help="collect one explicitly selected session")
    session.add_argument("session_key")
    session.add_argument("--max-bills", type=int)

    retry = commands.add_parser("retry-failures", help="explicitly retry failed downloads from a run")
    retry.add_argument("--run-id", type=int, required=True)

    inventory = commands.add_parser(
        "inventory-backfill",
        help="inventory all official sessions since 2014R1 or selected sessions",
    )
    inventory.add_argument(
        "--session",
        dest="session_keys",
        action="append",
        help="official session key to include; repeat as needed (default: full resolved scope)",
    )
    inventory.add_argument(
        "--probe-remote-sizes",
        action="store_true",
        help="perform bounded HEAD/Range size probes after inventory",
    )
    inventory.add_argument(
        "--force-full",
        action="store_true",
        help="perform authoritative full session comparisons instead of incremental watermarks",
    )

    preflight = commands.add_parser(
        "archive-preflight", help="show a read-only Download Archive estimate"
    )
    archive = commands.add_parser(
        "download-archive", help="download eligible payloads from the durable inventory"
    )
    for command in (preflight, archive):
        command.add_argument(
            "--session",
            dest="session_keys",
            action="append",
            help="inventoried session key to include; repeat as needed (default: all inventoried)",
        )
        command.add_argument(
            "--kind",
            dest="document_kinds",
            action="append",
            choices=(
                "public_testimony",
                "legacy_testimony",
                "committee_presentation",
                "floor_letter",
                "committee_document_other",
                "unknown",
            ),
            help="document kind to include; repeat as needed",
        )
        command.add_argument(
            "--retryable-failures-only",
            action="store_true",
            help="select only prior retryable/interrupted/changed/missing-local work",
        )

    show = commands.add_parser("show-bill", help="show a stored bill and its related records")
    show.add_argument("session_key")
    show.add_argument("bill_id")

    resume = commands.add_parser("resume-run", help="resume an interrupted or low-space-paused run")
    resume.add_argument("run_id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "serve":
        from .web import create_app

        app = create_app({"START_WORKER": True})
        runtime = app.extensions["legiview"]["runtime"]
        app.run(
            host=args.host or runtime.config.host,
            port=args.port or runtime.config.port,
            debug=runtime.config.debug,
            use_reloader=False,
        )
        return 0

    read_only = args.command in {"show-bill", "archive-preflight"}
    try:
        runtime = build_runtime(
            normalize_interrupted=not read_only,
            clean_parts=not read_only,
            exclusive=not read_only,
        )
    except InstanceAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "collect-bill":
        run_id = runtime.collection.create_collect_bill_run(args.session_key, args.bill_id)
        return _execute_and_print(runtime.collection, run_id)

    if args.command == "collect-session":
        run_id = runtime.collection.create_collect_session_run(args.session_key, max_bills=args.max_bills)
        return _execute_and_print(runtime.collection, run_id)

    if args.command == "retry-failures":
        try:
            run_id = runtime.collection.create_retry_failures_run(args.run_id)
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return _execute_and_print(runtime.collection, run_id)

    if args.command == "inventory-backfill":
        if not args.session_keys:
            print(
                "Reminder: an all-history inventory is a full refresh. Run it only "
                "within the Oregon Legislature's published 5:00 p.m.–6:00 a.m. "
                "Pacific window and no more than once per day.",
                file=sys.stderr,
            )
        elif args.force_full:
            print(
                "Reminder: --force-full ignores retained watermarks. Follow the "
                "Oregon Legislature's published full-refresh window and daily limit.",
                file=sys.stderr,
            )
        try:
            run_id = runtime.collection.create_inventory_backfill_run(
                args.session_keys,
                probe_remote_sizes=args.probe_remote_sizes,
                force_full=args.force_full,
            )
        except (ValueError, ODataError) as exc:
            print(f"Inventory Backfill could not be created: {exc}", file=sys.stderr)
            return 2
        return _execute_and_print(runtime.collection, run_id)

    if args.command == "archive-preflight":
        try:
            result = runtime.collection.download_archive_preflight(
                args.session_keys,
                document_kinds=args.document_kinds,
                retryable_failures_only=args.retryable_failures_only,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False, default=str))
        return 0 if result.known_bytes_fit else 1

    if args.command == "download-archive":
        try:
            preflight_result = runtime.collection.download_archive_preflight(
                args.session_keys,
                document_kinds=args.document_kinds,
                retryable_failures_only=args.retryable_failures_only,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("Download Archive preflight:", file=sys.stderr)
        print(
            json.dumps(preflight_result.as_dict(), indent=2, ensure_ascii=False, default=str),
            file=sys.stderr,
        )
        if preflight_result.unknown_size_pending:
            print(
                "Warning: the known-byte estimate is a lower bound because one or "
                "more eligible payload sizes are unknown.",
                file=sys.stderr,
            )
        if not preflight_result.known_bytes_fit:
            print(
                "Download Archive was not started because known pending bytes would "
                "cross the configured free-space floor.",
                file=sys.stderr,
            )
            return 1
        try:
            run_id = runtime.collection.create_download_archive_run(
                args.session_keys,
                document_kinds=args.document_kinds,
                retryable_failures_only=args.retryable_failures_only,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return _execute_and_print(runtime.collection, run_id)

    if args.command == "resume-run":
        if not runtime.collection.runs.requeue(args.run_id):
            print(f"Run #{args.run_id} is not interrupted or paused.", file=sys.stderr)
            return 2
        return _execute_and_print(runtime.collection, args.run_id)

    if args.command == "show-bill":
        from .services.source_mapping import normalize_bill_id, normalize_session_key

        session_key = normalize_session_key(args.session_key)
        _, _, compact, _ = normalize_bill_id(args.bill_id)
        bill = runtime.storage.get_bill(session_key, compact)
        if bill is None:
            print(f"No stored bill found for {session_key} / {compact}.", file=sys.stderr)
            return 1
        payload: dict[str, Any] = {
            "bill": bill,
            "sponsors": runtime.storage.list_bill_sponsors(int(bill["id"])),
            "documents": runtime.storage.list_bill_documents(int(bill["id"])),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    return 2


def _execute_and_print(collection, run_id: int) -> int:  # noqa: ANN001
    print(f"Created durable run #{run_id}.", file=sys.stderr)
    status = collection.execute_run(run_id)
    run = collection.runs.get_run(run_id) or {"id": run_id, "status": status}
    payload = {
        "run_id": run_id,
        "status": status,
        "stage": run.get("stage"),
        "bills_completed": run.get("bills_completed", 0),
        "sessions_total": run.get("sessions_total", 0),
        "sessions_completed": run.get("sessions_completed", 0),
        "sessions_incomplete": run.get("sessions_incomplete", 0),
        "sessions_failed": run.get("sessions_failed", 0),
        "documents_discovered": run.get("documents_discovered", 0),
        "documents_downloaded": run.get("documents_downloaded", 0),
        "documents_skipped": run.get("documents_skipped", 0),
        "documents_failed": run.get("documents_failed", 0),
        "bytes_downloaded": run.get("bytes_downloaded", 0),
        "error_count": run.get("error_count", 0),
    }
    print(json.dumps(payload, indent=2))
    # A partial archive is useful and remains inspectable, but it is not a
    # successful outcome for shell automation.  Callers can distinguish a
    # clean collection without parsing the JSON payload.
    return 0 if status == "completed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
