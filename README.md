# API Drift Radar

API Drift Radar is an external API compatibility and migration assistant being built to answer:

> What changed in an external dependency, how does it affect our application, and what should we do about it?

The project started with a Python CLI that detects changes in observed JSON response structures. It is now moving toward published contract discovery and background monitoring, with code impact analysis and reviewable migration assistance planned afterward.

## Current progress

**Milestone 1 — JSON structure comparison: implemented.**

- Fetch JSON responses and extract field paths and types.
- Save a local baseline and compare later responses against it.
- Report added fields, absent fields, and type changes.
- Support nested objects, arrays, and mixed element types.
- Validate baseline ownership and write baseline files atomically.

The repository has been reorganized into one Python backend package and a separate frontend directory. The existing suite passed **134 tests and doctests** during that migration.

**Milestone 2 — Contract discovery and monitoring: current development focus.**

Discovery (`radar.discovery.orchestrator.discover`) is implemented and tested; monitoring, the HTTP API, the frontend, OpenAPI comparison and SQLite persistence are not. There is no running HTTP API or dashboard yet.

What discovery does, within shared request, time, size and host limits:

- Normalizes a URL or host, then searches in order: a target that is itself a specification URL, provider mappings (Stripe and GitHub are bundled), common specification locations, documentation pages, the public APIs.guru directory, and navigation (typed links, an RFC 9727 catalogue, Swagger/Redoc viewer configuration, sibling documentation hosts, framework probes).
- Validates OpenAPI 3.0/3.1 documents (bounded; YAML nesting is checked before parsing), captures `$ref` references, and matches the contract to the target by explicit, evidenced rules.
- Returns one of six statuses: `validated`, `unconfirmed`, `ambiguous`, `rejected`, `inaccessible`, `not_found`. `unconfirmed` means a contract declares the target host but nothing confirms it is complete and current (for example a directory copy); it must not be used to raise drift alerts. Each package records whether it came from the provider or a directory copy (`source_kind`).
- Reports unsupported formats, documentation-only data, authentication barriers and search coverage as optional details. An optional, default-off language-model fallback with a cost ledger and hard spending limits is available.

Sample outcomes for the API and frontend owners are in `docs/examples/discovery-outcomes/`; see `docs/component-interfaces.md` for interfaces and status.

## Try the current CLI

Requires Python 3.10 or later. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './backend[test]'
radar --help
```

Create a baseline, then check the same endpoint later. Replace the example URL with an accessible JSON endpoint:

```sh
radar baseline https://api.example.com/data --baseline .radar/demo-baseline.json
radar check https://api.example.com/data --baseline .radar/demo-baseline.json
```

Example findings:

```text
  + user.email (added, string)
  ~ id (type changed: number -> string)
```

`python -m radar.cli` is an equivalent entry point. Without `--baseline`, the CLI uses `baseline.json` in the current working directory. Existing baselines are replaced only when `baseline --overwrite` is requested; `check` does not advance the baseline.

| Exit code | Meaning |
|---|---|
| `0` | Success with no differences |
| `1` | Structural differences found |
| `2` | Invalid input, fetch failure, or baseline error |

The CLI compares structure, not response values. Array order and length do not create findings, and empty arrays provide no evidence about element structure. An observed JSON response is not a complete published API contract, and a structural difference alone does not establish application breakage.

See the [CLI guide](backend/src/radar/cli/README.md) for commands and limitations.

## Next: contract discovery and monitoring

Milestone 2 will let a developer submit an API URL or provider domain, discover an accessible published OpenAPI contract, and monitor it over time.

Planned capabilities:

- **Discovery and matching:** bounded searches of known provider mappings, common specification locations, and supported links in official documentation. Validate relevance and expose ambiguous candidates for selection.
- **Reproducible snapshots:** retain original documents, required references, normalized comparison data, and provenance using SQLite metadata and immutable local files.
- **Contract comparison:** detect supported operation, parameter, required-field, type, enum, and documentation changes. Classify findings as potentially breaking, non-breaking under documented rules, documentation-only, or uncertain.
- **Background execution:** run manual and scheduled checks through a shared workflow, with explicit failure states and protection against overlapping work.
- **Backend API and web interface:** configure sources, select candidates, pause monitoring, trigger checks, and review history, findings, and both snapshots.
- **Acceptance environment:** demonstrate unchanged, changed, ambiguous, and failed captures against a controlled API, including restart persistence and labeled expected findings.

For monitoring, the first successful capture will establish the reference. Later successful captures will compare against the previous valid snapshot; failed checks will preserve it. Unchanged checks will still be recorded without duplicating findings. Historical findings will remain available.

This milestone focuses on REST OpenAPI. Supported versions and comparison coverage will be documented during implementation. Application code impact, GraphQL, and MCP compatibility are outside its scope.

## Intended architecture

```text
Web frontend ──→ Backend HTTP API ──→ Shared Radar components
                        │                       │
                  Accepted work           Stored results
                        │                       │
                        ▼                       ▼
                 Background runner ──→ SQLite + contract files
                        │
                        ▼
              External published contracts
```

Radar is a separate background analysis system. The HTTP API and runner share one Python application and its component modules. Accepted work and scheduled monitoring should continue when the browser closes, while the backend and runner remain running.

Discovery finds and validates candidates; comparison produces findings; storage owns records and artifacts; monitoring coordinates the lifecycle. The frontend submits actions and presents evidence. Later, application instrumentation will supply evidence asynchronously so expensive analysis stays outside the application's request path.

| Area | Current or intended choice |
|---|---|
| Backend and CLI | Python; one package under `backend/` |
| Current verification | pytest, unittest-based tests, Hypothesis, doctests |
| Monitoring persistence | SQLite metadata and immutable local files — planned |
| HTTP framework and scheduler | To be selected during implementation |
| Frontend framework | Not selected; directory scaffold only |
| Deployment | Local development first; public deployment is not configured |

## Repository layout

```text
api-drift-radar/
├── backend/
│   ├── pyproject.toml          # Python dependencies and CLI entry point
│   ├── src/radar/
│   │   ├── discovery/          # Normalization, bounded fetching, candidate search, fallback input reduction
│   │   ├── comparison/         # Existing JSON structure engine
│   │   ├── storage/            # SQLite history and immutable contract files
│   │   │   └── migrations/     # SQLite schema migrations, shipped in the package
│   │   ├── monitoring/         # Check workflow and scheduling scaffold
│   │   ├── api/                # HTTP API scaffold
│   │   ├── cli/                # Working terminal commands
│   │   ├── domain/             # Shared domain model scaffold
│   │   └── config.py           # Runtime path configuration
│   └── tests/                  # Unit tests and local fetcher integration tests
├── web/
│   ├── package.json            # Placeholder; no build scripts yet
│   ├── src/
│   │   ├── features/           # Sources, checks, findings
│   │   ├── components/         # Shared UI scaffold
│   │   └── lib/                # API client scaffold
│   └── tests/
├── samples/controlled-api/     # Controlled provider scaffold
├── tests/acceptance/           # Full-system test scaffold
├── docs/                       # Setup, architecture, interfaces, decisions
├── .env.example
└── .radar/                     # Ignored local runtime data
```

Monitoring history is stored in `.radar/radar.db` (SQLite, created and migrated automatically) and captured contracts in `.radar/contracts/` (immutable, one copy per distinct content). See [setup](docs/setup.md) and the storage section of [component interfaces](docs/component-interfaces.md#storage-contract-snapshots-and-check-history-issue-10).

## Discovery search budget

Discovery defaults to 40 requests over 90 seconds, reserving 10 requests for
contract references. Navigation can inspect 16 pages/assets across up to 8 hosts,
with a page depth of 4. The optional LLM now helps before speculative probes or
navigation exhaustion, using only observed, unexplored links. Calls are skipped
when there is insufficient capacity to follow suggestions. Enable it explicitly
with `python -m radar.discovery TARGET --llm`; the existing two-call limit and
$5 cumulative discovery cap still apply. Deterministic discovery needs no model.

## Development and verification

Run the backend tests and doctests from the repository root:

```sh
python -m pytest -c backend/pyproject.toml backend/tests backend/src
```

The suite (about 1,380 tests) covers structural comparison and the CLI, and for discovery: input normalization, bounded fetching, validation, matching (including a labeled corpus), candidate search, the directory, sibling hosts and navigation, `unconfirmed` handling, the orchestrator end to end, and the sample outcome files. Network access is faked or goes to a local HTTP server; no live provider is needed. Supported Python is 3.10 or later; the suite is expected to pass on all three platforms and these versions. CI runs the suite on Linux, Windows and macOS with Python 3.10, 3.12 and 3.14 (`.github/workflows/tests.yml`).

The live accuracy benchmark is separate and needs the network (no model, no cost):

```sh
python -m radar.discovery.benchmark                                   # development set
python -m radar.discovery.benchmark --targets backend/benchmark/holdout.json
```

Report recall for `validated` and `unconfirmed` separately; the development set was used while building discovery, so only the holdout is an unbiased figure. These tests do not certify the future contract-monitoring workflow. Full-system acceptance tests remain unimplemented.

`.env.example` documents the optional `RADAR_DATA_DIR` setting for monitoring paths. Environment files are not loaded automatically; export variables in your shell. The existing CLI's default baseline path remains unchanged.

## Longer-term direction

After contract monitoring, the roadmap adds:

1. **Code impact analysis:** connect changed operations and fields to affected integration code and source locations.
2. **Runtime evidence:** attach observed usage and failures, with explicit observation windows and coverage limitations.
3. **Migration assistance and verification:** produce evidence-backed reports and reviewable patches, then record what validation actually establishes.
4. **MCP compatibility:** extend monitoring and analysis to external MCP tool definitions.
5. **Cleanup recommendations:** identify potentially obsolete integration code for human review.
6. **Integrated case study:** demonstrate the workflow reproducibly and measure correctness against labeled scenarios.

Discovery, validation, comparison, and storage are intended to work deterministically without an LLM. Optional LLM assistance may later help suggest discovery links and produce migration reports or patches, with bounded spending and recorded evaluations.

Contract changes are not automatically application failures. Missing runtime observations are not proof that code is unused. Suggested patches and deletions remain reviewable, and reports must make unsupported analysis and verification limits explicit.

## Documentation

- [Local setup](docs/setup.md)
- [Architecture and component responsibilities](docs/architecture.md)
- [Component interfaces](docs/component-interfaces.md)
- [CLI usage and limitations](backend/src/radar/cli/README.md)
- [Design decisions](docs/decisions/)
