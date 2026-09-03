# Archive layout and recovery

This document describes the Phase 1 contract between SQLite metadata and archived
document bytes. Configure a dedicated archive root, normally outside the LegiView
repository. The root must be a real directory rather than a filesystem root, file,
link, or reparse-point alias.

Phase 1 enforces exclusive mutation ownership with an operating-system lock file next
to SQLite. A second web server or mutating CLI command against the same database is
rejected before startup recovery or workers run. The `show-bill` command disables
startup recovery and is safe to use concurrently as a read-only inspection command.
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
  <official_session_key>/
    <compact_bill_id>/
      <document_kind>/
        <source_numeric_id>/
          <sanitized_filename>
```

Examples:

```text
C:\LegiViewArchive\2026R1\SB1501\public_testimony\244133\Testimony.pdf
C:\LegiViewArchive\2014R1\HB4111\committee_presentation\32769\RileyC testimony.pdf
C:\LegiViewArchive\2026R1\SB1501\floor_letter\4701\Vote Yes On SB 1501 B.pdf
```

The source numeric ID is scoped to its document family. A public-testimony ID is not
assumed to identify the same thing as an equal floor-letter or committee-document ID.
The canonical database identity is:

```text
session + bill + source entity type + source document ID
```

The normalized document kinds are `public_testimony`, `legacy_testimony`,
`committee_presentation`, `floor_letter`, `committee_document_other`, and `unknown`.
Phase 1 downloads the first four when an official download URL exists. Other and
unknown committee documents remain browseable metadata with `not_applicable` download
state unless stronger source evidence permits reclassification.

## Filenames and registered paths

The first payload name is derived from a trusted source title when available, with a
stable fallback. LegiView:

- normalizes Unicode;
- replaces Windows control characters and `<>:"/\\|?*`;
- removes unsafe trailing spaces and periods;
- guards reserved device names such as `CON`, `NUL`, `COM1`, and `LPT1`;
- limits component length;
- accepts only validated session, bill, kind, and numeric-ID components; and
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
byte count, MIME type, SHA-256, validation state, and status.

The first retained filename is unnumbered. A later candidate version uses a stable
suffix before its extension:

```text
Testimony.pdf
Testimony__v0002.pdf
Testimony__v0003.pdf
```

A source metadata change can trigger a new fetch. LegiView validates and hashes the
new candidate before changing the logical document's current-version pointer. If the
SHA-256 matches an already retained payload, the existing immutable version is reused.
If it differs, both versions remain. Previously retained bytes are never silently
overwritten or deleted.

An idempotent rerun preserves first-seen timestamps and stable keys, updates current
source metadata and last-seen timestamps, and validates the recorded local file. A
completed record is skipped only if the file exists under the registered path and
passes its stored byte, type, and SHA-256 expectations. A missing or invalid local
file becomes recoverable work instead of being treated as complete.

## Streaming and atomic promotion

Downloads are restricted to HTTP(S) on the configured Oregon Legislature allowlist.
Redirect targets are checked again. A transfer:

1. reserves/checks disk space against the configured floor;
2. creates `<final-name>.part` without replacing an existing path;
3. streams bounded chunks while counting bytes and calculating SHA-256;
4. checks advertised/expected length, declared MIME type, and strong file signatures;
5. flushes the staged file to disk; and
6. atomically promotes it to the final name without replacing unrelated bytes.

Retryable HTTP and network failures use bounded backoff and honor `Retry-After`.
Validation failures, redirects outside the allowlist, and destination conflicts are
recorded rather than forced through. Low free space sets the document to
`paused_low_space` and pauses its durable run.

## Restart and `.part` recovery

Normal handled transfer failures remove the `.part` file owned by that attempt. An
abrupt process or machine stop can leave both durable `running` state and staged
bytes. After acquiring the mutation lock and before starting work, the web runtime
and mutating CLI commands:

1. changes `running` collection runs and items to `interrupted`;
2. changes `downloading` documents and versions to `interrupted`; and
3. removes incomplete `.part` files beneath the one explicitly configured archive
   root without following directory links.

A `.part` file alone is not a trusted completion marker, so normal startup discards
it and leaves the associated logical document eligible for an explicit retry. Queued
runs are picked up when the web worker starts. Interrupted and low-space-paused runs
require **Resume** in the UI or:

```powershell
.\.venv\Scripts\python.exe -m olis_archive resume-run <run-id>
```

Failed documents associated with an earlier run can instead be explicitly retried in
a new run, including a terminal failure the operator deliberately chooses to
reattempt:

```powershell
.\.venv\Scripts\python.exe -m olis_archive retry-failures --run-id <source-run-id>
```

Because valid completed files are revalidated and skipped, either path can safely
repeat discovery without duplicating logical rows or known payloads.

## Backup and trust boundary

Back up the SQLite database and the entire archive root together. The database alone
does not contain payload bytes, and the archive alone does not contain all source and
run provenance. LegiView does not delete remote-source records merely because a later
query omits them, and it is not a destructive mirror.

Archived files remain untrusted even after type and hash validation. LegiView does
not execute them and provides no antivirus scanning, OCR, or content extraction in
Phase 1.
