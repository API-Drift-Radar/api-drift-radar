"""The optional language-model fallback: suggest documentation links when deterministic discovery found nothing.

Off unless the caller passes a suggester. For each documentation page already fetched, the page is reduced to
the few spec-related items, a model is asked which of them is the specification, and only suggestions that
literally appear on the page are kept. The resulting URLs are fetched through the shared bounded fetcher and
judged by the same validation, matching and reference-capture rules as every other candidate: the model can
suggest a link, never certify a contract. Every call is authorised against the budget first and recorded in the
cost ledger afterwards.
"""

from dataclasses import dataclass
from typing import Sequence

from radar.discovery.candidates import CandidateSearchResult, fetch_candidates
from radar.discovery.fetch import FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget
from radar.discovery.llm_cost import BudgetRefused, CostLedger
from radar.discovery.llm_input import ReductionLimits, reduce_page
from radar.discovery.llm_suggestions import (
    LlmReply, Suggester, SuggesterError, build_prompt, parse_suggestions,
)
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, MatchingEvidence

STOP_CODES = frozenset({'unauthorized', 'payment_required', 'missing_api_key', 'invalid_api_key', 'rate_limited',
                        'insecure_base_url'})
LIMITATIONS = (
    'Suggested by a language model reading a documentation page; the link appears on that page.',
    'OpenAPI validity and relevance have not been assessed.',
)


@dataclass(frozen=True)
class FallbackCall:
    page_url: str
    outcome: str  # suggested, no_suggestions, unusable_reply, cached, no_relevant_items, refused, error
    detail: str | None = None
    urls: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class FallbackResult:
    search: CandidateSearchResult
    calls: tuple[FallbackCall, ...]

    @property
    def usd(self):
        return sum(c.usd for c in self.calls)


def search_llm_fallback(
    request: DiscoveryRequest,
    budget: DiscoveryBudget,
    pages: Sequence[FetchResult],
    suggester: Suggester,
    ledger: CostLedger,
    *,
    max_pages: int = 2,
    reduction_limits: ReductionLimits | None = None,
    allow_loopback: bool = False,
    cache: dict | None = None,
    response_cache: dict | None = None,
) -> FallbackResult:
    """Consult the model about each page, then fetch (not trust) what it suggests.

    A page whose question was already answered in this run costs nothing (`response_cache`). A refused or
    failed call is recorded and, for credential, credit and rate-limit failures, ends the consultation.
    """
    target = normalize_target(request)
    response_cache = {} if response_cache is None else response_cache
    calls, suggested = [], {}
    for page in pages[:max_pages]:
        reduced = reduce_page(page, reduction_limits)
        if not reduced.items:
            calls.append(FallbackCall(page.final_url, 'no_relevant_items',
                                      '; '.join(n.code for n in reduced.notes) or 'nothing on the page looked like a specification link'))
            continue
        prompt = build_prompt(reduced, target_host=target.hostname)
        reply: LlmReply | None = response_cache.get(prompt.fingerprint)
        usd = 0.0
        if reply is not None:
            ledger.record_cached(prompt_fingerprint=prompt.fingerprint)
            outcome_hint = 'cached'
        else:
            chars = len(prompt.instructions) + len(prompt.input_text)
            try:
                ledger.authorize(chars)
            except BudgetRefused as refused:
                calls.append(FallbackCall(page.final_url, 'refused', f'{refused.code}: {refused.message}'))
                break  # every further call would be refused for the same reason
            try:
                reply = suggester.suggest(prompt)
            except SuggesterError as error:
                ledger.record_failure(error.code, prompt_fingerprint=prompt.fingerprint)
                calls.append(FallbackCall(page.final_url, 'error', f'{error.code}: {error.message}'.strip(': ')))
                if error.code in STOP_CODES:
                    break
                continue
            entry = ledger.record(reply.usage, prompt_fingerprint=prompt.fingerprint, prompt_chars=chars)
            usd = entry.total_usd
            response_cache[prompt.fingerprint] = reply
            outcome_hint = None
        suggestions = parse_suggestions(reply.text, reduced)
        if suggestions.problem:
            outcome, detail = 'unusable_reply', suggestions.problem
        elif suggestions.urls:
            outcome, detail = 'suggested', f'{len(suggestions.urls)} URL(s) accepted, {len(suggestions.rejected)} rejected'
        else:
            outcome, detail = 'no_suggestions', f'{len(suggestions.rejected)} suggestion(s) rejected' if suggestions.rejected else None
        calls.append(FallbackCall(page.final_url, outcome_hint or outcome, detail, suggestions.urls, suggestions.rejected,
                                  usd, reply.usage.input_tokens, reply.usage.output_tokens))
        for url in suggestions.urls:
            suggested.setdefault(url, page.final_url)

    candidates = tuple(ContractCandidate(
        source_url=url, discovery_method='llm_suggestion', discovery_source=page_url,
        evidence=(MatchingEvidence('llm_suggestion',
                                   f'A language model reading {page_url} suggested this link; it appears on that page.',
                                   page_url),),
        limitations=LIMITATIONS) for url, page_url in suggested.items())
    search = fetch_candidates(target, candidates, budget, allow_loopback=allow_loopback, limitations=LIMITATIONS, cache=cache)
    return FallbackResult(search, tuple(calls))
