"""Spending limits and a persistent per-call cost ledger for the language-model fallback.

Every consultation of a model is recorded as one JSON line: model, vendor, tokens, the provider's cost and
the gateway's fee, and the outcome. The ledger is the budget's memory, so the cap holds across separate runs,
not just within one. Authorisation fails closed: an unreadable ledger, an oversized prompt, a call limit or a
cap that this call could exceed all refuse the call before any money is spent.

The ledger never stores prompts, page text, responses or credentials: only a prompt fingerprint.

    python -m radar.discovery.llm_cost .radar/llm_cost.jsonl --budget 5
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading

from radar.discovery.llm_suggestions import LlmUsage

OK, ERROR, CACHED, REFUSED = 'ok', 'error', 'cached', 'refused'
CHARS_PER_TOKEN = 3  # deliberately pessimistic for English and URLs


@dataclass(frozen=True)
class LlmLimits:
    """Hard caps. Defaults suit a total discovery budget of about $5."""

    max_total_usd: float = 5.0  # cumulative, across runs, as recorded in the ledger
    max_calls_per_run: int = 2
    max_input_chars: int = 16_000  # instructions plus page text, roughly 5,000 tokens
    max_output_tokens: int = 400
    # Used only to ESTIMATE a call that comes back unpriced, and to size the worst case before a call.
    fallback_input_usd_per_million: float = 5.0
    fallback_output_usd_per_million: float = 25.0

    def __post_init__(self):
        for name in ('max_total_usd', 'fallback_input_usd_per_million', 'fallback_output_usd_per_million'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid {name}.')
        for name in ('max_calls_per_run', 'max_input_chars', 'max_output_tokens'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'Invalid {name}.')


class BudgetRefused(Exception):
    """The call was not made. `code` says which rule refused it."""

    def __init__(self, code, message=''):
        self.code, self.message = code, message
        super().__init__(f'{code}: {message}' if message else code)


@dataclass(frozen=True)
class LedgerEntry:
    timestamp: str
    purpose: str
    outcome: str  # ok, error, cached, refused
    prompt_fingerprint: str | None = None
    model: str | None = None
    vendor: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None  # as reported by the provider
    fee_usd: float | None = None  # as reported by the gateway
    total_usd: float = 0.0  # what counts against the budget: cost + fee, or an estimate if unpriced
    estimated: bool = False
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, ensure_ascii=True)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        and value >= 0 else None


class CostLedger:
    """Authorises calls against the limits and records what each one cost. Thread-safe within a process;
    use one process at a time per ledger file."""

    def __init__(self, path=None, limits: LlmLimits | None = None, clock=None):
        self.path = Path(path) if path is not None else None
        self.limits = limits or LlmLimits()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._memory: list[LedgerEntry] = []
        self.calls_this_run = 0

    # -- reading ---------------------------------------------------------------------------------
    def _read(self):
        """(entries, unreadable_line_count) from the file, or from memory when there is no file."""
        if self.path is None:
            return list(self._memory), 0
        entries, bad = [], 0
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(LedgerEntry(**json.loads(line)))
                except (ValueError, TypeError):
                    bad += 1
        return entries, bad

    def entries(self) -> list[LedgerEntry]:
        return self._read()[0]

    def spent_usd(self) -> float:
        return sum(e.total_usd for e in self._read()[0])

    # -- deciding --------------------------------------------------------------------------------
    def worst_case_usd(self, prompt_chars: int) -> float:
        limits = self.limits
        tokens_in = math.ceil(prompt_chars / CHARS_PER_TOKEN)
        return (tokens_in * limits.fallback_input_usd_per_million
                + limits.max_output_tokens * limits.fallback_output_usd_per_million) / 1_000_000

    def authorize(self, prompt_chars: int):
        """Raise BudgetRefused (and log it) unless one more call is allowed. Call before every request."""
        with self._lock:
            limits = self.limits
            entries, unreadable = self._read()
            spent = sum(e.total_usd for e in entries)
            worst = self.worst_case_usd(prompt_chars)
            if unreadable:
                code, message = 'ledger_unreadable', f'{unreadable} ledger line(s) could not be read, so spend is unknown.'
            elif prompt_chars > limits.max_input_chars:
                code, message = 'prompt_too_large', f'{prompt_chars} characters exceed the {limits.max_input_chars} limit.'
            elif self.calls_this_run >= limits.max_calls_per_run:
                code, message = 'call_limit', f'{limits.max_calls_per_run} call(s) per run already made.'
            elif spent >= limits.max_total_usd:
                code, message = 'budget_exhausted', f'${spent:.4f} spent of ${limits.max_total_usd:.2f}.'
            elif spent + worst > limits.max_total_usd:
                code, message = 'budget_would_be_exceeded', (
                    f'${spent:.4f} spent; this call could cost up to ${worst:.4f} of the ${limits.max_total_usd:.2f} cap.')
            else:
                self.calls_this_run += 1
                return
            self._append(LedgerEntry(self._now(), 'discovery_fallback', REFUSED, error=code))
        raise BudgetRefused(code, message)

    # -- recording -------------------------------------------------------------------------------
    def _now(self):
        return self._clock().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    def _append(self, entry: LedgerEntry):
        self._memory.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(entry.to_json() + '\n')
                handle.flush()
        return entry

    def record(self, usage: LlmUsage, *, prompt_fingerprint: str, prompt_chars: int,
               purpose: str = 'discovery_fallback') -> LedgerEntry:
        """Record a completed call. A call the provider did not price is counted at the pessimistic estimate."""
        cost, fee = _number(usage.cost_usd), _number(usage.fee_usd)
        estimated = cost is None
        if estimated:
            tokens_in = usage.input_tokens if usage.input_tokens is not None else math.ceil(prompt_chars / CHARS_PER_TOKEN)
            tokens_out = usage.output_tokens if usage.output_tokens is not None else self.limits.max_output_tokens
            cost = (tokens_in * self.limits.fallback_input_usd_per_million
                    + tokens_out * self.limits.fallback_output_usd_per_million) / 1_000_000
        total = cost + (fee or 0.0)
        with self._lock:
            return self._append(LedgerEntry(
                self._now(), purpose, OK, prompt_fingerprint, usage.model, usage.vendor, usage.response_id,
                usage.input_tokens, usage.output_tokens, usage.total_tokens, _number(usage.cost_usd), fee,
                round(total, 8), estimated))

    def record_failure(self, code: str, *, prompt_fingerprint: str, purpose: str = 'discovery_fallback'):
        """A call that failed. Its cost is unknown to us; the provider normally does not bill failed requests."""
        with self._lock:
            return self._append(LedgerEntry(self._now(), purpose, ERROR, prompt_fingerprint, error=str(code)[:80]))

    def record_cached(self, *, prompt_fingerprint: str, purpose: str = 'discovery_fallback'):
        with self._lock:
            return self._append(LedgerEntry(self._now(), purpose, CACHED, prompt_fingerprint))

    # -- monitoring ------------------------------------------------------------------------------
    def summary(self) -> dict:
        return summarize(*self._read(), budget_usd=self.limits.max_total_usd)


def summarize(entries, unreadable=0, *, budget_usd=None) -> dict:
    by_model = defaultdict(lambda: {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'usd': 0.0})
    by_day = defaultdict(lambda: {'calls': 0, 'usd': 0.0})
    totals = {'calls': 0, 'errors': 0, 'cached': 0, 'refused': 0, 'input_tokens': 0, 'output_tokens': 0,
              'cost_usd': 0.0, 'fee_usd': 0.0, 'total_usd': 0.0, 'estimated_calls': 0}
    for e in entries:
        key = {OK: 'calls', ERROR: 'errors', CACHED: 'cached', REFUSED: 'refused'}.get(e.outcome)
        if key:
            totals[key] += 1
        if e.outcome == OK:
            model = by_model[e.model or 'unknown']
            day = by_day[e.timestamp[:10]]
            for bucket in (model, day):
                bucket['calls'] += 1
                bucket['usd'] += e.total_usd
            model['input_tokens'] += e.input_tokens or 0
            model['output_tokens'] += e.output_tokens or 0
            totals['input_tokens'] += e.input_tokens or 0
            totals['output_tokens'] += e.output_tokens or 0
            totals['cost_usd'] += e.cost_usd or 0.0
            totals['fee_usd'] += e.fee_usd or 0.0
            totals['estimated_calls'] += 1 if e.estimated else 0
        totals['total_usd'] += e.total_usd
    result = {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in totals.items()},
              'unreadable_lines': unreadable,
              'by_model': {k: {**v, 'usd': round(v['usd'], 6)} for k, v in sorted(by_model.items())},
              'by_day': {k: {**v, 'usd': round(v['usd'], 6)} for k, v in sorted(by_day.items())}}
    if budget_usd is not None:
        result['budget_usd'] = budget_usd
        result['remaining_usd'] = round(max(0.0, budget_usd - totals['total_usd']), 6)
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m radar.discovery.llm_cost',
                                     description='Show what the language-model fallback has cost.')
    parser.add_argument('ledger', help='path to the JSON-lines ledger')
    parser.add_argument('--budget', type=float, default=None, help='budget in USD, to show what remains')
    args = parser.parse_args(argv)
    path = Path(args.ledger)
    if not path.exists():
        print(f'No ledger at {path}: nothing has been spent.')
        return 0
    ledger = CostLedger(path, LlmLimits(max_total_usd=args.budget if args.budget is not None else 5.0))
    s = ledger.summary()
    print(f'Calls: {s["calls"]} ok, {s["errors"]} failed, {s["cached"]} cached, {s["refused"]} refused')
    print(f'Tokens: {s["input_tokens"]:,} in, {s["output_tokens"]:,} out')
    print(f'Spend: ${s["total_usd"]:.6f}  (provider ${s["cost_usd"]:.6f} + gateway fee ${s["fee_usd"]:.6f}; '
          f'{s["estimated_calls"]} call(s) estimated because unpriced)')
    if args.budget is not None:
        print(f'Budget: ${args.budget:.2f}  remaining: ${s["remaining_usd"]:.6f}')
    for model, row in s['by_model'].items():
        print(f'  {model}: {row["calls"]} call(s), {row["input_tokens"]:,} in / {row["output_tokens"]:,} out, ${row["usd"]:.6f}')
    for day, row in s['by_day'].items():
        print(f'  {day}: {row["calls"]} call(s), ${row["usd"]:.6f}')
    if s['unreadable_lines']:
        print(f'WARNING: {s["unreadable_lines"]} unreadable line(s); spend may be understated and calls are refused.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
