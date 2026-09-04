# Expanded scope validation

Date: 2026-09-04
Release target: LegiView v0.3

This record covers the v0.3 expansion from HB/SB-only collection to the supported
Oregon legislative measure catalogue, the pre-2014 collection guardrail, inclusive
official-session ranges, and reverse-proxy/subpath deployment behavior.

## Live-source boundary

The live checks began at 10:47 PDT, with bounded catalogue follow-ups later on the
same date, against the official Oregon Legislature OData endpoint. They were small,
filtered or limited to the session catalogue, and capped with `$top`. Requests were
sequential, used the LegiView user agent, and were paced by at least 350 ms when
issued as a group. No document payload was requested, and no full-session or
all-history inventory was run. No acceptance mechanism, authentication control,
CAPTCHA, rate limit, or other gate was bypassed. Successful responses with captured
headers were HTTP 200 and supplied no `Retry-After`; no HTTP 429 occurred.

## Supported measure prefixes

One bounded request per prefix used this endpoint and parameter set:

```text
GET https://api.oregonlegislature.gov/odata/odataservice.svc/Measures
$filter=SessionKey ge '2014R1' and MeasurePrefix eq '<PREFIX>'
$select=SessionKey,MeasurePrefix,MeasureNumber,PrefixMeaning,RelatingTo,CreatedDate,ModifiedDate
$orderby=SessionKey desc,MeasureNumber asc
$top=1
$format=json
```

The latest representative returned for each prefix was:

| Prefix | Representative | Live `PrefixMeaning` |
| --- | --- | --- |
| `HB` | `2026R1/HB4001` | `House Bill` |
| `SB` | `2026R1/SB1501` | `Senate Bill` |
| `HJR` | `2026R1/HJR201` | `House Joint Resolution` |
| `SJR` | `2026R1/SJR201` | `Senate Joint Resolution` |
| `HCR` | `2026R1/HCR201` | `House Concurrent Resolution` |
| `SCR` | `2026R1/SCR201` | `Senate Concurrent Resolution` |
| `HR` | `2025R1/HR1` | `House Resolution` |
| `SR` | `2025R1/SR1` | `Senate Resolution` |
| `HJM` | `2026R1/HJM201` | `House Joint Memorial` |
| `SJM` | `2025R1/SJM1` | `Senate Joint Memorial` |
| `SM` | `2019R1/SM1` | `Senate Memorial` |
| `HM` | `2007R1/HM1` (separate pre-boundary probe; none at/after `2014R1`) | `House Memorial` |

The observed `PrefixMeaning` values agree with LegiView's explicit prefix catalogue.
The results also show why collection cannot assume that every supported prefix exists
in every session: `HR`, `SR`, and `SJM` had no newer representative than `2025R1`,
`SM` had no newer representative than `2019R1`, and `HM` had no post-2014 row.
The separate bounded `HM` query confirmed `2007R1/HM1` and its meaning. A `$top=1`
query whose filter excluded all 12 supported prefixes returned no row. That is
direct point-in-time evidence that the current source contains no thirteenth prefix;
unknown future values still fail closed for review rather than being guessed.

## Non-bill public-testimony identity

At `2026-09-04T10:49:06-07:00`, the following query returned two rows:

```text
GET https://api.oregonlegislature.gov/odata/odataservice.svc/CommitteePublicTestimonies
$filter=SessionKey eq '2025R1' and MeasurePrefix eq 'HJR' and MeasureNumber eq 11
$select=SessionKey,MeasurePrefix,MeasureNumber,CommTestId,DocumentDescription,SubmitterFirstName,SubmitterLastName,BehalfOf,Organization,PositionOnMeasureId,CommitteeCode,MeetingDate,PdfCreatedFlag,DocumentUrl,CreatedDate,ModifiedDate
$orderby=CommTestId asc
$top=2
$format=json
```

The returned durable source identities were:

- `2025R1/HJR11/CommitteePublicTestimony/146696`
- `2025R1/HJR11/CommitteePublicTestimony/147094`

Both rows had `DocumentDescription=Testimony`, `PdfCreatedFlag=Y`, committee
`HRULES`, meeting date `2025-03-10T08:00:00`, and position ID `3982`. Their official
`DocumentUrl` value was only the session root,
`https://olis.oregonlegislature.gov/liz/2025R1`; it was not a file URL. LegiView must
therefore continue to keep the source identity separate from these deterministic
OLIS routes:

```text
https://olis.oregonlegislature.gov/liz/2025R1/Measures/Overview/HJR11
https://olis.oregonlegislature.gov/liz/2025R1/Measures/Testimony/HJR11
https://olis.oregonlegislature.gov/liz/2025R1/Downloads/PublicTestimonyDocument/146696
```

Those URL shapes are recorded for mapping validation; no download URL was requested
during this check.

## Pre-2014 evidence and guardrail

At `2026-09-04T10:50:43-07:00`, this bounded query returned ten rows:

```text
GET https://api.oregonlegislature.gov/odata/odataservice.svc/Measures
$filter=SessionKey lt '2014R1'
$select=SessionKey,MeasurePrefix,MeasureNumber,PrefixMeaning,RelatingTo,CreatedDate,ModifiedDate
$orderby=SessionKey asc,MeasurePrefix asc,MeasureNumber asc
$top=10
$format=json
```

The result was `2007R1/HB2001` through `2007R1/HB2010`. The source therefore exposes
measure data beginning with the earliest official catalogue session, `2007R1`. All
ten rows had source-created timestamps in 2007 and the same later `ModifiedDate`,
`2020-06-26T19:41:22`.

Two further `2007R1` checks each used `$top=1`:

```text
GET .../CommitteePublicTestimonies
$filter=SessionKey eq '2007R1'
$orderby=CommTestId asc
$top=1

GET .../CommitteeMeetingDocuments
$filter=SessionKey eq '2007R1'
$orderby=CommitteeMeetingDocumentId asc
$top=1
```

Both returned zero rows. Basic measure records thus exist for an older era in which
neither structured document family on which LegiView's completeness model depends
yielded a record. This does not prove that the source has no other historical
representation; it proves that LegiView must not silently apply its validated
2014-and-later completeness contract to those older sessions. The application should
continue to display older official sessions as unsupported, reject them before run
creation, and use the official `2014R1` `BeginDate` as the supported boundary for
range selection.

### Official session catalogue

An initial oldest-first request incorrectly selected unsupported `SessionType` and
returned HTTP 400. A corrected bounded follow-up through LegiView's OData client was:

```text
GET https://api.oregonlegislature.gov/odata/odataservice.svc/LegislativeSessions
$orderby=BeginDate asc,SessionKey asc
$top=1
```

It returned `2007R1`, named `2007 Regular Session`, beginning
`2007-01-08T00:00:00` and ending `2007-06-28T00:00:00`. A second capped catalogue
read (`$top=1000`, seven supported fields only) returned 41 rows, no continuation,
and no key outside LegiView's modern session-key syntax. `2007R1` was the first row.
The resolver nevertheless retains future unrecognized or malformed catalogue rows
as visibly unavailable diagnostics, excludes them from persistence and collection,
and freezes their reasons into the run scope rather than allowing one legacy anomaly
to break supported-session discovery.

## Automated validation

The normal suite is network-independent; these live checks are evidence, not tests
that run by default. The current suite covers:

- the complete supported-prefix catalogue, identifier normalization, official
  meanings, originating chambers, measure types, and exact OData scope filters;
- fail-closed handling for unknown prefixes, out-of-scope source rows, and returned
  measure identities that differ from the requested measure;
- migration and database constraints for every supported prefix/type/chamber tuple,
  plus stable storage and non-bill session/export counts;
- targeted non-bill collection through persistence, child-document testimony
  reconciliation, downloading, and durable completion, plus historical non-bill
  source filtering, child-entity lookup, and deterministic archive paths;
- complete paged official-session discovery, chronology-based `2014R1` boundaries,
  inclusive From/To expansion, special/interim-session retention, frozen run scope,
  and rejection of unknown, reversed, tampered, or pre-boundary selections;
- subpath-safe generated navigation, static assets, forms, API/export/download links,
  redirects, and session-cookie paths under `/legiview`;
- one-hop `ProxyFix` behavior, forwarded-prefix/host validation, explicit trusted
  hosts, loopback-only proxy-mode binding, persistent-secret requirements, and
  unaffected direct development at `/`;
- existing paging, retry, cancellation, reconciliation, download-validation,
  persistence, and archive-recovery regressions.

Final full-suite result: `346 passed, 4 skipped` in 74.49 seconds on Windows.
The four skips are symlink-hardening cases for which this Windows account lacks
the symlink privilege; the corresponding behavior remains covered where symlinks
are available.
