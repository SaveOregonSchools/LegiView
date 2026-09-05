# Archive layout and recovery

This document describes the contract between SQLite metadata and archived document
bytes. Phase 2 keeps the proven Phase 1 hierarchy and immutable version model; no
payload-layout migration is required. The portable default archive is
`<project_root>/archive`, although a dedicated absolute location can be configured.
The root must be a dedicated real directory rather than a broad personal/shared
directory, filesystem root, file, link, or reparse-point alias. LegiView records this
contract in `.legiview-archive-root`. On the first upgraded startup it can adopt a
marker-less archive only when the entire existing tree has the exact legacy layout
described below and contains at least one regular payload as positive evidence;
unrelated or empty lookalike directory trees are rejected without cleanup.

Phase 1 enforces exclusive mutation ownership with an operating-system lock file next
to SQLite. A second web server or mutating CLI command against the same database is
rejected before startup recovery or workers run. The `show-measure` command (and the
compatible legacy `show-bill` alias) disables startup recovery and is safe to use
concurrently as a read-only inspection command.
The lock coordinates LegiView processes; it cannot protect against unrelated programs
editing archive files directly.

Settings are persisted for the next startup rather than hot-swapping a live archive
root or worker graph. During shutdown the manager stops accepting new work, leaves
waiting run IDs durably queued, marks active work interrupted, and retains the
mutation lock until worker threads are quiescent (or until the operating system ends
the process). This prevents startup recovery from deleting a `.part` file that an old
worker can still write.

## Deterministic hierarchy

Every in-scope payload has this directory shape:

```text
<archive_root>/
  .legiview-archive-root
  <official_session_key>/
    <compact_bill_id>/
      <document_kind>/
        <source_numeric_id>/
          <sanitized_filename>
```

Examples (the drive-letter root is only an illustration; stored paths are portable):

```text
C:\LegiViewArchive\2026R1\SB1501\public_testimony\244133\Testimony.pdf
C:\LegiViewArchive\2014R1\HB4111\committee_presentation\32769\RileyC testimony.pdf
C:\LegiViewArchive\2026R1\SB1501\floor_letter\4701\Vote Yes On SB 1501 B.pdf
```

The source numeric ID is scoped to its document family. A public-testimony ID is not
assumed to identify the same thing as an equal floor-letter or committee-document ID.
The canonical database identity is:

```text
session + measure + source entity type + source document ID
```

The normalized document kinds are `public_testimony`, `legacy_testimony`,
`committee_presentation`, `floor_letter`, `committee_document_other`, and `unknown`.
The first four are ordinarily downloadable when an official URL exists. Other and
unknown committee documents remain browseable metadata with `not_applicable` download
state unless stronger source evidence permits reclassification. Unknown raw values
are retained and diagnosed rather than discarded.

Inventory Backfill creates and reconciles these logical records without fetching the
payloads. Download Archive is an explicit later operation over the durable inventory.
Its session/kind/status filters and an inventory cutoff are frozen at run creation, so
documents discovered by a concurrent later inventory cannot silently expand the run.

## Filenames and registered paths

The first payload name is derived from a trusted source title when available, with a
stable fallback. LegiView:

- normalizes Unicode;
- replaces Windows control characters and `<>:"/\\|?*`;
- removes unsafe trailing spaces and periods;
- guards reserved device names such as `CON`, `NUL`, `COM1`, and `LPT1`;
- limits component length;
- accepts only validated session, measure, kind, and numeric-ID components; and
- applies a MIME/signature-supported extension when the source name is ambiguous.

The server-reported remote filename is stored separately when available. The official
download responses tested in the source spike did not provide `Content-Disposition`,
so their trusted OLIS titles and detected types supply the useful local name.

SQLite stores paths relative to the archive root with `/` separators. Any local-file
web action resolves that registered value beneath the current archive root, rejects
absolute or traversing paths, and serves only an existing regular non-symlink file.
Changing the archive root does not rewrite stored relative paths; move or copy the
whole hierarchy to the same relative layout before changing the setting.

## Logical documents and payload versions

`documents` holds one logical official record, its source provenance, latest download
state, and pointer to the current validated payload. `document_versions` records
retained byte versions with observation time, producing collection run, final source
URL, source modified date, ETag/Last-Modified when available, filename, relative path,
byte count, MIME type, optional SHA-256 for older records, validation state, and status.

The first retained filename is unnumbered. A later candidate version uses a stable
suffix before its extension:

```text
Testimony.pdf
Testimony__v0002.pdf
Testimony__v0003.pdf
```

A source metadata change can trigger a new fetch. LegiView validates the new candidate
before changing the logical document's current-version pointer. New downloads use
their registered path, filename, and byte count instead of calculating SHA-256, so a
later source revision receives the next immutable version path. Previously retained
bytes are never silently overwritten or deleted. Hashes stored by older releases are
retained and remain usable during explicit validation and recovery.

An idempotent rerun preserves first-seen timestamps and stable keys, updates current
source metadata and last-seen timestamps, and validates the recorded local file. A
completed record is skipped only if the file exists under the registered path with
the registered filename and byte count. A missing or mismatched local file becomes
recoverable work instead of being treated as complete.

## Streaming and atomic promotion

Downloads are restricted to HTTP(S) on the configured Oregon Legislature allowlist.
Redirect targets are checked again. A transfer:

1. reserves/checks disk space against the configured floor;
2. creates `<final-name>.part` without replacing an existing path;
3. streams bounded chunks while counting bytes;
4. checks advertised/expected length, declared MIME type, and strong file signatures;
5. closes the staged file through normal operating-system buffered I/O; and
6. atomically promotes it to the final name without replacing unrelated bytes.

Payload workers reuse persistent HTTP connections to the same source. LegiView does
not force a file and directory sync after every small payload; backups and ordinary
filesystem writeback provide the selected durability tradeoff for this local archive.

Retryable HTTP and network failures use bounded backoff and honor `Retry-After`.
Validation failures, redirects outside the allowlist, and destination conflicts are
recorded rather than forced through. Low free space sets the document to
`paused_low_space` and pauses its durable run. The user-facing floor is configured in
GB, where one LegiView GB is `1024 ** 3` bytes; reservations and checks continue in
bytes internally.

At historical scale, eligible rows are claimed atomically from SQLite rather than
loaded into one all-history Python queue. The configured 1–8 worker value, default 2,
controls concurrent network transfers. Promoted results enter a separate bounded
queue; one finalizer records small batches in SQLite while transfer workers claim the
next files. A claim creates or updates the durable document run item. Pause/cancel
stops new claims, concurrent workers cannot win the same row, and the existing
validation/version rules still govern promotion.

## Restart and `.part` recovery

Normal handled transfer failures remove the `.part` file owned by that attempt. An
abrupt process or machine stop can leave both durable `running` state and staged
bytes. After acquiring the mutation lock and before starting work, the web runtime
and mutating CLI commands:

1. changes `running` collection runs and items to `interrupted`;
2. changes `downloading` documents and versions to `interrupted`; and
3. verifies the archive ownership marker; and
4. removes incomplete `.part` files beneath that one explicitly configured,
   dedicated archive root without following directory links.

Recursive `.part` cleanup refuses to run when the marker is absent or invalid. The
normal locked startup creates the marker for an empty root or safely recognizes and
adopts an older LegiView-only tree first. It never treats an arbitrary nonempty
directory as an archive merely because it was entered in Settings.

A `.part` file alone is not a trusted completion marker, so normal startup discards
it and leaves the associated logical document eligible for an explicit retry. Queued
runs are picked up when the web worker starts. Interrupted and low-space-paused runs
require **Resume** in the UI or:

```powershell
.\.venv\Scripts\python.exe -m olis_archive resume-run <run-id>
```

Failed documents associated with an earlier run can instead be explicitly retried in
a new run. A selected Retry Failures action (or the targeted Phase 1 command) can
deliberately reattempt a terminal failure:

```powershell
.\.venv\Scripts\python.exe -m olis_archive retry-failures --run-id <targeted-source-run-id>
.\.venv\Scripts\python.exe -m olis_archive download-archive --session <session-key> --retryable-failures-only
```

The historical bulk command includes only retryable failures; it excludes terminal
validation failures. A normal Download Archive run audits recorded current files in
bounded background work, while retryable-failures-only mode intentionally avoids
auditing healthy downloads. An explicit retry of a registered current file is skipped
when its path, filename, and byte count still match.

See [recovery.md](recovery.md) for historical-run pause, claim, cancel, restart, and
operator-recovery semantics.

## Backup and trust boundary

Back up the SQLite database and the entire archive root together. The database alone
does not contain payload bytes, and the archive alone does not contain all source and
run provenance. LegiView does not delete remote-source records merely because a later
query omits them, and it is not a destructive mirror.

Archived files remain untrusted even after type/signature validation. LegiView does
not execute them and provides no antivirus scanning, OCR, or content extraction.
Ordinary files remain directly readable by external tools such as sist2, but LegiView
does not manage a sist2 process or index in Phase 2.
