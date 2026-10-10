"""Search a fixed set of common origin-level specification locations.

Retrieved documents are unvalidated: HTML and unrelated documents can appear
here. This component neither chooses a contract nor emits a validated outcome.
"""

from dataclasses import dataclass, replace
from urllib.parse import urlsplit, urlunsplit

from radar.discovery.fetch import FetchResult, fetch_document
from radar.discovery.input import normalize_target
from radar.discovery.limits import BudgetExceeded, DiscoveryBudget
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, NormalizedTarget


COMMON_SPEC_PATHS = ("/openapi.json", "/openapi.yaml", "/swagger.json")
SPEC_EXTENSIONS = (".json", ".yaml", ".yml")
SEARCH_LIMITATIONS = (
    "Only common origin-level specification locations were searched.",
    "Provider mappings and official-documentation links were not searched.",
    "Retrieved documents have not been validated as OpenAPI or matched to the target.",
)


@dataclass(frozen=True)
class RetrievedCandidate:
    candidate: ContractCandidate
    retrieval: FetchResult


@dataclass(frozen=True)
class CandidateSearchResult:
    target: NormalizedTarget
    candidates: tuple[RetrievedCandidate, ...]
    fetches: tuple[FetchResult, ...]
    skipped_urls: tuple[str, ...]
    stop_reason: str | None = None
    limitations: tuple[str, ...] = SEARCH_LIMITATIONS


def common_candidate_urls(target: NormalizedTarget) -> tuple[str, ...]:
    """Use the origin, never the endpoint path/query; preserve explicit ports."""
    parsed = urlsplit(target.normalized_url)
    return tuple(dict.fromkeys(
        urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        for path in COMMON_SPEC_PATHS
    ))


def search_common_locations(
    request: DiscoveryRequest,
    budget: DiscoveryBudget,
    *,
    allow_loopback: bool = False,
    cache: dict | None = None,
) -> CandidateSearchResult:
    """Fetch all supported locations without selecting the first successful one.

    The caller owns the budget and must reuse it for subsequent discovery and
    reference capture. Individual retrieval failures are retained. Exhaustion of
    a shared budget stops this strategy and explicitly lists unvisited locations.
    """
    target = normalize_target(request)
    candidates = tuple(ContractCandidate(
        source_url=url,
        discovery_method="common_location",
        discovery_source=url,
        limitations=("OpenAPI validity and relevance have not been assessed.",),
    ) for url in common_candidate_urls(target))
    return fetch_candidates(target, candidates, budget, allow_loopback=allow_loopback,
                            limitations=SEARCH_LIMITATIONS, cache=cache)


def fetch_candidates(target, locations, budget, *, allow_loopback=False, limitations=(),
                     document_limits=None, cache=None):
    """Fetch each distinct source URL once, retaining every provenance record.

    `document_limits` optionally maps a source URL to a larger per-document size
    cap for that URL only; other URLs keep the budget's cap. `cache`, shared across
    strategies of one run, maps (url, size cap) to a finished fetch so the same URL is
    never requested twice; a cached result costs no budget. Results that only reflect an
    exhausted budget are not cached.
    """
    document_limits = document_limits or {}
    cache = {} if cache is None else cache
    grouped = {}
    for candidate in locations:
        grouped.setdefault(candidate.source_url, []).append(candidate)
    urls = tuple(grouped)
    fetches = []
    candidates = []
    skipped = ()
    stop_reason = None
    for index, url in enumerate(urls):
        cache_key = (url, document_limits.get(url))
        result = cache.get(cache_key)
        if result is None:
            try:
                budget.remaining()
            except BudgetExceeded as error:
                stop_reason = error.code
            if stop_reason is None and budget.requests_used >= budget.limits.max_requests:
                stop_reason = "request_limit"
            if stop_reason is None and budget.bytes_used >= budget.limits.max_total_bytes:
                stop_reason = "total_size_limit"
            if stop_reason is not None:
                skipped = urls[index:]
                break

            extra = {'document_byte_limit': document_limits[url]} if url in document_limits else {}
            result = fetch_document(url, budget, allow_loopback=allow_loopback, **extra)
            if result.ok or result.failure.code not in {"request_limit", "deadline_exceeded", "total_size_limit"}:
                cache[cache_key] = result
        fetches.append(result)
        if result.ok:
            for candidate in grouped[url]:
                candidates.append(RetrievedCandidate(
                    candidate=replace(candidate, source_url=result.final_url),
                    retrieval=result,
                ))
        elif result.failure.code in {"request_limit", "deadline_exceeded", "total_size_limit"}:
            stop_reason = result.failure.code
            skipped = urls[index + 1:]
            break
    return CandidateSearchResult(target, tuple(candidates), tuple(fetches), skipped,
                                 stop_reason, limitations)


DIRECT_LIMITATIONS = (
    "The target looked like a specification file, so exactly that URL was fetched.",
    "The document has not been validated as OpenAPI or checked against any requested context.",
)


def looks_like_spec_url(target: NormalizedTarget) -> bool:
    return urlsplit(target.normalized_url).path.lower().endswith(SPEC_EXTENSIONS)


def search_direct_url(
    request: DiscoveryRequest,
    budget: DiscoveryBudget,
    *,
    allow_loopback: bool = False,
    cache: dict | None = None,
) -> CandidateSearchResult:
    """Fetch the target itself when its path ends in .json, .yaml or .yml; otherwise do nothing.

    The URL is treated as the document, not as an endpoint of the API. The result is one unvalidated
    candidate (discovery method `direct_url`). A path with such an extension can equally be an API
    endpoint (`/users.json`), so a response that is not a contract must be treated as a miss.
    """
    target = normalize_target(request)
    if not looks_like_spec_url(target):
        return CandidateSearchResult(target, (), (), (), None, DIRECT_LIMITATIONS)
    candidate = ContractCandidate(
        source_url=target.normalized_url, discovery_method="direct_url", discovery_source=target.normalized_url,
        limitations=("OpenAPI validity and relevance have not been assessed.",))
    return fetch_candidates(target, (candidate,), budget, allow_loopback=allow_loopback,
                            limitations=DIRECT_LIMITATIONS, cache=cache)
