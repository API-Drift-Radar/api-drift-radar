"""A `Suggester` backed by Merge Gateway's native Responses API (POST {base}/responses).

Built from the gateway's documented schema: the request is `input` (messages), `instructions`, `model`
("provider/model"), `max_tokens`; the reply text is in `output[].content[]` parts of type `text`; usage is
`usage.input_tokens/output_tokens/total_tokens/cost` (USD, null when the route is unpriced), and with
`include_routing_metadata` the gateway's own charge arrives separately as `routing.merge_fee_usd`.

The API key comes from the MERGE_API_KEY environment variable (or the constructor), is sent only in the
Authorization header, and never appears in a repr, an error message, a ledger line or a log.
"""

import json
import math
import os
import re

import requests

from radar.discovery.llm_suggestions import LlmReply, LlmUsage, SuggesterError, SuggestionPrompt

DEFAULT_BASE_URL = 'https://api-gateway.merge.dev/v1'
DEFAULT_MODEL = 'anthropic/claude-haiku-4-5'  # a small, inexpensive model; change with MERGE_MODEL
MAX_RESPONSE_BYTES = 1024 * 1024
_KEY = re.compile(r'[\x21-\x7e]{8,512}')


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _money(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def _text(value, limit=200):
    value = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(value))
    return value if len(value) <= limit else value[:limit - 1] + '…'


class MergeSuggester:
    def __init__(self, api_key=None, *, model=None, base_url=None, timeout=(5.0, 30.0), session=None,
                 allow_insecure_http=False, max_response_bytes=MAX_RESPONSE_BYTES):
        key = api_key if api_key is not None else os.environ.get('MERGE_API_KEY')
        if not key:
            raise SuggesterError('missing_api_key', 'Set the MERGE_API_KEY environment variable.')
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise SuggesterError('invalid_api_key', 'The API key has an unexpected format.')
        self._key = key
        self.model = model or os.environ.get('MERGE_MODEL') or DEFAULT_MODEL
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        if not self.base_url.startswith('https://') and not (allow_insecure_http and self.base_url.startswith('http://')):
            raise SuggesterError('insecure_base_url', 'The gateway URL must use HTTPS.')
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self._session = session or requests.Session()

    @classmethod
    def from_env(cls, **kwargs):
        return cls(**kwargs)

    def __repr__(self):
        return f'MergeSuggester(model={self.model!r}, base_url={self.base_url!r}, api_key=***)'

    def _clean(self, text):
        return _text(str(text).replace(self._key, '***'))

    def _error(self, code, message=''):
        return SuggesterError(code, self._clean(message))

    def request_body(self, prompt: SuggestionPrompt) -> dict:
        return {
            'model': self.model,
            'instructions': prompt.instructions,
            'input': [{'type': 'message', 'role': 'user', 'content': prompt.input_text}],
            'max_tokens': prompt.max_output_tokens,
            'temperature': 0,
            'stream': False,
            'store': False,  # do not keep the documentation page text on the gateway
            'include_routing_metadata': True,  # reports the gateway's own fee
        }

    def suggest(self, prompt: SuggestionPrompt) -> LlmReply:
        try:
            response = self._session.post(
                f'{self.base_url}/responses', data=json.dumps(self.request_body(prompt)).encode('utf-8'),
                headers={'Authorization': f'Bearer {self._key}', 'Content-Type': 'application/json',
                         'Accept': 'application/json', 'User-Agent': 'API-Drift-Radar/0.1'},
                timeout=self.timeout, allow_redirects=False, stream=True)
        except requests.exceptions.Timeout:
            raise self._error('timeout') from None
        except requests.exceptions.SSLError:
            raise self._error('tls_error') from None
        except requests.exceptions.RequestException as error:
            raise self._error('connection_error', type(error).__name__) from None
        try:
            body = self._read(response)
        finally:
            response.close()
        if 300 <= response.status_code < 400:
            raise self._error('unexpected_redirect', f'HTTP {response.status_code}')
        if response.status_code != 200:
            raise self._http_error(response, body)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise self._error('invalid_response', 'The reply was not JSON.') from None
        return self._reply(data)

    def _read(self, response):
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise self._error('response_too_large')
                chunks.append(chunk)
        except requests.exceptions.Timeout:
            raise self._error('timeout') from None
        except requests.exceptions.RequestException:
            raise self._error('connection_error', 'The reply was cut off.') from None
        return b''.join(chunks)

    def _http_error(self, response, body):
        status = response.status_code
        detail = ''
        try:
            data = json.loads(body)
            error = data.get('error', data) if isinstance(data, dict) else {}
            detail = error.get('message') if isinstance(error, dict) else error
        except (ValueError, AttributeError, RecursionError):
            pass
        code = {401: 'unauthorized', 403: 'unauthorized', 402: 'payment_required', 429: 'rate_limited',
                400: 'bad_request', 404: 'bad_request', 422: 'bad_request'}.get(status, 'server_error' if status >= 500 else 'http_error')
        message = f'HTTP {status}' + (f': {detail}' if isinstance(detail, str) and detail else '')
        if status == 429 and response.headers.get('Retry-After'):
            message += f' (retry after {_text(response.headers["Retry-After"], 20)})'
        return self._error(code, message)

    def _reply(self, data) -> LlmReply:
        if not isinstance(data, dict):
            raise self._error('invalid_response', 'The reply was not an object.')
        texts = []
        for item in data.get('output') if isinstance(data.get('output'), list) else []:
            if isinstance(item, dict) and item.get('type') == 'message' and item.get('role', 'assistant') == 'assistant':
                content = item.get('content')
                if isinstance(content, str):
                    texts.append(content)
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                        texts.append(part['text'])
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        routing = data.get('routing') if isinstance(data.get('routing'), dict) else {}
        cost = _money(usage.get('cost'))
        if cost is None:
            cost = _money(routing.get('cost_usd'))
        return LlmReply(''.join(texts), LlmUsage(
            _count(usage.get('input_tokens')), _count(usage.get('output_tokens')), _count(usage.get('total_tokens')),
            cost, _money(routing.get('merge_fee_usd')),
            data.get('model') if isinstance(data.get('model'), str) else None,
            data.get('vendor') if isinstance(data.get('vendor'), str) else None,
            data.get('id') if isinstance(data.get('id'), str) else None))


def main(argv=None) -> int:
    """One tiny, budget-checked call to confirm the key, model and cost tracking work.

        MERGE_API_KEY=... python -m radar.discovery.merge_gateway

    Costs a small fraction of a cent. The call is recorded in the cost ledger like any other.
    """
    import argparse
    from pathlib import Path
    from radar.discovery.llm_cost import BudgetRefused, CostLedger, LlmLimits

    parser = argparse.ArgumentParser(prog='python -m radar.discovery.merge_gateway', description=main.__doc__.splitlines()[0])
    parser.add_argument('--ledger', default='.radar/llm_cost.jsonl', help='cost ledger file (default: %(default)s)')
    parser.add_argument('--budget', type=float, default=5.0, help='total USD budget for the ledger (default: %(default)s)')
    parser.add_argument('--model', default=None, help='provider/model (default: MERGE_MODEL or %s)' % DEFAULT_MODEL)
    parser.add_argument('--base-url', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--allow-insecure-http', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        suggester = MergeSuggester(model=args.model, base_url=args.base_url, allow_insecure_http=args.allow_insecure_http)
        ledger = CostLedger(Path(args.ledger), LlmLimits(max_total_usd=args.budget, max_output_tokens=60))
        prompt = SuggestionPrompt('Reply with exactly this JSON and nothing else: {"urls": []}',
                                  'Page: https://example.test/docs\n<items>\nnothing\n</items>',
                                  'https://example.test/docs', 60)
        chars = len(prompt.instructions) + len(prompt.input_text)
        ledger.authorize(chars)
        try:
            reply = suggester.suggest(prompt)
        except SuggesterError as error:
            ledger.record_failure(error.code, prompt_fingerprint=prompt.fingerprint)
            raise
        entry = ledger.record(reply.usage, prompt_fingerprint=prompt.fingerprint, prompt_chars=chars)
    except (SuggesterError, BudgetRefused) as error:
        print(f'FAILED: {error}')
        return 1
    print(f'OK  model={entry.model}  vendor={entry.vendor}')
    print(f'    reply: {reply.text.strip()[:80]!r}')
    print(f'    tokens: {entry.input_tokens} in / {entry.output_tokens} out')
    print(f'    cost: ${entry.cost_usd if entry.cost_usd is not None else "unpriced (estimated)"}'
          f' + gateway fee ${entry.fee_usd if entry.fee_usd is not None else "n/a"}'
          f'  = ${entry.total_usd:.6f} counted against the ${args.budget:.2f} budget')
    print(f'    ledger: {args.ledger}   (report: python -m radar.discovery.llm_cost {args.ledger} --budget {args.budget:g})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
