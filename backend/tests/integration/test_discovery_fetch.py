"""Local HTTP server proves real streaming, redirects, and failure behavior."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from radar.discovery.fetch import fetch_document
from radar.discovery.limits import DiscoveryBudget, FetchLimits


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/openapi.json')
                self.end_headers()
                return
            if self.path == '/missing':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            if self.path == '/truncated':
                self.send_header('Content-Length', '100')
            self.end_headers()
            if self.path == '/slow':
                time.sleep(0.2)
            if self.path == '/drip':
                for _ in range(30):
                    self.wfile.write(b'x')
                    self.wfile.flush()
                    time.sleep(0.02)
                return
            body = b'{"openapi":"3.0.3"}'
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope='module')
def server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join()


def test_print_real_results(server):
    cases = [('/openapi.json', {}, None), ('/redirect', {}, None), ('/missing', {}, 'http_error'),
             ('/truncated', {}, 'incomplete_response'), ('/openapi.json', {'max_document_bytes': 5}, 'document_size_limit'),
             ('/slow', {'read_timeout': 0.05}, 'timeout'), ('/drip', {'discovery_timeout': 0.12}, 'deadline_exceeded')]
    for path, limits, failure in cases:
        result = fetch_document(server + path, DiscoveryBudget(FetchLimits(**limits)), allow_loopback=True)
        actual = result.failure.code if result.failure else 'success'
        print(f'{path}: {actual}; HTTP={result.status}; bytes={len(result.content) if result.content is not None else None}; requests={len(result.attempts)}')
        assert (result.failure.code if result.failure else None) == failure
        if result.ok:
            assert result.content == b'{"openapi":"3.0.3"}'
    blocked = fetch_document(server, DiscoveryBudget())
    print(f'Default loopback policy: {blocked.failure.code}')
    assert blocked.failure.code == 'blocked_destination'
