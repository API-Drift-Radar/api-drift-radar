from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from radar.discovery.documentation import search_documentation
from radar.discovery.limits import DiscoveryBudget
from radar.domain.discovery import DiscoveryRequest


def test_print_documentation_results():
    requested = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested.append(self.path)
            if self.path == '/docs':
                body = (b'<a href="/contracts/payments.yaml">Download OpenAPI</a>'
                        b'<a href="/contracts/payments.yaml">OpenAPI again</a>'
                        b'<a href="/other-docs">Other docs</a>'
                        b'<script>SwaggerUIBundle({"url":"/contracts/analytics.json"});</script>'
                        b'<script>SwaggerUIBundle({url: runtimeValue});</script>')
                media = 'text/html; charset=utf-8'
            elif self.path == '/documentation':
                self.send_error(404)
                return
            else:
                body = b'{"openapi":"3.0.3"}'
                media = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = search_documentation(DiscoveryRequest(f'http://127.0.0.1:{server.server_port}/v1/customers'),
                                       DiscoveryBudget(), allow_loopback=True)
        print('\nDocumentation search:')
        for doc in result.documents:
            print(f'{doc.final_url}: HTTP {doc.status}')
        for candidate in result.search.candidates:
            print(f'{candidate.candidate.discovery_method}: {candidate.candidate.source_url} — unvalidated candidate')
        print('Notes:', ', '.join(n.code for n in result.notes))
        print('Requests:', len(requested), '; recursive documentation crawl: none')
        print('Contract validation: not performed')
        assert len(result.search.candidates) == 2
        assert requested == ['/docs', '/documentation', '/contracts/payments.yaml', '/contracts/analytics.json']
        assert any(n.code == 'unsupported_configuration' for n in result.notes)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
