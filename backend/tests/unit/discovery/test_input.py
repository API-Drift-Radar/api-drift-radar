import pytest

from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.domain.discovery import DiscoveryOutcome, DiscoveryRequest, DiscoveryStatus


@pytest.mark.parametrize("target,url,host,path", [
    ("api.example.com", "https://api.example.com/", "api.example.com", "/"),
    ("  API.Example.com  ", "https://api.example.com/", "api.example.com", "/"),
    ("https://api.example.com/v1/customers?limit=10", "https://api.example.com/v1/customers?limit=10", "api.example.com", "/v1/customers"),
    ("http://localhost:8080/spec", "http://localhost:8080/spec", "localhost", "/spec"),
    ("example.com:443", "https://example.com:443/", "example.com", "/"),
    ("https://[::1]:8080/", "https://[::1]:8080/", "::1", "/"),
    ("https://bücher.example", "https://xn--bcher-kva.example/", "xn--bcher-kva.example", "/"),
])
def test_normalization(target, url, host, path):
    result = normalize_target(DiscoveryRequest(target))
    assert result.original_target == target
    assert (result.normalized_url, result.hostname, result.path) == (url, host, path)
    assert result.method is None
    assert result.api_version is None


def test_explicit_context_is_preserved():
    result = normalize_target(DiscoveryRequest("https://example.com/v1/customers", "get", "2026-01", "Payments"))
    assert (result.method, result.api_version, result.product) == ("GET", "2026-01", "Payments")


@pytest.mark.parametrize("target", [
    "", "  ", None, 12, "ftp://example.com", "https://user:secret@example.com",
    "https://example.com:99999", "https://example.com:0", "https://example.com:abc",
    "https://example.com:", "https://", "https://bad host.com", "https://bad\nhost.com",
    "https://-bad.example", "https://bad_.example", "https://example..com",
    "https://example.com/#fragment", "https://example.com/#", "https://example.com\\path",
    "example.com/path", "https://[::1", "https://example.com/\x00",
])
def test_invalid_target(target):
    with pytest.raises(DiscoveryInputError):
        normalize_target(DiscoveryRequest(target))


@pytest.mark.parametrize("field,value", [("method", ""), ("method", "G ET"), ("method", 1), ("api_version", " "), ("product", 5)])
def test_invalid_context(field, value):
    with pytest.raises(DiscoveryInputError):
        normalize_target(DiscoveryRequest("example.com", **{field: value}))


def test_outcome_cannot_claim_validation_without_package():
    with pytest.raises(ValueError):
        DiscoveryOutcome(DiscoveryStatus.VALIDATED)


def test_unsuccessful_outcome():
    outcome = DiscoveryOutcome(DiscoveryStatus.NOT_FOUND, limitations=("Bounded search only",))
    assert outcome.package is None
    assert outcome.limitations == ("Bounded search only",)
