# Inventory completeness and source state

LegiView distinguishes “a run stopped” from “the source inventory is complete.” A
session is assessed from durable per-source sync results, OLIS display checks, and
unresolved anomalies. Downloading files is deliberately not a condition of inventory
completeness.

## Catalogue discovery and the support boundary

Session discovery pages the complete official `LegislativeSessions` catalogue. It
does not silently remove older official rows. LegiView locates the official
`2014R1` record and uses that record's `BeginDate` as the validated support boundary;
session ordering and eligibility come from official dates, not lexical session keys,
suffixes, or `DefaultSession`.

Schema-compatible official sessions before that boundary are persisted for source
provenance and shown as unavailable in Inventory Backfill, but they are disabled and
rejected by server-side UI/CLI validation. A malformed or unrecognized catalogue row
is retained in the resolved view and frozen guardrail diagnostics but is not written
into schema-constrained session tables. Neither kind receives inventory work items
or joins the supported-session completeness denominator. Consequently, seeing an
older catalogue row with no inventory run is not an incomplete inventory condition.
For a selected supported range, the exact inclusive official-chronology expansion is
frozen in the durable run; only those keys contribute to that run's completeness.

## Session inventory states

| State | Meaning |
| --- | --- |
| `not_started` | No historical inventory has started for the session. |
| `inventory_running` | A durable session item is currently being processed. |
| `inventory_complete` | Every required discovery/reconciliation step succeeded and no unresolved material anomaly is known. |
| `inventory_complete_with_errors` | Discovery is demonstrably complete; remaining documented errors do not create a material discovery gap. |
| `inventory_incomplete` | A source, parser, or reconciliation problem could mean records were missed. |
| `inventory_failed` | The session could not be processed far enough to establish a usable inventory result. |
| `interrupted` | Processing stopped unexpectedly and requires an explicit resume. |

A session can be `inventory_complete` only after:

- its official `LegislativeSessions` row was resolved;
- complete supported legislative-measure retrieval succeeded;
- required legislators, committees, sponsors, meetings, agenda items, committee
  documents, and public testimony retrieval succeeded;
- complete floor-letter retrieval succeeded;
- every required OLIS testimony/presentation candidate was successfully checked, or
  was explicitly proven not applicable;
- document normalization and source-presence reconciliation finished; and
- no unresolved anomaly is known to create a material discovery gap.

The end of a process, a zero-row response, or a run status by itself does not prove
those conditions. `inventory_complete_with_errors` is reserved for understood,
non-material errors; it is not a label for a session with an uncertain source query
or parser result.

## Per-source sync state and watermarks

`source_sync_state` is keyed by session and entity set. It records the strategy,
attempt and success times, full/incremental times, inclusive source watermark when
supported, producing run, returned count, reconciliation outcome, and any incomplete
failure state.

Entities with reliable `CreatedDate`/`ModifiedDate` fields support incremental
queries. After a successful query, LegiView retains the greatest observed source
timestamp. The next query deliberately overlaps that boundary with `ge`, and stable
upserts make equal-timestamp rows idempotent. A fetch, validation, or page-persistence
failure returns no commit-ready cursor, so the saved watermark is not advanced.

`FloorLetters` has no current source created/modified field. LegiView therefore uses
a complete session-scoped comparison by stable `FloorLetterId`; it does not invent a
watermark. A full session comparison is also required before any entity can be
declared missing. An incremental-only response cannot establish disappearance.

## Source presence is archival, not destructive

Measures and documents retain one of these states:

- `active`: present in the latest successful authoritative comparison;
- `missing`: absent from a successful complete relevant session/entity comparison;
- `unknown`: no authoritative presence decision is currently safe.

Missing records remain in SQLite, and any retained payload versions stay on disk.
LegiView records when a record first became missing and when it was last reconciled.
If it returns in a later complete query, it becomes active again and the transition
is retained in source-presence event history.

A timeout, HTTP error, malformed page, failed page persistence, or incremental-only
query never mass-marks older rows missing.

## Anomalies and materiality

Source anomalies are durable, fingerprinted review records. Re-observing the same
condition updates its last-seen/run provenance and occurrence count rather than
flooding the database with duplicates. Records can be resolved explicitly without
erasing their history.

Examples include:

- raw `DocumentType` or normalized-kind drift for the same identity;
- an unknown committee-document type;
- OData/OLIS count mismatch, OData-only, or page-only records;
- malformed source IDs or missing normally required URLs;
- unexpected hosts;
- a parser anomaly that could hide displayed records; and
- live `$metadata` missing an entity set or property required by the mapper.

Informational or warning-level drift can coexist with a complete inventory when the
record set remains proven. `affects_completeness` is the decisive flag: an unresolved
anomaly with that flag prevents `inventory_complete`. In particular, a failed or
anomalous required OLIS page is material. A successfully parsed OData/OLIS mismatch
is retained for operator review, while both sides' rows remain in the inventory.

## Inventory versus archive completeness

Inventory answers which source records are known. Archive acquisition answers which
eligible current payloads have been validated and retained. The Session Status and
Download Archive views therefore report these separately:

- total and downloadable documents;
- current payloads downloaded;
- pending/missing and retryable failures;
- terminal or non-downloadable records;
- known remote bytes and unknown-size documents; and
- local archive bytes.

When remote-size probing was enabled, the sum of known lengths is a **lower bound**
whenever any eligible document still has unknown size. A size probe does not validate
or download a payload and does not change inventory completeness.

Do not describe the corpus as a “complete historical archive” merely because all
sessions are inventory-complete. That phrase also requires the intended Download
Archive scope to finish with no unresolved eligible payload failures.
