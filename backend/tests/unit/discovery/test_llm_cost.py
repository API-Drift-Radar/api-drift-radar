from datetime import datetime, timezone
import json

import pytest

from radar.discovery.llm_cost import BudgetRefused, CostLedger, LlmLimits, main, summarize
from radar.discovery.llm_suggestions import LlmUsage

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
USAGE = LlmUsage(input_tokens=1000, output_tokens=50, total_tokens=1050, cost_usd=0.0012, fee_usd=0.0001,
                 model='anthropic/claude-haiku-4-5', vendor='anthropic', response_id='resp_1')


def ledger(path=None, **limits):
    return CostLedger(path, LlmLimits(**limits), clock=lambda: NOW)


def test_a_priced_call_counts_provider_cost_plus_the_gateway_fee():
    book = ledger()
    book.authorize(2000)
    entry = book.record(USAGE, prompt_fingerprint='f' * 64, prompt_chars=2000)
    assert entry.outcome == 'ok' and entry.estimated is False and entry.total_usd == pytest.approx(0.0013)
    assert (entry.cost_usd, entry.fee_usd, entry.model, entry.vendor) == (0.0012, 0.0001, 'anthropic/claude-haiku-4-5', 'anthropic')
    assert entry.timestamp == '2026-10-09T12:00:00Z' and book.spent_usd() == pytest.approx(0.0013)


def test_an_unpriced_call_is_counted_at_a_pessimistic_estimate_never_as_free():
    book = ledger(fallback_input_usd_per_million=10, fallback_output_usd_per_million=40)
    entry = book.record(LlmUsage(input_tokens=1000, output_tokens=100), prompt_fingerprint='a', prompt_chars=3000)
    assert entry.estimated is True and entry.cost_usd is None
    assert entry.total_usd == pytest.approx((1000 * 10 + 100 * 40) / 1e6)
    no_tokens = book.record(LlmUsage(), prompt_fingerprint='b', prompt_chars=3000)  # nothing reported at all
    assert no_tokens.estimated and no_tokens.total_usd == pytest.approx((1000 * 10 + 400 * 40) / 1e6)


@pytest.mark.parametrize('cost', [None, -1.0, float('nan'), float('inf'), True, '0.5'])
def test_a_nonsensical_reported_cost_is_treated_as_unpriced(cost):
    entry = ledger().record(LlmUsage(input_tokens=10, output_tokens=10, cost_usd=cost), prompt_fingerprint='a', prompt_chars=30)
    assert entry.estimated is True and entry.total_usd > 0


def test_calls_per_run_are_limited():
    book = ledger(max_calls_per_run=2)
    book.authorize(100)
    book.authorize(100)
    with pytest.raises(BudgetRefused) as refused:
        book.authorize(100)
    assert refused.value.code == 'call_limit'


def test_an_oversized_prompt_is_refused_before_any_cost():
    with pytest.raises(BudgetRefused) as refused:
        ledger(max_input_chars=1000).authorize(1001)
    assert refused.value.code == 'prompt_too_large'


def test_the_total_cap_refuses_once_reached_and_when_one_more_call_could_cross_it():
    book = ledger(max_total_usd=0.01, fallback_input_usd_per_million=5, fallback_output_usd_per_million=25)
    # worst case for a 3000-char prompt: 1000 tokens * $5/M + 400 * $25/M = $0.015, over the whole cap
    with pytest.raises(BudgetRefused) as refused:
        book.authorize(3000)
    assert refused.value.code == 'budget_would_be_exceeded'
    book = ledger(max_total_usd=0.02)
    book.authorize(3000)
    book.record(LlmUsage(input_tokens=1, output_tokens=1, cost_usd=0.0195), prompt_fingerprint='a', prompt_chars=3000)
    with pytest.raises(BudgetRefused) as refused:
        book.authorize(30)
    assert refused.value.code == 'budget_would_be_exceeded'
    book.record(LlmUsage(input_tokens=1, output_tokens=1, cost_usd=0.01), prompt_fingerprint='b', prompt_chars=3)
    with pytest.raises(BudgetRefused) as refused:
        book.authorize(30)
    assert refused.value.code == 'budget_exhausted'


def test_every_refusal_is_recorded_without_spending():
    book = ledger(max_calls_per_run=1)
    book.authorize(100)
    with pytest.raises(BudgetRefused):
        book.authorize(100)
    refusals = [e for e in book.entries() if e.outcome == 'refused']
    assert len(refusals) == 1 and refusals[0].error == 'call_limit' and refusals[0].total_usd == 0


def test_the_budget_holds_across_runs_because_the_ledger_is_a_file(tmp_path):
    path = tmp_path / 'ledger' / 'llm_cost.jsonl'
    first = ledger(path, max_total_usd=0.02)
    first.authorize(100)
    first.record(LlmUsage(input_tokens=5, output_tokens=5, cost_usd=0.0099), prompt_fingerprint='a', prompt_chars=100)
    second = ledger(path, max_total_usd=0.02)  # a new process, a new object
    assert second.spent_usd() == pytest.approx(0.0099) and second.calls_this_run == 0
    with pytest.raises(BudgetRefused) as refused:
        second.authorize(100)
    assert refused.value.code == 'budget_would_be_exceeded'
    lines = path.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])['outcome'] == 'ok' and json.loads(lines[1])['outcome'] == 'refused'


def test_an_unreadable_ledger_fails_closed(tmp_path):
    path = tmp_path / 'llm_cost.jsonl'
    path.write_text('{"timestamp": "x", "purpose": "p", "outcome": "ok", "total_usd": 4.99}\nnot json\n')
    book = ledger(path)
    with pytest.raises(BudgetRefused) as refused:
        book.authorize(100)
    assert refused.value.code == 'ledger_unreadable'
    assert book.summary()['unreadable_lines'] == 1


def test_the_ledger_never_stores_prompts_pages_responses_or_credentials(tmp_path):
    path = tmp_path / 'llm_cost.jsonl'
    book = ledger(path)
    book.authorize(100)
    book.record(USAGE, prompt_fingerprint='f' * 64, prompt_chars=100)
    book.record_failure('http_error', prompt_fingerprint='e' * 64)
    book.record_cached(prompt_fingerprint='f' * 64)
    keys = {key for line in path.read_text().splitlines() for key in json.loads(line)}
    assert keys <= {'timestamp', 'purpose', 'outcome', 'prompt_fingerprint', 'model', 'vendor', 'response_id',
                    'input_tokens', 'output_tokens', 'total_tokens', 'cost_usd', 'fee_usd', 'total_usd', 'estimated', 'error'}


def test_failures_and_cache_hits_cost_nothing_but_are_visible():
    book = ledger()
    book.record_failure('timeout', prompt_fingerprint='a')
    book.record_cached(prompt_fingerprint='b')
    summary = book.summary()
    assert (summary['errors'], summary['cached'], summary['calls'], summary['total_usd']) == (1, 1, 0, 0)


def test_summary_totals_by_model_and_day():
    book = ledger(max_total_usd=1.0)
    book.record(USAGE, prompt_fingerprint='a', prompt_chars=100)
    book.record(LlmUsage(input_tokens=500, output_tokens=20, cost_usd=0.0005, model='openai/gpt-5.1'),
                prompt_fingerprint='b', prompt_chars=100)
    book.record(LlmUsage(input_tokens=10, output_tokens=10), prompt_fingerprint='c', prompt_chars=30)
    s = book.summary()
    assert s['calls'] == 3 and s['input_tokens'] == 1510 and s['estimated_calls'] == 1
    assert s['by_model']['anthropic/claude-haiku-4-5'] == {'calls': 1, 'input_tokens': 1000, 'output_tokens': 50, 'usd': 0.0013}
    assert set(s['by_model']) == {'anthropic/claude-haiku-4-5', 'openai/gpt-5.1', 'unknown'}
    assert s['by_day']['2026-10-09']['calls'] == 3 and s['budget_usd'] == 1.0
    assert s['remaining_usd'] == pytest.approx(1.0 - s['total_usd'], abs=1e-5)
    assert summarize([], 0)['total_usd'] == 0


def test_the_command_line_report(tmp_path, capsys):
    path = tmp_path / 'llm_cost.jsonl'
    assert main([str(path)]) == 0 and 'nothing has been spent' in capsys.readouterr().out
    book = ledger(path)
    book.authorize(100)
    book.record(USAGE, prompt_fingerprint='a', prompt_chars=100)
    assert main([str(path), '--budget', '5']) == 0
    out = capsys.readouterr().out
    assert 'Calls: 1 ok' in out and 'anthropic/claude-haiku-4-5' in out and 'remaining: $4.99' in out
    assert '2026-10-09' in out


@pytest.mark.parametrize('kwargs', [{'max_total_usd': -1}, {'max_total_usd': float('nan')}, {'max_calls_per_run': 0},
                                    {'max_input_chars': True}, {'max_output_tokens': 1.5},
                                    {'fallback_input_usd_per_million': -1}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        LlmLimits(**kwargs)
