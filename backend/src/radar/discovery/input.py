"""Pure discovery input normalization; no fetching or relevance inference."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from radar.domain.discovery import DiscoveryRequest, NormalizedTarget


class DiscoveryInputError(ValueError):
    """The supplied discovery request cannot be interpreted safely."""


def _optional_text(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryInputError(f"{name} must be a nonempty string when supplied.")
    return value.strip()


def normalize_target(request: DiscoveryRequest) -> NormalizedTarget:
    """Accept an HTTP(S) URL or bare host; preserve paths, queries and context.

    Fragments are rejected because they do not identify an HTTP resource.
    Hostnames are normalized to ASCII IDNA. Network destination restrictions
    and DNS resolution belong to the future fetcher, not this function.
    """
    if not isinstance(request, DiscoveryRequest):
        raise DiscoveryInputError("Expected a DiscoveryRequest.")
    if not isinstance(request.target, str) or not request.target.strip():
        raise DiscoveryInputError("Target must be a nonempty string.")
    target = request.target.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in target):
        raise DiscoveryInputError("Target must not contain whitespace or control characters.")
    if "\\" in target:
        raise DiscoveryInputError("Target must not contain backslashes.")
    if "#" in target:
        raise DiscoveryInputError("URL fragments are not supported.")
    if "://" not in target:
        # Only bare hosts (with optional port) get an implicit scheme.
        if any(c in target for c in "/?"):
            raise DiscoveryInputError("Provide an explicit HTTP or HTTPS URL for a path or query.")
        target = "https://" + target
    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
    except ValueError:
        raise DiscoveryInputError("Provide a valid URL and hostname.") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise DiscoveryInputError("Unsupported URL scheme. Use HTTP or HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise DiscoveryInputError("URLs containing credentials are not supported.")
    if not hostname:
        raise DiscoveryInputError("Provide a valid hostname.")
    try:
        port = parsed.port
        if port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError:
        raise DiscoveryInputError("Port must be between 1 and 65535.") from None
    try:
        address = ipaddress.ip_address(hostname)
        host = address.compressed
        authority = f"[{host}]" if address.version == 6 else host
    except ValueError:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise DiscoveryInputError("Provide a valid hostname.") from None
        labels = host.rstrip(".").split(".")
        if len(host.rstrip(".")) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise DiscoveryInputError("Provide a valid hostname.")
        authority = host
    if port is not None:
        authority += f":{port}"
    method = _optional_text(request.method, "Method")
    if method is not None:
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method):
            raise DiscoveryInputError("Method must be a valid HTTP token.")
        method = method.upper()
    path = parsed.path or "/"
    return NormalizedTarget(
        original_target=request.target,
        normalized_url=urlunsplit((parsed.scheme.lower(), authority, path, parsed.query, "")),
        hostname=host,
        path=path,
        method=method,
        api_version=_optional_text(request.api_version, "API version"),
        product=_optional_text(request.product, "Product"),
    )
