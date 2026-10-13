"""Print candidate search results from a controlled local provider."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from radar.discovery.candidates import search_common_locations
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.domain.discovery import DiscoveryRequest


def test_common_location_search(capsys):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != '/openapi.yaml':
                self.send_error(404)
                return
            content = b'openapi: 3.0.3\ninfo:\n  title: Controlled API\n  version: "1"\npaths: {}\n'
            self.send_response(200)
            self.send_header('Content-Type', 'application/yaml')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f'http://127.0.0.1:{server.server_port}/v1/customers'
    try:
        result = search_common_locations(DiscoveryRequest(target), DiscoveryBudget(), allow_loopback=True)
        with capsys.disabled():
            print(f'\nTarget: {target}')
            for fetch in result.fetches:
                print(f'{fetch.requested_url}: HTTP {fetch.status}; ' + ('candidate retrieved' if fetch.ok else fetch.failure.code))
            print(f'Retrieved candidates: {len(result.candidates)}')
            print('Contract validation: not performed')
        assert [f.status for f in result.fetches] == [404, 200, 404]
        assert len(result.candidates) == 1 and result.stop_reason is None
        assert result.candidates[0].retrieval.content.startswith(b'openapi:')
        limited = search_common_locations(DiscoveryRequest(target), DiscoveryBudget(FetchLimits(max_requests=1)), allow_loopback=True)
        assert limited.stop_reason == 'request_limit'
        assert len(limited.fetches) == 1 and len(limited.skipped_urls) == 2
        with capsys.disabled():
            print(f'One-request budget: {limited.stop_reason}; {len(limited.skipped_urls)} locations skipped')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
