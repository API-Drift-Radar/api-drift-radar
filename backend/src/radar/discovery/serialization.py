"""JSON-ready views of discovery results, for the HTTP API and the web interface.

Pure functions, no I/O. The contract bytes are never inlined: a package is described by its
URL, size and SHA-256, and by a content fingerprint, so a response stays small (a real contract
can be megabytes) and the bytes are served by a separate snapshot endpoint. The field names are
a proposal for the API owner; they are generated from these functions and pinned by the sample
files under docs/examples/discovery-outcomes.
"""

from datetime import datetime, timezone
import hashlib

from radar.discovery.evaluation import package_fingerprint
from radar.discovery.validation import HTTP_METHODS
from radar.domain.discovery import (
    ArtifactFinding, CapturedDocument, ContractCandidate, Coverage, DiscoveryOutcome, LeadRecord, MatchingEvidence,
    ValidatedContractPackage,
)


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def evidence_to_dict(evidence: MatchingEvidence) -> dict:
    """`outcome` is match, mismatch, indeterminate or not_requested; null for discovery hints."""
    return {'criterion': evidence.criterion, 'outcome': evidence.outcome, 'description': evidence.description,
            'source_url': evidence.source_url}


def rejection_from_text(text: str) -> dict:
    """Split the recorded `stage:code: reason` string; keep the raw text if it does not parse."""
    head, separator, reason = text.partition(': ')
    stage, colon, code = head.partition(':')
    if separator and colon and stage and code and ' ' not in head:
        return {'stage': stage, 'code': code, 'reason': reason}
    return {'stage': None, 'code': None, 'reason': text}


def document_to_dict(document: CapturedDocument) -> dict:
    return {'url': document.source_url, 'media_type': document.media_type, 'size_bytes': len(document.content),
            'sha256': hashlib.sha256(document.content).hexdigest(), 'retrieved_at': _time(document.retrieved_at)}


def candidate_to_dict(candidate: ContractCandidate) -> dict:
    """Every evaluated document. `accepted` is true when nothing rejected it."""
    return {
        'source_url': candidate.source_url,
        'discovery_method': candidate.discovery_method,
        'discovery_source': candidate.discovery_source,
        'accepted': not candidate.rejection_reasons,
        'rejections': [rejection_from_text(r) for r in candidate.rejection_reasons],
        'evidence': [evidence_to_dict(e) for e in candidate.evidence],
        'limitations': list(candidate.limitations),
    }


def package_to_dict(package: ValidatedContractPackage) -> dict:
    contract = package.parsed_contract
    info = contract.get('info', {}) if isinstance(contract.get('info'), dict) else {}
    paths = contract.get('paths') if isinstance(contract.get('paths'), dict) else {}
    operations = sum(1 for item in paths.values() if isinstance(item, dict)
                     for method in HTTP_METHODS if isinstance(item.get(method), dict))
    servers = contract.get('servers') if isinstance(contract.get('servers'), list) else []
    return {
        'source_url': package.candidate.source_url,
        'discovery_method': package.candidate.discovery_method,
        'discovery_source': package.candidate.discovery_source,
        'content_fingerprint': package_fingerprint(package),
        'openapi_version': package.openapi_version,
        'title': info.get('title'),
        'info_version': info.get('version'),
        'server_urls': [s['url'] for s in servers if isinstance(s, dict) and isinstance(s.get('url'), str)],
        'path_count': sum(1 for key in paths if not str(key).startswith('x-')),
        'operation_count': operations,
        'root_document': document_to_dict(package.root_document),
        'referenced_documents': [document_to_dict(d) for d in package.referenced_documents],
        'evidence': [evidence_to_dict(e) for e in package.candidate.evidence],
        'limitations': list(package.limitations),
    }


def artifact_to_dict(finding: ArtifactFinding) -> dict:
    """category: unsupported_description, documentation_only, authentication_required or
    unsupported_dynamic_configuration. `kind` names the format or barrier (swagger_2, apidoc, google_discovery,
    smithy, asyncapi, postman_collection, authentication, swagger_ui_configuration, ...)."""
    return {'category': finding.category, 'kind': finding.kind, 'url': finding.url, 'detail': finding.detail,
            'discovery_method': finding.discovery_method, 'parent_url': finding.parent_url, 'status': finding.status}


def lead_to_dict(lead: LeadRecord) -> dict:
    """One navigation step. outcome: fetched, failed, not_examined, skipped_depth, skipped_limit, skipped_unrelated."""
    return {'url': lead.url, 'parent_url': lead.parent_url, 'mechanism': lead.mechanism, 'kind': lead.kind,
            'depth': lead.depth, 'outcome': lead.outcome}


def coverage_to_dict(coverage: Coverage | None) -> dict | None:
    """How much of the bounded search was completed. `complete` is false when any limit cut it short."""
    if coverage is None:
        return None
    return {'complete': coverage.complete, 'limits_reached': list(coverage.limits_reached),
            'leads_examined': coverage.leads_examined, 'leads_unexamined': coverage.leads_unexamined,
            'unexamined': [lead_to_dict(lead) for lead in coverage.unexamined],
            'requests_used': coverage.requests_used, 'requests_limit': coverage.requests_limit,
            'reserved_for_references': coverage.reserved_for_references, 'hosts_contacted': list(coverage.hosts_contacted)}


def outcome_to_dict(outcome: DiscoveryOutcome) -> dict:
    """status is one of validated, ambiguous, rejected, inaccessible, not_found.

    `package` is set only when validated; `alternatives` only when ambiguous (resolve with an
    explicit choice, no re-fetch needed). `candidates` lists every evaluated document, accepted or
    not; `attempts` every fetch and judgement; `limitations` the bounds and caveats that apply. The optional
    `artifacts` (unsupported descriptions, documentation-only data, authentication barriers, unsupported dynamic
    configuration), `trail` (navigation steps with parent and mechanism) and `coverage` (which limits cut the search
    short) explain what was found besides a contract and how complete the search was.
    """
    return {
        'status': outcome.status.value,
        'package': None if outcome.package is None else package_to_dict(outcome.package),
        'alternatives': [package_to_dict(p) for p in outcome.packages],
        'candidates': [candidate_to_dict(c) for c in outcome.candidates],
        'attempts': [{'url': a.url, 'stage': a.stage, 'outcome': a.outcome, 'reason': a.reason}
                     for a in outcome.attempts],
        'limitations': list(outcome.limitations),
        'artifacts': [artifact_to_dict(a) for a in outcome.artifacts],
        'trail': [lead_to_dict(lead) for lead in outcome.trail],
        'coverage': coverage_to_dict(outcome.coverage),
    }
