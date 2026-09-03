-- LegiView Phase 1 schema.
-- Local timestamps are written as UTC RFC-3339 strings by the storage service.

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY CHECK (length(trim(key)) > 0),
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL UNIQUE,
    run_type TEXT NOT NULL CHECK (run_type IN ('collect_bill', 'collect_session', 'retry_failures')),
    requested_session_key TEXT,
    requested_bill_id_compact TEXT,
    requested_scope_json TEXT NOT NULL DEFAULT '{}',
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

CREATE INDEX IF NOT EXISTS idx_collection_runs_status_queued
    ON collection_runs(status, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_runs_scope
    ON collection_runs(requested_session_key, requested_bill_id_compact, queued_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY CHECK (length(trim(session_key)) > 0),
    source_session_id TEXT,
    session_name TEXT,
    session_type TEXT,
    session_year INTEGER,
    begin_date TEXT,
    end_date TEXT,
    source_url TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_source_id
    ON sessions(source_session_id)
    WHERE source_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS legislators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    legislator_code TEXT NOT NULL CHECK (length(trim(legislator_code)) > 0),
    source_legislator_id TEXT,
    first_name TEXT,
    middle_name TEXT,
    last_name TEXT,
    suffix TEXT,
    display_name TEXT,
    chamber TEXT CHECK (chamber IS NULL OR chamber IN ('House', 'Senate')),
    party TEXT,
    district TEXT,
    email TEXT,
    active INTEGER CHECK (active IS NULL OR active IN (0, 1)),
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (session_key, legislator_code)
);

CREATE INDEX IF NOT EXISTS idx_legislators_name
    ON legislators(session_key, last_name, first_name);

CREATE TABLE IF NOT EXISTS committees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    committee_code TEXT NOT NULL CHECK (length(trim(committee_code)) > 0),
    source_committee_id TEXT,
    committee_name TEXT,
    house_of_action TEXT,
    chamber TEXT,
    committee_type TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (session_key, committee_code)
);

CREATE INDEX IF NOT EXISTS idx_committees_name
    ON committees(session_key, committee_name);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    measure_id TEXT,
    measure_prefix TEXT NOT NULL CHECK (measure_prefix IN ('HB', 'SB')),
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
    UNIQUE (session_key, bill_id_compact),
    UNIQUE (id, session_key, bill_id_compact)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_session_measure_id
    ON bills(session_key, measure_id)
    WHERE measure_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bills_session_chamber_number
    ON bills(session_key, bill_chamber, measure_number);
CREATE INDEX IF NOT EXISTS idx_bills_title
    ON bills(bill_title);
CREATE INDEX IF NOT EXISTS idx_bills_last_synced
    ON bills(last_synced_at DESC);

CREATE TABLE IF NOT EXISTS bill_sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    source_measure_sponsor_id TEXT NOT NULL CHECK (length(trim(source_measure_sponsor_id)) > 0),
    raw_sponsor_type TEXT,
    raw_sponsor_level TEXT,
    normalized_category TEXT NOT NULL DEFAULT 'unknown',
    legislator_code TEXT,
    committee_code TEXT,
    resolved_display_name TEXT,
    sponsor_kind TEXT NOT NULL DEFAULT 'other'
        CHECK (sponsor_kind IN ('legislator', 'committee', 'other')),
    print_order INTEGER,
    pre_session_filed_message TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (bill_id, source_measure_sponsor_id)
);

CREATE INDEX IF NOT EXISTS idx_bill_sponsors_bill_category_order
    ON bill_sponsors(bill_id, normalized_category, print_order, id);
CREATE INDEX IF NOT EXISTS idx_bill_sponsors_legislator
    ON bill_sponsors(legislator_code)
    WHERE legislator_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bill_sponsors_committee
    ON bill_sponsors(committee_code)
    WHERE committee_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS committee_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    source_meeting_id TEXT NOT NULL CHECK (length(trim(source_meeting_id)) > 0),
    committee_id INTEGER REFERENCES committees(id),
    committee_code TEXT,
    committee_name TEXT,
    meeting_date TEXT,
    location TEXT,
    meeting_type TEXT,
    agenda_url TEXT,
    source_url TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (session_key, source_meeting_id)
);

CREATE INDEX IF NOT EXISTS idx_committee_meetings_committee_date
    ON committee_meetings(session_key, committee_code, meeting_date);

CREATE TABLE IF NOT EXISTS committee_agenda_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    source_agenda_item_id TEXT NOT NULL CHECK (length(trim(source_agenda_item_id)) > 0),
    committee_meeting_id INTEGER REFERENCES committee_meetings(id),
    bill_id INTEGER REFERENCES bills(id),
    measure_id TEXT,
    bill_id_compact TEXT,
    agenda_order INTEGER,
    agenda_item_type TEXT,
    description TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (session_key, source_agenda_item_id)
);

CREATE INDEX IF NOT EXISTS idx_committee_agenda_items_meeting
    ON committee_agenda_items(committee_meeting_id, agenda_order);
CREATE INDEX IF NOT EXISTS idx_committee_agenda_items_bill
    ON committee_agenda_items(bill_id);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    bill_id_compact TEXT NOT NULL,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN (
            'public_testimony', 'legacy_testimony', 'committee_presentation',
            'floor_letter', 'committee_document_other', 'unknown'
        )
    ),
    source_section TEXT NOT NULL,
    source_entity_type TEXT NOT NULL CHECK (length(trim(source_entity_type)) > 0),
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    raw_document_type TEXT,
    classification_method TEXT,
    classification_confidence TEXT,
    title TEXT,
    exhibit_reference TEXT,
    submitter TEXT,
    on_behalf_of TEXT,
    testimony_position TEXT,
    city_organization TEXT,
    meeting_date TEXT,
    committee_code TEXT,
    committee_name TEXT,
    chamber TEXT,
    letter_date TEXT,
    description TEXT,
    committee_meeting_id INTEGER REFERENCES committee_meetings(id),
    committee_agenda_item_id INTEGER REFERENCES committee_agenda_items(id),
    source_url TEXT,
    canonical_download_url TEXT,
    source_created_at TEXT,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    download_status TEXT NOT NULL DEFAULT 'discovered' CHECK (
        download_status IN (
            'discovered', 'queued', 'downloading', 'downloaded',
            'failed_retryable', 'failed_terminal', 'paused_low_space',
            'interrupted', 'missing_local', 'changed_remote', 'not_applicable'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    last_error TEXT,
    http_status INTEGER,
    remote_filename TEXT,
    local_filename TEXT,
    local_relative_path TEXT,
    mime_type TEXT,
    advertised_bytes INTEGER CHECK (advertised_bytes IS NULL OR advertised_bytes >= 0),
    downloaded_bytes INTEGER CHECK (downloaded_bytes IS NULL OR downloaded_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    downloaded_at TEXT,
    validation_status TEXT NOT NULL DEFAULT 'not_validated',
    current_version_id INTEGER REFERENCES document_versions(id),
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (bill_id, session_key, bill_id_compact)
        REFERENCES bills(id, session_key, bill_id_compact),
    UNIQUE (session_key, bill_id_compact, source_entity_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_bill_kind
    ON documents(bill_id, document_kind, meeting_date, id);
CREATE INDEX IF NOT EXISTS idx_documents_download_status
    ON documents(download_status, last_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_documents_filters
    ON documents(session_key, document_kind, committee_code, testimony_position);
CREATE INDEX IF NOT EXISTS idx_documents_submitter
    ON documents(submitter);
CREATE INDEX IF NOT EXISTS idx_documents_last_seen_run
    ON documents(last_seen_run_id);

CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    collection_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    observed_at TEXT NOT NULL,
    source_url TEXT,
    source_modified_at TEXT,
    etag TEXT,
    last_modified TEXT,
    remote_filename TEXT,
    local_filename TEXT,
    local_relative_path TEXT,
    advertised_bytes INTEGER CHECK (advertised_bytes IS NULL OR advertised_bytes >= 0),
    downloaded_bytes INTEGER CHECK (downloaded_bytes IS NULL OR downloaded_bytes >= 0),
    mime_type TEXT,
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    status TEXT NOT NULL CHECK (
        status IN (
            'discovered', 'queued', 'downloading', 'downloaded',
            'failed_retryable', 'failed_terminal', 'paused_low_space',
            'interrupted', 'missing_local', 'changed_remote', 'not_applicable'
        )
    ),
    validation_status TEXT NOT NULL DEFAULT 'not_validated',
    http_status INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (document_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_versions_payload
    ON document_versions(document_id, sha256)
    WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_versions_document_observed
    ON document_versions(document_id, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_document_versions_status
    ON document_versions(status, observed_at);

CREATE TABLE IF NOT EXISTS collection_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    bill_id INTEGER REFERENCES bills(id),
    document_id INTEGER REFERENCES documents(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'completed', 'skipped', 'failed_retryable',
            'failed_terminal', 'paused', 'canceled', 'interrupted'
        )
    ),
    current_activity TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0 CHECK (progress_current >= 0),
    progress_total INTEGER CHECK (progress_total IS NULL OR progress_total >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    interrupted_at TEXT,
    updated_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, item_type, item_key)
);

CREATE INDEX IF NOT EXISTS idx_collection_run_items_run_stage
    ON collection_run_items(run_id, stage, status, id);
CREATE INDEX IF NOT EXISTS idx_collection_run_items_document
    ON collection_run_items(document_id, run_id)
    WHERE document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS collection_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    run_item_id INTEGER REFERENCES collection_run_items(id),
    error_fingerprint TEXT NOT NULL,
    stage TEXT NOT NULL,
    session_key TEXT,
    bill_id_compact TEXT,
    source_entity_type TEXT,
    source_id TEXT,
    document_id INTEGER REFERENCES documents(id),
    source_url TEXT,
    error_class TEXT NOT NULL,
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    message TEXT NOT NULL,
    first_occurred_at TEXT NOT NULL,
    last_occurred_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    details_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT,
    UNIQUE (run_id, error_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_collection_errors_run_unresolved
    ON collection_errors(run_id, resolved_at, last_occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_errors_retryable
    ON collection_errors(retryable, resolved_at, last_occurred_at DESC);

CREATE TABLE IF NOT EXISTS source_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    run_item_id INTEGER REFERENCES collection_run_items(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL,
    entity_set TEXT,
    source_url TEXT NOT NULL,
    request_params_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    completed_at TEXT,
    succeeded INTEGER CHECK (succeeded IS NULL OR succeeded IN (0, 1)),
    http_status INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    etag TEXT,
    last_modified TEXT,
    response_sha256 TEXT CHECK (response_sha256 IS NULL OR length(response_sha256) = 64),
    item_count INTEGER CHECK (item_count IS NULL OR item_count >= 0),
    continuation_url TEXT,
    error_class TEXT,
    error_message TEXT,
    response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_fetches_run_time
    ON source_fetches(run_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_fetches_url_time
    ON source_fetches(source_url, fetched_at DESC);
