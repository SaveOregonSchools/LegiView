# LegiView

LegiView is a local Flask application for collecting Oregon House and Senate bill
metadata and in-scope documents from the Oregon Legislative Information System
(OLIS). Phase 1 supports one explicitly selected bill or session, stores structured
records and durable run state in SQLite, and archives validated document payloads in
a deterministic filesystem hierarchy.

The interface follows Save Oregon Schools' EdScanner visual language. Collection,
parsing, persistence, downloading, and job execution live outside the Flask routes,
so the web UI and CLI use the same services.

## Before using Oregon Legislative data

Read the official [Oregon Legislative Data page](https://www.oregonlegislature.gov/citizen_engagement/Pages/data.aspx)
and [Open Data acceptable-use agreement](https://www.oregonlegislature.gov/citizen_engagement/Documents/OLODataAcceptableUseAgreement.pdf)
before running a collection.

The Phase 1 source spike found that the published OData endpoint accepted ordinary
filtered requests without presenting an HTML form, redirect, cookie, or other
interactive acceptance gate. The linked agreement nevertheless says that users must
electronically accept its terms. LegiView does not accept terms, automate an
acceptance step, or bypass a gate for you. If the Legislature presents an acceptance
step, stop and complete it yourself through the official process before continuing.

Keep the conservative defaults, prefer individual bill collection, use
`--max-bills` while validating a session, honor any service response or restriction,
and do not use LegiView to evade throttling. See
[docs/source_mapping.md](docs/source_mapping.md) for the observed source behavior.

## Requirements and setup

- Windows with Python 3.11 or newer
- Network access to the official Oregon Legislature OData and OLIS hosts
- Enough free disk space for the selected documents

From PowerShell in the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Edit `.env` before the first run. In particular, put the archive and database in
dedicated locations that are normally outside this Git checkout:

```dotenv
LEGIVIEW_ARCHIVE_ROOT=C:\LegiViewArchive
LEGIVIEW_DATABASE_PATH=C:\LegiViewData\legiview.sqlite3
```

The application creates those directories when possible. Do not choose a filesystem
root such as `C:\` as the archive root. `.env`, SQLite files, the default `data/`
directory, archives, and `.part` files are ignored by Git.

LegiView uses an operating-system lock beside the SQLite database for every process
that can mutate collection state or archive files. A second web server or mutating
CLI command against the same database exits instead of running startup recovery
concurrently. The read-only `show-bill` command does not take that lock and is safe to
run while the web server is collecting. On shutdown, waiting work stays durably
queued and active work is marked interrupted; the lock is not released while a
worker can still mutate files.

## Start the web application

The standard Windows command is:

```powershell
.\run.bat
```

Equivalently:

```powershell
.\.venv\Scripts\python.exe -m olis_archive serve
```

Then open <http://127.0.0.1:5000/>. LegiView binds only to `127.0.0.1` by default.
The explicit overrides are:

```powershell
.\.venv\Scripts\python.exe -m olis_archive serve --host 127.0.0.1 --port 5000
```

Put the global `--verbose` option before the command to show informational logs:

```powershell
.\.venv\Scripts\python.exe -m olis_archive --verbose serve
```

## Collect a bill in the UI

1. Open **Settings** and confirm the archive root, timeouts, worker counts, request
   delay, and minimum free-space floor.
2. Open **Collect a Bill**.
3. Enter an official session key such as `2026R1` and an `HB` or `SB` identifier such
   as `SB1501`.
4. Submit the form. The request only creates and enqueues a durable run; source access
   and downloads occur in bounded background workers.
5. Follow the Run Detail page. An active page refreshes every 15 seconds from
   persisted stage and item records.
6. Use **Browse Bills** and **Browse Documents** to inspect the stored result. Bill
   Detail separates chief and regular sponsors and groups documents by source class.

Collect Session uses the same engine. A session can be large, so begin with a small
**Maximum bills** value.

## Command-line interface

The CLI writes a durable run before doing work and prints a JSON result summary.
Unlike the web UI, collection commands wait for the run to finish in the current
terminal.

```powershell
# Collect one bill.
.\.venv\Scripts\python.exe -m olis_archive collect-bill 2026R1 SB1501

# Collect an explicitly selected session.
.\.venv\Scripts\python.exe -m olis_archive collect-session 2026R1

# Limit a session run during validation.
.\.venv\Scripts\python.exe -m olis_archive collect-session 2026R1 --max-bills 10

# Display a stored bill, sponsors, and documents as JSON.
.\.venv\Scripts\python.exe -m olis_archive show-bill 2026R1 SB1501

# Retry recoverable document failures associated with an earlier run.
.\.venv\Scripts\python.exe -m olis_archive retry-failures --run-id 12

# Resume the same interrupted or low-space-paused run.
.\.venv\Scripts\python.exe -m olis_archive resume-run 12
```

Use `python -m olis_archive --help` or a command's `--help` for its exact options.
Collection exits successfully only for `completed`. A `completed_with_errors` run
prints its complete JSON counters but returns a nonzero shell status so automation
cannot silently treat a partial archive as complete; inspect Run Detail for its
durable errors.

## Configuration

Environment values are loaded from `.env` without overriding values already set in
the process environment. Settings saved through the UI are stored in SQLite and
overlay the corresponding environment defaults for future runtimes.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `LEGIVIEW_ARCHIVE_ROOT` | `data/archive` | Payload archive root; an external dedicated directory is recommended. |
| `LEGIVIEW_DATABASE_PATH` | `data/legiview.sqlite3` | SQLite database; configured by environment, not the Settings page. |
| `LEGIVIEW_REQUEST_TIMEOUT` | `30` | OData, OLIS HTML, and download timeout in seconds. |
| `LEGIVIEW_ODATA_WORKERS` | `1` | Concurrent durable collection workers, from 1 to 4. |
| `LEGIVIEW_DOWNLOAD_WORKERS` | `2` | Document download workers within a collection, from 1 to 8. |
| `LEGIVIEW_HTML_CONCURRENCY` | `1` | Concurrent OLIS HTML requests, from 1 to 2. |
| `LEGIVIEW_MIN_FREE_SPACE_BYTES` | `1073741824` | Free-space floor preserved during downloads. |
| `LEGIVIEW_INTER_REQUEST_DELAY` | `0.25` | Minimum delay between OData/OLIS HTML requests in seconds. |
| `LEGIVIEW_HOST` | `127.0.0.1` | Flask bind host. |
| `LEGIVIEW_PORT` | `5000` | Flask port. |
| `LEGIVIEW_DEBUG` | `0` | Flask debug flag. Leave disabled for normal use. |

Numeric settings are range-checked. The UI rejects a filesystem root or an existing
non-directory archive path. Saved settings take effect after LegiView is restarted;
the current runtime is never hot-swapped because a paused or canceled request can
still be winding down safely. Run records retain their stored configuration
snapshots for provenance; queued or resumed work uses the effective settings of the
runtime that executes it.

## Storage and file safety

SQLite stores source metadata, raw source values, settings, logical documents,
immutable payload versions, collection stages, counters, and durable errors. The
archive stores document bytes under:

```text
<archive_root>/<session>/<bill>/<document_kind>/<source_numeric_id>/<filename>
```

Only relative archive paths are stored in SQLite. Completed downloads are streamed
to `.part`, checked for length and file type, hashed with SHA-256, flushed, and
atomically promoted without overwriting unrelated bytes. A valid completed file is
revalidated and skipped on an idempotent rerun. See
[docs/archive_layout.md](docs/archive_layout.md) for identity, versioning, and
recovery details.

Before serving a registered local file, the UI rechecks its recorded filename, MIME,
byte count, SHA-256, and path components. Mutating localhost forms also require a
session-bound CSRF token.

**Every downloaded document is untrusted.** LegiView validates basic format and
integrity but does not provide antivirus scanning. It never executes downloaded
PDF, Office, archive, or script content. Keep operating-system security protections
enabled and assess files before opening them.

## Restart, resume, and retry

Run state is durable. On startup LegiView:

1. applies database migrations;
2. changes records left falsely `running` or `downloading` to recoverable
   `interrupted` states;
3. removes incomplete `.part` files only beneath the configured archive root; and
4. starts web workers and enqueues runs that were already `queued`.

An interrupted run is not silently restarted. Resume it from Run Detail or with
`resume-run`. When the free-space floor pauses a run, restore space and resume the
same way. Resume re-enters the collection engine, and already valid payloads are
verified and skipped.

Use **Retry Failures** or `retry-failures --run-id` to create a separate durable run
for failed documents from an earlier run. The explicit action can reattempt both
recoverable and terminal failures; it is the operator's authority to begin a new
attempt cycle. Valid completed documents are not selected or redownloaded, and a
failure that persists remains visible for review.

## Tests

The default suite is self-contained and does not require OLIS network access:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

It covers source mapping, parsing fixtures, persistence and idempotency, job states,
archive-path safety, downloader behavior through a local test server, startup
recovery, UI routes, and CLI wiring. See
[docs/phase1_validation.md](docs/phase1_validation.md) for the source-spike evidence
and live validation record.

## Phase 1 boundaries

- Mutating web and CLI processes are single-owner per database and archive. Phase 1
  uses a cross-process lock rather than distributing work across application
  processes; concurrent `show-bill` remains read-only safe.
- Only Oregon `HB` and `SB` measures are collected.
- Collection is on demand for one bill or one explicitly selected session. There is
  no multi-year history orchestrator, recurring scheduler, or destructive mirror.
- Modern submitted testimony uses one narrowly scoped OLIS HTML page because its
  displayed listing complements OData. There is no generic crawler or browser
  automation.
- Committee records classified as other or unknown are retained for context but are
  not payload-download targets unless stronger source evidence classifies them as
  testimony or a presentation.
- There is no OCR, text extraction, full-text/sist2 indexing, semantic or AI analysis,
  natural-language search, reporting suite, or sponsor-network visualization.
- There is no built-in antivirus, user authentication, cloud hosting, PostgreSQL,
  Redis, or Celery.
- Detection of changed payloads relies primarily on official source modification
  metadata. Previously retained bytes are never silently replaced.

## License

LegiView's software code is copyright (C) 2026 Save Oregon Schools, LLC and is
licensed under the GNU Affero General Public License version 3. See
[LICENSE](LICENSE) for the full license text.

LegiView is distributed without any warranty; without even the implied warranty
of merchantability or fitness for a particular purpose.

The Save Oregon Schools name, logo, and related branding are not licensed for
reuse under the GNU Affero General Public License. See
[TRADEMARKS.md](TRADEMARKS.md) for the project's trademark and branding notice.
