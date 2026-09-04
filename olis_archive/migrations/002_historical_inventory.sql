-- LegiView Phase 2 historical inventory and production-scale archive state.
-- Migration 001 is immutable.  collection_runs is rebuilt here because SQLite
-- cannot alter a CHECK constraint in place.

CREATE TABLE collection_runs_phase2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL UNIQUE,
    run_type TEXT NOT NULL CHECK (
        run_type IN (
            'collect_bill', 'collect_session', 'retry_failures',
            'inventory_backfill', 'download_archive'
        )
    ),
    requested_session_key TEXT,
    requested_bill_id_compact TEXT,
    requested_scope_json TEXT NOT NULL DEFAULT '{}',
    scope_cutoff_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'completed', 'completed_with_errors',
            'failed', 'paused', 'canceled', 'interrupted'
        )
    ),
    stage TEXT NOT NULL DEFAULT 'queued',
    current_activity TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    interrupted_at TEXT,
    updated_at TEXT NOT NULL,
    sessions_total INTEGER NOT NULL DEFAULT 0 CHECK (sessions_total >= 0),
    sessions_completed INTEGER NOT NULL DEFAULT 0 CHECK (sessions_completed >= 0),
    sessions_incomplete INTEGER NOT NULL DEFAULT 0 CHECK (sessions_incomplete >= 0),
    sessions_failed INTEGER NOT NULL DEFAULT 0 CHECK (sessions_failed >= 0),
    bills_total INTEGER NOT NULL DEFAULT 0 CHECK (bills_total >= 0),
    bills_completed INTEGER NOT NULL DEFAULT 0 CHECK (bills_completed >= 0),
    documents_discovered INTEGER NOT NULL DEFAULT 0 CHECK (documents_discovered >= 0),
    documents_queued INTEGER NOT NULL DEFAULT 0 CHECK (documents_queued >= 0),
    documents_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (documents_downloaded >= 0),
    documents_skipped INTEGER NOT NULL DEFAULT 0 CHECK (documents_skipped >= 0),
    documents_failed INTEGER NOT NULL DEFAULT 0 CHECK (documents_failed >= 0),
    bytes_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (bytes_downloaded >= 0),
    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}'
);

INSERT INTO collection_runs_phase2(
    id, run_uuid, run_type, requested_session_key, requested_bill_id_compact,
    requested_scope_json, status, stage, current_activity, queued_at, started_at,
    finished_at, interrupted_at, updated_at, bills_total, bills_completed,
    documents_discovered, documents_queued, documents_downloaded,
    documents_skipped, documents_failed, bytes_downloaded, error_count,
    config_snapshot_json, summary_json
)
SELECT
    id, run_uuid, run_type, requested_session_key, requested_bill_id_compact,
    requested_scope_json, status, stage, current_activity, queued_at, started_at,
    finished_at, interrupted_at, updated_at, bills_total, bills_completed,
    documents_discovered, documents_queued, documents_downloaded,
    documents_skipped, documents_failed, bytes_downloaded, error_count,
    config_snapshot_json, summary_json
FROM collection_runs;

DROP TABLE collection_runs;
ALTER TABLE collection_runs_phase2 RENAME TO collection_runs;

CREATE INDEX idx_collection_runs_status_queued
    ON collection_runs(status, queued_at DESC);
CREATE INDEX idx_collection_runs_scope
    ON collection_runs(requested_session_key, requested_bill_id_compact, queued_at DESC);
CREATE INDEX idx_collection_runs_type_status
    ON collection_runs(run_type, status, queued_at DESC, id DESC);

ALTER TABLE collection_run_items ADD COLUMN session_key TEXT;

UPDATE collection_run_items
SET session_key = COALESCE(
    (SELECT b.session_key FROM bills b WHERE b.id = collection_run_items.bill_id),
    (SELECT d.session_key FROM documents d WHERE d.id = collection_run_items.document_id),
    (SELECT r.requested_session_key FROM collection_runs r WHERE r.id = collection_run_items.run_id)
)
WHERE session_key IS NULL;

ALTER TABLE bills ADD COLUMN source_presence TEXT NOT NULL DEFAULT 'active'
    CHECK (source_presence IN ('active', 'missing', 'unknown'));
ALTER TABLE bills ADD COLUMN missing_from_source_since TEXT;
ALTER TABLE bills ADD COLUMN last_source_reconciled_at TEXT;

ALTER TABLE documents ADD COLUMN source_presence TEXT NOT NULL DEFAULT 'active'
    CHECK (source_presence IN ('active', 'missing', 'unknown'));
ALTER TABLE documents ADD COLUMN missing_from_source_since TEXT;
ALTER TABLE documents ADD COLUMN last_source_reconciled_at TEXT;
ALTER TABLE documents ADD COLUMN displayed_in_olis INTEGER
    CHECK (displayed_in_olis IS NULL OR displayed_in_olis IN (0, 1));
ALTER TABLE documents ADD COLUMN display_reconciled_at TEXT;
ALTER TABLE documents ADD COLUMN reconciliation_origin TEXT;

CREATE TABLE source_sync_state (
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    entity_set TEXT NOT NULL CHECK (length(trim(entity_set)) > 0),
    sync_strategy TEXT NOT NULL CHECK (length(trim(sync_strategy)) > 0),
    last_attempted_at TEXT NOT NULL,
    last_successful_sync_at TEXT,
    last_full_session_sync_at TEXT,
    last_incremental_sync_at TEXT,
    source_watermark TEXT,
    last_successful_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    last_returned_source_count INTEGER
        CHECK (last_returned_source_count IS NULL OR last_returned_source_count >= 0),
    last_reconciliation_outcome TEXT,
    is_incomplete INTEGER NOT NULL DEFAULT 1 CHECK (is_incomplete IN (0, 1)),
    last_failure_at TEXT,
    last_error_class TEXT,
    last_error_message TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_key, entity_set)
);

CREATE INDEX idx_source_sync_state_incomplete
    ON source_sync_state(is_incomplete, session_key, entity_set);
CREATE INDEX idx_source_sync_state_success
    ON source_sync_state(last_successful_sync_at DESC, session_key, entity_set);

CREATE TABLE session_archive_state (
    session_key TEXT PRIMARY KEY REFERENCES sessions(session_key) ON UPDATE CASCADE,
    inventory_status TEXT NOT NULL DEFAULT 'not_started' CHECK (
        inventory_status IN (
            'not_started', 'inventory_running', 'inventory_complete',
            'inventory_complete_with_errors', 'inventory_incomplete',
            'inventory_failed', 'interrupted'
        )
    ),
    last_inventory_started_at TEXT,
    last_inventory_completed_at TEXT,
    last_inventory_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    last_successful_inventory_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    last_download_started_at TEXT,
    last_download_completed_at TEXT,
    last_download_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    display_reconciliation_status TEXT,
    last_testimony_reconciled_at TEXT,
    source_anomaly_count INTEGER NOT NULL DEFAULT 0 CHECK (source_anomaly_count >= 0),
    material_anomaly_count INTEGER NOT NULL DEFAULT 0 CHECK (material_anomaly_count >= 0),
    completeness_details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_session_archive_inventory_status
    ON session_archive_state(inventory_status, session_key);
CREATE INDEX idx_session_archive_last_inventory
    ON session_archive_state(last_inventory_completed_at DESC, session_key);

CREATE UNIQUE INDEX idx_bills_id_session
    ON bills(id, session_key);

CREATE TABLE olis_display_reconciliations (
    bill_id INTEGER PRIMARY KEY REFERENCES bills(id),
    session_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'checked_with_records', 'checked_zero', 'not_applicable',
            'failed_fetch', 'parser_anomalous'
        )
    ),
    checked_at TEXT NOT NULL,
    run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    odata_record_count INTEGER CHECK (odata_record_count IS NULL OR odata_record_count >= 0),
    displayed_record_count INTEGER
        CHECK (displayed_record_count IS NULL OR displayed_record_count >= 0),
    page_only_count INTEGER CHECK (page_only_count IS NULL OR page_only_count >= 0),
    odata_only_count INTEGER CHECK (odata_only_count IS NULL OR odata_only_count >= 0),
    source_url TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (bill_id, session_key),
    FOREIGN KEY (bill_id, session_key) REFERENCES bills(id, session_key)
);

CREATE INDEX idx_olis_display_session_status
    ON olis_display_reconciliations(session_key, status, checked_at DESC);

CREATE TABLE source_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_fingerprint TEXT NOT NULL UNIQUE CHECK (length(anomaly_fingerprint) = 64),
    anomaly_type TEXT NOT NULL CHECK (length(trim(anomaly_type)) > 0),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    affects_completeness INTEGER NOT NULL DEFAULT 0
        CHECK (affects_completeness IN (0, 1)),
    session_key TEXT REFERENCES sessions(session_key) ON UPDATE CASCADE,
    bill_id INTEGER REFERENCES bills(id),
    bill_id_compact TEXT,
    document_id INTEGER REFERENCES documents(id),
    source_entity_type TEXT,
    source_id TEXT,
    source_url TEXT,
    message TEXT NOT NULL,
    previous_value_json TEXT,
    current_value_json TEXT,
    first_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    last_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    details_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT
);

CREATE INDEX idx_source_anomalies_review
    ON source_anomalies(resolved_at, severity, affects_completeness, last_observed_at DESC);
CREATE INDEX idx_source_anomalies_session_type
    ON source_anomalies(session_key, anomaly_type, last_observed_at DESC);
CREATE INDEX idx_source_anomalies_document
    ON source_anomalies(document_id, last_observed_at DESC)
    WHERE document_id IS NOT NULL;

CREATE TABLE document_remote_probes (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id),
    run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    probe_status TEXT NOT NULL CHECK (length(trim(probe_status)) > 0),
    probed_at TEXT NOT NULL,
    http_status INTEGER,
    final_url TEXT,
    content_type TEXT,
    content_length INTEGER CHECK (content_length IS NULL OR content_length >= 0),
    etag TEXT,
    last_modified TEXT,
    error_class TEXT,
    error_message TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_document_remote_probes_status
    ON document_remote_probes(probe_status, probed_at DESC, document_id);
CREATE INDEX idx_document_remote_probes_known_size
    ON document_remote_probes(content_length, document_id)
    WHERE content_length IS NOT NULL;

CREATE TABLE source_presence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('bill', 'document')),
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    bill_id INTEGER REFERENCES bills(id),
    document_id INTEGER REFERENCES documents(id),
    source_entity_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    previous_presence TEXT NOT NULL CHECK (previous_presence IN ('active', 'missing', 'unknown')),
    new_presence TEXT NOT NULL CHECK (new_presence IN ('active', 'missing', 'unknown')),
    changed_at TEXT NOT NULL,
    run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    CHECK (
        (entity_type = 'bill' AND bill_id IS NOT NULL AND document_id IS NULL)
        OR
        (entity_type = 'document' AND bill_id IS NOT NULL AND document_id IS NOT NULL)
    )
);

CREATE INDEX idx_source_presence_events_entity
    ON source_presence_events(entity_type, source_entity_type, source_id, changed_at DESC);
CREATE INDEX idx_source_presence_events_session
    ON source_presence_events(session_key, changed_at DESC);

CREATE INDEX idx_collection_run_items_session
    ON collection_run_items(run_id, item_type, session_key, status, id);
CREATE INDEX idx_collection_run_items_claim
    ON collection_run_items(run_id, item_type, status, document_id);
CREATE INDEX idx_sessions_chronology
    ON sessions(session_year, begin_date, session_key);
CREATE INDEX idx_bills_presence_page
    ON bills(session_key, source_presence, id);
CREATE INDEX idx_bills_session_compact
    ON bills(session_key, bill_id_compact, id);
CREATE INDEX idx_bill_sponsors_display
    ON bill_sponsors(resolved_display_name, bill_id);
CREATE INDEX idx_documents_archive_claim
    ON documents(session_key, source_presence, download_status, first_seen_at, id);
CREATE INDEX idx_documents_presence_entity
    ON documents(session_key, source_entity_type, source_presence, last_seen_run_id, id);
CREATE INDEX idx_documents_display_state
    ON documents(session_key, displayed_in_olis, reconciliation_origin, document_kind, id);
CREATE INDEX idx_documents_bill_compact_page
    ON documents(session_key, bill_id_compact, id);
CREATE INDEX idx_collection_errors_review
    ON collection_errors(session_key, stage, retryable, resolved_at, last_occurred_at DESC);
