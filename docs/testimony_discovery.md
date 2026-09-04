# Testimony and presentation discovery

LegiView uses structured OData as the inventory source and one narrowly scoped OLIS
measure page as a display-reconciliation source. It does not crawl arbitrary links or
use browser automation. Payload acquisition remains a separate Download Archive
operation.

## Supported legislative-measure scope

The official Oregon Legislature measure taxonomy has six categories, each with a
House- and Senate-origin form:

| Official category | House-origin | Senate-origin |
| --- | --- | --- |
| Bill | `HB` | `SB` |
| Joint Resolution | `HJR` | `SJR` |
| Concurrent Resolution | `HCR` | `SCR` |
| Resolution | `HR` | `SR` |
| Joint Memorial | `HJM` | `SJM` |
| Memorial | `HM` | `SM` |

LegiView applies the same testimony-candidate and reconciliation rules to all 12
prefixes. The initial `H` or `S` records the originating chamber; for joint and
concurrent measures it does not mean that only the originating chamber acts. See the
[official measure-types page](https://www.oregonlegislature.gov/citizen_engagement/Pages/Measure-Types.aspx)
and [source mapping](source_mapping.md#legislative-measure-mapping-and-supported-prefixes).
An unexpected prefix is treated as a source-contract issue instead of being guessed
into a known category.

## Modern public testimony

For sessions with the modern testimony model, the official
`CommitteePublicTestimonies` entity set is primary. LegiView persists each row and
normalizes its logical document identity from:

```text
session + measure + CommitteePublicTestimony + CommTestId
```

The useful structured fields include submitter names, `BehalfOf`, organization,
description, position ID, committee, meeting date, source dates, and
`PdfCreatedFlag`. Only the observed position IDs are translated: `3981` is Neutral,
`3982` is Oppose, and `3983` is Support. Unknown IDs remain visible rather than being
guessed.

LegiView then requests the known measure Testimony page and parses its
`ExhibitsTable`. The table is rendered in the initial HTML. Its JavaScript DataTables
pagination is client-side, so there is no hidden Ajax endpoint or need to click every
page. Links are read from the recognized table/section rather than counted globally,
because responsive markup can repeat a link.

Rows are reconciled by numeric `CommTestId`:

- present in both sources: one logical document with both provenances;
- OData-only: retained, flagged as a discrepancy, and marked not displayed only
  after a successful page parse;
- OLIS-only: retained as a page-only logical source record and flagged for review;
- failed fetch or anomalous parse: existing records remain, display state stays
  unknown, and the session cannot be called cleanly complete.

`PdfCreatedFlag=Y` is readiness metadata, not proof that usable bytes exist. The
downloader still requires a non-empty, type-valid payload.

## Historical presentations and legacy testimony

For pre-2021 pages, OLIS directs users to “Presentations Displayed in Committee.”
The structured source is `CommitteeMeetingDocuments`, and displayed rows use
`CommitteeMeetingDocument/<id>` rather than the modern public-testimony family.
LegiView reconciles by `CommitteeMeetingDocumentId`.

Classification is deliberately conservative:

- exact raw `Presentation` becomes `committee_presentation`;
- observed non-payload/context types such as `Witness Registration`, `Meeting
  Material`, `Preliminary SMS`, `Revenue Impact Statement`, `Fiscal Impact
  Statement`, and `Budget Report` remain `committee_document_other`;
- an unknown non-empty raw type is retained as `committee_document_other` with an
  unrecognized-type diagnostic;
- an absent/unclassifiable type is retained as `unknown`.

No additional raw value is mapped to `legacy_testimony` without stronger observed
per-record evidence. In particular, a future raw string containing “testimony” is
retained and diagnosed until its source semantics are verified; the word alone is
not an undocumented enum mapping.

Being displayed in the historical presentation section is stronger display
evidence, but words such as “testimony” or “letter” in a title or filename alone do
not change the normalized kind. Different source IDs are never deduplicated merely
because their title, submitter, filename, or hash matches.

## Candidate-page rules

To avoid fetching an OLIS page for every supported measure, a measure becomes a
reconciliation candidate when its structured session inventory contains at least
one of:

- a `CommitteePublicTestimonies` row;
- a committee document classified as presentation/legacy testimony capable;
- an unrecognized committee-document type whose display behavior is not yet safe to
  assume; or
- an agenda item whose structured meeting/agenda type is `Public Hearing`.

Reasons are recorded from structured fields, not filename keywords. When structured
evidence leaves a reasonable completeness risk, checking the one known page is
preferred. Measures without a reason are explicitly `not_applicable`, not silently
treated as a successful zero-result page.

## Display-check states

| State | Meaning | Completeness effect |
| --- | --- | --- |
| `checked_with_records` | The recognized table parsed successfully and contained rows. | Satisfies the page check, subject to mismatch review. |
| `checked_zero` | The page parsed successfully and explicitly said no items were displayed. | Satisfies the page check. |
| `not_applicable` | Structured candidate rules did not require the page. | Satisfies the check only for a genuine non-candidate. |
| `failed_fetch` | The page could not be fetched successfully. | Material completeness gap. |
| `parser_anomalous` | Markup suggested relevant documents or changed structure, but the parser could not prove a complete result. | Material completeness gap. |

`documents.displayed_in_olis` is tri-state:

- `1`: the source ID appeared in a successfully parsed display;
- `0`: the OData row did not appear in a successfully parsed applicable display;
- `NULL`: no successful display decision is available.

The reconciliation timestamp records when that decision was made. A failed fetch or
parser anomaly must never write a false `0`.

Successful display results are durable. During an ordinary incremental inventory,
LegiView reuses a prior successful result for an unchanged candidate and requests
OLIS again only for new/changed candidate evidence or a prior failed/anomalous page.
A forced authoritative comparison deliberately rechecks all current candidates. If
a successful authoritative source comparison proves a formerly anomalous measure is
no longer a candidate, its stale current-state display anomalies are resolved rather
than leaving the session permanently incomplete.

Reconciliation state is stored independently for the
`CommitteePublicTestimony` and `CommitteeMeetingDocument` source families. A measure
that legitimately exposes both cannot let one result overwrite the other, and a
later successful check resolves only stale discrepancies for its own family before
recording any mismatch that is still present.

## Confirmed source behavior

The 2026-09-03 source spike established:

- live OData metadata includes `CommitteePublicTestimonies`, although the linked
  one-page data-model PDF does not;
- `2026R1/SB1501` returned 379 unique OData testimony IDs but displayed 378;
- OData-only ID `255890` advertised `PdfCreatedFlag=Y` but its numeric download route
  returned HTTP 200 with zero bytes and no MIME type;
- `2014R1/HB4111` displayed exactly the 7 `Presentation` records among 11 structured
  committee documents;
- `2015R1/HB2745` displayed exactly the 45 `Presentation` records among 50 structured
  committee documents.

An additional bounded expanded-scope check on 2026-09-04 found 162 non-`HB`/`SB`
measures in `2025R1` (`HCR` 42, `SCR` 34, `SJR` 34, `HJR` 22, `HJM` 14,
`SJM` 10, `HR` 3, and `SR` 3). The equivalent `2014R1` query found 19 (`SCR`
7, `HCR` 5, `SJR` 4, and one each of `HJM`, `SJM`, and `SR`). A targeted
whole-catalogue query observed exact row `2007R1/HM1` with meaning `House
Memorial`, but no `HM` at or after the supported `2014R1` boundary, and observed
`SM` in supported sessions including `2017R1` and `2019R1`. A bounded exclusion
query found no source prefix outside the 12-prefix catalogue. These are
point-in-time query results, not enum guarantees or permanent statements of prefix
availability.

`2025R1/HJR11` provided the bounded non-bill testimony validation: structured
queries returned 10 sponsors, one committee agenda item, two committee meeting
documents, 114 `CommitteePublicTestimonies` rows, and zero floor letters. Its
canonical OLIS testimony page was reachable at
<https://olis.oregonlegislature.gov/liz/2025R1/Measures/Testimony/HJR11>. This
confirms that candidate selection and source identity must be measure-generic;
the observed counts are not a permanent cardinality assertion.

The zero-byte record is retained as an auditable upstream discrepancy and truthful
download failure. It is never promoted as a valid file or retried forever without an
explicit operator action.

See [source_mapping.md](source_mapping.md) for the exact source fields and numeric
download routes, and [completeness.md](completeness.md) for how anomalies affect a
session result.
