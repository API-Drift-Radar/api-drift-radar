"""Run discovery end to end and report one explicit outcome.

Strategies run in a fixed order under ONE shared budget and one fetch cache: provider
mappings, common locations, then official-documentation links. All of them run, so an
ambiguity in a later strategy is not hidden by an earlier hit. Every retrieved document is
judged on its own (validation, matching, reference capture); this module only combines the
verdicts. It never picks between valid contracts and never uses an LLM.
"""

from dataclasses import replace

from radar.discovery.candidates import search_common_locations
from radar.discovery.documentation import search_documentation
from radar.discovery.evaluation import evaluate_candidate, package_fingerprint
from radar.discovery.fetch import FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.providers import search_provider_mappings
from radar.domain.discovery import (
    DiscoveryAttempt, DiscoveryOutcome, DiscoveryRequest, DiscoveryStatus, MatchingEvidence,
)


BUDGET_CODES = frozenset({'request_limit', 'deadline_exceeded', 'total_size_limit'})
# What a catch-all web server returns for a guessed path. For a guessed location this is a
# clean miss, not a rejected contract; for a location a provider or documentation page named
# explicitly, the same response is a real rejection.
NON_SPEC_CODES = frozenset({'html_document', 'empty_document', 'not_json_or_yaml', 'not_an_object', 'not_openapi'})
BOUNDED = ('Discovery is bounded: it tried provider mappings, common specification locations and '
           'links in documentation pages, within fixed request, time and size limits.')
NOT_PROOF = 'Failing to find a contract is not proof that none is published.'


def _attempt(stage, fetch: FetchResult, documentation_link=False):
    """(DiscoveryAttempt, category) where category is one of ok, not_found, inaccessible, budget, ignored."""
    if fetch.ok:
        return DiscoveryAttempt(fetch.requested_url, stage, 'retrieved'), 'ok'
    code = fetch.failure.code
    if code in BUDGET_CODES:
        category, outcome = 'budget', 'budget_exhausted'
    elif code == 'http_error' and fetch.status in (404, 410):
        category, outcome = 'not_found', 'not_found'
    elif code == 'invalid_url':
        category, outcome = 'ignored', 'invalid_url'
    elif code == 'blocked_destination' and documentation_link:
        category, outcome = 'ignored', 'blocked'  # a link on an untrusted page pointed at a private address
    else:
        category, outcome = 'inaccessible', 'inaccessible'
    return DiscoveryAttempt(fetch.requested_url, stage, outcome, f'{code}: {fetch.failure.reason}'), category


def _mapping_for(retrieved, mappings):
    candidate, requested = retrieved.candidate, retrieved.retrieval.requested_url
    return next((m for m in mappings
                 if candidate.discovery_method == 'provider_mapping' and m.spec_url == requested
                 and m.provenance_url == candidate.discovery_source), None)


def _is_non_spec(candidate_method, evaluation):
    rejection = evaluation.rejection
    return (candidate_method == 'common_location' and rejection is not None
            and rejection.stage == 'validation' and rejection.code in NON_SPEC_CODES)


def discover(
    request: DiscoveryRequest,
    *,
    limits: FetchLimits | None = None,
    budget: DiscoveryBudget | None = None,
    registry_path=None,
    documentation_urls=None,
    documentation_limits=None,
    validation_limits=None,
    capture_limits=None,
    allow_loopback: bool = False,
) -> DiscoveryOutcome:
    """Find, validate and match a published OpenAPI contract for `request`.

    Outcomes: validated (exactly one distinct complete contract fits), ambiguous (several do,
    all returned in `packages`; use `select_candidate`), rejected (documents were found but
    none passed), inaccessible (nothing passed and some source could not be reached) or
    not_found (nothing passed and every source answered with a clean miss). Raises
    DiscoveryInputError for a request that cannot be interpreted.
    """
    target = normalize_target(request)
    budget = budget or DiscoveryBudget(limits)
    cache = {}
    options = dict(allow_loopback=allow_loopback, cache=cache)

    providers = search_provider_mappings(request, budget, registry_path=registry_path, **options)
    common = search_common_locations(request, budget, **options)
    docs = search_documentation(request, budget, documentation_urls=documentation_urls,
                                limits=documentation_limits, **options)

    attempts, counts = [], {'not_found': 0, 'inaccessible': 0, 'budget': 0}
    stops, skipped = [], []

    def record(stage, searched, extra=(), documentation_link=False):
        for fetch in extra:
            attempt, category = _attempt(f'{stage}_page', fetch)
            attempts.append(attempt)
            counts[category] = counts.get(category, 0) + 1
        for fetch in searched.fetches:
            attempt, category = _attempt(stage, fetch, documentation_link)
            attempts.append(attempt)
            counts[category] = counts.get(category, 0) + 1
        if searched.stop_reason:
            stops.append(f'{stage}: {searched.stop_reason}')
        skipped.extend(searched.skipped_urls)

    record('provider_mapping', providers.search)
    record('common_location', common)
    record('documentation', docs.search, extra=docs.documents, documentation_link=True)

    ordered = [(r, _mapping_for(r, providers.mappings)) for r in providers.search.candidates]
    ordered += [(r, None) for r in common.candidates]
    ordered += [(r, None) for r in docs.search.candidates]

    accepted, rejected = {}, []
    for retrieved, mapping in ordered:
        evaluation = evaluate_candidate(retrieved, target, budget, mapping=mapping, validation_limits=validation_limits,
                                        capture_limits=capture_limits, allow_loopback=allow_loopback)
        if evaluation.accepted:
            fingerprint = package_fingerprint(evaluation.package)
            first = accepted.get(fingerprint)
            if first is None:
                accepted[fingerprint] = evaluation
            else:  # the same contract found a second way: keep one, record where else it was seen
                also = MatchingEvidence('also_found', f'The same contract was also found at {retrieved.retrieval.final_url} '
                                        f'via {retrieved.candidate.discovery_method}.', retrieved.retrieval.final_url)
                merged = replace(first.package.candidate, evidence=(*first.package.candidate.evidence, also))
                accepted[fingerprint] = replace(first, candidate=merged,
                                                package=replace(first.package, candidate=merged))
            continue
        method = retrieved.candidate.discovery_method
        if _is_non_spec(method, evaluation):
            attempts.append(DiscoveryAttempt(retrieved.retrieval.requested_url, method, 'not_a_contract',
                                             f'{evaluation.rejection.code}: {evaluation.rejection.reason}'))
            counts['not_found'] += 1
            continue
        rejected.append(evaluation)

    packages = tuple(e.package for e in accepted.values())
    candidates = tuple(p.candidate for p in packages) + tuple(e.candidate for e in rejected)
    notes = [BOUNDED]
    if stops or skipped:
        notes.append('The search was cut short by its limits (' + ', '.join(stops or ['unvisited locations']) + '); '
                     f'{len(skipped)} location(s) were not visited, so other contracts may exist.')

    if len(packages) == 1:
        status, package = DiscoveryStatus.VALIDATED, packages[0]
        notes.extend(package.limitations)
        packages = ()
    elif len(packages) > 1:
        status, package = DiscoveryStatus.AMBIGUOUS, None
        notes.append(f'{len(packages)} distinct contracts fit the request; none was chosen. Supply more context '
                     '(an operation, API version or product) or select one explicitly.')
    elif rejected:
        status, package = DiscoveryStatus.REJECTED, None
        notes.append('Documents were found but none passed validation, matching and reference capture; '
                     'see each candidate\'s rejection reasons.')
    elif counts['inaccessible']:
        status, package = DiscoveryStatus.INACCESSIBLE, None
        notes.append('Some sources could not be reached, so a contract may exist behind them. ' + NOT_PROOF)
    else:
        status, package = DiscoveryStatus.NOT_FOUND, None
        notes.append(NOT_PROOF)
    return DiscoveryOutcome(status, candidates, tuple(attempts), tuple(dict.fromkeys(notes)), package, packages)


def select_candidate(outcome: DiscoveryOutcome, source_url: str) -> DiscoveryOutcome:
    """Turn an ambiguous outcome into a validated one by explicit choice; no network access.

    `source_url` is the chosen package's candidate URL, or a URL recorded as an alternative
    location of the same contract.
    """
    if outcome.status is not DiscoveryStatus.AMBIGUOUS:
        raise ValueError('Only an ambiguous outcome can be resolved by selection.')
    for package in outcome.packages:
        urls = {package.candidate.source_url, *(e.source_url for e in package.candidate.evidence
                                                  if e.criterion == 'also_found')}
        if source_url in urls:
            note = f'Selected explicitly from {len(outcome.packages)} alternatives: {package.candidate.source_url}.'
            return DiscoveryOutcome(DiscoveryStatus.VALIDATED, outcome.candidates, outcome.attempts,
                                    (*outcome.limitations, note), package)
    raise ValueError('The selected URL is not one of the alternatives.')
