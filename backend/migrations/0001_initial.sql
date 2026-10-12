-- Radar storage, version 1: sources, contract bundles, snapshots (and where
-- each of their files came from), checks, and the changes found between
-- snapshots.
--
-- Timestamps are UTC ISO-8601 text with microseconds, so text order is
-- time order. Columns ending in _json hold JSON text.
--
-- History is append-only. Contracts, snapshots and changes never change
-- once written; a check run changes only while its status is 'running'.
-- Nothing in history can be deleted.

CREATE TABLE sources (
    id                 TEXT PRIMARY KEY,
    locator            TEXT NOT NULL,          -- API URL or provider domain
    config_json        TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    latest_snapshot_id TEXT REFERENCES snapshots(id)  -- current valid reference
);
CREATE INDEX sources_by_locator ON sources (locator);

-- One row per stored bundle in contracts/<fingerprint>/. Inserted only
-- after the bundle is complete on disk.
CREATE TABLE contracts (
    fingerprint           TEXT PRIMARY KEY,
    entry_point           TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    normalized_sha256     TEXT NOT NULL,
    document_count        INTEGER NOT NULL CHECK (document_count > 0),
    total_size            INTEGER NOT NULL CHECK (total_size >= 0),
    created_at            TEXT NOT NULL
);

-- One row per successful capture. Several snapshots can share a bundle.
CREATE TABLE snapshots (
    id                    TEXT PRIMARY KEY,
    source_id             TEXT NOT NULL REFERENCES sources(id),
    fingerprint           TEXT NOT NULL REFERENCES contracts(fingerprint),
    captured_at           TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    openapi_version       TEXT,                    -- e.g. 3.1.0, from discovery
    content_fingerprint   TEXT,                    -- discovery's format-independent identity
    media_type            TEXT,
    provenance_json       TEXT NOT NULL DEFAULT '{}'  -- discovery method, evidence, version evidence
);
CREATE INDEX snapshots_by_source ON snapshots (source_id, captured_at);

-- Where each stored file of a snapshot came from. The bundle holds the bytes
-- under `path`; the URL and retrieval time belong to this capture.
CREATE TABLE snapshot_documents (
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id),
    path         TEXT NOT NULL,                    -- file path inside the bundle
    source_url   TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    media_type   TEXT,
    is_root      INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    PRIMARY KEY (snapshot_id, path)
);
CREATE UNIQUE INDEX snapshot_documents_one_root
    ON snapshot_documents (snapshot_id) WHERE is_root = 1;

-- One row per check attempt, whatever its outcome.
CREATE TABLE check_runs (
    id                 TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES sources(id),
    status             TEXT NOT NULL CHECK (status IN (
                           'running', 'initial', 'unchanged', 'changed',
                           'failed', 'ambiguous')),
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    before_snapshot_id TEXT REFERENCES snapshots(id),  -- reference compared with
    snapshot_id        TEXT REFERENCES snapshots(id),  -- capture this check made
    failure_stage      TEXT,                           -- e.g. discovery, fetch, comparison
    failure_reason     TEXT,                           -- e.g. not_found, inaccessible, rejected
    error              TEXT,
    detail_json        TEXT NOT NULL DEFAULT '{}',     -- e.g. ambiguous candidates
    limitations_json   TEXT NOT NULL DEFAULT '[]',     -- comparison coverage limits
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK ((status IN ('initial', 'unchanged', 'changed')) = (snapshot_id IS NOT NULL)),
    CHECK ((status IN ('failed', 'ambiguous')) = (failure_stage IS NOT NULL)),
    CHECK ((status = 'initial') <= (before_snapshot_id IS NULL)),
    CHECK ((status IN ('unchanged', 'changed')) <= (before_snapshot_id IS NOT NULL))
);
CREATE INDEX check_runs_by_source ON check_runs (source_id, started_at);
CREATE INDEX check_runs_by_status ON check_runs (status);

-- One row per difference a changed check found.
CREATE TABLE changes (
    id                 TEXT PRIMARY KEY,
    check_run_id       TEXT NOT NULL REFERENCES check_runs(id),
    source_id          TEXT NOT NULL REFERENCES sources(id),
    before_snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    after_snapshot_id  TEXT NOT NULL REFERENCES snapshots(id),
    ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
    change_type        TEXT NOT NULL,
    location           TEXT NOT NULL,
    direction          TEXT,                    -- request, response, or none
    classification     TEXT NOT NULL CHECK (classification IN (
                           'potentially_breaking', 'non_breaking',
                           'documentation_only', 'uncertain')),
    summary            TEXT NOT NULL,
    rationale          TEXT,
    before_value_json  TEXT,
    after_value_json   TEXT,
    details_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (check_run_id, ordinal)
);
CREATE INDEX changes_by_source ON changes (source_id);

CREATE TRIGGER contracts_immutable BEFORE UPDATE ON contracts
BEGIN SELECT RAISE(ABORT, 'contracts are immutable'); END;
CREATE TRIGGER contracts_permanent BEFORE DELETE ON contracts
BEGIN SELECT RAISE(ABORT, 'contracts cannot be deleted'); END;

CREATE TRIGGER snapshots_immutable BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER snapshots_permanent BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots cannot be deleted'); END;

CREATE TRIGGER snapshot_documents_immutable BEFORE UPDATE ON snapshot_documents
BEGIN SELECT RAISE(ABORT, 'snapshot documents are immutable'); END;
CREATE TRIGGER snapshot_documents_permanent BEFORE DELETE ON snapshot_documents
BEGIN SELECT RAISE(ABORT, 'snapshot documents cannot be deleted'); END;

CREATE TRIGGER check_runs_final BEFORE UPDATE ON check_runs
WHEN OLD.status != 'running'
BEGIN SELECT RAISE(ABORT, 'finished check runs are immutable'); END;
CREATE TRIGGER check_runs_identity BEFORE UPDATE OF id, source_id, started_at ON check_runs
BEGIN SELECT RAISE(ABORT, 'a check run keeps its id, source and start time'); END;
CREATE TRIGGER check_runs_permanent BEFORE DELETE ON check_runs
BEGIN SELECT RAISE(ABORT, 'check runs cannot be deleted'); END;

CREATE TRIGGER changes_immutable BEFORE UPDATE ON changes
BEGIN SELECT RAISE(ABORT, 'changes are immutable'); END;
CREATE TRIGGER changes_permanent BEFORE DELETE ON changes
BEGIN SELECT RAISE(ABORT, 'changes cannot be deleted'); END;
