"""The optional language-model fallback: choose which observed links to examine next when navigation found nothing.

Off unless the caller passes a suggester. Each documentation page already fetched is reduced to its title,
headings, links (each with an identifier) and configuration snippets; a model chooses identifiers; the chosen
links become LEADS for the ordinary navigation queue. This module fetches nothing: the queue fetches them under
the shared request, byte, time, depth and host limits, and any contract reached is judged by the same
validation, matching and reference capture as every other candidate. The model can steer the search, never
certify a contract or supply a URL. Every call is authorised against the spending limits first and recorded in
the cost ledger afterwards.
"""

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from radar.discovery.fetch import FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.llm_cost import BudgetRefused, CostLedger
from radar.discovery.llm_input import ReductionLimits, reduce_page
from radar.discovery.llm_suggestions import (
    LlmReply, Suggester, SuggesterError, build_prompt, parse_suggestions,
)
from radar.discovery.navigation import P_LLM, Lead
from radar.domain.discovery import DiscoveryRequest

STOP_CODES = frozenset({'unauthorized', 'payment_required', 'missing_api_key', 'invalid_api_key', 'rate_limited',
                        'insecure_base_url'})


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
    leads: tuple[Lead, ...]  # links to examine next, for the navigation queue to fetch
    calls: tuple[FallbackCall, ...]

    @property
    def usd(self):
        return sum(c.usd for c in self.calls)


def search_llm_fallback(
    request: DiscoveryRequest,
    pages: Sequence[FetchResult],
    suggester: Suggester,
    ledger: CostLedger,
    *,
    max_pages: int = 2,
    reduction_limits: ReductionLimits | None = None,
    response_cache: dict | None = None,
    depth_of: Callable[[str], int] = lambda url: 0,
    can_consult: Callable[[], bool] = lambda: True,
    eligible_link: Callable[[str, int], bool] | None = None,
) -> FallbackResult:
    """Consult the model about each page and return the links it chose, as leads. Nothing is fetched here.

    A page whose question was already answered in this run costs nothing (`response_cache`). A refused or
    failed call is recorded and, for credential, credit and rate-limit failures, ends the consultation.
    """
    target = normalize_target(request)
    response_cache = {} if response_cache is None else response_cache
    calls, leads = [], {}
    for page in pages[:max_pages]:
        if not can_consult():
            calls.append(FallbackCall(page.final_url, 'skipped_capacity',
                                      'Insufficient navigation capacity to pursue model suggestions; no call made.'))
            break
        reduced = reduce_page(page, reduction_limits)
        if eligible_link is not None:
            items = tuple(item for item in reduced.items
                          if not item.url or eligible_link(item.url, depth_of(page.final_url) + 1))
            reduced = replace(reduced, items=items, text='\n'.join(item.text for item in items))
        if not any(item.id for item in reduced.items):  # nothing the model could choose
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
            if eligible_link is not None and not eligible_link(url, depth_of(page.final_url) + 1):
                continue
            leads.setdefault(url, Lead(url, 'page', 'llm_suggestion', page.final_url, depth_of(page.final_url) + 1, P_LLM))

    return FallbackResult(tuple(leads.values()), tuple(calls))
