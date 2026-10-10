# Component interfaces

## Existing Python interfaces

- `radar.comparison`: `extract_structure`, `compare_structures`, `compare_extracted`, and existing structural types.
- `radar.storage.baseline`: `baseline_path`, `save_baseline`, `load_baseline`.
- `radar.storage.atomic_write`: `write_file_atomically`.
- `radar.cli.fetch`: `fetch_json`.
- `radar.cli.main`: `main(argv=None)` returns exit code 0 (no differences), 1 (differences), or 2 (failure).

## Milestone 2 boundaries to implement

Discovery returns a validated contract package or explicit ambiguity/failure outcome. Comparison consumes two validated packages and returns findings and coverage limitations. Neither owns persistence or scheduling.

Storage persists sources, snapshots, checks, findings, and artifacts. Monitoring coordinates these components and advances references only after successful processing. The API returns a check identifier while work continues in the runner. The web frontend uses the HTTP API.

Concrete shared types and request/response examples must be agreed before implementing those components. No HTTP endpoint or wire schema is implemented by this restructure.

## Discovery input and result models (issue #9, component 1)

`radar.discovery.input.normalize_target(DiscoveryRequest(...))` returns a
`NormalizedTarget` or raises `DiscoveryInputError`. This is a pure operation:
no DNS resolution, network requests, persistence, or contract validation occurs.
Bare hosts default to HTTPS; targets containing paths or queries require an
explicit HTTP(S) scheme. Paths and queries are retained, fragments and embedded
credentials are rejected, and optional methods are uppercased. API versions are
never inferred from paths. Local hosts are syntactically accepted for controlled
fixtures; the future fetcher must enforce network destination policy.

`radar.domain.discovery` defines requests, normalized targets, candidates,
attempts, matching evidence, captured documents, validated packages, and outcomes.
Outcome statuses are `validated`, `ambiguous`, `not_found`, `inaccessible`, and
`rejected`. Only a validated outcome carries a package. These are interface
records, not implementations of discovery or OpenAPI validation; a package must
only be constructed by the future validator after all required references resolve.
Mixed-result status precedence remains part of the later orchestration component.

Captured content remains in memory for a future storage caller. Frozen dataclasses
prevent attribute reassignment but do not deeply freeze parsed document mappings.

## Bounded document fetcher (issue #9, component 2)

```python
from radar.discovery.fetch import fetch_document
from radar.discovery.limits import DiscoveryBudget

budget = DiscoveryBudget()  # One instance per sequential discovery run.
result = fetch_document("https://api.example.com/openapi.json", budget)
if result.ok:
    print(result.final_url, result.status, result.content_type, result.content)
else:
    print(result.failure.code, result.failure.reason)
```

A `FetchResult` includes requested/final URLs, HTTP status, media type, original
body bytes, a UTC retrieval timestamp, and attempted URLs/statuses. Failed results
never expose partial content. HTTP 429 responses retain `Retry-After`; there are
no automatic retries. A successful HTTP fetch does not establish OpenAPI validity
or relevance. The orchestrator will map fetch failures to discovery outcomes.

Default `FetchLimits`: 20 request attempts (including redirects), 3-second DNS
wait/connection timeout, 5-second read timeout, 30-second discovery deadline,
5 MiB per document (a verified provider mapping may allow more for its own URL), 32 MiB total body bytes, and 3 redirects per fetch. The shared
budget counts failures, partial body reads, and future reference requests. For
unknown-length responses, one sentinel byte may be consumed to detect overflow.
Do not create a new budget per candidate or use one budget concurrently.

Each redirect is checked again. All resolved addresses must be public by default;
the TCP connection is pinned to a checked answer while HTTPS retains the original
hostname for TLS verification. `allow_loopback=True` permits only loopback in
addition to public destinations, for controlled development tests. Other private
addresses remain blocked. HTTPS-to-HTTP redirects are rejected. Environment
proxies, cookies, authentication, and compressed response bodies are unsupported;
the fetcher requests identity encoding and rejects other content encodings.

The platform DNS resolver runs on a daemon thread with a bounded caller wait.
Python cannot cancel an outstanding OS DNS call; after timeout that thread may
remain until the resolver returns. Request budgets bound resolver calls within a
run. This implementation is for sequential local discovery, not a public
multi-tenant service. Documents are returned in memory; nothing is persisted.

Print controlled-server results with:

```sh
python -m pytest -c backend/pyproject.toml backend/tests/integration/test_discovery_fetch.py -q -s
```

Run fetcher unit tests with:

```sh
python -m pytest -c backend/pyproject.toml backend/tests/unit/discovery/test_document_fetch.py -v
```

## Common-location candidate search (issue #9, component 3a)

```python
from radar.discovery.candidates import search_common_locations
from radar.discovery.limits import DiscoveryBudget
from radar.domain.discovery import DiscoveryRequest

result = search_common_locations(
    DiscoveryRequest("http://127.0.0.1:8765/v1/customers"),
    DiscoveryBudget(),
    allow_loopback=True,  # Only for a controlled local server.
)
for fetch in result.fetches:
    print(fetch.requested_url, fetch.status,
          "candidate retrieved" if fetch.ok else fetch.failure.code)
print("Retrieved candidates:", len(result.candidates))
print("Skipped:", result.skipped_urls, "Stop reason:", result.stop_reason)
print("Contract validation: not performed")
```

`search_common_locations` normalizes the request, derives its origin, and tries
`/openapi.json`, `/openapi.yaml`, then `/swagger.json`. Endpoint paths and queries
are not appended to these locations. An explicit port is retained. Generated
locations are deduplicated in order. The supplied budget is shared across all
fetches, redirects, and any later strategy. No new budget is created internally.

The result retains the normalized target (including optional matching context),
all fetch results, unvisited locations, a budget stop reason when applicable,
and explicit search limitations. Each successful retrieval is an unvalidated
`RetrievedCandidate`: candidate provenance plus the original `FetchResult`.
Its source URL is the final URL; its discovery source is the probed URL. Redirect
attempts remain in the retrieval record. Separate probes redirecting to the same
URL retain their individual provenance; cross-strategy candidate consolidation
will be handled later.

Failures such as HTTP 404, HTTP 403, and timeout remain individually visible.
Ordinary fetch failures do not stop the next probe. Exhausted shared budgets stop
the search and identify skipped locations. A total-size-limit failure also stops
this strategy conservatively, even when rejection happened from Content-Length
before reading the body. A per-document-size failure permits the next probe.

The search does not stop at the first successful download and never declares a
validated match, ambiguity, or a final discovery status. HTTP 200 HTML, unrelated
content, and empty bodies remain possible candidates until validation. No provider
mapping, documentation parsing, endpoint-parent probing, or OpenAPI validation is
implemented by this strategy. No candidates here does not prove no contract exists.

Run the controlled-server example (no server setup or external provider needed):

```sh
python -m pytest -c backend/pyproject.toml backend/tests/integration/test_candidate_search.py -q -s
```

For manual use, serve an `openapi.json` or `openapi.yaml` file locally on port 8765,
then run the Python example above. Candidate search currently has a Python API,
not a new `radar` CLI subcommand.

## Provider mapping search (issue #9, component 3b)

`radar.discovery.providers.search_provider_mappings(request, budget,
registry_path=None, allow_loopback=False)` returns a `ProviderSearchResult` with
all applicable `mappings` and a `search` containing the same candidate/fetch/skip
records as common-location search. The budget is reused, not reset.

The bundled `discovery/data/providers.json` deliberately has an empty provider
list. The mapping mechanism is implemented; no real providers are claimed as
supported yet. Add entries only after verifying the specification location and
provenance against official sources. Controlled test entries are not bundled.

Registry format (fictional example for a separate local file):

```json
{
  "schema_version": 1,
  "providers": [
    {
      "id": "example-payments",
      "hosts": ["api.example.test"],
      "product": "payments",
      "api_version": "v1",
      "spec_url": "https://docs.example.test/specs/payments.yaml",
      "provenance_url": "https://docs.example.test/payments"
    }
  ]
}
```

Hosts match exactly after case/IDNA normalization and removal of a trailing DNS
root dot. No suffix, wildcard, subdomain, or substring matching occurs. A mapping
host cannot include a scheme, port, or path. Matching is host-level; a target port
is not part of registry selection. Specification URLs remain explicit and subject
to the existing fetcher's destination policy. Product/version hints do not filter
or rank mappings; all matching records remain available for later relevance
assessment. Request context is retained in the normalized target.

Each distinct normalized specification URL is fetched once per strategy call.
Multiple mapping records pointing to it retain separate provenance records sharing
the retrieval result; candidate-record count is therefore not unique-document
count or evidence of ambiguity. Cross-strategy deduplication is still future work.
Redirects retain the mapped request URL and final source URL. A mapping's official
provenance URL is recorded, not fetched or verified by this component.

The local JSON registry is validated completely before fetching: schema version,
required/unknown fields, unique IDs, hosts, and explicit HTTP(S) URLs. Limits are
1 MiB, 100 mappings, and 20 hosts per mapping. Malformed configuration raises an
error instead of silently appearing as an unknown provider. Unknown hosts perform
no requests and do not trigger automatic fallback; the future orchestrator will
combine strategies. Individual access failures remain in `search.fetches`.

Print controlled provider results:

```sh
python -m pytest -c backend/pyproject.toml backend/tests/integration/test_provider_search.py -q -s
```

Call manually with your own registry file:

```python
from radar.discovery.providers import search_provider_mappings
from radar.discovery.limits import DiscoveryBudget
from radar.domain.discovery import DiscoveryRequest

result = search_provider_mappings(
    DiscoveryRequest("api.example.test"), DiscoveryBudget(),
    registry_path=".radar/providers.json",
)
print("Matched mappings:", [mapping.id for mapping in result.mappings])
for fetch in result.search.fetches:
    print(fetch.requested_url, fetch.status,
          "unvalidated candidate" if fetch.ok else fetch.failure.code)
```

## LLM fallback input reduction (issue #9, optional fallback, component A)

`radar.discovery.llm_input.reduce_page(document, limits=None)` takes a retrieved
documentation page (`FetchResult`) and returns a `ReducedPage`: the spec-related
items only, in document order, plus the combined text a later model step may see.
It is pure and deterministic: no network, no model, no JavaScript execution, no cost.

Kept: links whose URL or label mentions a spec (or JSON/YAML links with a label),
short windows of inline script text around OpenAPI/Swagger/Redoc keywords,
`<script src>` URLs with those keywords, `<link rel="service-desc">`/`describedby`
and JSON/YAML alternates, spec-related `<meta>` tags, and `spec-url`-style
attributes. Relative URLs resolve against the page (honouring `<base href>`);
fragments are removed; same-page anchors and non-HTTP(S) links are dropped.
Control characters are stripped and quotes replaced so an item stays on one line.

`ReductionLimits` defaults: 1 MiB page, 12,000 characters, 100 items, 500
characters per snippet, 120 per label. Exceeding a limit sets `truncated` and adds
a note; earliest items win. Non-HTML, non-UTF-8, oversized, unparseable, and failed
pages return an empty result with a note instead of raising.

The page is untrusted. Reduction only limits what a model could see; it is not a
defence by itself. The planned verifier (component B) accepts a model-suggested URL
only if it literally appears in this reduced text, and every accepted URL still goes
through the shared-budget fetcher and the validation and matching steps.

## OpenAPI document validation (issue #9, gate 1)

```python
from radar.discovery.validation import validate_document

result = validate_document(retrieved_bytes)          # never raises for document content
if result.ok:
    result.summary      # ContractSummary: version, title, info_version, server_urls, operations, ...
    result.document     # parsed mapping, for reference capture (no second parse)
    result.limitations  # e.g. structural-only, no operations, unexpanded path-item $refs
else:
    result.rejection    # ValidationRejection(stage='validation', code, reason, location)
```

Pure: bytes in, typed result out; no network or files. It judges one document's
structure only. Reference capture and relevance matching are separate gates.

Accepted: OpenAPI 3.0.x and 3.1.x as JSON or YAML (UTF-8, BOM allowed). Required:
`openapi`, `info.title` and `info.version` (strings), `paths` for 3.0, and at least one
of `paths`/`components`/`webhooks` for 3.1. Servers, when present, must be a list of
objects with a string `url`. Path keys must start with `/` (or `x-`); operations are
the `get`/`put`/`post`/`delete`/`options`/`head`/`patch`/`trace` objects. Webhook keys
are free-form names. `info.version` is the document's own version, not the
provider's API version. A valid document with no operations is accepted with a
limitation, since there is nothing to monitor; callers decide what to do with it.

| Rejection code | Meaning |
|---|---|
| `empty_document`, `size_limit_exceeded`, `unsupported_encoding` | nothing to read, over the byte limit, or not UTF-8 |
| `html_document` | an HTML page (typical for a catch-all route) |
| `not_json_or_yaml` | markup, binary, or a parse error (position only, content is not echoed) |
| `not_an_object` | parses, but is a scalar or list |
| `not_openapi` | an object without an `openapi` field (unrelated content) |
| `unsupported_version` | Swagger/OpenAPI 2.x, 3.2+, 4+ |
| `invalid_version` | `openapi` is missing a usable string, e.g. unquoted `3.0` in YAML |
| `invalid_structure` | a required field or object is missing or has the wrong type; `location` is a JSON pointer |
| `duplicate_key` | the same key twice (JSON or YAML), because parsers would disagree on the content |
| `circular_reference` | a recursive YAML alias |
| `nesting_limit_exceeded`, `node_limit_exceeded` | depth or value-count bounds hit |

`ValidationLimits` defaults: 32 MiB, 2,000,000 values, depth 64. Measured on the
real Stripe and GitHub specs (up to ~256K values, depth 26, parsed in 0.06 to 1.1 s),
that leaves about 8x and 2.5x headroom. The walk is iterative and counts every visit,
so a YAML alias expansion bomb exhausts the value budget instead of time or memory.

YAML is read with PyYAML's safe loader adjusted toward YAML 1.2 core scalars:
`on`/`no`/`yes` stay strings, unquoted dates such as `2022-11-28` stay strings, `1:30`
and leading-zero numbers stay strings, and numeric keys such as `200:` become the
string `"200"`. Without this, `info.version: 2022-11-28` would silently become a date.
Unquoted `version: 1.0` is a number and is rejected, with advice to quote it.

Limitations: structural validation only, not the official OpenAPI JSON Schema (every
accepted result says so). Parsing cannot be interrupted, so the byte limit is the
bound on parse time; with libyaml a 10 MB YAML file takes about 1 s, without it
(pure-Python fallback) about 6 s. `$ref` targets are not read here. Checked against
the real Stripe and GitHub specs (JSON and YAML forms give identical summaries); those
live checks are manual and not part of the test suite.

## Issue #9 status and resume plan

Done and tested (fixtures only; live provider checks were manual): input normalization,
result models, bounded fetcher, common-location search, provider-mapping search with
verified Stripe and GitHub entries, documentation-link extraction, OpenAPI document
validation (gate 1), LLM-fallback component A.

Documentation-link discovery (`radar/discovery/documentation.py`): extraction is fixed and
unit-tested. It reads Swagger UI `url`/`urls` and `Redoc.init` string literals from
JavaScript without evaluating them (dynamic or overriding configuration is reported as
unsupported), `spec-url` attributes on Redoc/RapiDoc elements, and links with a known
spec filename or an OpenAPI/Swagger label on a `.json`/`.yaml`/`.yml` path. Each candidate
records whether it is same-origin or cross-origin relative to the documentation page.
A controlled-server demo test exists (`tests/integration/test_documentation_search.py`).
Still to do: integration scenarios for trusted-origin redirects, failed seeds, limits and
duplicate links, and its own section in this document. External initializer scripts are not fetched (recorded limitation).

Fallback plan (default off, only when a suggester is passed in; the model may only
suggest URLs and can never certify a contract):

- B. Suggestion verification: parse `{"urls": [...]}`, accept only URLs that literally
  appear in the reduced text, cap at 5, normalize, emit `ContractCandidate`s with
  `discovery_method="llm_suggestion"`.
- C. Suggester interface plus recorded-response replay for tests.
- D. Budget/cost ledger: caps on calls, tokens, retries, dollars; cache by page hash;
  in-memory with a hook (SQLite persistence belongs to storage, issue #10).
- E. OpenAI-compatible adapter (works with Ollama for free local use); API key from
  an environment variable, never committed. Live runs are manual, not in tests.
- F. `search_llm_fallback(request, budget, suggester, pages)`: reuse already-fetched
  documentation pages, fetch verified suggestions through the shared `DiscoveryBudget`.

Remaining after the fallback: required `$ref`
capture, relevance matching, orchestration with status precedence, end-to-end
acceptance tests, and official-source verification of any real provider mapping.
Open decisions: cross-origin spec links from documentation, default documentation
seeds, whether to run every strategy or stop at the first valid candidate, and
which provider backs the live fallback (Merge Gateway API format not yet known).

## Verified provider mappings (issue #9)

Each bundled entry in `backend/src/radar/discovery/data/providers.json` was checked
against the provider's own repository and documentation, and the file was fetched
through Radar's fetcher. Entries are never added from memory. A mapping is a
discovery hint; the downloaded file is still validated and matched like any other.

| ID | Host | Spec URL | Provenance | Checked 2026-10-09 |
|---|---|---|---|---|
| `stripe` | `api.stripe.com` | `raw.githubusercontent.com/stripe/openapi/master/latest/openapi.spec3.json` | `github.com/stripe/openapi/tree/master/latest` | Repo owned by `stripe`; its `latest/` README calls this the GA public spec (v1 and v2). OpenAPI 3.0.0, `servers: https://api.stripe.com/`, `info.version` `2026-09-30.endive`, 4,663,536 bytes. Radar fetched it with bytes identical to a separate download. Allowance 8 MiB. |
| `github-rest-2022-11-28` | `api.github.com` | `raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.2022-11-28.json` | `github.com/github/rest-api-description/tree/main/descriptions/api.github.com` | GitHub's docs list `2022-11-28` as supported and as the default when no `X-GitHub-Api-Version` header is sent. OpenAPI 3.0.3, `servers: https://api.github.com`, 12,945,027 bytes, `api_version` hint `2022-11-28`. Allowance 16 MiB. |
| `github-rest-2026-03-10` | `api.github.com` | same directory, `api.github.com.2026-03-10.json` | same | GitHub's docs list `2026-03-10` as supported. OpenAPI 3.0.3, same server, 12,905,003 bytes, `api_version` hint `2026-03-10`. Allowance 16 MiB. |

Registry entries may set an optional `max_document_bytes` (integer, 1 byte to 32 MiB).
It raises the per-document size cap for that entry's spec URL only; the shared
total-bytes budget (default 32 MiB) still bounds the whole run, and the largest
allowance wins when several entries share a URL. Other fetches keep the 5 MiB cap.

Notes from verification:

- Stripe's `latest/` README lists `spec3.json`, but the real file is
  `openapi.spec3.json` (`latest/spec3.json` returns 404). The legacy `openapi/`
  directory is v1 only. The URL points at the `master` branch, so the content
  changes over time; reproducibility comes from the stored content fingerprint.
  Its allowance leaves headroom over the current ~4.4 MiB.
- GitHub has one entry per documented API version. All GitHub spec files report
  `info.version` `1.1.4`, so only the file name identifies the provider version. A
  bare `api.github.com` request therefore returns two candidates, which later
  orchestration must report as ambiguous unless an API-version hint selects one. The
  unversioned `api.github.com.json` is not mapped: nothing read here documents which
  version it represents. The `main` branch URLs also move over time.
- Both GitHub files together are ~25.9 MB, which is why the default total budget is
  32 MiB. With the default limits, both are fetched in about one second on a normal
  connection.
- Live fetching is a manual check, kept out of the test suite. The tests pin the
  registry contents and exact-host matching only.
