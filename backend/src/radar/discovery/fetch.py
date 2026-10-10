"""Bounded HTTP GET retrieval. This module does not discover or validate specs.

Uses direct connections pinned to a checked DNS answer. Environment proxies,
authentication, cookies, decompression, and automatic retries are not used.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.limits import BudgetExceeded, DiscoveryBudget
from radar.domain.discovery import DiscoveryRequest


@dataclass(frozen=True)
class FetchAttempt:
    url: str
    status: int | None


@dataclass(frozen=True)
class FetchFailure:
    code: str
    reason: str


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int | None
    content_type: str | None
    content: bytes | None
    retrieved_at: datetime | None
    attempts: tuple[FetchAttempt, ...]
    failure: FetchFailure | None = None
    retry_after: str | None = None

    @property
    def ok(self):
        return self.failure is None


class _FetchError(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(reason)


def _resolve(host, port, timeout):
    """Bound caller wait for platform DNS, which has no portable cancellation."""
    results = queue.Queue(maxsize=1)

    def resolve():
        try:
            results.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            results.put(error)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        result = results.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("DNS resolution timed out.") from None
    if isinstance(result, OSError):
        raise result
    return result


def _checked_address(host, port, timeout, allow_loopback):
    answers = _resolve(host, port, timeout)
    if not answers:
        raise _FetchError("connection_error", "DNS returned no addresses.")
    for _, _, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        effective = getattr(ip, "ipv4_mapped", None) or ip
        if not effective.is_global and not (allow_loopback and effective.is_loopback):
            raise _FetchError("blocked_destination", "Destination resolves to a non-public address.")
    # No second hostname resolution is performed when connecting.
    return answers[0]


def _connection(parsed, answer, timeout):
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)

    def connect_checked_address(address, timeout, source_address=None):
        family, socktype, protocol, _, sockaddr = answer
        sock = socket.socket(family, socktype, protocol)
        try:
            started = time.monotonic()
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Connection deadline exceeded.")
            sock.settimeout(remaining)  # TLS handshake shares the connection budget.
            return sock
        except BaseException:
            sock.close()
            raise

    # HTTPSConnection still uses the original hostname for SNI and certificate
    # validation. Only its TCP connection factory is replaced.
    connection._create_connection = connect_checked_address
    return connection


def fetch_document(url: str, budget: DiscoveryBudget, *, allow_loopback=False) -> FetchResult:
    """Fetch using a shared run budget; loopback is opt-in for controlled tests.

    Failures contain no partial document. Byte usage includes partial reads from
    failed captures. Redirects each consume a request and repeat destination checks.
    """
    current = url
    attempts = []
    status = content_type = retry_after = None

    def failed(code, reason):
        return FetchResult(url, current, status, content_type, None, None,
                           tuple(attempts), FetchFailure(code, reason), retry_after)

    try:
        for redirect_count in range(budget.limits.max_redirects + 1):
            status = content_type = retry_after = None
            current = normalize_target(DiscoveryRequest(current)).normalized_url
            parsed = urlsplit(current)
            budget.claim_request()
            attempts.append(FetchAttempt(current, None))
            answer = _checked_address(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                      min(budget.limits.connect_timeout, budget.remaining()), allow_loopback)
            connection = _connection(parsed, answer, min(budget.limits.connect_timeout, budget.remaining()))
            response = timer = None
            try:
                connection.connect()
                sock = connection.sock
                sock.settimeout(min(budget.limits.read_timeout, budget.remaining()))

                def abort_at_deadline():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

                timer = threading.Timer(budget.remaining(), abort_at_deadline)
                timer.daemon = True
                timer.start()
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("GET", path, headers={
                    "Accept": "application/json, application/yaml, text/yaml, text/html, */*",
                    "Accept-Encoding": "identity",
                    "User-Agent": "API-Drift-Radar/0.1",
                })
                response = connection.getresponse()
                budget.remaining()
                status = response.status
                content_type = response.getheader("Content-Type")
                retry_after = response.getheader("Retry-After")
                attempts[-1] = FetchAttempt(current, status)
                if status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise _FetchError("invalid_redirect", "Redirect has no Location header.")
                    if redirect_count == budget.limits.max_redirects:
                        raise _FetchError("redirect_limit", "Redirect limit reached.")
                    destination = urljoin(current, location)
                    if parsed.scheme == "https" and urlsplit(destination).scheme.lower() == "http":
                        raise _FetchError("blocked_redirect", "HTTPS to HTTP redirects are not allowed.")
                    current = destination
                    continue
                if not 200 <= status < 300:
                    raise _FetchError("http_error", f"HTTP status {status}.")
                if status == 206:
                    raise _FetchError("partial_response", "Partial documents are not accepted.")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise _FetchError("unsupported_encoding", "Server ignored identity encoding; compressed documents are not supported.")
                length = response.getheader("Content-Length")
                expected = None
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise _FetchError("invalid_response", "Invalid Content-Length header.")
                    expected = int(length)
                    if response.getheader("Transfer-Encoding"):
                        raise _FetchError("invalid_response", "Conflicting response framing headers.")
                    if expected > budget.limits.max_document_bytes:
                        raise _FetchError("document_size_limit", "Document exceeds size limit.")
                    if expected > budget.limits.max_total_bytes - budget.bytes_used:
                        raise _FetchError("total_size_limit", "Document exceeds remaining byte budget.")
                encoding = response.getheader("Transfer-Encoding")
                if encoding is not None and encoding.lower() != "chunked":
                    raise _FetchError("invalid_response", "Unsupported transfer encoding.")
                chunks = []
                size = 0
                while True:
                    budget.remaining()
                    # One sentinel byte detects overflow for unknown-length bodies.
                    count = min(65536, budget.limits.max_document_bytes - size + 1,
                                budget.limits.max_total_bytes - budget.bytes_used + 1)
                    chunk = response.read1(count)
                    budget.record_bytes(len(chunk))
                    budget.remaining()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > budget.limits.max_document_bytes:
                        raise _FetchError("document_size_limit", "Document exceeds size limit.")
                    chunks.append(chunk)
                if expected is not None and size != expected:
                    raise _FetchError("incomplete_response", "Response ended before Content-Length bytes arrived.")
                return FetchResult(url, current, status, content_type, b"".join(chunks),
                                   datetime.now(timezone.utc), tuple(attempts))
            finally:
                if timer is not None:
                    timer.cancel()
                if response is not None:
                    response.close()
                connection.close()
    except DiscoveryInputError as error:
        return failed("invalid_url", str(error))
    except BudgetExceeded as error:
        return failed(error.code, "Discovery budget exhausted.")
    except _FetchError as error:
        return failed(error.code, error.reason)
    except (OSError, http.client.HTTPException, UnicodeError) as error:
        try:
            budget.remaining()
        except BudgetExceeded:
            return failed("deadline_exceeded", "Discovery deadline exceeded.")
        if isinstance(error, TimeoutError):
            return failed("timeout", "Connection or read timed out.")
        if isinstance(error, ssl.SSLError):
            return failed("tls_error", "TLS connection or certificate validation failed.")
        code = "incomplete_response" if isinstance(error, http.client.IncompleteRead) else "connection_error"
        return failed(code, "Could not retrieve a complete HTTP response.")
