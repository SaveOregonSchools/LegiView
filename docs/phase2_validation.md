# Phase 2 validation record

This record separates repeatable offline evidence, controlled live-source checks,
and the whole-history acceptance run. SQLite timestamps are UTC; the live work below
spanned 2026-09-03–04 Pacific time and is recorded as 2026-09-04 UTC.

## Access boundary

The operator explicitly confirmed that they reviewed and agree to the Oregon
Legislature Open Data acceptable-use agreement. Their review, and the application's
bounded requests, found no separate form, submission workflow, cookie, redirect, or
interactive acceptance gate. LegiView did not automate acceptance or bypass an
access control. If the Legislature introduces an official acceptance mechanism, the
operator must stop and use it.

The whole-history validation started after 5:00 p.m. Pacific, treated the run as the
permitted once-daily full refresh, used OData as the primary structured source, and
used only narrowly selected OLIS testimony pages for display reconciliation. No
proxy, alternate host, CAPTCHA workaround, parallel scraping, or rate-limit evasion
was used. The clients honor 429/`Retry-After`, bound retries, restrict requests and
redirects to the official hosts, and the historical collector retains a durable
logical source-fetch ledger. Low-level retries are bounded and logged; redirect hops
are bounded and validated. Neither is stored as an individual durable fetch row.

Runs 18 through 21 used temporary validation settings: `storage/archive`, a 7.5 GiB
floor, one OData worker, HTML concurrency 1, two download workers, and no inter-request
delay. Runs 22 through 26 were configured with the intended defaults: `archive`, a
5 GiB floor, one OData worker, HTML concurrency 1, two download workers, and a
0.25-second delay. Run 25 deliberately overrode its floor briefly for the low-space
test described below. Run 26 is the production-scale evidence for default pacing.

## Confirmed source behavior

The Phase 1 source-discovery results remained correct during the Phase 2 runs:

| Mapping or behavior | Live evidence |
| --- | --- |
| OData service/metadata | The official OData v3 endpoint responded and the live metadata continued to expose `CommitteePublicTestimonies`. |
| Required source spellings | The official `MeasureSponsor.LegislatoreCode` and `CommitteeAgendaItem.CommitteCode` spellings remained required. |
| Bill title | `Measure.RelatingTo` remained the OLIS Bill Title source; `RelatingToFull` was preserved separately. Run 19 retained both values for `2014R1/HB4001`. |
| OData continuation | Relative `odata.nextLink` values and opaque skiptokens were followed without constructing replacement cursors. |
| Modern testimony | Run 20 found 379 OData testimony records for `2026R1/SB1501`; the OLIS page displayed 378. OData remained primary. |
| Known zero-byte source record | Public-testimony ID `255890` remained OData-only and returned HTTP 200 with zero bytes/no usable MIME type. It is retained as `failed_terminal`, `invalid`, “Downloaded file is empty”; it was not falsely completed. |
| Historical presentations | Run 18 found 11 committee records for `2014R1/HB4111`; seven were displayed presentations and four remained other committee metadata. |
| Floor-letter cursor | `FloorLetter` still lacked source-created/source-modified fields needed for a safe watermark, so it used a complete session-scoped comparison by stable ID. |

The detailed property mapping is in [source_mapping.md](source_mapping.md), and the
Phase 1 discovery ledger remains in [phase1_validation.md](phase1_validation.md).

## Automated and migration validation

The normal test suite is network-independent. The final pass completed with **279
passed, four expected Windows symlink-permission skips, and zero failures in 51.46
seconds**:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The four skips cover file/directory path-containment cases that require the Windows
test account to hold the create-symbolic-link privilege. They are permission skips,
not failing assertions. `pip check`, Python bytecode compilation for `olis_archive`
and `tests`, CLI help startup, and `git diff --check` also completed cleanly (apart
from Git's informational working-tree line-ending notices). A final live-database
Flask smoke check returned HTTP 200 for health, home, Inventory Backfill, Download
Archive, Session Status, Operations, Bills, Documents, Runs, Settings, and Help.

Coverage includes configuration/path portability; Phase 1 database upgrades; all
old and new run types; session chronology/scope freezing; paged and incremental
OData ingestion; conservative cursor commits; testimony/presentation reconciliation;
source disappearance, return, unknown type, and type drift; restart/pause/cancel;
bounded archive claims; low-space pausing; payload validation/versioning; query
plans; SQL pagination; and streaming CSV exports.

### Migration and backward-compatibility evidence

| Check | Result |
| --- | --- |
| Phase 1 rows preserved | Populated schema-1 copies upgrade without data loss or foreign-key violations. |
| Existing run types | `collect_bill`, `collect_session`, and `retry_failures` remain accepted. |
| Phase 2 run types | `inventory_backfill` and `download_archive` are accepted by the extended run model. |
| Migration 001 integrity | Unchanged Git blob SHA-1: `b0665f9692644f6735221d023626f6ddba3a6cd4`. |
| Ordered migrations | 002 adds the historical inventory model; 003 separates display-reconciliation source families; 004 adds bounded archive claim plans; 005 adds claim cursors. All five migrations are applied to the live database. |
| Live SQLite integrity | `PRAGMA integrity_check` returned `ok`; `PRAGMA foreign_key_check` returned no rows. The five stored migration checksums exactly match the SQL migration files. |
| Legacy settings | A legacy byte floor remains readable and converts to GiB; the new GB value has precedence and is saved without destructively deleting the old generic setting. |

## Controlled live validation

| Scenario | Exact observed result |
| --- | --- |
| Historical presentation regression | Run 18, `2014R1/HB4111`, completed `01:43:44–01:43:53Z`: 11 logical documents; seven presentations downloaded and validated; four metadata-only committee records; 931,420 bytes; zero errors. |
| Zero-document/older metadata regression | Run 19, `2014R1/HB4001`, completed `01:44:00–01:44:03Z`: five sponsor rows, zero documents, zero errors. |
| Modern/high-testimony regression | Run 20, `2026R1/SB1501`, completed with the documented source error `01:44:08–01:48:23Z`: 397 documents (379 public testimony, 12 metadata-only/non-downloadable committee records, six floor letters), 357 downloaded, 27 valid-file skips, one terminal zero-byte failure, and 47,150,094 bytes downloaded. The four outcomes account for all 397 records. |
| Complete older session | Run 21, `2014R1`, completed `01:51:36–01:58:27Z`: 247 bills (159 HB/88 SB) and 2,350 logical documents (1,351 displayed presentations, 934 other committee records, 65 floor letters). It checked 204 candidate pages; 43 bills were not applicable. |
| Complete modern session | Run 22, `2026R1`, completed `01:59:21–02:18:11Z`: 286 bills (183 HB/103 SB) and 33,898 non-missing logical documents (31,438 public-testimony identities, 53 presentations, 2,318 other committee records, 89 floor letters). |
| Modern source discrepancies | Run 22 retained 191 nonmaterial reconciliation anomalies: 51 count mismatches, 53 OData-only display candidates, and 87 OLIS-page-only testimony rows. It had no material anomaly and no inventory error. An additional 34 previously observed testimony rows were conservatively retained as source-missing, outside the 33,898 current count. |
| Special-session/low-document case | Run 23, `2024S1` (“2024 1st Special Session”), completed `02:19:05–02:19:09Z`: one bill (`SB5801`), three sponsors, one Budget Report committee record, and no testimony or floor letters. |
| Incremental/idempotent rerun | Run 24 repeated `2024S1` `02:19:24–02:19:27Z`: one bill and the same one logical document, with stable identities and no duplicate. Date-capable entities used inclusive overlap; public testimony and floor letters used full-session comparison. |
| Limited payload acquisition | Run 25 froze session `2014R1`, kind `floor_letter`, and cutoff `2026-09-04T02:20:29.673Z`. Preflight reported 65 pending, zero known pending bytes, 65 unknown sizes, and a lower-bound estimate. It completed all 65 current payloads, 16,632,922 bytes, with no terminal failure. |
| Download low-space and restart recovery | Run 25 was deliberately stopped/restarted and its floor was temporarily raised above available space. Its recorded preflight at `02:20:29.673Z` observed 510,382,641,152 free bytes and the normal 5 GiB floor. Both `LowDiskSpace` errors (FloorLetter IDs 641 and 642) occurred at `02:21:06.659Z` and were resolved at `02:21:35.223Z` and `02:21:47.537Z`; those two items reached attempt three, all 65 ultimately validated, and no `.part` remained. The exact temporary override and a second contemporaneous free-byte sample were not persisted, so they are not reconstructed here. |
| Large rendered testimony page | During Run 26, `2025R1/SB210` produced a rendered OLIS page larger than the former 8 MiB bound. After an offline regression fixture/test and a still-bounded 32 MiB limit were added, the same run resumed and reconciled 14,221 displayed rows against 14,222 OData rows (one OData-only row and no page-only row). |
| Inventory stop/restart recovery | Whole-history Run 26 was deliberately interrupted and explicitly resumed as the same run. During a transient OLIS/DNS outage, the operator interrupted the original process without advancing incomplete source state; a bounded official request later succeeded and the same run resumed. The 74 outage-error rows across 37 bills (two display-source families per bill) were retained as resolved history rather than erased. The run then completed successfully. The automatic consecutive-failure pause added afterward is covered offline, not claimed as a live event. |

Counters in a resumed session item's `details_json` can describe the final incremental
overlap rather than total retained rows. The bill/document totals above and below are
therefore computed from the indexed base tables and source-presence state, not copied
from an overlap counter.

## Manual persisted-source comparisons

After Run 26 stopped, a stratified random sample was selected *before* making live
requests. SQLite `ORDER BY random() LIMIT 1` chose one current active record from each
predeclared, bounded-page cohort: historical displayed presentations (24,722
candidates), modern displayed testimony (27,610), and short-session non-displayed
committee records (77). Candidate OLIS pages were limited to 1–25 displayed rows to
keep validation source-respectful. Each selection was then checked with one narrowly
filtered OData request and its OLIS page using the normal User-Agent, host restrictions,
and pacing:

| Random cohort/result | Live and persisted comparison |
| --- | --- |
| Historical presentation: `2014R1/HB4125`, `CommitteeMeetingDocument` ID `32894` | The live raw OData object exactly matched persisted `raw_json`: `Presentation`, `SiengP testimony`, submitter `AOC`, committee `HJUD`, meeting `2014-02-06T13:00:00`. The eight-row OLIS page included the identity; persisted presence was active and `displayed=1`. |
| Modern testimony: `2021R1/HB3123`, `CommitteePublicTestimony` ID `9256` | The live raw OData object exactly matched persisted `raw_json`: `Testimony`, submitter `Joe Spendolini`, on behalf of `Klamath County Chamber Government Affairs`, position `Support`, city `Klamath Falls`, committee `HBH`, meeting `2021-03-01T08:00:00`. The 23-row OLIS page included the identity; persisted presence was active and `displayed=1`. |
| Short-session other record: `2020S2/HB4301`, `CommitteeMeetingDocument` ID `225690` | The live raw OData object exactly matched persisted `raw_json`: `Revenue Impact Statement`, `HB 4301 (revenue impact statement)`, submitter `staff`, committee `JP2SS`, meeting `2020-08-10T10:15:00`. The nine-row OLIS page did not include the non-presentation identity; persisted presence was active and `displayed=0`. |

An earlier deliberately selected cross-era comparison supplied a second independent
set of identities:

| Era/case | Live source result | Persisted comparison |
| --- | --- | --- |
| Historical presentation: `2014R1/SB1556`, `CommitteeMeetingDocument` ID `35228` | OData returned type `Presentation`, title `HarcleroadD testimony`, submitter `ODAA`, committee `SJUD`, meeting `2014-02-11T08:00:00`. The OLIS page displayed three records and included the target. | Stable identity, raw OData fields, active presence, parsed page identity, and `displayed=1` all matched. |
| Modern testimony: `2021R1/SB300`, `CommitteePublicTestimony` ID `13152` | OData returned title `Testimony`, submitter `Trent Hanson`, position ID `3983` (`Support`), city `Albany`, committee `JCT`, meeting `2021-03-09T08:00:00`. The OLIS page displayed eight records and included the target. | Stable identity, raw OData fields, active presence, parsed page identity, and `displayed=1` all matched. |
| Special-session non-displayed committee record: `2020S2/SB1701`, `CommitteeMeetingDocument` ID `225689` | OData returned type `Revenue Impact Statement`, title `SB 1701 -1 Revenue Impact Statement`, submitter `Staff`, committee `J2SS`, meeting `2020-08-10T10:20:00`. The OLIS page displayed three presentation records and did not include this target. | Stable identity, raw OData fields, active presence, parsed page result, and `displayed=0` all matched. |

## Whole-history inventory acceptance

Run 26 is the single whole-history Inventory Backfill acceptance run. It started at
`2026-09-04T02:23:27.444Z` (`2026-09-03 7:23:27 p.m. PDT`) with
`probe_remote_sizes=false`, `force_full=false`, and this frozen 27-session official
scope:

`2014R1, 2015R1, 2015I1, 2016R1, 2017R1, 2017I1, 2018R1, 2018S1, 2019R1, 2019I1, 2020R1, 2020S1, 2020S2, 2020S3, 2021R1, 2021I1, 2021S1, 2021S2, 2022R1, 2023R1, 2023I1, 2024R1, 2024S1, 2025R1, 2025I1, 2025S1, 2026R1`.

OLIS testimony HTML remains bounded at 32 MiB. This accommodates the observed
`2025R1/SB210` rendered testimony page, which exceeded the former 8 MiB cap,
without permitting unbounded page reads.

The list was resolved from official `LegislativeSessions.BeginDate` chronology at or
after `2014R1`; suffixes, year parity, and regular-session assumptions were not used.

Run 26 completed successfully at `2026-09-04T07:34:09.098Z` after processing all
27 sessions: 27 complete, zero incomplete, zero failed, zero unresolved run errors,
and no remote-size probe. Its final catalog contains:

| Acceptance measure | Final result |
| --- | ---: |
| Sessions in frozen scope / inventory complete | 27 / 27 |
| House bills / Senate bills | 11,022 / 7,250 |
| Total HB/SB measures | 18,272 |
| Current non-missing logical documents | 386,111 |
| Public testimony | 232,178 |
| Committee presentations / historical displayed records | 76,374 |
| Floor letters | 2,384 |
| Other/unknown committee records | 75,175 / 0 |
| OLIS candidate pages successful / not applicable / failed | 10,468 / 7,804 / 0 |
| Source-presence active / unknown (current) / missing (retained, excluded) | 384,917 / 1,194 / 34 |
| Material unresolved anomalies | 0 |

Every session/entity sync row is complete: 243 of 243 (27 sessions × nine OData
entities). The per-session bill/document results are:

| Session | HB | SB | Current documents | Inventory |
| --- | ---: | ---: | ---: | --- |
| `2014R1` | 159 | 88 | 2,350 | complete |
| `2015R1` | 1,617 | 1,024 | 24,300 | complete |
| `2015I1` | 0 | 0 | 0 | complete |
| `2016R1` | 150 | 103 | 4,046 | complete |
| `2017R1` | 1,525 | 1,122 | 31,351 | complete |
| `2017I1` | 0 | 0 | 0 | complete |
| `2018R1` | 165 | 67 | 6,380 | complete |
| `2018S1` | 1 | 0 | 10 | complete |
| `2019R1` | 1,509 | 1,104 | 37,952 | complete |
| `2019I1` | 0 | 0 | 0 | complete |
| `2020R1` | 172 | 81 | 7,383 | complete |
| `2020S1` | 15 | 8 | 1,109 | complete |
| `2020S2` | 5 | 6 | 84 | complete |
| `2020S3` | 2 | 4 | 8 | complete |
| `2021R1` | 1,465 | 925 | 39,326 | complete |
| `2021I1` | 0 | 0 | 0 | complete |
| `2021S1` | 0 | 2 | 28 | complete |
| `2021S2` | 0 | 4 | 12 | complete |
| `2022R1` | 160 | 94 | 9,812 | complete |
| `2023R1` | 1,685 | 1,151 | 59,585 | complete |
| `2023I1` | 0 | 0 | 0 | complete |
| `2024R1` | 170 | 96 | 15,218 | complete |
| `2024S1` | 0 | 1 | 1 | complete |
| `2025R1` | 2,037 | 1,267 | 111,536 | complete |
| `2025I1` | 0 | 0 | 0 | complete |
| `2025S1` | 2 | 0 | 1,722 | complete |
| `2026R1` | 183 | 103 | 33,898 | complete |

The 2,611 unresolved reconciliation anomalies are all nonmaterial warnings: 1,194
OLIS-page-only identities, 817 OData-only display candidates, and 600 count
mismatches. There are no unresolved unknown-type, type-drift, malformed-record,
metadata-drift, or host-policy anomalies. Thirty-four previously seen `2026R1/HB4001`
testimony identities are retained as source-missing rather than destructively deleted.
Thus 384,917 active plus 1,194 unknown identities equal the 386,111 current-document
total; including the 34 retained-missing histories yields 386,145 stored identities.

Because size probing was deliberately disabled, Run 26 has no whole-history probe
result. This means remote known/unknown-size totals are **not probed**, not that the
payloads are zero bytes. Inventory completion also does not mean the full historical
payload archive has been downloaded.

Manual archive-root relocation/reconciliation was also checked after Run 26. The
current dedicated `archive` root has a valid `.legiview-archive-root` ownership marker,
zero `.part` files, and 490 payload files. Thirty-four older payloads found only under
the Phase 1 `data/archive` default were copied into absent paths in the current root
with exclusive creation plus size/SHA-256 verification. The deterministic relative
paths, immutable `document_versions`, `current_version_id`, SHA-256 deduplication, and
`__v0002` suffix model were unchanged; no version-layout migration was introduced.
All 490 recorded downloaded document versions then validated at their current paths:
zero missing and zero mismatched. The old ignored `data/archive` tree remains intact
as a rollback copy; it was not destructively removed. Offline ownership regressions
also verify that arbitrary nonempty roots are rejected and recursive `.part` cleanup
requires the marker. One current in-scope terminal source record remains a known
source failure: zero-byte public-testimony ID `255890` for `2026R1/SB1501`. Normal
Download Archive eligibility excludes `failed_terminal`; a deliberate terminal retry
requires explicit operator selection.

## Historical differences observed

The official chronology contains regular, interim, short, and special keys. Several
resolved interim sessions have no HB/SB measures at all; other non-regular sessions
have very small but nonzero bill/document sets. Pre-2021 displayed testimony is
represented through `CommitteeMeetingDocument` presentation records, while modern
testimony is primarily `CommitteePublicTestimony`. Committee records also contain
non-testimony material such as budget reports. These are source-era differences,
not reasons to infer types from filenames or special-case individual sessions.

## Final sign-off checklist

- [x] Operator reviewed and agreed to the acceptable-use terms; no separate form was observed.
- [x] Three representative Phase 1 bills were run live.
- [x] Complete older and modern sessions were inventoried.
- [x] A resolved special session and zero-/low-document cases were validated.
- [x] High-testimony and historical-presentation cases were validated.
- [x] A limited Download Archive completed.
- [x] Inventory and download stop/restart recovery were exercised.
- [x] A completed session reran idempotently/incrementally.
- [x] The GB low-space pause was exercised.
- [x] Bounded manual OData/OLIS comparisons are recorded.
- [x] Whole-history Run 26 reached a terminal successful state and exact totals are recorded.
- [x] Migrations 004/005 are applied to the live database with clean integrity checks.
- [x] The final automated suite and static checks are recorded.

Do not describe this corpus as a “complete historical archive” merely because the
inventory completes. Full payload acquisition remains a separate, user-started
Download Archive operation and must finish its intended scope without unresolved
eligible payload failures before that description is accurate.
