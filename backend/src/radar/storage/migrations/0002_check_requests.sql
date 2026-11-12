-- Radar storage, version 2: checks that were requested but have not started.
--
-- A request reserves the identifier its check run will have, so a caller
-- (the CLI, later the HTTP API) gets that identifier at once and can look the
-- check up while it waits and after it runs. The runner starts a request by
-- creating the check run with the same identifier and setting started_at, in
-- one transaction.
--
-- A source has at most one pending request: asking again while one waits
-- returns the waiting one. Requests are history too: they are never deleted,
-- and the only change allowed is setting started_at once.

CREATE TABLE check_requests (
    check_id     TEXT PRIMARY KEY,                -- the id the check run gets
    source_id    TEXT NOT NULL REFERENCES sources(id),
    kind         TEXT NOT NULL CHECK (kind IN ('manual', 'scheduled')),
    requested_at TEXT NOT NULL,
    started_at   TEXT                             -- set once, when the check run is created
);
CREATE UNIQUE INDEX check_requests_one_pending_per_source
    ON check_requests (source_id) WHERE started_at IS NULL;
CREATE INDEX check_requests_pending
    ON check_requests (requested_at) WHERE started_at IS NULL;

CREATE TRIGGER check_requests_identity
BEFORE UPDATE OF check_id, source_id, kind, requested_at ON check_requests
BEGIN SELECT RAISE(ABORT, 'a check request keeps its id, source, kind and request time'); END;

CREATE TRIGGER check_requests_start_once BEFORE UPDATE OF started_at ON check_requests
WHEN OLD.started_at IS NOT NULL OR NEW.started_at IS NULL
BEGIN SELECT RAISE(ABORT, 'a check request is started once'); END;

-- Started means its check run exists, with the same id and source.
CREATE TRIGGER check_requests_started_with_its_run AFTER UPDATE OF started_at ON check_requests
WHEN NOT EXISTS (
    SELECT 1 FROM check_runs WHERE id = NEW.check_id AND source_id = NEW.source_id)
BEGIN SELECT RAISE(ABORT, 'a started request must have its check run'); END;

CREATE TRIGGER check_requests_permanent BEFORE DELETE ON check_requests
BEGIN SELECT RAISE(ABORT, 'check requests cannot be deleted'); END;
