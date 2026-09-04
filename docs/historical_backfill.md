# Historical inventory and archive workflow

Phase 2 covers every official Oregon legislative session whose official chronology
begins at or after `2014R1`, and only HB/SB measures. Inventory and payload download
are intentionally separate durable operations.

## Acceptable-use prerequisite

Before any source access, read the Oregon Legislature's
[data page](https://www.oregonlegislature.gov/citizen_engagement/Pages/data.aspx) and
[Open Data acceptable-use agreement](https://www.oregonlegislature.gov/citizen_engagement/Documents/OLODataAcceptableUseAgreement.pdf).
The public endpoint and operator review did not reveal an interactive gate or a
separate submission form during the 2026-09-03 validation. The operator explicitly
confirmed that they reviewed and agreed to the published terms and found no separate
form to submit. The agreement retains electronic-acceptance language. LegiView does
not accept terms for you or bypass an acceptance step. Before live source access,
review and agree to the published terms; if an official acceptance mechanism appears,
stop and use it.

Use OData rather than broad OLIS scraping. Keep the conservative defaults, honor
429/`Retry-After` and other service instructions, and do not use proxies, alternate
hosts, CAPTCHA bypass, or rate-limit evasion. The current published agreement permits
on-demand and incremental queries but limits whole-database/full refresh behavior to
once per day after business hours, stated as **5:00 p.m.–6:00 a.m. Pacific**. Treat a
complete all-history inventory as a full refresh and schedule it in that window.

## How scope is resolved and frozen

LegiView pages through the official `LegislativeSessions` set, locates the official
`2014R1` row, and compares `BeginDate` chronology. Every equal-or-later official
session is eligible, including regular, short, and special sessions. It does not
hardcode suffixes, calendar parity, or `DefaultSession`.

The user may select the complete eligible set or a subset. When Start is pressed,
the exact ordered session keys are stored in `requested_scope_json`, and one durable
session run item is created for each. A newly published session cannot silently join
an already-running job.

## Inventory Backfill

Inventory Backfill retrieves and reconciles metadata only. For each session it pages
and persists:

- the session and HB/SB measures;
- needed legislators and committees;
- sponsors;
- committee meetings and agenda items;
- committee meeting documents;
- `CommitteePublicTestimonies`;
- floor letters;
- normalized logical document metadata;
- narrowly selected OLIS testimony/presentation display checks;
- source presence and anomalies; and
- optional bounded remote-size probes.

Date-capable entities use a full session comparison initially and inclusive
Created/Modified watermarks for later incremental runs. `FloorLetters` always uses a
complete session comparison because its current metadata has neither source date.
Every continuation page is consumed before a cursor is committed. See
[source_mapping.md](source_mapping.md) and
[testimony_discovery.md](testimony_discovery.md).

On an incremental rerun, a previously successful OLIS display result is reused when
the bill, candidate source documents, and public-hearing agenda evidence were not
returned as new or changed by that run. New/changed candidates and any prior failed
or anomalous page are fetched again. A forced authoritative comparison rechecks every
current candidate.

### Start from the web UI

1. Start LegiView and open **Inventory Backfill** (`/inventory-backfill`).
2. Select **Resolve official sessions**, then review the returned session list,
   current per-session status, project/archive
   paths, disk free space, source concurrency/delay, and acceptable-use reminder.
   This source-access action is a CSRF-protected form submission; simply viewing the
   page does not contact the Legislature.
3. Keep all eligible sessions selected for a complete inventory, or select a smaller
   validation subset.
4. Optionally select **Probe remote sizes**. This adds HEAD/bounded Range checks but
   never downloads full payloads.
5. Leave **Force an authoritative full session comparison** off for an ordinary
   incremental rerun. Select it only for a deliberate disappearance check in the
   published full-refresh window and daily limit.
6. Press **Start Inventory Backfill** explicitly.
7. Follow Run Detail for durable session items, stages, counts, errors, and anomalies.
   Work continues if the browser closes.
8. Review **Session Status**, then drill into bills, documents, failures, anomalies,
   and recent runs before starting payload acquisition.

### Start from the CLI

The CLI creates the same durable run and waits in the current terminal. With no
`--session` options, the complete officially resolved in-scope set is selected. The
examples use the Windows virtual-environment executable; on Linux/macOS, replace
`.\.venv\Scripts\python.exe` with `.venv/bin/python`:

```powershell
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill
```

Select sessions by repeating the option, and optionally enable size probes:

```powershell
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill --session 2014R1 --session 2026R1 --probe-remote-sizes
.\.venv\Scripts\python.exe -m olis_archive inventory-backfill --session 2014R1 --force-full
```

`--force-full` deliberately ignores retained entity watermarks so a successful
session-scoped comparison can detect disappearances. Use it only under the published
full-refresh window/daily-limit rules. The CLI prints an explicit reminder before
whole-history or forced-full work.

Use `.\.venv\Scripts\python.exe -m olis_archive inventory-backfill --help` (or the
POSIX equivalent) as the executable source of truth for installed-version options.

## Download Archive

Download Archive consumes the existing inventory. It does not rediscover sessions or
silently run inventory. Before launch, LegiView resolves SQL-recorded counts for downloaded,
pending/missing, retryable failures, terminal/non-downloadable records, known pending
bytes, unknown-size records, free disk, GB floor, archive root, and worker count.
This preflight does not open or hash every recorded payload. A normal run validates
recorded current files in bounded background work before skipping them;
retryable-failures-only mode intentionally does not audit healthy downloads.

The run freezes selected session keys, optional kinds and status mode, plus a
`scope_cutoff_at` boundary. Only records first seen by that cutoff are claimable.
Workers atomically claim bounded rows from SQLite; there is no all-history in-memory
ID list or queue.

### Start from the web UI

1. Open **Download Archive** (`/download-archive`) after reviewing **Session Status**
   (`/session-status`).
2. Choose all inventoried sessions or specific sessions.
3. Optionally choose document kinds and the missing/pending or retryable-failure
   mode.
4. Select **Preview selected scope** and review the preflight counts, known-byte lower
   bound, unknown sizes, current free
   disk, configured floor, effective archive root, and worker count.
5. If known pending bytes alone would violate the floor, add disk space or change the
   setting and restart; launch remains blocked.
6. Press **Start Download Archive** explicitly.
7. Monitor Run Detail. Pause/cancel stops new claims and retains completed work;
   interruption and low-space states require an explicit Resume.

### Start from the CLI

Use the read-only preflight command before creating a run. It accepts the same
session/kind/retryable selection filters and prints the resolved estimate. With no
session or kind filters, the commands cover eligible current payloads in all
inventoried in-scope sessions:

```powershell
.\.venv\Scripts\python.exe -m olis_archive archive-preflight
.\.venv\Scripts\python.exe -m olis_archive download-archive
```

Examples of narrower frozen scopes:

```powershell
.\.venv\Scripts\python.exe -m olis_archive archive-preflight --session 2014R1
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2014R1
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2026R1 --kind public_testimony --kind floor_letter
.\.venv\Scripts\python.exe -m olis_archive download-archive --session 2026R1 --retryable-failures-only
```

Use each subcommand's `--help` for installed-version options. Terminal validation
failures are not included in normal automatic retry scope; reattempt them only through
the explicit failure-review/retry workflow.

Valid `--kind` values are `public_testimony`, `legacy_testimony`,
`committee_presentation`, `floor_letter`, `committee_document_other`, and `unknown`.
The latter two are normally metadata-only because they need stronger source evidence
and an official URL before they are downloadable.

## Progress and repeat runs

Historical run stages identify session resolution, source entity sync, OLIS display
reconciliation, document normalization, optional probes, presence reconciliation,
and finalization. The durable hierarchy reports sessions total/completed/incomplete/
failed plus bill, document, page, probe, error, and anomaly counts.

Repeating an inventory is idempotent. Stable rows are updated rather than duplicated,
successful watermarks overlap, and older payload versions remain untouched. A
successful full comparison may mark an absent source record `missing`, but never
deletes its row or bytes. A failed or incremental query cannot do so.

See [completeness.md](completeness.md) for status definitions,
[recovery.md](recovery.md) for restart semantics, and
[archive_layout.md](archive_layout.md) for payload identity/versioning.

## Audit exports

The export responses iterate SQLite in bounded batches:

- `/exports/sessions.csv` exports per-session inventory/archive state;
- `/exports/documents.csv` exports the current Browse Documents filters; and
- `/exports/operations.csv?view=all|errors|anomalies` exports operational review
  records (use `errors` or `anomalies` for one class).

The document export includes session, bill, normalized kind, source entity/type and
ID, raw type, title, submitter/on-behalf-of/position/organization/city, committee and
meeting date, source/download URLs, OLIS display and source-presence states, local
relative path, download status, current bytes, and SHA-256 where available. Exporting
does not read payload bodies or hold the entire historical result in memory.
The session export includes known/unknown size counts and an explicit lower-bound
flag beside the total known remote bytes.
