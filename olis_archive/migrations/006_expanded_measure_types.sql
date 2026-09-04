-- Expand the canonical measure scope beyond House and Senate bills.
--
-- SQLite cannot alter the CHECK constraint on bills.measure_prefix in place,
-- so preserve the stable bill IDs and every existing column while rebuilding
-- the table.  Child tables continue to reference those same IDs.

CREATE TABLE bills_expanded_measure_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    measure_id TEXT,
    measure_prefix TEXT NOT NULL CHECK (
        measure_prefix IN (
            'HB', 'SB', 'HJR', 'SJR', 'HCR', 'SCR',
            'HR', 'SR', 'HJM', 'SJM', 'HM', 'SM'
        )
    ),
    measure_type TEXT NOT NULL CHECK (
        measure_type IN (
            'bill', 'joint_resolution', 'concurrent_resolution',
            'resolution', 'joint_memorial', 'memorial'
        )
    ),
    measure_number TEXT NOT NULL CHECK (length(trim(measure_number)) > 0),
    bill_id_compact TEXT NOT NULL CHECK (length(trim(bill_id_compact)) > 0),
    bill_id_display TEXT NOT NULL CHECK (length(trim(bill_id_display)) > 0),
    bill_chamber TEXT NOT NULL CHECK (bill_chamber IN ('House', 'Senate')),
    at_the_request_of TEXT,
    title_source_field TEXT,
    bill_title TEXT,
    catchline TEXT,
    measure_summary TEXT,
    chapter_number TEXT,
    effective_date TEXT,
    vetoed INTEGER CHECK (vetoed IS NULL OR vetoed IN (0, 1)),
    emergency_clause TEXT,
    current_version TEXT,
    current_location TEXT,
    current_committee_code TEXT,
    current_subcommittee_code TEXT,
    current_committee_name TEXT,
    relating_to TEXT,
    relating_to_clause TEXT,
    relating_to_full TEXT,
    minority_catchline TEXT,
    fiscal_impact TEXT,
    revenue_impact TEXT,
    lc_number TEXT,
    prefix_meaning TEXT,
    enacted INTEGER CHECK (enacted IS NULL OR enacted IN (0, 1)),
    source_url TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_collected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    last_collected_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    source_presence TEXT NOT NULL DEFAULT 'active'
        CHECK (source_presence IN ('active', 'missing', 'unknown')),
    missing_from_source_since TEXT,
    last_source_reconciled_at TEXT,
    CHECK (
        (measure_prefix IN ('HB', 'SB') AND measure_type = 'bill')
        OR (measure_prefix IN ('HJR', 'SJR') AND measure_type = 'joint_resolution')
        OR (measure_prefix IN ('HCR', 'SCR') AND measure_type = 'concurrent_resolution')
        OR (measure_prefix IN ('HR', 'SR') AND measure_type = 'resolution')
        OR (measure_prefix IN ('HJM', 'SJM') AND measure_type = 'joint_memorial')
        OR (measure_prefix IN ('HM', 'SM') AND measure_type = 'memorial')
    ),
    CHECK (
        (measure_prefix IN ('HB', 'HJR', 'HCR', 'HR', 'HJM', 'HM')
            AND bill_chamber = 'House')
        OR (measure_prefix IN ('SB', 'SJR', 'SCR', 'SR', 'SJM', 'SM')
            AND bill_chamber = 'Senate')
    ),
    UNIQUE (session_key, bill_id_compact),
    UNIQUE (id, session_key, bill_id_compact)
);

INSERT INTO bills_expanded_measure_types(
    id, session_key, measure_id, measure_prefix, measure_type, measure_number,
    bill_id_compact, bill_id_display, bill_chamber, at_the_request_of,
    title_source_field, bill_title, catchline, measure_summary, chapter_number,
    effective_date, vetoed, emergency_clause, current_version, current_location,
    current_committee_code, current_subcommittee_code, current_committee_name,
    relating_to, relating_to_clause, relating_to_full, minority_catchline,
    fiscal_impact, revenue_impact, lc_number, prefix_meaning, enacted, source_url,
    source_created_at, source_modified_at, first_collected_at, last_seen_at,
    last_synced_at, last_collected_run_id, raw_json, source_presence,
    missing_from_source_since, last_source_reconciled_at
)
SELECT
    id, session_key, measure_id, measure_prefix,
    CASE measure_prefix
        WHEN 'HB' THEN 'bill'
        WHEN 'SB' THEN 'bill'
        WHEN 'HJR' THEN 'joint_resolution'
        WHEN 'SJR' THEN 'joint_resolution'
        WHEN 'HCR' THEN 'concurrent_resolution'
        WHEN 'SCR' THEN 'concurrent_resolution'
        WHEN 'HR' THEN 'resolution'
        WHEN 'SR' THEN 'resolution'
        WHEN 'HJM' THEN 'joint_memorial'
        WHEN 'SJM' THEN 'joint_memorial'
        WHEN 'HM' THEN 'memorial'
        WHEN 'SM' THEN 'memorial'
    END,
    measure_number, bill_id_compact, bill_id_display, bill_chamber,
    at_the_request_of, title_source_field, bill_title, catchline,
    measure_summary, chapter_number, effective_date, vetoed, emergency_clause,
    current_version, current_location, current_committee_code,
    current_subcommittee_code, current_committee_name, relating_to,
    relating_to_clause, relating_to_full, minority_catchline, fiscal_impact,
    revenue_impact, lc_number, prefix_meaning, enacted, source_url,
    source_created_at, source_modified_at, first_collected_at, last_seen_at,
    last_synced_at, last_collected_run_id, raw_json, source_presence,
    missing_from_source_since, last_source_reconciled_at
FROM bills;

DROP TABLE bills;
ALTER TABLE bills_expanded_measure_types RENAME TO bills;

CREATE UNIQUE INDEX idx_bills_session_measure_id
    ON bills(session_key, measure_id)
    WHERE measure_id IS NOT NULL;
CREATE INDEX idx_bills_session_chamber_number
    ON bills(session_key, bill_chamber, measure_number);
CREATE INDEX idx_bills_title
    ON bills(bill_title);
CREATE INDEX idx_bills_last_synced
    ON bills(last_synced_at DESC);
CREATE UNIQUE INDEX idx_bills_id_session
    ON bills(id, session_key);
CREATE INDEX idx_bills_presence_page
    ON bills(session_key, source_presence, id);
CREATE INDEX idx_bills_session_compact
    ON bills(session_key, bill_id_compact, id);

-- Every measure-scoped cursor was calculated against an HB/SB-only source
-- filter. Clear those success/cursor fields so the next scan is authoritative
-- for the expanded measure scope. Reference-only entity cursors remain valid.
-- Retain each row's last attempt/failure diagnostics and details JSON as
-- operational history.
UPDATE source_sync_state
SET last_successful_sync_at = NULL,
    last_full_session_sync_at = NULL,
    last_incremental_sync_at = NULL,
    source_watermark = NULL,
    last_successful_run_id = NULL,
    last_returned_source_count = NULL,
    last_reconciliation_outcome = 'invalidated_expanded_measure_scope',
    is_incomplete = 1,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE entity_set IN (
    'Measures', 'MeasureSponsors', 'CommitteeAgendaItems',
    'CommitteeMeetingDocuments', 'CommitteePublicTestimonies', 'FloorLetters'
);

-- A session previously marked complete only proved completeness for HB/SB.
-- Preserve download activity and durable source/anomaly records, but require a
-- new inventory pass before treating the session as downloadable/comprehensive.
UPDATE session_archive_state
SET inventory_status = 'not_started',
    last_inventory_started_at = NULL,
    last_inventory_completed_at = NULL,
    last_inventory_run_id = NULL,
    last_successful_inventory_run_id = NULL,
    display_reconciliation_status = NULL,
    last_testimony_reconciled_at = NULL,
    completeness_details_json =
        '{"invalidated_by_migration":6,"reason":"expanded_measure_scope"}',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- A pre-migration backfill can contain completed session items even when the
-- run itself is paused or interrupted.  Requeueing normally leaves completed
-- items untouched, which would incorrectly skip their expanded-scope rescan.
UPDATE collection_run_items
SET status = 'interrupted',
    current_activity = 'Inventory scope expanded; session must be rescanned',
    progress_current = 0,
    progress_total = NULL,
    started_at = NULL,
    finished_at = NULL,
    interrupted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    details_json = '{}'
WHERE item_type = 'session'
  AND run_id IN (
      SELECT id FROM collection_runs
      WHERE run_type = 'inventory_backfill'
        AND status IN ('queued', 'running', 'paused', 'interrupted')
  );

UPDATE collection_runs
SET sessions_completed = 0,
    sessions_incomplete = 0,
    sessions_failed = 0,
    bills_total = 0,
    bills_completed = 0,
    documents_discovered = 0,
    summary_json = '{}',
    current_activity = 'Inventory scope expanded; all sessions must be rescanned',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE run_type = 'inventory_backfill'
  AND status IN ('queued', 'running', 'paused', 'interrupted');
