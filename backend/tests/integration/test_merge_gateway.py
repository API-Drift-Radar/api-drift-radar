"""MergeSuggester against a local fake of Merge Gateway's documented Responses API, over real HTTP.

No real gateway, no real key and no money are involved: the key below is a made-up test value.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from radar.discovery.llm_suggestions import SuggesterError, SuggestionPrompt
from radar.discovery.merge_gateway import DEFAULT_BASE_URL, DEFAULT_MODEL, MergeSuggester

KEY = 'mg_TESTKEY_not_a_real_credential_0123456789'
PROMPT = SuggestionPrompt('Follow these rules.', 'Page: https://api.acme.com/docs\n<items>\nLINK x\n</items>',
                          'https://api.acme.com/docs', 400)

DOCUMENTED = {
    'id': 'resp_a1b2c3', 'created_at': '2026-10-09T12:00:00Z', 'model': 'anthropic/claude-haiku-4-5',
    'output': [{'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'text', 'text': '{"urls": ["https://api.acme.com/openapi.json"]}'}],
                'finish_reason': 'stop'}],
    'usage': {'input_tokens': 812, 'output_tokens': 41, 'total_tokens': 853, 'cost': 0.001017},
    'routing': {'cost_usd': 0.001017, 'merge_fee_usd': 0.0001},
    'vendor': 'anthropic', 'service_tier': 'standard'}


@contextmanager
def gateway(reply):
    """reply: dict (200 JSON) | (status, headers, body bytes) | callable(handler). Yields (base_url, requests)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            seen.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(self.rfile.read(length) or b'{}')})
            if callable(reply):
                return reply(self)
            status, headers, body = (200, {}, json.dumps(reply).encode()) if isinstance(reply, dict) else reply
            self.send_response(status)
            for name, value in {'Content-Type': 'application/json', **headers}.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads, server.block_on_close = True, False
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', seen
    finally:
        server.shutdown()
        server.server_close()


def client(base, **kwargs):
    return MergeSuggester(KEY, base_url=base, allow_insecure_http=True, **kwargs)


def test_documented_response_is_parsed_into_text_usage_and_both_costs():
    with gateway(DOCUMENTED) as (base, seen):
        reply = client(base).suggest(PROMPT)
    assert reply.text == '{"urls": ["https://api.acme.com/openapi.json"]}'
    usage = reply.usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (812, 41, 853)
    assert (usage.cost_usd, usage.fee_usd) == (0.001017, 0.0001)
    assert (usage.model, usage.vendor, usage.response_id) == ('anthropic/claude-haiku-4-5', 'anthropic', 'resp_a1b2c3')
    assert len(seen) == 1


def test_the_request_has_the_documented_shape_and_asks_not_to_store_the_page():
    with gateway(DOCUMENTED) as (base, seen):
        client(base, model='anthropic/claude-haiku-4-5').suggest(PROMPT)
    request = seen[0]
    assert request['path'] == '/v1/responses'
    assert request['headers']['Authorization'] == f'Bearer {KEY}' and request['headers']['Content-Type'] == 'application/json'
    assert request['body'] == {
        'model': 'anthropic/claude-haiku-4-5', 'instructions': 'Follow these rules.',
        'input': [{'type': 'message', 'role': 'user', 'content': PROMPT.input_text}],
        'max_tokens': 400, 'temperature': 0, 'stream': False, 'store': False, 'include_routing_metadata': True}
    assert KEY not in json.dumps(request['body'])


def test_an_unpriced_route_is_reported_as_unknown_not_free():
    reply = dict(DOCUMENTED, usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15, 'cost': None}, routing={})
    with gateway(reply) as (base, _):
        usage = client(base).suggest(PROMPT).usage
    assert usage.cost_usd is None and usage.fee_usd is None and usage.input_tokens == 10


def test_cost_falls_back_to_the_routing_figure_when_usage_has_none():
    reply = dict(DOCUMENTED, usage={'input_tokens': 1, 'output_tokens': 1}, routing={'cost_usd': 0.5, 'merge_fee_usd': 0})
    with gateway(reply) as (base, _):
        usage = client(base).suggest(PROMPT).usage
    assert (usage.cost_usd, usage.fee_usd) == (0.5, 0.0)


@pytest.mark.parametrize('usage', [{'input_tokens': -1, 'output_tokens': True, 'total_tokens': '5', 'cost': -3},
                                   {'input_tokens': 1.5, 'cost': float('inf')}, 'nonsense', None])
def test_nonsensical_usage_values_become_unknown(usage):
    with gateway(dict(DOCUMENTED, usage=usage, routing='x')) as (base, _):
        got = client(base).suggest(PROMPT).usage
    assert (got.input_tokens, got.output_tokens, got.total_tokens, got.cost_usd, got.fee_usd) == (None,) * 5


def test_text_is_taken_only_from_assistant_text_parts():
    reply = dict(DOCUMENTED, output=[
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': '{"urls": '},
                                                            {'type': 'tool_use', 'name': 'x', 'input': {}},
                                                            {'type': 'text', 'text': '[]}'}]},
        {'type': 'message', 'role': 'user', 'content': [{'type': 'text', 'text': 'ignored'}]},
        {'type': 'reasoning'}, 'junk', None])
    with gateway(reply) as (base, _):
        assert client(base).suggest(PROMPT).text == '{"urls": []}'


@pytest.mark.parametrize('output', [[], None, 'x', [{'type': 'message', 'role': 'assistant', 'content': []}]])
def test_an_empty_reply_still_returns_its_usage_so_the_cost_is_recorded(output):
    with gateway(dict(DOCUMENTED, output=output)) as (base, _):
        reply = client(base).suggest(PROMPT)
    assert reply.text == '' and reply.usage.cost_usd == 0.001017


@pytest.mark.parametrize('status,code', [(401, 'unauthorized'), (403, 'unauthorized'), (402, 'payment_required'),
                                         (429, 'rate_limited'), (400, 'bad_request'), (404, 'bad_request'),
                                         (422, 'bad_request'), (500, 'server_error'), (503, 'server_error'),
                                         (418, 'http_error')])
def test_http_errors_map_to_codes(status, code):
    with gateway((status, {}, b'{"error": {"message": "nope"}}')) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base).suggest(PROMPT)
    assert caught.value.code == code and 'nope' in caught.value.message and f'HTTP {status}' in caught.value.message


def test_rate_limit_reports_when_to_retry():
    with gateway((429, {'Retry-After': '30'}, b'{}')) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base).suggest(PROMPT)
    assert 'retry after 30' in caught.value.message


def test_a_server_that_echoes_the_key_cannot_leak_it_through_an_error():
    body = json.dumps({'error': {'message': f'bad credential {KEY} supplied'}}).encode()
    with gateway((401, {}, body)) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base).suggest(PROMPT)
    assert KEY not in str(caught.value) and KEY not in caught.value.message and '***' in caught.value.message
    assert KEY not in repr(client(base)) and '***' in repr(client(base))


def test_error_text_is_bounded_and_single_line():
    with gateway((500, {}, json.dumps({'error': {'message': 'x' * 5000 + '\nsecond line'}}).encode())) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base).suggest(PROMPT)
    assert len(caught.value.message) <= 300 and '\n' not in caught.value.message


def test_redirects_are_not_followed_so_the_key_is_never_forwarded():
    with gateway(DOCUMENTED) as (elsewhere, elsewhere_seen):
        with gateway((302, {'Location': f'{elsewhere}/responses'}, b'')) as (base, _):
            with pytest.raises(SuggesterError) as caught:
                client(base).suggest(PROMPT)
    assert caught.value.code == 'unexpected_redirect' and elsewhere_seen == []


@pytest.mark.parametrize('body', [b'not json', b'[1, 2]', b'', b'"text"'])
def test_malformed_replies_are_invalid_responses(body):
    with gateway((200, {}, body)) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base).suggest(PROMPT)
    assert caught.value.code == 'invalid_response'


def test_an_oversized_reply_is_refused():
    with gateway((200, {}, b'{"padding": "' + b'a' * 5000 + b'"}')) as (base, _):
        with pytest.raises(SuggesterError) as caught:
            client(base, max_response_bytes=1000).suggest(PROMPT)
    assert caught.value.code == 'response_too_large'


def test_a_slow_gateway_times_out():
    def slow(handler):
        time.sleep(1.5)

    with gateway(slow) as (base, _):
        started = time.monotonic()
        with pytest.raises(SuggesterError) as caught:
            client(base, timeout=(1.0, 0.3)).suggest(PROMPT)
    assert caught.value.code == 'timeout' and time.monotonic() - started < 1.4


def test_a_refused_connection_is_a_connection_error():
    with pytest.raises(SuggesterError) as caught:
        client('http://127.0.0.1:1/v1').suggest(PROMPT)
    assert caught.value.code == 'connection_error' and KEY not in str(caught.value)


# --- configuration ------------------------------------------------------------------

def test_the_key_comes_from_the_environment_and_a_missing_one_is_a_clear_error(monkeypatch):
    monkeypatch.delenv('MERGE_API_KEY', raising=False)
    with pytest.raises(SuggesterError) as caught:
        MergeSuggester.from_env()
    assert caught.value.code == 'missing_api_key' and 'MERGE_API_KEY' in caught.value.message
    monkeypatch.setenv('MERGE_API_KEY', KEY)
    monkeypatch.delenv('MERGE_MODEL', raising=False)
    suggester = MergeSuggester.from_env()
    assert suggester.model == DEFAULT_MODEL and suggester.base_url == DEFAULT_BASE_URL and KEY not in repr(suggester)
    monkeypatch.setenv('MERGE_MODEL', 'openai/gpt-5.1')
    assert MergeSuggester.from_env().model == 'openai/gpt-5.1'


@pytest.mark.parametrize('key', ['short', 'has space in it 12345678', 'new\nline_1234567890', 'ключ_unicode_12345678', 'x' * 600])
def test_keys_with_unexpected_characters_are_refused_before_any_header_is_built(key):
    with pytest.raises(SuggesterError) as caught:
        MergeSuggester(key)
    assert caught.value.code == 'invalid_api_key' and key not in str(caught.value)


def test_plain_http_is_refused_unless_explicitly_allowed_for_tests():
    with pytest.raises(SuggesterError) as caught:
        MergeSuggester(KEY, base_url='http://api-gateway.example/v1')
    assert caught.value.code == 'insecure_base_url'
    assert MergeSuggester(KEY, base_url='https://gateway.example/v1/').base_url == 'https://gateway.example/v1'


# --- the smoke command ------------------------------------------------------------------

def test_the_smoke_command_makes_one_recorded_call_and_reports_the_cost(tmp_path, capsys, monkeypatch):
    from radar.discovery.merge_gateway import main
    monkeypatch.setenv('MERGE_API_KEY', KEY)
    ledger = tmp_path / 'llm_cost.jsonl'
    with gateway(DOCUMENTED) as (base, seen):
        code = main(['--ledger', str(ledger), '--budget', '5', '--base-url', base, '--allow-insecure-http'])
    out = capsys.readouterr().out
    assert code == 0 and len(seen) == 1 and seen[0]['body']['max_tokens'] == 60
    assert 'OK' in out and '812 in / 41 out' in out and '$0.001117 counted against the $5.00 budget' in out
    assert KEY not in out and KEY not in ledger.read_text() and len(ledger.read_text().splitlines()) == 1


def test_the_smoke_command_fails_cleanly_without_a_key_or_with_a_bad_one(tmp_path, capsys, monkeypatch):
    from radar.discovery.merge_gateway import main
    monkeypatch.delenv('MERGE_API_KEY', raising=False)
    assert main(['--ledger', str(tmp_path / 'l.jsonl')]) == 1
    assert 'MERGE_API_KEY' in capsys.readouterr().out
    monkeypatch.setenv('MERGE_API_KEY', KEY)
    with gateway((401, {}, b'{"error": {"message": "invalid key"}}')) as (base, _):
        assert main(['--ledger', str(tmp_path / 'l.jsonl'), '--base-url', base, '--allow-insecure-http']) == 1
    out = capsys.readouterr().out
    assert 'FAILED: unauthorized' in out and KEY not in out
    assert json.loads((tmp_path / 'l.jsonl').read_text().splitlines()[0])['outcome'] == 'error'
