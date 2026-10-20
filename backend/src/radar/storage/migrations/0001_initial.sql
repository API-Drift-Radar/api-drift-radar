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

-- Where each document of a snapshot came from: one row per captured URL.
-- The bundle holds the bytes under `path`; identical content captured from
-- two URLs is stored once, so two rows may share a path.
CREATE TABLE snapshot_documents (
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id),
    source_url   TEXT NOT NULL,
    path         TEXT NOT NULL,                    -- file path inside the bundle
    retrieved_at TEXT NOT NULL,
    media_type   TEXT,
    is_root      INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    PRIMARY KEY (snapshot_id, source_url)
);
CREATE UNIQUE INDEX snapshot_documents_one_root
    ON snapshot_documents (snapshot_id) WHERE is_root = 1;

-- One row per check attempt, whatever its outcome.
CREATE TABLE check_runs (
    id                 TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES sources(id),
    status             TEXT NOT NULL CHECK (status IN (
                           'running', 'initial', 'unchanged', 'changed',
                           'failed', 'ambiguous', 'unconfirmed')),
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    before_snapshot_id TEXT REFERENCES snapshots(id),  -- reference compared with
    snapshot_id        TEXT REFERENCES snapshots(id),  -- capture this check made
    failure_stage      TEXT,                           -- e.g. discovery, fetch, comparison
    failure_reason     TEXT,                           -- e.g. not_found, inaccessible, rejected
    error              TEXT,
    detail_json        TEXT NOT NULL DEFAULT '{}',     -- e.g. ambiguous or unconfirmed candidates
    limitations_json   TEXT NOT NULL DEFAULT '[]',     -- comparison coverage limits
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK ((status IN ('initial', 'unchanged', 'changed')) = (snapshot_id IS NOT NULL)),
    CHECK ((status IN ('failed', 'ambiguous', 'unconfirmed')) = (failure_stage IS NOT NULL)),
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

-- Integrity rules. Storage code follows them too; enforcing them here means
-- no bug or manual edit can link records across sources or move a source's
-- reference to something that is not its own valid snapshot.

CREATE TRIGGER sources_identity BEFORE UPDATE OF id, locator, created_at ON sources
BEGIN SELECT RAISE(ABORT, 'a source keeps its id, locator and creation time'); END;

CREATE TRIGGER sources_latest_is_own_snapshot BEFORE UPDATE OF latest_snapshot_id ON sources
WHEN NEW.latest_snapshot_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM snapshots WHERE id = NEW.latest_snapshot_id AND source_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'latest snapshot must belong to the source'); END;

CREATE TRIGGER sources_latest_never_cleared BEFORE UPDATE OF latest_snapshot_id ON sources
WHEN OLD.latest_snapshot_id IS NOT NULL AND NEW.latest_snapshot_id IS NULL
BEGIN SELECT RAISE(ABORT, 'a source cannot lose its latest snapshot'); END;

CREATE TRIGGER check_runs_snapshots_on_insert BEFORE INSERT ON check_runs
WHEN (NEW.snapshot_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM snapshots WHERE id = NEW.snapshot_id AND source_id = NEW.source_id))
  OR (NEW.before_snapshot_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM snapshots WHERE id = NEW.before_snapshot_id AND source_id = NEW.source_id))
BEGIN SELECT RAISE(ABORT, 'a check can only refer to snapshots of its own source'); END;

CREATE TRIGGER check_runs_snapshots_on_update BEFORE UPDATE OF snapshot_id, before_snapshot_id ON check_runs
WHEN (NEW.snapshot_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM snapshots WHERE id = NEW.snapshot_id AND source_id = NEW.source_id))
  OR (NEW.before_snapshot_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM snapshots WHERE id = NEW.before_snapshot_id AND source_id = NEW.source_id))
BEGIN SELECT RAISE(ABORT, 'a check can only refer to snapshots of its own source'); END;

-- A change must belong to a finished 'changed' check and repeat exactly that
-- check's source and before/after snapshots.
CREATE TRIGGER changes_match_their_check BEFORE INSERT ON changes
WHEN NOT EXISTS (
    SELECT 1 FROM check_runs
    WHERE id = NEW.check_run_id
      AND status = 'changed'
      AND source_id = NEW.source_id
      AND before_snapshot_id = NEW.before_snapshot_id
      AND snapshot_id = NEW.after_snapshot_id)
BEGIN SELECT RAISE(ABORT, 'a change must match its changed check'); END;

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
