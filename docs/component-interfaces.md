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

Default `FetchLimits`: 40 request attempts (including redirects), 3-second DNS
wait/connection timeout, 5-second read timeout, 90-second discovery deadline,
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

## Reference scanning (issue #9, reference capture step 1)

`radar.discovery.references.scan_references(document, base_url, start=(), limits=None)`
finds every `$ref` in a parsed document, classifies it, and verifies the internal ones.
Pure: no network. `base_url` is the document's final retrieval URL, against which
relative references resolve. It returns a `ReferenceScan` with `external` (distinct
targets still to fetch, with occurrence counts and first location), `failures`
(`reference_unsupported` or `unresolvable_reference`, each with a JSON-pointer
`location`), `rejection` (the first failure, noting how many more), plus counts and
limitations. `classify_reference` and `resolve_pointer` are public building blocks.

Classification: `#/a/b` is internal and must resolve (percent-decoding and RFC 6901
escapes `~0`/`~1` are applied; list indexes must be canonical). A reference to the
containing document's own URL is internal. Other HTTP(S) targets are external, with the
fragment kept as a pointer into the target file. `#name` anchors are counted as
limitations, not verified. Non-HTTP(S) schemes, credentials, bad ports, absolute URLs
without a host, malformed pointers and control characters are unsupported.

What counts as a reference: any object with a string `$ref`, including siblings and
vendor extensions. Only unambiguous data keywords (`example`, `default`, `const`, `enum`)
are skipped, and not when they are property or component names (`properties`,
`schemas`, `responses`, `parameters`, ...). When unsure it follows, because a spurious
reference fails visibly while a missed one would make a contract look complete.

`start` scans one subtree while internal pointers still resolve against the whole
document; step 2 uses it to follow only the reachable parts of fetched files. Bounds:
5,000,000 visited values and 2,000 distinct external references by default; the walk
is iterative, so deep documents and shared-subtree expansion cannot overflow the stack
or run unbounded. Limitations: only `$ref` is followed (not `operationRef`,
`discriminator.mapping` or `externalValue`); `$id` base-URI changes are not applied.
Checked on the real Stripe and GitHub specs: all 4,505 and ~10,600 internal references
resolve, in under 0.1 s.

## External reference capture (issue #9, reference capture step 2)

```python
from radar.discovery.capture import capture_references, CaptureLimits

result = capture_references(root_final_url, validation.document, budget, allow_loopback=False)
if result.ok:
    result.documents    # CapturedDocument per referenced file: final URL, ORIGINAL bytes, time, media type
    result.references   # ResolvedReference edges: referrer, location, ref, requested/final URL, pointer
    result.limitations
else:
    result.rejection    # stage 'reference_capture', code, reason, location
    result.failure      # CaptureFailure: ref, target URL, underlying fetch code and HTTP status
    result.unprocessed  # references left unprocessed when it stopped
```

A contract is only complete when every document it depends on is captured. Capture
follows every external `$ref` from the validated root, fetching each file once through
the shared `DiscoveryBudget` (so request, time, size and redirect caps and private-address
blocking all apply), parsing it with the same bounded parser as validation, and checking
that each pointer exists. It follows only what is reachable: the subtree a pointer lands
on, plus any internal `#/...` targets and external references reachable from there. An
unused broken definition elsewhere in a shared file does not block a contract that never
uses it. The stored copy is still the whole file. It stops at the first failure and
returns **no** documents, so an incomplete capture can never be mistaken for a success.

Policy: cross-origin references are rejected unless `allow_cross_origin=True`; the origin
is always the root document's and is also checked after redirects, so a chain cannot walk
to another host. A referenced file must parse to an object or list, so a catch-all
"Not Found" page or bare text is never accepted as a definition. Relative references
resolve against each file's final URL.

| Rejection code | Meaning |
|---|---|
| `reference_unavailable` | fetch failed; `failure.fetch_code`/`http_status` carry the cause (404, timeout, blocked address, size limit, ...) |
| `capture_budget_exhausted` | the shared request, time or byte budget ran out; the contract is not known to be broken |
| `reference_cross_origin` | target (or its redirect) is outside the root's origin |
| `reference_invalid_document` | unparseable, HTML, duplicate keys, alias bomb, or not an object/list |
| `unresolvable_reference` | a pointer, internal or into another file, does not exist (reachable only) |
| `reference_unsupported` | non-HTTP(S) scheme, credentials, malformed URL or pointer |
| `reference_limit_exceeded` | file count, hop depth, total values or processed references over the limit |

`CaptureLimits` defaults: 50 files, depth 10, 10M values, 5,000 processed references,
cross-origin off. Locations in referenced files read `<file URL>#<pointer>`.

Verified with a fake network (55 unit tests) and real local HTTP servers (9 scenarios:
multi-file capture, missing file, redirect, cross-origin, blocked address, slow file,
oversized file, request budget, catch-all page). Deleting a guard makes tests fail for
each of origin, redirect, scalar, pointer, subtree, depth and cycle rules. The real
Stripe and GitHub specs are fully bundled, so they exercise only the internal path (all
4,505 and ~10,600 internal references resolve); external capture is covered by fixtures,
not live providers.

Bug found by the integration tests and fixed: an internal reference inside a fetched file
(`#/Error`) leads to another part of the same file that may hold more external references;
checking only that the pointer exists would have skipped them and returned an incomplete
capture as a success. Internal targets are now followed transitively.

Limitations: only `$ref` is followed (not `operationRef`, `discriminator.mapping`,
`externalValue`); `$id` base-URI changes are not applied; `#anchor` references are not
verified; fetching is sequential. Whole-file scope applies when a reference has no pointer.

## Candidate evaluation and discovery orchestration (issue #9, final assembly)

```python
from radar.discovery.orchestrator import discover, select_candidate
from radar.domain.discovery import DiscoveryRequest

outcome = discover(DiscoveryRequest("api.github.com", method="GET", api_version="2022-11-28"))
outcome.status       # validated | ambiguous | rejected | inaccessible | not_found
outcome.package      # ValidatedContractPackage (validated only)
outcome.packages     # the accepted alternatives (ambiguous only)
outcome.candidates   # every evaluated candidate, with evidence and rejection reasons
outcome.attempts     # every fetch: retrieved, not_found, inaccessible, blocked, budget_exhausted, not_a_contract
outcome.limitations  # bounds, cut-short notes, and the package's limitations
resolved = select_candidate(outcome, chosen_url)   # ambiguous -> validated, no network
```

`evaluate_candidate` (evaluation.py) runs one retrieved candidate through validation, then
matching, then reference capture (cheapest first, so a contract that does not fit never spends
request budget on its references). It returns a `ValidatedContractPackage` or exactly one
rejection tagged with its stage. `package_fingerprint` identifies a contract by its parsed content in a
canonical form (sorted object keys, list order kept, `true` kept apart from `1`), so the same contract
served as JSON and as YAML, or reformatted, is one contract; the original bytes are still what is stored.
Found on a live check: `api.weather.gov` serves identical contracts at `/openapi.json` and
`/openapi.yaml` (different bytes), which a raw-byte fingerprint wrongly reported as ambiguous.

`discover` (orchestrator.py) runs provider mappings, common locations and documentation links
in that order under ONE shared budget and one fetch cache (a URL is never fetched twice), judges
every retrieved document independently, merges identical contracts found more than once
(an `also_found` evidence record keeps the other location), and decides:

| Status | When |
|---|---|
| `validated` | exactly one distinct complete, matching contract |
| `ambiguous` | two or more distinct ones; none chosen, all in `packages` |
| `rejected` | documents were found but none passed, even if some other source was unreachable |
| `inaccessible` | nothing passed and a source failed (403, 5xx, timeout, TLS, private address) |
| `not_found` | nothing passed and every source was a clean miss (404/410, or a catch-all page or JSON error at a guessed location) |

A catch-all HTML page or unrelated JSON at a guessed location is a miss, recorded in `attempts`
as `not_a_contract` with its reason; the same response at a location a provider mapping or a
documentation page named is a real rejection. A link on an untrusted page that points at a
private address is recorded as `blocked` and does not make the result inaccessible. If the
search is cut short by its limits, the outcome says so and that other contracts may exist; there
is no separate status for that.

Shared-model changes (agree with the storage owner): `MatchingEvidence.outcome` (optional),
`DiscoveryOutcome.packages` (ambiguous only). The ambiguity of a GitHub request without a version
hint is intended: one mapping per dated API version, and a version hint selects one.

Checked live on 2026-10-09 (manual, not in the test suite): `api.stripe.com` validated from the
provider mapping in ~1 s; `POST https://api.stripe.com/v1/customers` validated with operation
evidence; `api.github.com` returned two alternatives (~3 s), `select_candidate` resolved it, and
`api_version="2026-03-10"` validated that version and rejected the other with `version_mismatch`.
A sub-product hint such as `product="billing"` was rejected for Stripe because the contract's own
text never says it (see the matching limits).

Direct URL: a target whose path ends in `.json`, `.yaml` or `.yml` is fetched exactly as given first
(`direct_url`). The URL names the document, not an endpoint or the API's host: servers declared on other
hosts are indeterminate, not a mismatch; operation checks do not apply; provenance is positive (the user
supplied it); version and product hints still reject, and a version hint can only contradict a version stated
in the contract's server paths. A valid result is the answer and no other location is searched. If the
response is not a contract (an API endpoint such as `/users.json`, a 404, an invalid file) the normal
strategies still run. A document over the per-document size limit (5 MiB by default) is refused with a note
naming `FetchLimits.max_document_bytes`; only provider-mapping entries carry their own allowance.
Checked live 2026-10-09: Stripe's raw spec URL and `api.weather.gov/openapi.yaml` validated with a single fetch.

Not done: no separate status for "search cut short".

## Issue #9 acceptance evidence

Run: `python -m pytest -c backend/pyproject.toml backend/tests/integration/test_discovery_acceptance.py -v`
(33 scenarios, ~2 s). They use real local HTTP servers and the real fetcher, with nothing patched;
`allow_loopback=True` is the only concession. A fixture-based result, not a claim about any provider.

| Acceptance criterion | Scenarios |
|---|---|
| A controlled API with a discoverable contract returns a validated candidate with source and evidence | common location, documentation link + redirect, provider mapping |
| Invalid or unrelated documents are rejected with reasons | Swagger 2.0, wrong server host, unsupported version, invalid structure, duplicate keys, missing operation/version/product; unrelated JSON recorded as a miss with its reason |
| Multiple plausible contracts produce an ambiguous result | two contracts, explicit selection, dated provider versions until a version is supplied, JSON+YAML copies merged with both locations kept |
| Inaccessible sources and unsuccessful discovery are distinguishable | 503/403/429/500, refused connection, slow server, private address refused; catch-all pages and 404s are `not_found` |
| Required external references are captured; incomplete contracts are not successes | multi-file capture with original bytes, missing file, broken internal reference, cross-origin refusal and opt-in |
| Explicit request, timeout and traversal limits | request budget, document size, redirects, reference depth and count, shared byte budget, deadline |
| Deterministic workflow without an LLM | only the controlled host is resolved; no model client is imported; repeated runs agree |

Matching quality is measured separately by `tests/unit/discovery/test_matching_corpus.py` (`-s` prints the
table): 39 labeled look-alike and legitimate cases, 0 false accepts and 0 false rejects, plus 4 known
limitations pinned so a rule change must update this list on purpose:

- A sub-product hint absent from a multi-product contract's text is rejected (Stripe `billing`).
- A provider whose API domain differs entirely from its main domain is rejected without a mapping that
  declares the API host.
- A product written differently (`ecommerce` vs `E-Commerce`) is rejected (whole-word match).
- A shared hosting suffix as the target (`github.io`) relates to every host beneath it (no public-suffix list).

Not demonstrated by fixtures and not claimed: behaviour against providers other than the Stripe, GitHub and
`api.weather.gov` spot checks done by hand on 2026-10-09.

## Discovery result JSON for the API and web interface

`radar.discovery.serialization.outcome_to_dict(outcome)` returns a JSON-ready view of a `DiscoveryOutcome`
(also `package_to_dict`, `candidate_to_dict`). Contract text is never inlined: a package carries URL, size,
SHA-256 and a content fingerprint, and rejection strings are split into `{stage, code, reason}`. Nine
generated samples with field notes are in `docs/examples/discovery-outcomes/` (see its README); a test fails
if they drift from the code. The field names are a proposal for the API owner, not a final wire format.

## Language-model fallback and cost tracking (issue #9, optional)

Off by default. It runs only when deterministic discovery found no valid contract and at least one
documentation page was fetched; a directly requested contract that validates never reaches it.

```sh
export MERGE_API_KEY=...            # never commit it; .env and .radar/ are gitignored
export MERGE_MODEL=anthropic/claude-haiku-4-5   # optional; this is the default (a small, inexpensive model)
python -m radar.discovery.merge_gateway         # one tiny call: checks key + model + cost tracking (< $0.001)
python -m radar.discovery.llm_cost .radar/llm_cost.jsonl --budget 5   # what has been spent
```
```python
from radar.discovery.llm_cost import CostLedger, LlmLimits
from radar.discovery.merge_gateway import MergeSuggester

outcome = discover(request, llm_suggester=MergeSuggester.from_env(),
                   llm_ledger=CostLedger(".radar/llm_cost.jsonl", LlmLimits(max_total_usd=5.0)))
```

How it works: each fetched documentation page is reduced to its few spec-related items (`llm_input`), a model
is asked which of them is the specification (`llm_suggestions`), and only suggestions that are real link
targets, or quoted in a script/tag, on that page are kept (a URL mentioned only in a link's label is not). The
kept URLs go through the normal fetcher and the same validation, matching and reference capture as every other
candidate (`discovery_method` is `llm_suggestion`; provenance is positive only if the contract is served from
the target's host). The model can suggest a link; it can never certify a contract. The page text is untrusted
data: the delimiters are neutralised and the instructions tell the model to ignore anything in it.

Provider: `MergeSuggester` speaks Merge Gateway's native Responses API, `POST {base}/responses`, with `store:
false` and `include_routing_metadata: true`. `usage.cost` is the provider's charge in USD (null when unpriced);
the gateway's own fee arrives separately as `routing.merge_fee_usd`, so spend counted is cost plus fee. The key
is read only from `MERGE_API_KEY`, sent only in the Authorization header, and never appears in a repr, error,
ledger line or log; redirects are not followed so it cannot be forwarded; plain HTTP is refused. Written from the
documented schema and tested against a local fake gateway; checked against the real service only by the smoke
command above, which you run with your own key.

Cost control (`llm_cost`): `LlmLimits` defaults: $5.00 total, 2 calls per run, 16,000 input characters, 400
output tokens. The ledger (`.radar/llm_cost.jsonl`, one JSON line per call) records model, vendor, tokens,
provider cost, gateway fee, total, whether the total was estimated, and the outcome (`ok`, `error`, `cached`,
`refused`); it stores a prompt fingerprint, never prompts, pages, replies or credentials. It is the budget's
memory, so the cap holds across runs. Before every call it refuses if the ledger is unreadable (fail closed),
the prompt is too large, the per-run limit is reached, the cap is reached, or one more worst-case call could cross
it (worst case is priced pessimistically at $5/$25 per million tokens). An unpriced reply is counted at that
estimate, never as free. Failed calls are recorded at $0 because providers normally do not bill them.
The outcome's `limitations` carry a one-line summary (calls, tokens, dollars this run, ledger total) and each
call appears in `attempts` with stage `llm_fallback`. Credential, credit and rate-limit errors end the
consultation after one try.

Limits: one process at a time per ledger file; the budget guard is only as good as the ledger file being kept;
a token estimate of 3 characters per token is deliberately pessimistic, not exact; no response caching across
runs; only documentation pages (not arbitrary links) are read.

## Discovery improvements: navigation, viewer configuration, honest reporting (issue #9, increment 2)

This increment widens where discovery looks and how honestly it reports; it does not widen what is accepted. Only OpenAPI
3.0/3.1 is a supported contract, and `validated` still means structural validation only. Everything below produces leads
or findings; acceptance remains the same deterministic validation, matching and reference capture.

### Navigation (`navigation.py`)

A prioritised queue of leads. Each lead records its **parent URL**, the **mechanism** that produced it, its role (`page`,
`description`, `catalog`, `config`, `script`) and depth, and every step is kept in `outcome.trail`. Priorities prefer explicit
publication over navigation over guessing:

| Order | Mechanism (`discovery_method` when it is a contract) | Source |
|---|---|---|
| 1 | `well_known_api_catalog`, `api_catalog` | RFC 9727 JSON Linkset at `/.well-known/api-catalog`; entries whose anchor host is related to the target (others are recorded as `skipped_unrelated`); nested catalogues, loops de-duplicated |
| 2 | `service_desc_link`, `service_doc_link`, `api_catalog_link` | HTTP `Link` headers and HTML `<link rel>` / `<a rel>` (RFC 8631, RFC 8288) on any fetched response |
| 3 | `documentation_link`, `spec_url_attribute`, `swagger_ui_config`, `redoc_config`, `swagger_config_url_entry`, `embedded_spec` | links and viewer configuration on documentation pages (below) |
| 4 | `viewer_initializer`, `requirejs_data_main` (scripts), `swagger_config_url` (configuration JSON) | small linked assets |
| 5 | `llm_suggestion` | a link a model chose among links it was shown (optional) |
| 6 | `origin_root`, `documentation_navigation` | the origin's homepage and the developer/API/reference links on it, scored by keyword; at most 6 followed per page, cross-origin links need a higher score |
| 7 | `framework_probe` | `/v3/api-docs`, `/openapi/v1.json`, `/swagger/v1/swagger.json`, `/v2/api-docs`, at the origin root and under at most one prefix taken from the target's own path |

Explicit evidence (catalogue, typed links, configuration on pages already fetched) is always followed. The origin's own
pages and the framework probes run **only when nothing has been accepted yet**: they are guesses and cost requests. Cross-origin
links are followed through the same protected fetcher (private addresses refused, redirects re-checked); the connection is kept
in the trail and **a link alone is never provenance or applicability** (see policy changes). The requested method is only a
matching hint: discovery issues GET requests for documentation and metadata and never calls the target endpoint. Pages and
fetched assets are untrusted data and are parsed, never executed.

`NavigationLimits` (defaults): 16 pages/scripts/configurations, depth 4 for pages (contracts, scripts and configuration may be one
hop further), 6 links per page, 80 leads, 20 catalogue entries, 4 catalogues, 2 initializers per page, 256 KiB per script or
configuration file, 2 probe contexts. `FetchLimits` gained `max_hosts` (8 distinct host:port pairs) and `reference_reserve` (default a
quarter of `max_requests`): navigation, configuration fetching and model-chosen links all share the request, byte, time and host
budget, and the reserve can be spent only by reference capture. `FetchLimits.deep()` is the research proposal (60 requests, 120 s).

### Viewer configuration (`viewer_config.py`)

Static only; nothing is executed. Read: Swagger UI `url`, `urls`, `configUrl` and embedded `spec` (inline in a script, or in the
JSON a `configUrl` serves, for example springdoc's), `Redoc.init('...')`, `<redoc spec-url>`, linked initializer files **by file
name** (`swagger-initializer.js`, `swagger-ui-init.js`, `redoc-init.js`, ...; never bundles, presets or app chunks), and RequireJS
`data-main` scripts that name apiDoc's `api_data` / `api_project` modules. Relative URLs inside a script or configuration resolve
against the **page that loads them**, as a browser does, and RequireJS module ids against the `data-main` script's directory. An
embedded specification is captured with its containing source as the candidate's source URL. A configuration computed at runtime
(for example Petstore's initializer, whose URL depends on `window.location`) is reported as `unsupported_dynamic_configuration`
with a code per reason; it is never guessed. apiDoc data files are recognised from the script's references and **not downloaded**.

### Honest reporting (optional, backward-compatible `DiscoveryOutcome` fields)

Statuses are unchanged (`validated`, `ambiguous`, `rejected`, `inaccessible`, `not_found`). New optional detail:

- `artifacts` (`ArtifactFinding`): `unsupported_description` (Swagger 2.0, Google Discovery, Smithy, AsyncAPI), `documentation_only`
  (apiDoc, Postman collection), `authentication_required` (401/403 with the status), `unsupported_dynamic_configuration`. A
  recognised unsupported description is also a rejected candidate (validation code `unsupported_format`, or `unsupported_version` for
  Swagger 2.0 and OpenAPI outside 3.0/3.1), so the outcome is `rejected`, not `not_found`. Documentation-only data alone is
  `not_found` with a note. None of these means no contract exists.
- `trail` (`LeadRecord`): every navigation step with parent, mechanism, kind, depth and outcome.
- `coverage` (`Coverage`): `complete`, `limits_reached` (`navigation_limit`, `request_limit`, `host_limit`, `deadline_exceeded`,
  `total_size_limit`, `depth_limit`, `page_limit`, `lead_limit`, `catalog_entry_limit`, `catalog_limit`), leads examined and
  unexamined (the first few listed), requests used against the limit, the reserve, and hosts contacted. An accepted contract and an
  incomplete search are reported separately and can both be true.

`serialization.py`, the CLI (`--trail`, `--deep`, `--docs-url`) and the owner samples under `docs/examples/discovery-outcomes/` show
all of them. Sixteen generated samples are drift-guarded by a test.

### Language-model fallback changes

The model now chooses **identifiers of links it was shown** (`{"choices": ["L3"]}`); it never supplies a URL. Its input is the page
title, up to six headings, up to 30 links (specification links first, then the best navigation links, each with an identifier and
including links that may lead toward a specification indirectly, such as "Integration guide"), and configuration snippets. A
chosen link becomes a lead in the navigation queue (`llm_suggestion`), fetched under the shared limits and judged like any other;
a chosen page can lead on to further links within the depth limit. A reply naming a URL instead is still accepted only if the URL is
a real link target or quoted configuration on the page (a URL in a link label does not count). Unknown identifiers, context lines and
snippets cannot be chosen. A model choice is never evidence of identity and carries no confidence value. The existing call, token and
spending limits and the ledger apply unchanged; automated tests use scripted model replies only.

### Policy and behavior changes to review

- **Provenance**: a contract linked from a page on a host related to the target, but hosted elsewhere, is now `indeterminate`
  (the connection is recorded), no longer `match`. Applicability still comes from the contract's declared servers, operation, version
  and product.
- Each discovery makes one extra request for the catalogue probe, even when a contract is found at once.
- The model sees more links than before (promising navigation links, headings), so a page costs more input tokens than the old
  specification-only view. The 16,000-character prompt cap is unchanged.
- A model-chosen link that is an HTML page is navigated, not rejected as a bad contract.

### Interface additions

`FetchResult.link_header`; `FetchLimits.max_hosts`, `reference_reserve`, `deep()`; `DiscoveryBudget.reference_phase()`,
`navigation_remaining()`; `domain.discovery.ArtifactFinding`, `LeadRecord`, `Coverage` and the optional `DiscoveryOutcome.artifacts`,
`trail`, `coverage`; validation rejection code `unsupported_format`; evidence criterion `navigation_path`; new `discovery_method` values
(table above); modules `links`, `catalog`, `formats`, `viewer_config`, `link_scoring`, `navigation`.

### Verification and what it does not show

Deterministic fixtures on real local servers cover each scenario in the brief (homepage to cross-origin documentation, framework
location under a context path, Swagger initializer to configuration to version alternatives, a catalogue with loops and unrelated
entries, an apiDoc site, wrong provider / unsupported format / authentication / timeout / budget exhaustion, and a scripted model
following an observed link while invented URLs and page instructions are refused). Breaking each key rule on purpose (explore gate,
initializer filter, host limit, reserve, depth, de-duplication, link headers, catalogue relevance, identifier checks, resolution base)
makes tests fail. The existing reference-capture, JSON/YAML equivalence, version ambiguity and CLI behavior tests are unchanged and
pass. This is a count of mechanisms exercised, not a measure of coverage of real providers: no recall or precision has been measured.

Optional live smoke test (network-dependent; not part of the test suite, results will change as sites change):

```sh
python -m radar.discovery https://www.fruityvice.com/api/fruit/apple --method GET --docs-url https://www.fruityvice.com/doc/index.html --trail
python -m radar.discovery https://petstore.swagger.io --docs-url https://petstore.swagger.io/ --trail
python -m radar.discovery api.stripe.com && python -m radar.discovery api.github.com --deep
python -m radar.discovery.llm_input https://docs.example.com/api      # what a model would be shown; free
```
On 2026-10-10 the first reported documentation-only apiDoc data and no contract; the second reported an unsupported dynamic Swagger
configuration (the Petstore initializer computes its URL at runtime); Stripe, `api.weather.gov` and GitHub behaved as before.

### Known limitations and deferred follow-up gaps

Not implemented: general GitHub repository crawling, a configured web-search service, browser automation, APIs.json / `llms.txt` /
sitemap reading, adapters for apiDoc, Google Discovery, Smithy, GraphQL, gRPC, MCP, OData or WSDL, inferred or LLM-generated contracts,
storage of source bindings, and frontend changes. Static viewer reading does not follow variables, `window.location` logic,
query-string configuration or external configuration other than a literal `configUrl`; only file-name-matched initializers and
RequireJS entry scripts are fetched. Navigation keyword scoring is fixed and English-only. Probes use fixed names and at most one
path prefix. Structural validation is still not the official OpenAPI JSON Schema. A bounded search cannot prove that no contract exists.

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

Reference capture is done (offline scan, internal verification, bounded external capture with
origin policy, integration tests). Relevance matching, candidate evaluation and orchestration are done. Remaining: end-to-end acceptance
tests over real HTTP, a labeled corpus for false-accept measurement, sample outcomes for the API and
frontend owners, and official-source verification of any further provider mapping.
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

## Navigation capacity and earlier model guidance (October 10, 2026)

Default discovery now permits 40 HTTP requests and 90 seconds, with 10 requests
reserved for reference capture. Navigation allows 16 pages/scripts/configurations,
page depth 4 (description/configuration assets may go one hop further), and 8
host:port pairs. The existing explicit deep preset remains 60 requests/120 seconds.
These are bounded engineering defaults, not measured guarantees of provider coverage.

When an optional suggester is configured, the navigator can pause without discarding
its queue. It pauses before framework probes, or once six navigation requests or
only two page slots remain, provided it has a page to show the model. Any contracts
already retrieved are evaluated first. If none passes, the model sees observed
links that have not already been visited or queued and that fit host/depth limits.
Pages with actionable links are preferred. Suggestions enter the same queue ahead
of speculative probes; deterministic traversal resumes afterward, including when
the model fails or has nothing useful to suggest. Existing ambiguity handling and
contract acceptance rules are unchanged.

Before each model consultation, at least three navigation requests, ten seconds,
and page/lead/byte capacity must remain. Otherwise the attempt is recorded as
`skipped_capacity`, and no model call is made. A model call can itself consume the
remaining wall-clock time; the shared deadline still governs subsequent fetching.
This capacity check is not a promise that a suggestion will reach a contract.

Paid use remains explicitly enabled with `--llm` or an injected `llm_suggester`.
The model still cannot invent accepted URLs or validate contracts. The two-call
per-run limit, recorded-response test mode, and persistent $5 discovery cost cap
are unchanged. No live model calls were needed to test this change.
