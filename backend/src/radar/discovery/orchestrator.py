"""Run discovery end to end and report one explicit outcome.

Strategies run in a fixed order under ONE shared budget (requests, bytes, time, hosts) and one fetch cache:
an explicit specification URL, provider mappings, common locations, documentation pages, then bounded
navigation (typed links, an RFC 9727 catalogue, viewer configuration and, when nothing has been accepted yet,
the origin's own pages and framework probes), with an optional language model choosing among observed links before
framework probes or navigation exhaustion. All of them run, so an ambiguity in a later strategy is not hidden by an earlier hit. Every
retrieved document is judged on its own (validation, matching, reference capture); this module only combines
the verdicts. It never picks between valid contracts, and a model never decides acceptance.
"""

from dataclasses import replace

from radar.discovery.candidates import search_common_locations, search_direct_url
from radar.discovery.documentation import search_documentation
from radar.discovery.evaluation import evaluate_candidate, package_fingerprint
from radar.discovery.fetch import FetchResult
from radar.discovery.formats import recognize_artifact
from radar.discovery.input import normalize_target
from radar.discovery.limits import BUDGET_STOP_CODES, DiscoveryBudget, FetchLimits
from radar.discovery.llm_cost import CostLedger
from radar.discovery.llm_fallback import search_llm_fallback
from radar.discovery.llm_input import reduce_page
from radar.discovery.navigation import Navigator, NavigationLimits
from radar.discovery.providers import search_provider_mappings
from radar.domain.discovery import (
    ArtifactFinding, Coverage, DiscoveryAttempt, DiscoveryOutcome, DiscoveryRequest, DiscoveryStatus, LeadRecord,
    MatchingEvidence,
)


BUDGET_CODES = BUDGET_STOP_CODES
# What a catch-all web server returns for a guessed path, or an API endpoint whose path merely ends in .json.
# For a guessed location (a common location, a framework probe) or a spec-looking target this is a clean miss,
# not a rejected contract; for a location a provider, a link or a configuration named explicitly, the same
# response is a real rejection.
NON_SPEC_CODES = frozenset({'html_document', 'empty_document', 'not_json_or_yaml', 'not_an_object', 'not_openapi'})
GUESSED_METHODS = frozenset({'common_location', 'direct_url', 'framework_probe'})
BOUNDED = ('Discovery is bounded: it tried provider mappings, common specification locations, documentation '
           'pages and links, and configuration they point to, within fixed request, time, size and host limits.')
NOT_PROOF = 'Failing to find a contract is not proof that none is published.'
MAX_UNEXAMINED_SHOWN = 10


def _attempt(stage, fetch: FetchResult, ignorable_blocks=False):
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
    elif code == 'blocked_destination' and ignorable_blocks:
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
    return (candidate_method in GUESSED_METHODS and rejection is not None
            and rejection.stage == 'validation' and rejection.code in NON_SPEC_CODES)


def _llm_note(fallback, ledger, pages):
    if not pages:
        return 'The language-model fallback was enabled but no documentation page was available to read.'
    made = [c for c in fallback.calls if c.outcome not in ('cached', 'no_relevant_items', 'refused', 'error', 'skipped_capacity')]
    tokens_in = sum(c.input_tokens or 0 for c in made)
    tokens_out = sum(c.output_tokens or 0 for c in made)
    total = ledger.summary()
    refused = [c for c in fallback.calls if c.outcome == 'refused']
    text = (f'Language-model fallback: {len(made)} call(s), {tokens_in} input / {tokens_out} output tokens, '
            f'${fallback.usd:.6f} this run; ledger total ${total["total_usd"]:.6f}'
            f'{" of $" + format(total["budget_usd"], ".2f") if "budget_usd" in total else ""}. '
            'The model chose among links it was shown; they were fetched and judged like any other lead, and '
            'the model certified nothing.')
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
    navigation_limits: NavigationLimits | None = None,
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
    not_found (nothing passed and every source answered with a clean miss, or only documentation data was
    found). Optional details on the outcome: `artifacts` (unsupported descriptions, documentation-only data,
    authentication barriers, unsupported dynamic configuration), `trail` (each navigation step with its parent
    and mechanism) and `coverage` (which limits cut the search short). Raises DiscoveryInputError for a
    request that cannot be interpreted.

    `llm_suggester` (default None: off) enables the language-model fallback, used only when nothing valid was
    found otherwise; `llm_ledger` is the cost ledger enforcing its budget (an in-memory one with default
    limits if omitted). The model chooses among links it was shown; they are fetched and judged like any other
    lead. The request's method is only a matching hint: discovery issues GET requests for documentation and
    metadata and never performs the named operation.
    """
    target = normalize_target(request)
    budget = budget or DiscoveryBudget(limits)
    cache = {}
    options = dict(allow_loopback=allow_loopback, cache=cache)

    attempts, counts = [], {'not_found': 0, 'inaccessible': 0, 'budget': 0}
    stops, skipped = [], []
    llm_note = None
    accepted, rejected = {}, []
    artifacts, judged, trail = [], set(), []
    logged = {'nav': 0, 'artifacts': 0}

    def record(stage, searched, extra=(), ignorable_blocks=False):
        for fetch in extra:
            attempt, category = _attempt(f'{stage}_page', fetch)
            attempts.append(attempt)
            counts[category] = counts.get(category, 0) + 1
            barrier(fetch, stage)
        for fetch in searched.fetches:
            attempt, category = _attempt(stage, fetch, ignorable_blocks)
            attempts.append(attempt)
            counts[category] = counts.get(category, 0) + 1
            barrier(fetch, stage)
        if searched.stop_reason:
            stops.append(f'{stage}: {searched.stop_reason}')
        skipped.extend(searched.skipped_urls)

    def barrier(fetch, mechanism):
        if not fetch.ok and fetch.status in (401, 403):
            artifacts.append(ArtifactFinding(
                'authentication_required', 'authentication', fetch.requested_url,
                f'The source answered HTTP {fetch.status}; whether a contract exists behind it is unknown.',
                mechanism, None, fetch.status))

    def judge(retrieved, mapping):
        marker = (retrieved.retrieval.final_url, retrieved.candidate.discovery_method, retrieved.candidate.discovery_source)
        if marker in judged:  # the same document reached by the same route twice (for example via two strategies)
            return
        judged.add(marker)
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
        rejection = evaluation.rejection
        if rejection.stage == 'validation' and rejection.code in ('unsupported_version', 'unsupported_format'):
            info = recognize_artifact(retrieved.retrieval.content)
            artifacts.append(ArtifactFinding(
                'unsupported_description', info.kind if info else 'openapi_unsupported_version', retrieved.retrieval.final_url,
                rejection.reason, method, retrieved.candidate.discovery_source))
        rejected.append(evaluation)

    # A URL that names a specification file is tried first. If it is a valid, matching contract it is the
    # answer: the user pointed at it, so other locations are not searched and cannot add false ambiguity.
    direct = search_direct_url(request, budget, **options)
    record('direct_url', direct)
    for retrieved in direct.candidates:
        judge(retrieved, None)
    requested_directly = bool(accepted)
    navigator = None

    if not requested_directly:
        providers = search_provider_mappings(request, budget, registry_path=registry_path, **options)
        common = search_common_locations(request, budget, **options)
        docs = search_documentation(request, budget, documentation_urls=documentation_urls,
                                    limits=documentation_limits, **options)
        record('provider_mapping', providers.search)
        record('common_location', common)
        record('documentation', docs.search, extra=docs.documents, ignorable_blocks=True)
        ordered = [(r, _mapping_for(r, providers.mappings)) for r in providers.search.candidates]
        ordered += [(r, None) for r in common.candidates]
        ordered += [(r, None) for r in docs.search.candidates]
        for retrieved, mapping in ordered:
            judge(retrieved, mapping)

        # Navigation always follows explicit evidence (typed links, the catalogue, viewer configuration on pages
        # already fetched); the origin's own pages and framework-convention probes only when nothing is accepted.
        untrusted = {n.url for n in docs.notes if n.code == 'unconfigured_documentation_origin'}
        seed_pages = [d for d in docs.documents if d.ok and d.final_url not in untrusted]
        navigator = Navigator(request, budget, limits=navigation_limits, allow_loopback=allow_loopback, cache=cache,
                              explore=not accepted)

        def run_navigation(*, pause_for_model=False):
            navigator.run(pause_for_model=pause_for_model)
            result = navigator.result()
            for mechanism, fetch in result.fetch_log[logged['nav']:]:
                attempt, category = _attempt(f'navigation:{mechanism}', fetch, ignorable_blocks=True)
                attempts.append(attempt)
                counts[category] = counts.get(category, 0) + 1
            logged['nav'] = len(result.fetch_log)
            for retrieved in navigator.take_candidates():
                judge(retrieved, None)
            return result

        navigator.seed(seed_pages)
        navigation = run_navigation(pause_for_model=llm_suggester is not None and not accepted)

        if not accepted and llm_suggester is not None:
            ledger = llm_ledger or CostLedger()
            pages = sorted((p.fetch for p in navigation.pages),
                           key=lambda f: -sum(1 for i in reduce_page(f).items
                                              if i.id and i.url and navigator.actionable_model_link(
                                                  i.url, navigator.depth_of(f.final_url) + 1)))
            fallback = search_llm_fallback(request, pages, llm_suggester, ledger, depth_of=navigator.depth_of,
                                           can_consult=navigator.can_consult_model,
                                           eligible_link=navigator.actionable_model_link)
            for call in fallback.calls:
                detail = call.detail + '; ' if call.detail else ''
                attempts.append(DiscoveryAttempt(call.page_url, 'llm_fallback', call.outcome, f'{detail}${call.usd:.6f}'))
            navigator.add_leads(fallback.leads)
            navigation = run_navigation()
            llm_note = _llm_note(fallback, ledger, pages)
            if any(c.outcome == 'skipped_capacity' for c in fallback.calls):
                llm_note += ' Model consultation was skipped because navigation capacity was insufficient.'
        else:
            # A contract may have been accepted before a soft pause. Finish the
            # deterministic queue so explicit alternatives still get evaluated.
            navigation = run_navigation()

        artifacts.extend(navigation.artifacts)
        trail.extend(navigation.trail)
        nav_limits = list(navigation.limits_reached)
        unexamined = list(navigation.unexamined)
    else:
        nav_limits, unexamined = [], []

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
    reached = list(dict.fromkeys([s.split(': ', 1)[-1] for s in stops] + nav_limits))
    if stops or skipped or nav_limits or unexamined:
        notes.append('The search was cut short by its limits (' + ', '.join(reached or ['unvisited locations']) + '); '
                     f'{len(skipped) + len(unexamined)} location(s) were not visited, so other contracts may exist.')
    findings = tuple(dict.fromkeys(artifacts))
    documentation = {}
    for finding in findings:
        if finding.category == 'documentation_only':
            documentation.setdefault(finding.kind, []).append(finding.url)
        elif finding.category == 'authentication_required':
            notes.append(f'{finding.url} requires authentication (HTTP {finding.status}); what it serves is unknown.')
        elif finding.category == 'unsupported_dynamic_configuration':
            notes.append(f'A documentation viewer at {finding.url} could not be read statically ({finding.detail}).')
    for kind, urls in documentation.items():
        notes.append(f'Published documentation data ({kind}) was found ({len(urls)} file(s), first: {urls[0]}), but it is '
                     'not an OpenAPI contract and no supported contract was accepted from it.')

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

    fetched = sum(1 for r in trail if r.outcome in ('fetched', 'failed'))  # every lead actually looked at
    coverage = Coverage(
        complete=not (stops or skipped or nav_limits or unexamined), limits_reached=tuple(reached), leads_examined=fetched,
        leads_unexamined=len(unexamined) + len(skipped), unexamined=tuple(unexamined[:MAX_UNEXAMINED_SHOWN]),
        requests_used=budget.requests_used, requests_limit=budget.limits.max_requests,
        reserved_for_references=budget.limits.reserve, hosts_contacted=tuple(sorted(budget.hosts)))
    return DiscoveryOutcome(status, candidates, tuple(attempts), tuple(dict.fromkeys(notes)), package, packages,
                            findings, tuple(trail), coverage)


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
                                    (*outcome.limitations, note), package, (), outcome.artifacts, outcome.trail,
                                    outcome.coverage)
    raise ValueError('The selected URL is not one of the alternatives.')
