"""Run discovery end to end and report one explicit outcome.

Strategies run in a fixed order under ONE shared budget and one fetch cache: provider
mappings, common locations, then official-documentation links. All of them run, so an
ambiguity in a later strategy is not hidden by an earlier hit. Every retrieved document is
judged on its own (validation, matching, reference capture); this module only combines the
verdicts. It never picks between valid contracts and never uses an LLM.
"""

from dataclasses import replace

from radar.discovery.candidates import search_common_locations, search_direct_url
from radar.discovery.documentation import search_documentation
from radar.discovery.evaluation import evaluate_candidate, package_fingerprint
from radar.discovery.fetch import FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.llm_cost import CostLedger
from radar.discovery.llm_fallback import search_llm_fallback
from radar.discovery.providers import search_provider_mappings
from radar.domain.discovery import (
    DiscoveryAttempt, DiscoveryOutcome, DiscoveryRequest, DiscoveryStatus, MatchingEvidence,
)


BUDGET_CODES = frozenset({'request_limit', 'deadline_exceeded', 'total_size_limit'})
# What a catch-all web server returns for a guessed path, or an API endpoint whose path merely ends in
# .json. For a guessed location or a spec-looking target this is a clean miss, not a rejected contract; for a location a provider or documentation page named
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
    return (candidate_method in {'common_location', 'direct_url'} and rejection is not None
            and rejection.stage == 'validation' and rejection.code in NON_SPEC_CODES)


def _llm_note(fallback, ledger, pages):
    if not pages:
        return 'The language-model fallback was enabled but no documentation page was available to read.'
    made = [c for c in fallback.calls if c.outcome not in ('cached', 'no_relevant_items', 'refused', 'error')]
    tokens_in = sum(c.input_tokens or 0 for c in made)
    tokens_out = sum(c.output_tokens or 0 for c in made)
    total = ledger.summary()
    refused = [c for c in fallback.calls if c.outcome == 'refused']
    text = (f'Language-model fallback: {len(made)} call(s), {tokens_in} input / {tokens_out} output tokens, '
            f'${fallback.usd:.6f} this run; ledger total ${total["total_usd"]:.6f}'
            f'{" of $" + format(total["budget_usd"], ".2f") if "budget_usd" in total else ""}. '
            'Suggested links were fetched and validated like any other candidate; the model certified nothing.')
    if refused:
        text += f' A call was refused by the budget rules ({refused[0].detail}).'
    return text


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
    llm_suggester=None,
    llm_ledger: CostLedger | None = None,
) -> DiscoveryOutcome:
    """Find, validate and match a published OpenAPI contract for `request`.

    Outcomes: validated (exactly one distinct complete contract fits), ambiguous (several do,
    all returned in `packages`; use `select_candidate`), rejected (documents were found but
    none passed), inaccessible (nothing passed and some source could not be reached) or
    not_found (nothing passed and every source answered with a clean miss). Raises
    DiscoveryInputError for a request that cannot be interpreted.

    `llm_suggester` (default None: off) enables the language-model fallback, used only when nothing valid was
    found otherwise; `llm_ledger` is the cost ledger enforcing its budget (an in-memory one with default
    limits if omitted). The model may suggest documentation links; every suggestion is fetched and judged
    like any other candidate.
    """
    target = normalize_target(request)
    budget = budget or DiscoveryBudget(limits)
    cache = {}
    options = dict(allow_loopback=allow_loopback, cache=cache)

    attempts, counts = [], {'not_found': 0, 'inaccessible': 0, 'budget': 0}
    stops, skipped = [], []
    llm_note = None
    accepted, rejected = {}, []

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

    def judge(retrieved, mapping):
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
            return
        method = retrieved.candidate.discovery_method
        if _is_non_spec(method, evaluation):
            attempts.append(DiscoveryAttempt(retrieved.retrieval.requested_url, method, 'not_a_contract',
                                             f'{evaluation.rejection.code}: {evaluation.rejection.reason}'))
            counts['not_found'] += 1
            return
        rejected.append(evaluation)

    # A URL that names a specification file is tried first. If it is a valid, matching contract it is the
    # answer: the user pointed at it, so other locations are not searched and cannot add false ambiguity.
    direct = search_direct_url(request, budget, **options)
    record('direct_url', direct)
    for retrieved in direct.candidates:
        judge(retrieved, None)
    requested_directly = bool(accepted)

    if not requested_directly:
        providers = search_provider_mappings(request, budget, registry_path=registry_path, **options)
        common = search_common_locations(request, budget, **options)
        docs = search_documentation(request, budget, documentation_urls=documentation_urls,
                                    limits=documentation_limits, **options)
        record('provider_mapping', providers.search)
        record('common_location', common)
        record('documentation', docs.search, extra=docs.documents, documentation_link=True)
        ordered = [(r, _mapping_for(r, providers.mappings)) for r in providers.search.candidates]
        ordered += [(r, None) for r in common.candidates]
        ordered += [(r, None) for r in docs.search.candidates]
        for retrieved, mapping in ordered:
            judge(retrieved, mapping)
        if not accepted and llm_suggester is not None:
            untrusted = {n.url for n in docs.notes if n.code == 'unconfigured_documentation_origin'}
            pages = [d for d in docs.documents if d.ok and d.final_url not in untrusted]
            ledger = llm_ledger or CostLedger()
            fallback = search_llm_fallback(request, budget, pages, llm_suggester, ledger,
                                           allow_loopback=allow_loopback, cache=cache)
            for call in fallback.calls:
                detail = call.detail + '; ' if call.detail else ''
                attempts.append(DiscoveryAttempt(call.page_url, 'llm_fallback', call.outcome,
                                                 f'{detail}${call.usd:.6f}'))
            record('llm_fallback', fallback.search)
            for retrieved in fallback.search.candidates:
                judge(retrieved, None)
            llm_note = _llm_note(fallback, ledger, pages)

    packages = tuple(e.package for e in accepted.values())
    candidates = tuple(p.candidate for p in packages) + tuple(e.candidate for e in rejected)
    notes = [BOUNDED]
    if llm_note:
        notes.append(llm_note)
    if requested_directly:
        notes.append('The contract was requested directly by its URL, so no other locations were searched.')
    oversized = [a.url for a in attempts if (a.reason or '').startswith('document_size_limit')]
    if oversized:
        notes.append(f'{len(oversized)} document(s) were refused for exceeding the per-document size limit '
                     f'(first: {oversized[0]}). Raise FetchLimits.max_document_bytes if a larger contract is expected.')
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
