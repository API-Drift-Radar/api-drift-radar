"""Deterministic transport tests; no live provider calls."""

import socket
import ssl
import time
from unittest.mock import patch

import pytest

from radar.discovery.fetch import _connection, fetch_document
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from urllib.parse import urlsplit


class Response:
    def __init__(self, body=b'{}', status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = self
        self.closed = False

    def connect(self):
        pass

    def settimeout(self, timeout):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass


@pytest.fixture
def transport():
    connections = []
    responses = []

    def factory(*args):
        connection = Connection(responses.pop(0))
        connections.append(connection)
        return connection

    with patch('radar.discovery.fetch._checked_address') as address, patch('radar.discovery.fetch._connection', side_effect=factory):
        yield responses, connections, address


def test_success_preserves_bytes_and_metadata(transport):
    responses, connections, _ = transport
    body = b'openapi: 3.0.3\n'
    responses.append(Response(body, headers={'Content-Type': 'application/yaml', 'Content-Length': str(len(body))}))
    budget = DiscoveryBudget()
    result = fetch_document('https://example.com/spec', budget)
    assert result.ok and result.content == body
    assert result.status == 200 and result.content_type == 'application/yaml'
    assert result.retrieved_at.utcoffset().total_seconds() == 0
    assert budget.requests_used == 1 and budget.bytes_used == len(body)
    assert connections[0].closed and connections[0].response.closed


def test_redirect_rechecks_destination_and_shares_budget(transport):
    responses, _, address = transport
    responses.extend([Response(status=302, headers={'Location': '/real'}), Response()])
    budget = DiscoveryBudget()
    result = fetch_document('https://example.com/spec', budget)
    assert result.ok and result.final_url == 'https://example.com/real'
    assert [a.status for a in result.attempts] == [302, 200]
    assert address.call_count == budget.requests_used == 2


@pytest.mark.parametrize('response,code', [
    (Response(status=404), 'http_error'),
    (Response(status=429, headers={'Retry-After': '60'}), 'http_error'),
    (Response(status=206), 'partial_response'),
    (Response(status=302), 'invalid_redirect'),
    (Response(status=302, headers={'Location': 'http://example.com/'}), 'blocked_redirect'),
    (Response(headers={'Content-Encoding': 'gzip'}), 'unsupported_encoding'),
    (Response(headers={'Content-Length': 'bad'}), 'invalid_response'),
    (Response(headers={'Content-Length': '2', 'Transfer-Encoding': 'chunked'}), 'invalid_response'),
    (Response(headers={'Transfer-Encoding': 'gzip'}), 'invalid_response'),
    (Response(b'123', headers={'Content-Length': '5'}), 'incomplete_response'),
])
def test_response_failures(transport, response, code):
    transport[0].append(response)
    result = fetch_document('https://example.com', DiscoveryBudget())
    assert result.failure.code == code and result.content is None
    if response.status == 429:
        assert result.retry_after == '60'


@pytest.mark.parametrize('headers', [{}, {'Content-Length': '6'}])
def test_document_size_limit(transport, headers):
    transport[0].append(Response(b'123456', headers=headers))
    result = fetch_document('https://example.com', DiscoveryBudget(FetchLimits(max_document_bytes=5)))
    assert result.failure.code == 'document_size_limit'


def test_shared_byte_budget(transport):
    transport[0].extend([Response(b'123'), Response(b'456')])
    budget = DiscoveryBudget(FetchLimits(max_total_bytes=5))
    assert fetch_document('https://example.com/a', budget).ok
    result = fetch_document('https://example.com/b', budget)
    assert result.failure.code == 'total_size_limit' and result.content is None


def test_shared_request_budget_and_no_retry(transport):
    transport[0].append(Response(status=503))
    budget = DiscoveryBudget(FetchLimits(max_requests=1))
    assert fetch_document('https://example.com', budget).failure.code == 'http_error'
    assert fetch_document('https://example.com', budget).failure.code == 'request_limit'
    assert budget.requests_used == 1


def test_redirect_limit(transport):
    transport[0].append(Response(status=302, headers={'Location': '/again'}))
    result = fetch_document('https://example.com', DiscoveryBudget(FetchLimits(max_redirects=0)))
    assert result.failure.code == 'redirect_limit'


def test_expired_deadline_never_connects(transport):
    budget = DiscoveryBudget()
    budget.deadline = time.monotonic() - 1
    assert fetch_document('https://example.com', budget).failure.code == 'deadline_exceeded'
    assert not transport[1]


@pytest.mark.parametrize('error,code', [(TimeoutError(), 'timeout'), (ssl.SSLError(), 'tls_error'), (OSError(), 'connection_error')])
def test_transport_errors(error, code):
    with patch('radar.discovery.fetch._checked_address', side_effect=error):
        assert fetch_document('https://example.com', DiscoveryBudget()).failure.code == code


def answers(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ips]


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '0.0.0.0', '::1'])
def test_nonpublic_destinations_blocked(ip):
    with patch('radar.discovery.fetch._resolve', return_value=answers(ip)):
        result = fetch_document('http://example.com', DiscoveryBudget())
    assert result.failure.code == 'blocked_destination'


def test_mixed_dns_answers_rejected():
    with patch('radar.discovery.fetch._resolve', return_value=answers('8.8.8.8', '127.0.0.1')):
        assert fetch_document('http://example.com', DiscoveryBudget()).failure.code == 'blocked_destination'


def test_loopback_exception_does_not_allow_private_network():
    with patch('radar.discovery.fetch._resolve', return_value=answers('10.0.0.1')):
        assert fetch_document('http://example.com', DiscoveryBudget(), allow_loopback=True).failure.code == 'blocked_destination'


def test_https_preserves_hostname_and_pins_socket():
    answer = answers('8.8.8.8')[0]
    connection = _connection(urlsplit('https://example.com/spec'), answer, 1)
    assert connection.host == 'example.com'
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    with patch('radar.discovery.fetch.socket.socket') as socket_factory:
        connection._create_connection(('example.com', 443), 1)
        socket_factory.return_value.connect.assert_called_once_with(answer[4])


def test_redirect_to_loopback_blocked(transport):
    transport[0].append(Response(status=302, headers={'Location': 'http://127.0.0.1/'}))
    # Use the real address policy on the second request.
    from radar.discovery.fetch import _FetchError
    transport[2].side_effect = [answers('8.8.8.8')[0], _FetchError('blocked_destination', 'Blocked')]
    assert fetch_document('http://example.com', DiscoveryBudget()).failure.code == 'blocked_destination'


@pytest.mark.parametrize('kwargs', [{'max_requests': 0}, {'max_redirects': -1}, {'read_timeout': float('nan')}, {'discovery_timeout': 0}, {'max_total_bytes': True}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        FetchLimits(**kwargs)
