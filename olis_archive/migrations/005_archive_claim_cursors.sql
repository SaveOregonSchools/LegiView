-- A Download Archive run walks each frozen session once in document-id order.
-- Persisting the high-water mark keeps failure-heavy runs linear and lets a
-- restarted process resume without rescanning completed sessions.
CREATE TABLE archive_claim_cursors (
    run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    session_ordinal INTEGER NOT NULL CHECK (session_ordinal >= 0),
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON UPDATE CASCADE,
    after_document_id INTEGER NOT NULL DEFAULT 0 CHECK (after_document_id >= 0),
    exhausted INTEGER NOT NULL DEFAULT 0 CHECK (exhausted IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_ordinal),
    UNIQUE (run_id, session_key)
);

CREATE INDEX idx_archive_claim_cursors_open
    ON archive_claim_cursors(run_id, exhausted, session_ordinal);

-- Keep the keyset walk ordered by id. The second, frozen eligible-status
-- predicate is applied by the claim query; this partial predicate contains
-- every status that any Download Archive run is allowed to claim.
CREATE INDEX idx_documents_archive_walk
    ON documents(session_key, id)
    WHERE canonical_download_url IS NOT NULL
      AND trim(canonical_download_url) <> ''
      AND download_status IN (
          'changed_remote', 'discovered', 'failed_retryable', 'failed_terminal',
          'interrupted', 'missing_local', 'paused_low_space', 'queued'
      );
