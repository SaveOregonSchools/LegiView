# OLIS source mapping

Initially observed against the official Oregon Legislature services on
**2026-09-03**, with the bounded expanded-measure checks noted below performed on
**2026-09-04**. This file records observed source behavior; it is not an invented
enum catalogue.
Unknown values must be retained verbatim and surfaced rather than coerced into a
known value.

## Official entry points and access boundary

- Legislature data page:
  <https://www.oregonlegislature.gov/citizen_engagement/Pages/data.aspx>
- Published OData base endpoint:
  <https://api.oregonlegislature.gov/odata/odataservice.svc/>
- Published metadata endpoint:
  <https://api.oregonlegislature.gov/odata/odataservice.svc/$metadata>
- Published data-model diagram:
  <https://www.oregonlegislature.gov/citizen_engagement/Documents/OLOData-Model.pdf>
- Official legislative measure types:
  <https://www.oregonlegislature.gov/citizen_engagement/Pages/Measure-Types.aspx>
- Legislative drafting/measure-prefix reference:
  <https://www.oregonlegislature.gov/la/Documents/administrator_manual2014.pdf>
- Acceptable-use agreement:
  <https://www.oregonlegislature.gov/citizen_engagement/Documents/OLODataAcceptableUseAgreement.pdf>
- OLIS:
  <https://olis.oregonlegislature.gov/>

The data page links directly to the service and metadata. During this spike the
service returned HTTP 200 to ordinary filtered requests with a descriptive User-Agent;
there was no HTML acceptance form, redirect, acceptance cookie, separate submission
form, or other interactive gate in the request path, and no gate was bypassed. The
operator explicitly confirmed that they reviewed and agreed to the published terms
and found no separate form to submit. The linked agreement nevertheless says users
must electronically accept its terms. LegiView must tell the operator to review and
agree to those terms before live collection; it must never automate acceptance or
evade a gate. If an official interactive mechanism is presented, the operator must
use it.

Relevant current agreement provisions include: prefer OData to regular site
scraping; on-demand queries are allowed; data is updated approximately every five
minutes; incremental database refreshes may run as needed; full refreshes are limited
to once per day after business hours (5:00 p.m.-6:00 a.m. Pacific); and clients must
not impair OLIS availability.

## Service document and metadata

The service document returned `application/json;odata=minimalmetadata` and exposed
these entity sets:

1. `LegislativeSessions`
2. `Measures`
3. `Committees`
4. `CommitteeMeetings`
5. `CommitteeAgendaItems`
6. `CommitteeStaffMembers`
7. `CommitteeMeetingDocuments`
8. `ConveneTimes`
9. `FloorSessionAgendaItems`
10. `Legislators`
11. `MeasureAnalysisDocuments`
12. `MeasureDocuments`
13. `MeasureHistoryActions`
14. `MeasureSponsors`
15. `CommitteeProposedAmendments`
16. `FloorLetters`
17. `CommitteeVotes`
18. `MeasureVotes`
19. `CommitteeMembers`
20. `CommitteePublicTestimonies`

`CommitteePublicTestimonies` is present in the live `$metadata`, but is absent from
the currently linked one-page data-model PDF. Prefer live metadata when the two
artifacts differ.

Important exact metadata details:

- `Measure` key: `MeasureNumber`, `MeasurePrefix`, `SessionKey`.
- `MeasureSponsor` key: `MeasureSponsorId` (`Edm.Decimal`).
- `Legislator` key: `LegislatorCode`, `SessionKey`.
- `Committee` key: `CommitteeCode`, `SessionKey`.
- `CommitteeAgendaItem` key: `CommitteeAgendaItemId`.
- `CommitteeMeeting` key: `CommitteeCode`, `MeetingDate`, `SessionKey`.
- `CommitteeMeetingDocument` key: `CommitteeMeetingDocumentId`.
- `FloorLetter` key: `FloorLetterId`.
- The sponsor property is misspelled in the official model as
  **`LegislatoreCode`**. Do not query `LegislatorCode` on `MeasureSponsors`.
- The agenda-item property is misspelled as **`CommitteCode`**. The corresponding
  committee/meeting/document property is correctly spelled `CommitteeCode`.
- `FloorLetter` has no `CreatedDate` or `ModifiedDate` properties in current
  metadata.
- JSON serializes `Edm.Decimal` identifiers/order fields as strings. For example,
  observed `MeasureSponsorId` and `PrintOrder` values were `"157656"` and `"1"`.
- Source `Edm.DateTime` values have no UTC offset. Preserve the source string and do
  not silently label it UTC without a documented source-time-zone rule.

## Legislative session mapping

| Source property | Local field | Notes |
| --- | --- | --- |
| `SessionKey` | `session_key` | Stable official key, for example `2026R1`. |
| `SessionName` | `session_name` | `2026 Regular Session` for `2026R1`. |
| `BeginDate` | source begin date | Non-null `Edm.DateTime`. |
| `EndDate` | source end date | Nullable. |
| `CreatedDate` | source created date | Preserve raw value. |
| `ModifiedDate` | source modified date | Nullable. |
| `DefaultSession` | source default flag | Do not use it in place of explicit user session selection. |

Observed `2026R1`: begin `2026-02-02T00:00:00`, nullable end date, session name
`2026 Regular Session`.

## Legislative-measure mapping and supported prefixes

Oregon's official measure-types page describes six categories. LegiView supports
the House- and Senate-origin prefix for each category, for 12 exact prefixes:

| Official category | House-origin prefix and meaning | Senate-origin prefix and meaning | Local `measure_type` |
| --- | --- | --- | --- |
| Bill | `HB` — House Bill | `SB` — Senate Bill | `bill` |
| Joint Resolution | `HJR` — House Joint Resolution | `SJR` — Senate Joint Resolution | `joint_resolution` |
| Concurrent Resolution | `HCR` — House Concurrent Resolution | `SCR` — Senate Concurrent Resolution | `concurrent_resolution` |
| Resolution | `HR` — House Resolution | `SR` — Senate Resolution | `resolution` |
| Joint Memorial | `HJM` — House Joint Memorial | `SJM` — Senate Joint Memorial | `joint_memorial` |
| Memorial | `HM` — House Memorial | `SM` — Senate Memorial | `memorial` |

The first letter establishes the originating chamber: every `H...` prefix maps to
`House`, and every `S...` prefix maps to `Senate`. For joint and concurrent
measures, this is an origin rule, not a claim that only that chamber acts on the
measure. The database retains the established bill-oriented table and identifier
field names for compatibility, while `measure_type` makes the generic meaning
explicit.

This supported-prefix catalogue is deliberately exact. A new or unexpected source
prefix is retained as a source-contract error for review rather than silently
coerced into one of these meanings. Likewise, the observations below do not make
the current set an undocumented promise about all past or future source values.

| Source property | Local field | Observed rule |
| --- | --- | --- |
| `SessionKey` | `session_key` | Exact official key. |
| `MeasurePrefix` | `measure_prefix` | Exact member of the 12-prefix supported catalogue above. |
| `MeasureNumber` | `measure_number` | `Edm.Int32`; do not store leading-zero display variants as the number. |
| prefix + number | compact/display IDs | Examples: `SB1501` / `SB 1501` and `HJR11` / `HJR 11`; identity is canonicalized and checked against prefix and number. |
| `PrefixMeaning` | raw prefix meaning | Preserve the source text as an audit field; the local type/chamber mapping comes from the explicit catalogue above. |
| `AtTheRequestOf` | `at_the_request_of` | Preserve exact punctuation/text. |
| `RelatingTo` | `bill_title` and `relating_to` | **Confirmed authoritative OLIS “Bill Title” display source.** |
| `RelatingToFull` | `relating_to_full` | Preserve separately; it can be longer than the displayed Bill Title. |
| `CatchLine` | `catchline` | First text shown under OLIS “Catchline/Summary”. |
| `MeasureSummary` | `measure_summary` | Full digest/summary shown after the catchline. Preserve whitespace/raw source. |
| `ChapterNumber` | `chapter_number` | Nullable string, not an integer in metadata. |
| `EffectiveDate` | `effective_date` | Nullable official `Edm.DateTime`; do not infer from measure text when populated. |
| `Vetoed` | `vetoed` | Nullable boolean. |
| `EmergencyClause` | `emergency_clause` | Nullable boolean. |
| `CurrentVersion` | `current_version` | Nullable one-character string. |
| `CurrentLocation` | `current_location` | Non-null source status text. |
| `CurrentCommitteeCode` | `current_committee_code` | Nullable. |
| `CurrentSubCommittee` | raw/current subcommittee | Nullable. |
| `CreatedDate` / `ModifiedDate` | OData source dates | Preserve independently of local first-seen/last-synced timestamps. |

### Bounded expanded-scope observations

Filtered live OData queries on 2026-09-04 confirmed that the non-bill categories
are populated within LegiView's supported historical range:

| Session | Non-`HB`/`SB` rows | Counts by observed prefix |
| --- | ---: | --- |
| `2025R1` | 162 | `HCR` 42, `SCR` 34, `SJR` 34, `HJR` 22, `HJM` 14, `SJM` 10, `HR` 3, `SR` 3 |
| `2014R1` | 19 | `SCR` 7, `HCR` 5, `SJR` 4, `HJM` 1, `SJM` 1, `SR` 1 |

A targeted whole-catalogue check found `2007R1/HM1` with
`PrefixMeaning=House Memorial`; no `HM` row was returned at or after the validated
`2014R1` support boundary. `SM` was observed in supported sessions including
`2017R1` and `2019R1`. A `$top=1` filter excluding every one of the 12 mapped
prefixes returned no row. Those are bounded observations of the current source, not
an enum guarantee or a promise that a new prefix can never appear.

`2025R1/HJR11` was used as a bounded non-bill integration sample. Its structured
queries returned 10 sponsor rows, one committee agenda item, two committee meeting
documents, 114 public-testimony rows, and no floor letters. The canonical OLIS
testimony page was also reachable at
<https://olis.oregonlegislature.gov/liz/2025R1/Measures/Testimony/HJR11>.

There is no `BillTitle` property. OLIS overview HTML was compared directly to OData:

- `2026R1/SB1501` displays `Relating to the Moda Center; and declaring an
  emergency.` as Bill Title, exactly its OData `RelatingTo`.
- `2014R1/HB4111` displays `Relating to public infrastructure; and declaring an
  emergency.`, exactly its OData `RelatingTo`.
- `2014R1/HB4001` demonstrates why `RelatingToFull` is separate: it includes
  `creating new provisions; amending ORS 238.088`, while the displayed/title
  `RelatingTo` omits that fuller clause.

## Sponsor mapping and resolution

The following combinations were observed in all 2,063 `MeasureSponsors` rows
returned for the filtered `2014R1` session, plus the current `2026R1/SB1501`
records:

| `SponsorType` | `SponsorLevel` | Code fields | Count in 2014R1 | Normalized interpretation |
| --- | --- | --- | ---: | --- |
| `Member` | `Chief` | `LegislatoreCode` set | 357 | chief, `legislator` |
| `Member` | `Regular` | `LegislatoreCode` set | 1,506 | regular, `legislator` |
| `Committee` | `Regular` | `CommitteeCode` set | 10 | regular, `committee` |
| `Presession` | `Regular` | neither code; message set | 190 | `other` / filing notice, not a person |

These strings are case-sensitive source values. Only `Chief` and `Regular` were
observed for `SponsorLevel` in this spike. `Presession` is not a named sponsor: its
meaning is carried by `PresessionFiledMessage`, such as `(Presession filed.)` or a
long chamber-rule filing notice. Preserve and display the notice separately; do not
render it as an unnamed regular sponsor. Preserve any future unknown type/level.

Resolution rules confirmed from live records:

- For `SponsorType == "Member"`, join `(SessionKey, LegislatoreCode)` to
  `(Legislator.SessionKey, Legislator.LegislatorCode)` and display the trimmed
  `FirstName + LastName`. There is no Legislator navigation property on
  `MeasureSponsor` in current metadata.
- For `SponsorType == "Committee"`, join `(SessionKey, CommitteeCode)` and form the
  public display name from `CommitteeType + CommitteeName` when appropriate. For
  example, `2014R1/HB4155` sponsor code `HRULES` resolves to type
  `House Committee On` and name `Rules` (display `House Committee On Rules`).
- Keep the source code even after resolving a display name.

Examples: `2014R1/HB4001` has chief members Margaret Doherty (`Rep Doherty`) and
Floyd Prozanski (`Sen Prozanski`), regular members Bill Kennemer (`Rep Kennemer`)
and Jeff Kruse (`Sen Kruse`), plus one `Presession` filing notice. `2026R1/SB1501`
has 12 `Member` rows: six chief and six regular.

## Committee context mapping

`CommitteeAgendaItem` joins a measure by `SessionKey`, `MeasurePrefix`, and
`MeasureNumber`. Join its misspelled `CommitteCode` plus `SessionKey` to `Committee`.
Join a meeting/document using the exact source `MeetingDate`, not a date-only value.

Observed example for `2014R1/HB4111`:

- Agenda item `110066`: `CommitteCode=HTED`, meeting
  `2014-02-05T15:00:00`, type `Public Hearing`, action `Heard`.
- Committee `HTED`: `CommitteeName=Transportation and Economic Development`,
  `CommitteeType=House Committee On`, `HouseOfAction=H`.
- The matching `CommitteeMeeting` has location `HR E`, status code `S`, status
  `Scheduled`, agenda revision 2, and an official `AgendaUrl`.

## Committee documents and normalized kinds

`CommitteeMeetingDocument` supplies `CommitteeMeetingDocumentId`, session,
committee, exact meeting timestamp, exhibit reference/title, submitter, raw
`DocumentType`, optional measure prefix/number, official `DocumentUrl`, and source
created/modified dates.

Raw values actually observed:

| Sample | Observed `DocumentType` values |
| --- | --- |
| `2014R1/HB4111` | `Presentation` (7), `Witness Registration` (2), `Meeting Material` (2) |
| `2015R1/HB2745` | `Presentation` (45), `Meeting Material` (4), `Witness Registration` (1) |
| `2026R1/SB1501` | `Preliminary SMS`, `Witness Registration`, `Revenue Impact Statement`, `Fiscal Impact Statement`, `Meeting Material`, `Budget Report` |

Safe normalization established in Phase 1 and retained in Phase 2:

- Exact raw `Presentation` -> `committee_presentation`, retaining source section
  `presentations_displayed_in_committee` when confirmed by the OLIS page.
- The other values above -> `committee_document_other` unless a stronger structured
  source establishes another kind.
- Do **not** use words such as “testimony” or “letter” in an old exhibit title as the
  sole reason to convert it to `legacy_testimony`. Preserve the title and raw type;
  use `legacy_testimony` only when stronger per-record evidence exists.
- Unknown raw values -> `unknown` or `committee_document_other` with the raw value
  visible; never discard them.

## Modern submitted written public testimony

Official page pattern:

`https://olis.oregonlegislature.gov/liz/<SESSION>/Measures/Testimony/<MEASURE>`

Example:
<https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501>

### Current OData source

The live service now exposes `CommitteePublicTestimonies`. Its useful properties map
as follows:

| Source property | Local meaning |
| --- | --- |
| `CommTestId` | Numeric public-testimony document ID. |
| `SubmitterFirstName` + `SubmitterLastName` | Trim each and combine for submitter. Source values can contain trailing spaces. |
| `BehalfOf` | On behalf of. |
| `Organization` | OLIS “City or Organization”. |
| `DocumentDescription` | Display title/type; observed `Testimony`, `Letter`, `Report`, and `Article`. |
| `PositionOnMeasureId` | Preserve raw ID and map only confirmed values below. |
| `SessionKey`, `MeasurePrefix`, `MeasureNumber` | Legislative-measure identity. |
| `CommitteeCode`, `MeetingDate` | Join to committee and meeting. |
| `PdfCreatedFlag` | Raw readiness flag; it is not sufficient proof of valid bytes. |
| `DocumentUrl` | Not a file URL in the observed records; it was only the session root. |
| `CreatedDate`, `ModifiedDate` | Source timestamps. |

Confirmed `2026R1` position mapping, joined by `CommTestId` to the labels rendered in
the OLIS HTML table:

- `3981` -> `Neutral` (example document `246889`)
- `3982` -> `Oppose` (example document `244244`)
- `3983` -> `Support` (example document `244133`)

Preserve any unrecognized ID and leave its normalized position unknown.

The metadata declares an unusual composite entity key containing
`CommitteeCode`, `CommTestId`, `CreatedDate`, `MeetingDate`, `PdfCreatedFlag`,
`SubmitterFirstName`, and `SubmitterLastName`. The tested measure had unique
`CommTestId` values. LegiView's cross-source document identity should still include
session + measure + source entity type + numeric ID so document families cannot collide.

### HTML shape and pagination

The initial HTTP response contains the complete displayed table; browser automation
and a second data request are not needed:

- Table id is `ExhibitsTable` despite this being public testimony.
- Headers are `Title`, `Submitter`, `On Behalf of`, `Position`,
  `City or Organization`, `Meeting`, and `Committee`.
- On `2026R1/SB1501`, raw HTML contained 378 data rows and 378 unique public
  testimony IDs.
- The page references
  `/liz/Areas/Measures/Scripts/Overview/testimonyDsp.js`. That script only calls
  jQuery DataTables on the already-rendered table with `pageLength: 100`, a
  10/25/50/100/All menu, and no Ajax/server-side source.
- Parse rows from the table/section, not global link occurrences: the page contains
  each document href twice for responsive markup (756 occurrences for 378 IDs).
- A successful “No items to display” response is a valid zero-result page.

The whole-history Phase 2 run also exposed a legitimate high-volume instance of
the same modern table shape. `2025R1/SB210` rendered 14,221 displayed testimony
rows while OData returned 14,222 records (one OData-only row). Its HTML exceeded
8 MiB, so LegiView's still-bounded OLIS HTML ceiling is 32 MiB. This was a volume
variant, not a new parser structure or document type.

### OData/HTML reconciliation quirk

For the same live `2026R1/SB1501` query, OData returned 379 unique
`CommitteePublicTestimonies` rows, while OLIS displayed 378. OData-only ID `255890`
had `PdfCreatedFlag=Y`, but a HEAD request to its canonical numeric route returned
HTTP 200 with content length 0 and no MIME type. It was correctly absent from the
displayed table. Consequently:

1. OData is the preferred structured metadata source.
2. Parse the returned OLIS page to obtain the complete *displayed* list and its
   official download links/labels.
3. Reconcile by `CommTestId`.
4. Do not treat `PdfCreatedFlag=Y` alone as successful download validation; reject
   empty payloads and require MIME/signature/byte validation.
5. Retain an OData-only record for audit if desired, but do not claim it was displayed
   or downloaded; mark the source discrepancy explicitly.

## Historical testimony/presentation behavior

OLIS itself states on pre-2021 pages that testimony for sessions before the 2021
Regular Session can be found in **Presentations Displayed in Committee**.

The same `ExhibitsTable` id is reused, but its old shape has only `Title`,
`Submitter`, `Meeting`, and `Committee`. Old rows link to
`CommitteeMeetingDocument/<id>`, not `PublicTestimonyDocument/<id>`. All rows are
present in the initial HTML and receive only client-side DataTables pagination.

Two exact reconciliations were tested:

- `2014R1/HB4111`: HTML displayed 7 rows/IDs. OData returned 11 committee documents:
  exactly the 7 raw `Presentation` records shown in that section, plus two
  `Witness Registration` and two `Meeting Material` records not shown there.
- `2015R1/HB2745`: HTML displayed 45 rows/IDs. OData returned 50 committee documents:
  exactly the 45 raw `Presentation` records shown, plus four `Meeting Material` and
  one `Witness Registration` record not shown there.

Deduplicate the HTML and OData representations by
`CommitteeMeetingDocumentId`. Preserve both source section and raw `DocumentType`.

## Floor letters

`FloorLetter` supplies `FloorLetterId`, nullable measure prefix/number, session,
`LetterDate`, one-character `Chamber`, `LetterDescription`, `LetterTitle`, and
`FloorLetterUrl`. Observed chamber values on `2026R1/SB1501` were `S` and `H`, mapped
to Senate and House while retaining the raw value. Current metadata supplies no
source created/modified timestamp and no submitter property.

`2026R1/SB1501` had six OData floor letters (IDs 4701, 4703, 4704, 4716, 4717,
4718), and its OLIS page displayed the same six. OData already contains the required
floor-letter fields and canonical download URL.

## Confirmed numeric download routes

| Family | Canonical official route | Live HEAD validation |
| --- | --- | --- |
| Public testimony | `https://olis.oregonlegislature.gov/liz/<SESSION>/Downloads/PublicTestimonyDocument/<CommTestId>` | `2026R1/244133`: HTTP 200, `application/pdf`, 118,237 bytes. |
| Committee meeting document/presentation | `https://olis.oregonlegislature.gov/liz/<SESSION>/Downloads/CommitteeMeetingDocument/<CommitteeMeetingDocumentId>` | `2026R1/313285`: PDF, 140,038 bytes; `2014R1/32769`: PDF, 221,677 bytes; `2015R1/49696`: PDF, 50,422 bytes. |
| Floor letter | `https://olis.oregonlegislature.gov/liz/<SESSION>/Downloads/FloorLetter/<FloorLetterId>` | `2026R1/4701`: HTTP 200, `application/pdf`, 84,387 bytes. |

The tested responses did not provide `Content-Disposition`; use a trusted/sanitized
source title plus MIME/signature-supported extension or a stable fallback filename.
Never derive the numeric route for one family using an ID from another family.

## OData request and continuation behavior

- Use filtered, URL-encoded OData queries and request JSON.
- Live `Legislators` and `Committees` requests accepted OData v3 source-date
  predicates such as `ModifiedDate gt datetime'2026-01-01T00:00:00'`. A combined
  `ModifiedDate ge ... or CreatedDate ge ...` predicate was also verified on
  2026-09-03. LegiView uses an inclusive, per-session retained source-date
  watermark for subsequent reference refreshes, while the first refresh remains
  complete. The inclusive boundary intentionally overlaps stable upserts.
- Response shape uses `odata.metadata` and `value` (rather than only the OData v4
  `@odata.context` spelling).
- A filtered `2015R1` sponsor response returned an `odata.nextLink` whose value was
  relative, beginning `../odataservice.svc/MeasureSponsors?...` and ending with an
  opaque `$skiptoken=73816M` in the observation. Resolve it against the current URL;
  do not concatenate it blindly or assume continuation links are absolute.
- Follow `odata.nextLink` until absent. Treat the token as opaque and retain the same
  User-Agent, timeout, retry, and throttling behavior on each request.

## Phase 2 historical sync contract

The 2026-09-03 metadata inspection supports session-scoped acquisition of the
following sets. The historical orchestrator uses the official property names shown
here and checks the live `$metadata` contract before trusting a full backfill.

| Entity set | Stable ordering/key fields used by inventory | Incremental source-date fields | Presence strategy |
| --- | --- | --- | --- |
| `Measures` | `MeasurePrefix`, `MeasureNumber` | `CreatedDate`, `ModifiedDate` | Supported-measure full session comparison; inclusive watermark on later incremental runs. |
| `Legislators` | `LegislatorCode` | `CreatedDate`, `ModifiedDate` | Session-scoped reference upsert; inclusive watermark supported. |
| `Committees` | `CommitteeCode` | `CreatedDate`, `ModifiedDate` | Session-scoped reference upsert; inclusive watermark supported. |
| `MeasureSponsors` | `MeasureSponsorId` | `CreatedDate`, `ModifiedDate` | Supported-measure upsert; official `LegislatoreCode` spelling retained. |
| `CommitteeMeetings` | `CommitteeCode`, `MeetingDate` | `CreatedDate`, `ModifiedDate` | Session-scoped upsert. |
| `CommitteeAgendaItems` | `CommitteeAgendaItemId` | `CreatedDate`, `ModifiedDate` | Supported-measure upsert; official `CommitteCode` spelling retained. |
| `CommitteeMeetingDocuments` | `CommitteeMeetingDocumentId` | `CreatedDate`, `ModifiedDate` | Supported-measure structured document inventory. |
| `CommitteePublicTestimonies` | `CommTestId` | `CreatedDate`, `ModifiedDate` | Supported-measure primary modern testimony inventory. |
| `FloorLetters` | `FloorLetterId` | none in current metadata | Complete session/supported-measure comparison every time; never a fabricated watermark. |

Every request includes an exact `SessionKey` predicate. Measure-scoped sets also use
one shared exact predicate enumerating `HB`, `SB`, `HJR`, `SJR`, `HCR`, `SCR`, `HR`,
`SR`, `HJM`, `SJM`, `HM`, and `SM`. Returned rows are validated against that scope;
an out-of-scope session or unsupported prefix is a source error rather than something
silently persisted.

The initial/forced-full strategy is authoritative only after every continuation page
has been fetched, validated, and persisted. A successful full comparison can then
update presence state. Later date-capable queries use:

```text
(CreatedDate ge datetime'<watermark>' or ModifiedDate ge datetime'<watermark>')
```

The boundary is intentionally inclusive. Incremental results never prove source
disappearance. The next watermark is committed only after complete page consumption;
a fetch or persistence failure leaves the prior successful cursor in place.

The historical session boundary is also source-derived. LegiView locates official
session key `2014R1`, compares official `BeginDate` values, and includes every session
with equal or later chronology. It does not infer scope from key suffixes, calendar
parity, `DefaultSession`, or a regular-session-only list, so later short and special
sessions remain eligible. The exact selected keys are frozen into the run scope.

Live/captured metadata validation treats a missing required entity set or mapper
property as a material diagnostic. It explicitly expects the confirmed official
typos rather than silently substituting a guessed corrected name. Source timestamps
remain source strings; current `Edm.DateTime` values do not carry a UTC offset.

## Remote-size probe behavior

An optional inventory probe uses the same official-host and redirect restrictions as
downloads. It tries HEAD first and, when HEAD is rejected or lacks a usable total,
uses a bounded one-byte Range GET. The partial response's `Content-Length` is not
mistaken for the full object size; a total is accepted from a valid
`Content-Range`. Probe status, time, HTTP status, final URL, type, known length,
ETag, and Last-Modified are retained when available. No full response body is fetched
merely to estimate size.

The probe is advisory. Unknown sizes remain eligible, and the sum of known lengths is
reported as a lower bound rather than as a complete storage forecast.

## Representative measures and fixture candidates

### `2025R1/HJR11` (expanded-scope sample)

- Exercises the same structured measure, sponsor, agenda, committee-document, and
  public-testimony identities used by the generic collection pipeline.
- The bounded source counts were 10 sponsors, one agenda item, two committee
  documents, 114 public-testimony rows, and zero floor letters.
- These counts are a point-in-time validation sample, not a permanent cardinality
  assertion.

### `2026R1/SB1501` (required modern sample)

- Bill Title source verified as `RelatingTo`.
- `AtTheRequestOf=null`, chapter `74`, version `B`, emergency clause true,
  effective date `2026-03-31T00:00:00`, vetoed false.
- 12 sponsors (six chief, six regular), five committee agenda items, 12 committee
  documents, 379 OData testimony records/378 displayed download records, and six
  floor letters at observation time.
- Good sanitized fixtures: Measure JSON; sponsor JSON; committee-document JSON;
  floor-letter JSON; public-testimony JSON; and the complete returned HTML table.
  Preserve the 378-row shape in the parser fixture, but replace personal text if the
  fixture policy requires sanitization.

### `2014R1/HB4111` (required at-request + pre-2021 behavior)

- Exact `AtTheRequestOf`:
  `(at the request of House Interim Committee on Transportation and Economic Development for Innovation in Infrastructure Task Force)`.
- Bill Title source verified as `RelatingTo`; chapter `66`; emergency clause true;
  effective date `2014-03-13T00:00:00`.
- One `Presession` filing-notice sponsor row.
- Seven old presentation-table rows and the exact 7/11 HTML-to-OData classification
  behavior described above. This is a compact, strong legacy parser fixture.

### `2014R1/HB4001` (required older multiple-sponsor behavior)

- Four actual member sponsors: two chief and two regular, with resolvable legislator
  records, plus one `Presession` filing notice.
- Demonstrates differing `RelatingTo`/`RelatingToFull` values and nullable chapter,
  current version, and effective date.

### Additional rich legacy fixture

`2015R1/HB2745` has 45 presentation rows and five other committee documents. It is
useful for testing client-pagination completeness, but `2014R1/HB4111` is smaller
and already satisfies the pre-2021 requirement.

## Unresolved or deliberately unassumed points

- The current agreement PDF retains an electronic-acceptance requirement even
  though neither the public endpoint nor the operator's site review exposed a
  separate submission form during this spike. Application documentation must keep
  the user's obligation explicit and require use of any official mechanism that may
  later appear.
- Only observed sponsor and document values above are mapped. The strings are not an
  exhaustive promise for future sessions.
- The 12 supported measure prefixes come from the official six-category mapping,
  but the session counts and observed historical presence of individual prefixes
  are point-in-time findings. They are not used as undocumented source enums or
  guarantees that a prefix will appear in a particular session.
- The three observed testimony position IDs are confirmed for the tested modern
  session, not presumed universal forever.
- A `Presentation` title can say “testimony”, “letter”, or something else. Without
  stronger structured evidence, keep the normalized kind `committee_presentation`
  and the source section/raw type rather than making a filename/title-only semantic
  claim.
- OData can expose a record whose advertised readiness conflicts with the actual
  downloadable bytes and OLIS display state. File validation and provenance are
  therefore mandatory, and the HTML list remains useful for reconciliation.

See [testimony_discovery.md](testimony_discovery.md) for candidate and tri-state
display rules, and [completeness.md](completeness.md) for cursor, presence, and
anomaly effects on session status.
