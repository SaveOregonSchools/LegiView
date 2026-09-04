# LegiView

LegiView is a local Flask application for building a durable Oregon legislative data
and document archive. It catalogs every official Oregon HB/SB from session `2014R1`
forward, reconciles testimony and historical presentations, and can then acquire
validated payloads into ordinary files under a deterministic archive tree.

Phase 2 extends the proven targeted bill/session collector with two separate,
resumable workflows:

- **Inventory Backfill** discovers and reconciles metadata without downloading every
  payload.
- **Download Archive** consumes a frozen slice of that inventory and safely downloads
  eligible current payloads.

Source access, parsing, persistence, downloading, workers, and Flask routes remain
separate. The web UI and CLI use the same durable collection services.

## Before using Oregon legislative data

Read the official [Oregon Legislative Data page](https://www.oregonlegislature.gov/citizen_engagement/Pages/data.aspx)
and [Open Data acceptable-use agreement](https://www.oregonlegislature.gov/citizen_engagement/Documents/OLODataAcceptableUseAgreement.pdf)
before running a collection.

The 2026-09-03 source spike and operator review found that ordinary filtered requests
reached the published OData service without an HTML form, redirect, acceptance
cookie, separate submission form, or other interactive gate. The operator explicitly
confirmed that they reviewed and agreed to the published terms; no separate form to
submit was found. The linked agreement nevertheless describes electronic acceptance.
Before live collection, review and agree to the published terms; if the Legislature
presents an official acceptance mechanism, use it. LegiView does not accept terms or
bypass a gate for you.

Prefer OData, retain the conservative concurrency and delay defaults, honor
429/`Retry-After` and all source instructions, and never use proxies, alternate-host
evasion, CAPTCHA bypass, or rate-limit evasion. The current published agreement
allows on-demand and incremental requests but limits whole-database/full refresh
behavior to once per day after business hours, stated as **5:00 p.m.–6:00 a.m.
Pacific**. Treat an all-history inventory as a full refresh and schedule it in that
window.

See [docs/source_mapping.md](docs/source_mapping.md) for observed source behavior.

## Requirements

- Python 3.11 or newer on Windows, Linux, or macOS
- network access to the official Oregon Legislature OData and OLIS hosts
- enough free disk for the selected archive scope

### Windows setup

From PowerShell in the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Start the application with:

```powershell
.\run.bat
```

or:

```powershell
.\.venv\Scripts\python.exe -m olis_archive serve
```

### Linux/macOS setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
cp .env.example .env
.venv/bin/python -m olis_archive serve
```

Then open <http://127.0.0.1:5000/>. LegiView binds only to localhost by default.
Use `serve --host` for another loopback address and `--port` for an explicit port.
Put global `--verbose` before the command when informational logs are useful. The
local web app accepts only loopback Host headers by default, which also protects
against forged-host and DNS rebinding requests. Non-loopback hosting is outside the
Phase 2 deployment scope.

## Configuration

The checked-in `.env.example` uses portable relative paths:

```dotenv
# Copy to .env or set these variables in your shell.
# Optional base for relative paths. If omitted, LegiView uses the application root.
# LEGIVIEW_PROJECT_ROOT=.

LEGIVIEW_DATABASE_PATH=data/legiview.sqlite3
LEGIVIEW_ARCHIVE_ROOT=archive

LEGIVIEW_REQUEST_TIMEOUT=30
LEGIVIEW_ODATA_WORKERS=1
LEGIVIEW_DOWNLOAD_WORKERS=2
LEGIVIEW_HTML_CONCURRENCY=1
LEGIVIEW_MIN_FREE_SPACE_GB=5
LEGIVIEW_INTER_REQUEST_DELAY=0.25
LEGIVIEW_HOST=127.0.0.1
LEGIVIEW_PORT=5000
LEGIVIEW_DEBUG=0
```

Relative database/archive paths resolve against the effective
`LEGIVIEW_PROJECT_ROOT`, never the caller's working directory. With no overrides the
database is `<project_root>/data/legiview.sqlite3` and the archive is
`<project_root>/archive`. Absolute Windows/POSIX paths remain supported.

The free-space setting is in GB, with one LegiView GB equal to `1024 ** 3` bytes.
`LEGIVIEW_MIN_FREE_SPACE_GB` wins when present; otherwise the deprecated
`LEGIVIEW_MIN_FREE_SPACE_BYTES` and legacy saved setting are converted for backward
compatibility. Downloads pause before crossing the floor.

The Settings page stores portable archive input, shows its resolved path and the
effective project/database paths, and applies worker/archive changes after restart.
See [docs/configuration.md](docs/configuration.md) for precedence and all variables.

## Inventory Backfill

Inventory Backfill resolves the official session catalogue and includes every
session at or after the official `2014R1` `BeginDate`, including short and special
sessions. Only HB/SB measures are in scope. The exact selected keys are frozen in the
durable run.

It pages and persists sessions, measures, references, sponsors, committee context,
committee documents, `CommitteePublicTestimonies`, floor letters, normalized document
identities, OLIS display reconciliation, source presence, and anomalies. Optional
HEAD/bounded-Range probes estimate remote sizes. It never silently starts payload
download.

In the UI:

1. Open **Inventory Backfill** (`/inventory-backfill`) and select **Resolve official
   sessions**.
2. Review the returned session list, completeness state, paths, disk space, source
   limits, and acceptable-use reminder.
3. Keep all sessions selected, or choose a validation subset.
4. Optionally choose **Probe remote sizes**.
5. Use **Force an authoritative full session comparison** only when a deliberate
   disappearance check is needed; it ignores retained watermarks and is subject to
   the published full-refresh window and daily limit.
6. Press **Start Inventory Backfill** and follow the durable Run Detail.
7. Review **Session Status**, documents, failures, and anomalies afterward.

From the CLI, omitted session options mean the complete resolved scope. These and the
remaining CLI examples use the Windows virtual-environment executable; on Linux/macOS,
replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`:

```powershell
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill --session 2014R1 --session 2026R1 --probe-remote-sizes
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill --session 2014R1 --force-full
```

## Download Archive

Download Archive operates only on inventoried records. Its session keys, optional
document kinds/status mode, and inventory cutoff are frozen when the run is created.
Rows are claimed atomically from SQLite in bounded batches; LegiView does not build an
all-history in-memory queue.

In the UI:

1. Open **Download Archive** (`/download-archive`) after reviewing **Session Status**
   (`/session-status`).
2. Choose all inventoried or selected sessions, and optionally document kinds or the
   retryable-failure mode.
3. Select **Preview selected scope**, then review recorded-downloaded, pending,
   failure, non-downloadable, known-byte,
   unknown-size, disk-free, floor, archive-root, and worker-count preflight values.
   The preflight is SQL-only: it reports recorded state without opening or hashing
   every archived file. A normal Download Archive run validates recorded current
   files in bounded background work and skips them only when their bytes remain
   valid. Retryable-failures-only mode deliberately does not audit healthy recorded
   downloads.
4. Press **Start Download Archive** explicitly.
5. Use Run Detail to pause, cancel, or resume. Completed files are always retained.

From the CLI, first inspect the same scope without mutating it, then start the run:

```powershell
.\.venv\Scripts\python.exe -m olis_archive archive-preflight
.\.venv\Scripts\python.exe -m olis_archive download-archive
.\.venv\Scripts\python.exe -m olis_archive archive-preflight --session 2014R1
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2014R1
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2026R1 --kind public_testimony --kind floor_letter
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2026R1 --retryable-failures-only
```

See [docs/historical_backfill.md](docs/historical_backfill.md) for the complete
workflow and [docs/completeness.md](docs/completeness.md) for session-state meanings.

## Targeted collection and CLI

The Phase 1 tools remain supported for maintenance and debugging:

```powershell
# Collect one bill, including eligible payloads.
.\.venv\Scripts\python.exe -m olis_archive collect-bill 2026R1 SB1501

# Collect one explicit session; limit during source validation when useful.
.\.venv\Scripts\python.exe -m olis_archive collect-session 2026R1
.\.venv\Scripts\python.exe -m olis_archive collect-session 2026R1 --max-bills 10

# Display a stored bill, sponsors, and documents as JSON.
.\.venv\Scripts\python.exe -m olis_archive show-bill 2026R1 SB1501

# Resume one interrupted or low-space-paused run.
.\.venv\Scripts\python.exe -m olis_archive resume-run 12

# Explicitly retry failed documents from a targeted Phase 1 bill/session run.
.\.venv\Scripts\python.exe -m olis_archive retry-failures --run-id 12

# Historical bulk retries remain bounded and SQL-backed through Download Archive.
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2026R1 --retryable-failures-only
```

Bulk retryable-only mode excludes terminal failures. To deliberately reattempt a
terminal validation failure, review and select that record on the **Retry Failures**
page; it will remain terminal if validation fails again. The targeted Phase 1
`retry-failures --run-id` command can also include terminal failures from that
targeted source run, but it intentionally rejects Phase 2 historical bulk run IDs.

Use `.\.venv\Scripts\python.exe -m olis_archive --help` (or the POSIX equivalent)
or a subcommand's `--help` for exact options. A
mutating CLI command creates durable state and holds the same exclusive mutation lock
as the web app. It returns success only for a clean completed outcome; partial/error
summaries remain in SQLite and produce a nonzero shell status.

## Storage and file safety

SQLite stores source metadata and raw values, sync/completeness state, source
presence, display reconciliation, anomalies, probes, settings, logical documents,
immutable versions, runs/items/stages, counters, and errors. Payloads use:

```text
<archive_root>/<session>/<bill>/<document_kind>/<source_numeric_id>/<filename>
```

Only relative archive paths are stored. Downloads use official-host and redirect
validation, free-space reservations, streaming `.part` files, length/type/signature
validation, SHA-256, flush, and atomic no-replace promotion. Equal content reuses its
retained version; changed bytes create `__v0002` and later immutable suffixes. No
successful source comparison, cancel, retry, or low-space event deletes records or
previous payloads.

The archive root must be dedicated to LegiView. A `.legiview-archive-root` marker
authorizes recursive startup maintenance; empty roots are initialized, recognizable
legacy LegiView trees are adopted once, and arbitrary nonempty directories are
rejected without cleanup.

Before serving a registered local file, the UI rechecks its path, filename, type,
size, and hash. Mutating forms require a session-bound CSRF token.

**Downloaded files are untrusted.** LegiView never executes them and does not provide
antivirus scanning, Office automation, OCR, or content extraction. See
[docs/archive_layout.md](docs/archive_layout.md).

## Pause, restart, resume, and retry

Run state is durable. Startup verifies the dedicated archive ownership marker,
normalizes falsely active runs/items/downloads to `interrupted`, removes incomplete
`.part` files only beneath that owned archive root, and enqueues work that was already
queued. An interrupted run requires an explicit Resume. Already completed session
items and valid files are skipped.

Pause/cancel stops new historical document claims and retains completed work.
Duplicate Resume requests cannot multiply work. Retry Failures is a separate explicit
attempt cycle; terminal validation failures such as the known zero-byte upstream
testimony are never retried forever automatically.

Historical OLIS display reconciliation pauses the same durable inventory run after
three consecutive retryable page failures. This circuit breaker limits requests
during a broader source outage. Once source access is healthy, explicitly resume the
same run; completed session/entity work is retained.

Every mutating process uses an OS lock beside SQLite. A second writer exits rather
than racing startup recovery or archive changes. See
[docs/recovery.md](docs/recovery.md).

## Browse, status, operations, and exports

The dashboard summarizes historical scope and archive health. **Session Status** at
`/session-status` gives per-session counts and drill-downs. Consolidated
**Operations** at `/operations` separates Errors and Anomalies and supplies SQL
filters. Bill, document, run, session, error, and anomaly lists are paginated for
historical scale. CSV exports stream rather than materializing the corpus in Python:

- `/exports/sessions.csv` for session inventory;
- `/exports/documents.csv` for document inventory, honoring Browse Documents filters;
- `/exports/operations.csv?view=all|errors|anomalies` for operational review.

When probe sizes are incomplete, known remote bytes are labeled a **lower bound**.
Inventory completeness and payload archive completeness are reported separately.

## Tests and validation

The default automated suite is offline:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```bash
.venv/bin/python -m pytest -q
```

Live tests are explicitly gated and must follow the acceptable-use agreement. See
[docs/phase2_validation.md](docs/phase2_validation.md) for automated, controlled-live,
and whole-history acceptance evidence. Phase 1's original source/run ledger remains
in [docs/phase1_validation.md](docs/phase1_validation.md).

## Phase 2 boundaries

LegiView remains a local, single-mutation-owner application. Phase 2 does not add a
recurring scheduler, sist2 process/index management, OCR, AI/LLM analysis, semantic
classification, natural-language search, graphs/scoring, an antivirus pipeline,
PostgreSQL, Redis, Celery, multi-user authentication, cloud hosting, or destructive
mirroring. The archive remains ordinary files suitable for an external sist2 index.

## License

LegiView's software code is copyright (C) 2026 Save Oregon Schools, LLC and licensed
under the GNU Affero General Public License version 3. See [LICENSE](LICENSE).

LegiView is distributed without any warranty, including the implied warranties of
merchantability or fitness for a particular purpose.

The Save Oregon Schools name, logo, and related branding are not licensed for reuse
under the AGPL. See [TRADEMARKS.md](TRADEMARKS.md).
