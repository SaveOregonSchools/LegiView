# Pause, cancel, restart, resume, and retry

Inventory Backfill and Download Archive use the same durable run, item, error, and
worker model as targeted Phase 1 collections. The browser is only a control/status
surface: closing it does not stop work. SQLite is the authority for what was queued,
started, completed, skipped, failed, paused, canceled, or interrupted.

## One mutation owner

Every process that can change collection state or archive files must acquire the
operating-system lock next to the SQLite database. A second web server or mutating
CLI process against the same database exits before it can run migrations or startup
recovery. Read-only inspection/preflight remains concurrent when the packaged schema
is already current; a first-run or upgrade bootstrap takes the lock before migrating.

Within that mutation owner, one durable collection run is dispatched at a time.
The active run may use its configured OData, OLIS HTML, and document-download
limits internally, but separately queued runs cannot multiply those limits or race
the same document state.

On orderly shutdown, the worker manager stops accepting new work, leaves waiting run
IDs durably queued, marks active work interrupted, and retains the lock until worker
threads have stopped. This prevents a replacement process from cleaning a `.part`
file while an old transfer could still write it.

## Pause

Low disk space pauses acquisition before the configured free-space floor would be
violated. For a Download Archive run, workers stop claiming new document rows.
Already-running streams finish or wind down under the same Phase 1 safety rules; the
scope and completed files remain durable.

Restore sufficient space, or deliberately change the GB floor in Settings and
restart, then resume the same run. LegiView never deletes older versions to make
space.

## Cancel

Cancel stops new claims and causes active work to wind down. Everything already
inventoried or validly downloaded is retained. Cancel does not delete source rows,
payloads, versions, anomalies, or provenance and does not silently turn into a new
run. A canceled run is terminal and is not resumable; create a new deliberately
scoped run if later work is wanted.

## Restart and interruption recovery

After obtaining the mutation lock and before workers start, LegiView:

1. applies ordered database migrations;
2. verifies or initializes `.legiview-archive-root` for the dedicated archive;
3. converts runs and run items left `running` to `interrupted`;
4. converts documents and versions left `downloading` to `interrupted`;
5. removes untrusted/incomplete `.part` files only beneath that owned archive root,
   without following directory links; and
6. enqueues work that was already durably `queued`.

An arbitrary nonempty directory cannot be claimed implicitly. Only an empty
directory or an unmistakable marker-less legacy LegiView hierarchy can receive the
marker; otherwise startup stops before recursive cleanup. Keep the archive root
dedicated to LegiView and do not remove or edit its marker.

An interrupted run is not silently restarted. Resume is an explicit operator
decision. Session items already completed remain completed, and valid current files
are revalidated and skipped. Download Archive document claims are lazy and atomic;
active claims become interrupted/recoverable, while a second worker cannot claim the
same row concurrently.

Inside an interrupted inventory session, a prior successful OLIS page check is
reused only when its bill, testimony/presentation, and agenda observations do not
postdate that check. This comparison uses persisted observation times rather than
only the run ID, so a same-run resume avoids duplicate page requests while genuinely
new or refreshed candidate inputs are checked again.

To resume in the UI, open **Run History**, select the run, and use **Resume** on Run
Detail. From Windows PowerShell (use `.venv/bin/python` on Linux/macOS):

```powershell
.\.venv\Scripts\python.exe -m olis_archive resume-run <run-id>
```

Duplicate Resume clicks do not multiply work: the durable status transition and the
in-process queue both reject an already-queued/active run.

## Retry versus resume

Resume continues the frozen scope of one interrupted or low-space-paused run. For a
historical Download Archive run, that means the original session keys, document-kind
and status filters, and inventory cutoff remain unchanged.

Retry Failures creates a separate durable attempt from selected failures belonging
to a targeted Phase 1 bill/session run:

```powershell
.\.venv\Scripts\python.exe -m olis_archive retry-failures --run-id <source-run-id>
```

For historical `inventory_backfill` or `download_archive` runs, use the bounded,
database-claimed workflow instead of materializing failure IDs into a run snapshot:

```powershell
.\.venv\Scripts\python.exe -m olis_archive download-archive --session <session-key> --retryable-failures-only
```

That bulk mode excludes terminal failures. To deliberately reattempt one, review and
select it on the **Retry Failures** page. The targeted CLI command can include
terminal failures from a Phase 1 bill/session source run, but rejects historical
bulk run IDs so their scope cannot be materialized as an unbounded ID snapshot.

On **Retry Failures**, **Queue selected retries** snapshots only the checked rows on
the current page. **Queue all matching failures** instead freezes every eligible
document matching the current run/session/bill filters as durable SQL-backed run
items. It does not place the full ID set in run JSON or an in-memory queue, and a
document repaired after the snapshot is skipped without changing its valid state.

Ordinary automated retries are bounded and reserved for retryable network/HTTP
conditions; `Retry-After` is honored. Terminal validation failures are not looped
forever. A selected terminal retry is the operator's authority to try that record
again. Valid completed files are not selected by retry-only work. Normal Download
Archive runs separately audit recorded current files in bounded background work and
skip them only after validation.

Historical OLIS reconciliation also has an outage circuit breaker. Three consecutive
retryable testimony-page failures pause the same durable inventory run before a broad
site outage can generate thousands of failed requests. Any successfully returned and
parsed page resets the counter. After source access is healthy, explicitly resume the
same run so its completed session and entity work is retained.

The known OData-only public-testimony record that returns HTTP 200 with zero bytes is
therefore a truthful terminal failure. It remains reviewable and can be retried only
through an explicit operator action; it is never promoted as a valid version.

## Idempotency and immutable payloads

Inventory uses stable source keys and upserts, so repeating a completed session does
not duplicate sessions, bills, sponsors, source documents, or logical documents.
Incremental watermarks overlap inclusively. A failed query does not advance its
cursor, and only a complete authoritative comparison can mark retained rows missing.

For payloads:

- an unchanged valid current file is revalidated and skipped;
- equal bytes reuse the existing SHA-256 version rather than storing a duplicate;
- changed bytes create the next immutable `__v0002`-style version; and
- the logical document's current-version pointer changes only after validation and
  atomic promotion succeeds.

## Operator recovery checklist

1. Read the run's final/current activity, session items, durable errors, and source
   anomalies before acting.
2. For low space, compare current free disk to the configured GB floor. Free space or
   change configuration deliberately; do not remove registered versions ad hoc.
3. Restart LegiView if configuration changed.
4. Resume the same interrupted/paused run when its frozen scope should continue.
5. Use Retry Failures only when a new attempt cycle is intended.
6. Confirm the run and Session Status pages after completion; a finished run does not
   by itself prove inventory or archive completeness.

Back up the SQLite database and archive tree together before manual filesystem
repair. Do not rename or replace registered bytes independently of the database.
