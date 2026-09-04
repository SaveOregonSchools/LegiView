-- Keep the lazy Download Archive anti-join bounded as a run accumulates
-- durable document items.  The existing claim index is status-first for
-- resuming queued work; this companion index serves the run/document
-- existence probe used when selecting previously unclaimed inventory rows.
CREATE INDEX idx_collection_run_items_archive_document
    ON collection_run_items(run_id, item_type, document_id, status);
