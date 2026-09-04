# Phase 1 validation record

This record separates the source-discovery spike from end-to-end LegiView collector
runs. All observations below were made on 2026-09-03. Oregon Legislature records can
change later, so future runs should record their own source counts and timestamps.

## Access boundary

The official
[Open Data acceptable-use agreement](https://www.oregonlegislature.gov/citizen_engagement/Documents/OLODataAcceptableUseAgreement.pdf)
was reviewed as part of the source spike. Ordinary filtered requests and the live
collector runs received responses without an acceptance form, redirect, acceptance
cookie, or other interactive gate. No gate was bypassed.

The agreement nevertheless states that electronic acceptance is required. An operator
must read, accept, and follow the current terms through the Legislature's official
process if an acceptance step is presented. LegiView must not automate or evade it.
The validations used conservative, bounded access against only the documented OData,
OLIS testimony, and download routes.

Full source evidence and mappings are in
[source_mapping.md](source_mapping.md). No personal testimony text, complete HTML
page, or binary payload is reproduced here.

The sibling references were inspected read-only at EdScanner commit
`c93c2f875e91b60c78cf1b70e4c5339e965a918c` and EviManager commit
`75cd819654a7f49446672d2a3f7edf1c32d91ad5`; both working trees remained clean.
LegiView itself was delivered into the supplied standalone workspace, which was not
initialized as a Git worktree during this phase.

## Completed source-discovery spike

| Check | Observed result |
| --- | --- |
| Official entry point | The Legislature data page published `https://api.oregonlegislature.gov/odata/odataservice.svc/`. |
| Metadata | `$metadata` was fetched and inspected. Current metadata includes `CommitteePublicTestimonies`; exact keys/types and the source typos `LegislatoreCode` and `CommitteCode` were recorded. |
| Required entities | Live records were inspected for LegislativeSession, Measure, MeasureSponsor, Legislator, Committee, CommitteeAgendaItem, CommitteeMeeting, CommitteeMeetingDocument, CommitteePublicTestimony, and FloorLetter. |
| Bill title | OLIS Bill Title matched `Measure.RelatingTo` for the tested bills. `RelatingToFull` remains a separate retained value. |
| Sponsor values | Across 2,063 `2014R1` sponsor rows: Member/Chief 357, Member/Regular 1,506, Committee/Regular 10, and Presession/Regular 190. Member and committee codes resolved through session-scoped reference records. |
| Committee document types | `2014R1/HB4111`: Presentation 7, Witness Registration 2, Meeting Material 2. `2015R1/HB2745`: Presentation 45, Meeting Material 4, Witness Registration 1. `2026R1/SB1501`: 12 non-presentation committee documents using six observed raw types. |
| Testimony table | The initial `2026R1/SB1501` HTML contained all 378 displayed rows/IDs. DataTables performs client-side pagination only; no browser automation or secondary data request was needed. |
| OData/HTML reconciliation | OData returned 379 unique public-testimony rows while HTML displayed 378. OData-only ID 255890 advertised readiness but returned HTTP 200 with zero bytes and no MIME type. |
| Position mapping | Observed `3981` = Neutral, `3982` = Oppose, and `3983` = Support; unknown future values remain visible. |
| Numeric routes | Public testimony 244133, committee documents 313285/32769/49696, and floor letter 4701 returned valid PDFs from their respective official numeric route families. |
| OData continuation | A live response used a relative `odata.nextLink` with an opaque skip token; the client resolves and follows it until absent. |
| Required historical cases | `2014R1/HB4111` confirmed AtTheRequestOf and pre-2021 presentation behavior. `2014R1/HB4001` confirmed multiple chief/regular sponsors plus a Presession notice. |

## Automated suite

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

| Field | Result |
| --- | --- |
| Run time | 2026-09-03 11:36 PDT; 15.75 seconds |
| Python/platform | Python 3.14.5; Windows 11 (`10.0.26200`) |
| Passed | 91 |
| Skipped | 2 |
| Failed | 0 |
| Network behavior | The default suite made no live OLIS requests. |
| Skips | Two downloader/web file-symlink safety cases skipped because symlink creation was unavailable to the test user on this Windows host; lexical/reparse rejection still has non-symlink coverage. |

Coverage includes mapping and parser fixtures, OData continuation, idempotent SQLite
upserts and constraints, immutable document versions, run states and errors, archive
path safety, a local HTTP download server, retry and low-space handling, restart and
`.part` recovery, source-date incremental reference refresh, strict signature/hash
validation, competing download claims, worker shutdown, CSRF-protected Flask routes,
safe registered-file access, CLI wiring, and the cross-process instance lock.

Optional automated live tests, if added later, must remain disabled by default and be
explicitly gated with `OLIS_LIVE_TESTS=1`.

## Live run ledger

The commands used the same `CollectionService` as the Flask UI. Run numbering follows
the durable validation database; this ledger lists the acceptance-relevant runs and
omits intermediate diagnostics.

| Run | Scope | Result |
| ---: | --- | --- |
| 1 | `collect-bill 2014R1 HB4111` | `completed`; 11 logical documents, seven downloaded presentations, four metadata-only committee documents, 931,420 bytes, zero errors. |
| 2 | Rerun `2014R1/HB4111` | `completed`; all seven valid payloads skipped and no document version was duplicated. |
| 3 | `collect-bill 2014R1 HB4001` | `completed`; zero documents, two chief and two regular resolved member sponsors, plus the Presession filing notice. |
| 4 | `collect-bill 2026R1 SB1501` | `completed_with_errors`; 397 logical documents, 384 downloaded, one terminal empty-payload failure, 48,642,859 bytes. |
| 6 | Rerun `2026R1/SB1501` | `completed_with_errors`; all 384 valid payloads skipped, the one terminal source failure retained, and no new version created. |
| 7 | Explicit retry from the modern failure | `completed_with_errors`; only public-testimony ID 255890 was selected. It returned empty again and remained failed; no valid payload was touched. |
| 8 | `collect-session 2026R1 --max-bills 1` | `completed`; selected `2026R1/HB4001`, downloaded 34 of 34 files (1,199,762 bytes), zero errors. |
| 9 | Final post-hardening rerun `2014R1/HB4111` | `completed`; incremental reference refresh succeeded, all seven retained PDFs revalidated/skipped, zero downloads and zero errors. |

### Historical bill: `2014R1/HB4111`

Commands:

```powershell
.\.venv\Scripts\python.exe -m olis_archive --verbose collect-bill 2014R1 HB4111
.\.venv\Scripts\python.exe -m olis_archive show-bill 2014R1 HB4111
```

Run 1 retained the exact AtTheRequestOf value documented in the source mapping and
the single Presession filing notice. Its 11 OData committee documents reconciled to:

- seven `Presentation` records classified as `committee_presentation`, displayed in
  the historical presentation section and downloaded;
- two `Witness Registration` records retained as metadata-only other committee
  documents; and
- two `Meeting Material` records retained as metadata-only other committee documents.

All seven in-scope payloads were validated and stored with SHA-256, totaling 931,420
bytes. The four unrelated committee records were truthfully retained without being
silently called testimony. The run finished with zero errors.

Run 2 repeated discovery using the stable source identities. It revalidated and
skipped all seven completed files and created no duplicate logical rows, files, or
payload versions.

After the final reliability/security changes, Run 9 repeated this bill through the
finished collector. The reference stage exercised the verified source-date filter
path, retained 90 local legislators and 33 committees, and refreshed only the rows
returned at the inclusive watermarks. All seven PDFs again revalidated and were
skipped; the run finalized with zero errors and no new payload version.

### Older multiple sponsors: `2014R1/HB4001`

Commands:

```powershell
.\.venv\Scripts\python.exe -m olis_archive collect-bill 2014R1 HB4001
.\.venv\Scripts\python.exe -m olis_archive show-bill 2014R1 HB4001
```

Run 3 completed with no documents to download. It persisted and resolved two chief
member sponsors and two regular member sponsors while retaining the separate
Presession filing notice. The UI/CLI did not present that filing message as an
unnamed legislator.

### Modern bill: `2026R1/SB1501`

Commands:

```powershell
.\.venv\Scripts\python.exe -m olis_archive --verbose collect-bill 2026R1 SB1501
.\.venv\Scripts\python.exe -m olis_archive show-bill 2026R1 SB1501
```

Run 4 confirmed the Bill Title mapping from `Measure.RelatingTo`, 12 sponsors (six
chief and six regular), five committee agenda items, and this canonical document set:

| Normalized kind | Logical records | Payload result |
| --- | ---: | --- |
| `public_testimony` | 379 | 378 downloaded; ID 255890 rejected as empty |
| `committee_document_other` | 12 | Metadata only, as required for the observed non-presentation raw types |
| `floor_letter` | 6 | Six downloaded |
| **Total** | **397** | **384 downloaded, 12 metadata only, 1 failed** |

The 384 validated payloads totaled 48,642,859 bytes. SQLite contained 384 distinct
SHA-256/version records, and every version retained its producing run and final source
URL provenance. The OData-only public-testimony record 255890 again returned an empty
payload. MIME/signature and byte validation rejected it, recorded a terminal error,
and produced no false completed file. The run therefore correctly reported
`completed_with_errors` rather than hiding the source discrepancy.

Run 6 revalidated and skipped all 384 completed payloads, retained the single terminal
failure, reported `completed_with_errors`, and created no new payload versions.

Run 7 was an explicit retry of Run 4's failures. Its durable scope contained only ID
255890; the source remained empty and validation failed again. The retry neither
selected nor redownloaded any of the 384 valid files.

### Bounded session collection

Command:

```powershell
.\.venv\Scripts\python.exe -m olis_archive collect-session 2026R1 --max-bills 1
```

Run 8 honored the one-bill limit and selected `2026R1/HB4001`. It used the same bill
collector, durable stages, canonical storage, and downloader as Collect Bill. It
discovered 34 logical documents, then downloaded and validated all 34 files
(1,199,762 bytes) with zero errors.

## Database and archive verification

| Check | Result |
| --- | --- |
| Foreign keys | `PRAGMA foreign_key_check` returned zero violations after the live runs. |
| Stable logical identities | Bill reruns did not duplicate bills, sponsors, or logical documents. |
| Immutable versions | HB4111 remained at seven payload versions after its rerun; SB1501 remained at 384 after rerun and targeted retry. |
| SHA-256 | Every completed live payload had a stored SHA-256; SB1501 had 384 distinct validated SHA/version rows. |
| Provenance | The 384 SB1501 version rows all retained collection-run and source-URL provenance. |
| Metadata-only records | Four HB4111 and 12 SB1501 other committee documents were retained without being downloaded or misclassified. |
| Empty response | SB1501 testimony ID 255890 never became a completed file or version. |
| Idempotent files | Both bill reruns skipped valid local files and did not create duplicate versions. |
| Retry isolation | Run 7 touched only its single selected failed source identity. |
| Independent on-disk rehash | All 425 downloaded files (50,774,041 bytes) were read again after implementation: zero missing files, byte-count mismatches, or SHA-256 mismatches; zero `.part` files remained. |
| SQLite integrity | `PRAGMA integrity_check` returned `ok`; stable-key queries found zero duplicate bill, sponsor, or document identities. |

The automated downloader tests separately verified `.part` staging, streamed hashes,
content-length failures, MIME/signature validation, extension correction, redirects,
429/`Retry-After`, 5xx retry, collision refusal, atomic no-replace promotion, free-space
protection, and safe startup cleanup.

## Web UI and process-lock validation

The application was started on `127.0.0.1:5055` for representative UI validation.

| Check | Result |
| --- | --- |
| Required pages | Home, Collect Bill, Collect Session, Browse Bills, Bill Detail, Browse Documents, Document Detail, Run History, Run Detail, Retry Failures, Settings, and Help/Data Sources returned HTTP 200. |
| Supporting endpoint | `/health` returned HTTP 200. |
| Visual inspection | Home, Browse Bills, the live SB1501 Bill Detail, and Settings were inspected. The home page used the two colored module columns, eight At a Glance statistics, and recent runs; bill detail grouped the live documents. |
| Durable web work | Route tests confirmed collection POST creates a queued run without executing source access in the request. Run Detail reads persisted stages, items, and errors and refreshes active runs every 15 seconds. |
| Registered file access | Route tests served a registered regular file and rejected traversal and missing paths. Runtime also rejects symlinks; the OS prevented creation of the separate symlink test fixture. |
| Restart/low-space | Automated integration tests confirmed startup interruption normalization and `.part` cleanup, plus low-space pause and resume with completed files skipped. |
| Cross-process mutation | While the server owned the database lock, a second mutating CLI process was rejected before recovery or collection. |
| Concurrent read | `show-bill` ran successfully while the server held the mutation lock; it performed no startup normalization or `.part` cleanup. |
| Shutdown ownership | Worker tests confirmed queued runs remain durable, active runs become interrupted, non-quiescent threads remain tracked, and the instance lock is released only after quiescence (otherwise OS process teardown owns release). |
| Local POST/file hardening | All mutating forms require a session CSRF token. Registered files are revalidated against stored filename, MIME, length, and SHA-256 before serving with `nosniff` and sandbox response headers. |

## Final sign-off

| Acceptance area | Result |
| --- | --- |
| Source mapping | Passed; official endpoint/metadata, observed enums, HTML behavior, numeric routes, and historical differences documented. |
| Modern bill collection | Passed with one truthfully retained upstream empty-payload failure; 397 logical records and 384 validated files. |
| Historical bill collection | Passed; seven presentations downloaded and four other committee records retained as metadata. |
| Multiple-sponsor mapping | Passed; chief/regular member resolution and Presession notice verified. |
| File hierarchy, hashes, staging, rerun, retry | Passed through live runs plus the local downloader suite. |
| Database constraints and restart | Passed; zero foreign-key violations and durable restart tests successful. |
| UI | Passed route and representative visual validation. |
| CLI shared-service execution | Passed bill, session, show, rerun, and targeted retry validations. |
| Complete automated suite | 91 passed, 2 environment-only skips, 0 failed in 15.75 seconds. |

At Phase 1 sign-off, the known limitations were on-demand HB/SB scope, single-owner
mutation rather than distributed workers, no scheduled all-history orchestration, no
OCR/full-text/AI analysis, no antivirus, and source-change detection that relied
primarily on official modification metadata. Phase 2 subsequently extended the same
run model with full-history inventory, per-entity durable cursors, completeness, and
separate archive acquisition; see [phase2_validation.md](phase2_validation.md). The
zero-byte upstream testimony record remains a known source-data/download discrepancy,
not a silently accepted archive payload.
