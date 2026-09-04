-- Preserve one durable OLIS display-reconciliation result per bill and source
-- family.  Phase 2 migration 002 stored only one row per bill, even though one
-- OLIS page can reconcile both public testimony and committee presentations.

CREATE TABLE olis_display_reconciliations_phase3 (
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    source_entity_type TEXT NOT NULL CHECK (length(trim(source_entity_type)) > 0),
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
    PRIMARY KEY (bill_id, source_entity_type),
    FOREIGN KEY (bill_id, session_key) REFERENCES bills(id, session_key)
);

INSERT INTO olis_display_reconciliations_phase3(
    bill_id, source_entity_type, session_key, status, checked_at, run_id,
    odata_record_count, displayed_record_count, page_only_count,
    odata_only_count, source_url, details_json
)
SELECT
    r.bill_id,
    CASE
        -- The migration-002 writer used public testimony for non-candidates.
        WHEN r.status = 'not_applicable' THEN 'CommitteePublicTestimony'
        -- For candidates it selected public testimony whenever that source was
        -- present; otherwise it selected the committee-document result.
        WHEN EXISTS (
            SELECT 1 FROM documents d
            WHERE d.bill_id = r.bill_id
              AND d.source_entity_type = 'CommitteePublicTestimony'
        ) THEN 'CommitteePublicTestimony'
        ELSE 'CommitteeMeetingDocument'
    END,
    r.session_key, r.status, r.checked_at, r.run_id,
    r.odata_record_count, r.displayed_record_count, r.page_only_count,
    r.odata_only_count, r.source_url, r.details_json
FROM olis_display_reconciliations r;

DROP TABLE olis_display_reconciliations;
ALTER TABLE olis_display_reconciliations_phase3
    RENAME TO olis_display_reconciliations;

CREATE INDEX idx_olis_display_session_status
    ON olis_display_reconciliations(session_key, status, checked_at DESC);
CREATE INDEX idx_olis_display_bill_checked
    ON olis_display_reconciliations(bill_id, checked_at DESC, source_entity_type);
