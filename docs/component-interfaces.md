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
5 MiB per document, 20 MiB total body bytes, and 3 redirects per fetch. The shared
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

## Issue #9 status and resume plan

Done and tested (284 backend tests pass; fixtures only, no live providers):
input normalization, result models, bounded fetcher, common-location search,
provider-mapping search (registry empty), LLM-fallback component A (above).

Draft, untested: `radar/discovery/documentation.py` (documentation-link discovery).
Known gaps to fix before relying on it:

1. Swagger UI parsing accepts strict JSON only. Real pages use JS literals
   (`SwaggerUIBundle({url: '/openapi.json'})`), so they are reported unsupported.
   Plan: a small literal scanner for top-level `url`/`urls` string values; no evaluation.
2. Label-only links (e.g. "OpenAPI" pointing at an HTML page) are fetched. Plan: require a
   known spec filename, or a label plus a `.json`/`.yaml`/`.yml` path.
3. Redoc/RapiDoc `spec-url` is not extracted; candidates do not record same-origin vs
   cross-origin; an inline import should move to the top of the module.
4. No unit or integration tests, and no entry in this document yet.

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

Remaining after the fallback: OpenAPI validation (3.0.x/3.1.x), required `$ref`
capture, relevance matching, orchestration with status precedence, end-to-end
acceptance tests, and official-source verification of any real provider mapping.
Open decisions: cross-origin spec links from documentation, default documentation
seeds, whether to run every strategy or stop at the first valid candidate, and
which provider backs the live fallback (Merge Gateway API format not yet known).
