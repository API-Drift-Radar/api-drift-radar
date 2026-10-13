"""Recognise API artifacts that are NOT supported OpenAPI 3.0/3.1 contracts, without adapting them.

Detection is only recognition: it lets a result say what was found (a Swagger 2.0 description, a Google
Discovery document, a Smithy model, apiDoc documentation data) instead of lumping it with malformed files or
reporting nothing. No contract adapter exists for any of these, and none is implied.
"""

from dataclasses import dataclass
import re
from typing import Any, Mapping

UNSUPPORTED_DESCRIPTION = 'unsupported_description'  # a formal machine-readable description we cannot compare
DOCUMENTATION_ONLY = 'documentation_only'  # documentation data or a collection, not a formal contract


@dataclass(frozen=True)
class ArtifactInfo:
    kind: str  # swagger_2, google_discovery, smithy, asyncapi, apidoc, postman_collection
    category: str  # UNSUPPORTED_DESCRIPTION or DOCUMENTATION_ONLY
    label: str


_APIDOC_DATA = re.compile(r'^\s*define\(\s*\{[^{}]{0,200}"api"\s*:\s*\[', re.S)
_APIDOC_PROJECT = re.compile(r'^\s*define\(\s*\{.{0,4000}?"apidoc"\s*:\s*"', re.S)


def identify_description(document: Mapping[str, Any]) -> ArtifactInfo | None:
    """What kind of non-OpenAPI-3 artifact a parsed JSON/YAML object is, if recognisable."""
    if not isinstance(document, Mapping):
        return None
    swagger = document.get('swagger')
    if isinstance(swagger, (str, int, float)) and str(swagger).startswith('2'):
        return ArtifactInfo('swagger_2', UNSUPPORTED_DESCRIPTION, 'Swagger 2.0')
    if 'asyncapi' in document and isinstance(document.get('asyncapi'), str):
        return ArtifactInfo('asyncapi', UNSUPPORTED_DESCRIPTION, 'AsyncAPI')
    if isinstance(document.get('smithy'), str) and isinstance(document.get('shapes'), Mapping):
        return ArtifactInfo('smithy', UNSUPPORTED_DESCRIPTION, 'Smithy model')
    kind = document.get('kind')
    if (isinstance(kind, str) and kind.startswith('discovery#')) or (
            'discoveryVersion' in document and ('resources' in document or 'methods' in document)):
        return ArtifactInfo('google_discovery', UNSUPPORTED_DESCRIPTION, 'Google API Discovery document')
    info = document.get('info')
    if isinstance(info, Mapping) and 'postman' in str(info.get('schema', '')).lower() and isinstance(document.get('item'), list):
        return ArtifactInfo('postman_collection', DOCUMENTATION_ONLY, 'Postman collection')
    if isinstance(document.get('apidoc'), str) and 'name' in document:
        return ArtifactInfo('apidoc', DOCUMENTATION_ONLY, 'apiDoc project metadata')
    return None


def recognize_artifact(content: bytes) -> ArtifactInfo | None:
    """Recognise an artifact from raw bytes: parsed JSON/YAML, or apiDoc's JavaScript `define({...})` wrappers."""
    from radar.discovery.validation import parse_document  # local import: validation imports this module

    try:
        text = content.decode('utf-8-sig')
    except UnicodeError:
        return None
    head = text[:8000]
    if _APIDOC_DATA.match(head):
        return ArtifactInfo('apidoc', DOCUMENTATION_ONLY, 'apiDoc endpoint data')
    if _APIDOC_PROJECT.match(head):
        return ArtifactInfo('apidoc', DOCUMENTATION_ONLY, 'apiDoc project metadata')
    start = text.lstrip()[:1]
    if start not in ('{', '['):
        return None
    parsed = parse_document(content)
    if not parsed.ok:
        return None
    value = parsed.value
    if isinstance(value, list):
        sample = [v for v in value[:5] if isinstance(v, dict)]
        if sample and all({'type', 'url', 'name'} <= set(v) and 'group' in v for v in sample):
            return ArtifactInfo('apidoc', DOCUMENTATION_ONLY, 'apiDoc endpoint data')
        return None
    return identify_description(value)
