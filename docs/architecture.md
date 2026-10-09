# Architecture

One backend Python application supplies shared components to an HTTP API and a separately running background runner. A web frontend submits configuration/actions and presents stored results. Accepted and scheduled checks must continue independently of the browser while the backend and runner remain active.

## Component responsibilities

- `discovery`: find published contract candidates and establish relevance.
- `comparison`: structural comparison today; separate OpenAPI comparison rules in Milestone 2. Observed JSON structures are not complete published contracts.
- `storage`: existing baseline/atomic file utilities; future SQLite metadata and immutable snapshot storage.
- `monitoring`: future shared manual/scheduled workflow, background execution, and recovery.
- `api`: future HTTP request validation, dispatch, and response schemas.
- `cli`: existing terminal commands and JSON response fetch adapter.
- `domain`: future shared source, snapshot, check, and finding definitions. Existing structural finding types remain with the comparison engine.

Snapshots retain original contracts and required references, normalized representations, and provenance. SQLite stores metadata and histories. The initial capture establishes a reference; later successful checks compare against the previous valid snapshot. Failures preserve that reference. Unchanged checks still retain execution history.

Framework, scheduler, and public deployment choices remain undecided. Later code impact, runtime evidence, migration, verification, MCP compatibility, and cleanup capabilities can be introduced as backend modules when their milestones begin.
