"""Decide whether a validated contract belongs to the API that was asked about.

Pure: no network. Each check returns match, mismatch, indeterminate or
not_requested with a plain-language description; there are no scores. Any
mismatch rejects a candidate, anything that cannot be checked stays
indeterminate and is never counted as a match.
"""

from dataclasses import dataclass, replace
import ipaddress
import itertools
import re
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from radar.discovery.validation import ContractSummary, ValidationRejection, ValidationResult
from radar.domain.discovery import NormalizedTarget


MATCH, MISMATCH, INDETERMINATE, NOT_REQUESTED = 'match', 'mismatch', 'indeterminate', 'not_requested'
SAME, TARGET_UNDER_SERVER, SERVER_UNDER_TARGET = 'same', 'target_under_server', 'server_under_target'
MAX_SERVERS = 100
MAX_VARIABLE_COMBINATIONS = 32
_PLACEHOLDER = re.compile(r'\{([^{}]*)\}')
_RELATION_TEXT = {SAME: 'the same host as', TARGET_UNDER_SERVER: 'a parent domain of',
                  SERVER_UNDER_TARGET: 'a subdomain of'}


@dataclass(frozen=True)
class MatchCheck:
    criterion: str
    outcome: str
    description: str
    source_url: str


def _text(value, limit=120):
    value = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(value))
    return value if len(value) <= limit else value[:limit - 1] + '…'


def _normalize_host(host):
    """Lowercase ASCII (IDNA) host without a trailing dot, an IP object, or None if invalid."""
    if not isinstance(host, str) or not host or len(host) > 253:
        return None
    host = host.strip('[]') if host.startswith('[') else host
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        return None
    labels = host.split('.')
    if not host or any(not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', label) for label in labels):
        return None
    return host


def hosts_related(target_host: str, server_host: str) -> str | None:
    """How a server host relates to the target host, or None.

    Equal hosts, or one a DNS-label-boundary subdomain of the other. The shorter side
    needs at least two labels so that `com` or `localhost` never relate hosts. IP
    addresses relate only when equal. No public-suffix list is used, so a shared
    suffix such as `github.io` as a target relates to every host beneath it.
    """
    target, server = _normalize_host(target_host), _normalize_host(server_host)
    if target is None or server is None:
        return None
    if not isinstance(target, str) or not isinstance(server, str):
        return SAME if target == server else None
    if target == server:
        return SAME
    if target.endswith('.' + server) and server.count('.') >= 1:
        return TARGET_UNDER_SERVER
    if server.endswith('.' + target) and target.count('.') >= 1:
        return SERVER_UNDER_TARGET
    return None


@dataclass(frozen=True)
class _Server:
    raw: str
    absolute: bool
    host: str | None = None  # None with absolute=True: the host depends on an unresolvable variable
    port: int | None = None
    path: str | None = None  # base path; None when unknown (variable) or not rooted at "/"


def _expansions(raw, variables):
    """All URLs from substituting server variables (defaults and enum values), or None."""
    names = list(dict.fromkeys(_PLACEHOLDER.findall(raw)))
    if not names:
        return [raw]
    choices = []
    for name in names:
        spec = variables.get(name) if isinstance(variables, dict) else None
        values = []
        if isinstance(spec, dict):
            if isinstance(spec.get('enum'), list):
                values.extend(v for v in spec['enum'] if isinstance(v, str))
            if isinstance(spec.get('default'), str):
                values.append(spec['default'])
        values = list(dict.fromkeys(values))
        if not values:
            return None
        choices.append(values)
    total = 1
    for values in choices:
        total *= len(values)
    if total > MAX_VARIABLE_COMBINATIONS:
        return None
    urls = []
    for combination in itertools.product(*choices):
        url = raw
        for name, value in zip(names, combination):
            url = url.replace('{' + name + '}', value)
        urls.append(url)
    return urls


def _servers(document: Mapping[str, Any]):
    """Resolved server options, whether the list was truncated, and any servers skipped."""
    entries = document.get('servers')
    entries = entries if isinstance(entries, list) else []
    options = []
    for entry in entries[:MAX_SERVERS]:
        raw = entry.get('url') if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            options.append(_Server(str(raw), True))  # malformed: cannot be ruled out
            continue
        try:
            netloc_has_variable = '{' in urlsplit(raw).netloc
            urls = _expansions(raw, entry.get('variables'))
            if urls is None:
                options.append(_Server(raw, bool(urlsplit(raw).netloc) or '://' in raw))
                continue
            for url in urls:
                parts = urlsplit(url)
                path = parts.path if parts.path.startswith('/') or not parts.path else None
                if not parts.netloc:
                    options.append(_Server(raw, False, path=path if raw.startswith('/') else None))
                else:
                    options.append(_Server(raw, True, parts.hostname, parts.port, path))
            if netloc_has_variable and not urls:
                options.append(_Server(raw, True))
        except ValueError:
            options.append(_Server(raw, True))
    return options, len(entries) > MAX_SERVERS


def check_server_host(target: NormalizedTarget, document: Mapping[str, Any], source_url: str) -> MatchCheck:
    """Do the contract's declared servers include a host related to the target host?

    match: some absolute server host is related. mismatch: every server is an absolute
    URL with a known host and none is related. indeterminate: relative or missing
    servers, host variables with no usable value, or anything that could hide a match.
    """
    servers, truncated = _servers(document)
    target_port = urlsplit(target.normalized_url).port
    for server in servers:
        if not server.absolute or server.host is None:
            continue
        relation = hosts_related(target.hostname, server.host)
        if relation and not (target_port and server.port and target_port != server.port):
            return MatchCheck(
                'server_host', MATCH,
                f'Server {_text(server.raw)!r} declares {_text(server.host)}, '
                f'{_RELATION_TEXT[relation]} the target host {_text(target.hostname)}.', source_url)
    if not servers:
        return MatchCheck('server_host', INDETERMINATE,
                          'The contract declares no servers, so its host cannot be compared.', source_url)
    unresolved = [s for s in servers if not s.absolute or s.host is None]
    if unresolved or truncated:
        reason = ('only the first {} servers were compared'.format(MAX_SERVERS) if truncated
                  else 'some servers are relative or depend on variables without a usable value')
        return MatchCheck('server_host', INDETERMINATE,
                          f'No declared server host is related to {_text(target.hostname)}, but {reason}.', source_url)
    shown = ', '.join(dict.fromkeys(_text(s.host, 60) for s in servers[:5]))
    return MatchCheck('server_host', MISMATCH,
                      f'None of the declared server hosts ({shown}) is related to the target host '
                      f'{_text(target.hostname)}.', source_url)


# --- operation ---------------------------------------------------------------

_PARAMETER = re.compile(r'\{[^{}/]+\}')
MAX_REPORTED_PATH = 120


def _template_segments(template):
    """Per path segment, the literal pieces around its {parameters}."""
    return [_PARAMETER.split(segment) for segment in template.split('/')]


def _segment_matches(pieces, segment):
    """Match one segment against literals separated by one-or-more-character parameters.

    Leftmost matching of the literal pieces is exact for this wildcard form and is
    linear, so no template can cause catastrophic backtracking.
    """
    if len(pieces) == 1:
        return segment == pieces[0]
    first, last = pieces[0], pieces[-1]
    if not segment.startswith(first) or not segment.endswith(last):
        return False
    limit = len(segment) - len(last)
    position = len(first)
    for literal in pieces[1:-1]:
        position += 1  # the preceding parameter needs at least one character
        found = segment.find(literal, position, limit)
        if found < 0:
            return False
        position = found + len(literal)
    return position + 1 <= limit  # so does the final one


def _segments_match(template, segments):
    expected = _template_segments(template)
    return len(expected) == len(segments) and all(_segment_matches(p, s) for p, s in zip(expected, segments))


def path_matches(template: str, path: str) -> bool:
    return _segments_match(template, path.split('/'))


def _trim(path):
    return path.rstrip('/') or '/'


def _find(operations, method, path):
    """Operations (METHOD, template) matching `path`, optionally only for `method`."""
    # Decode each segment separately so an encoded slash (%2F) stays inside its segment.
    decoded = [unquote(segment, errors='replace') for segment in path.split('/')]
    return [(m, t) for m, t in operations
            if (method is None or m == method) and (_trim(t) == path or _segments_match(_trim(t), decoded))]


def _relevant_servers(target, document, everything=False):
    """Servers that can describe the target host: not an absolute server on an unrelated host.

    `everything` keeps every declared server, for a contract requested by its own URL, where the
    target host is where the document lives, not the API's host.
    """
    servers, _ = _servers(document)
    if everything:
        return servers
    return [s for s in servers
            if not (s.absolute and s.host is not None and hosts_related(target.hostname, s.host) is None)]


def check_operation(target: NormalizedTarget, summary: ContractSummary, document: Mapping[str, Any],
                    source_url: str) -> MatchCheck:
    """Does the contract define the operation the target names?

    With a method, the target path must exist for that method (mismatch if not). A path
    without a method is evidence only when it exists; a miss may just be a docs URL, so it
    stays indeterminate. A server base path is tried first, then the path as given. A
    miss is never a mismatch when `$ref` path items or variable server paths hide it.
    """
    def check(outcome, description):
        return MatchCheck('operation', outcome, description, source_url)

    path = _trim(target.path)
    if path == '/':
        if target.method:
            return check(INDETERMINATE, f'{target.method} was given without an endpoint path, so no operation was checked.')
        return check(NOT_REQUESTED, 'No endpoint path or method was given.')
    relevant = _relevant_servers(target, document)
    bases = sorted({_trim(s.path) if s.path and s.path != '/' else '' for s in relevant if s.path is not None},
                   key=len, reverse=True)
    base_unknown = any(s.path is None for s in relevant)  # variable, malformed or unrooted relative path
    operations = summary.operations
    label = f'{target.method} {_text(path, MAX_REPORTED_PATH)}' if target.method else _text(path, MAX_REPORTED_PATH)
    for base in [*(b for b in bases if b), '']:
        if base and not (path == base or path.startswith(base + '/')):
            continue
        remainder = _trim(path[len(base):] or '/') if base else path
        found = _find(operations, target.method, remainder)
        if found:
            method, template = found[0]
            where = f' after removing the server base path {_text(base, 60)}' if base else ''
            return check(MATCH, f'{method} {_text(template, MAX_REPORTED_PATH)} is defined for {label}{where}.')
    hidden = summary.unexpanded_path_items or base_unknown
    if not target.method:
        return check(INDETERMINATE, f'No path matching {label} was found, but the URL path may not be an API endpoint.')
    if hidden:
        reason = (f'{summary.unexpanded_path_items} path item(s) are only $ref' if summary.unexpanded_path_items
                  else 'a server base path depends on a variable')
        return check(INDETERMINATE, f'{label} was not found, but {reason}, so it cannot be ruled out.')
    others = sorted({m for m, t in _find(operations, None, path)})
    note = f' (the path exists for {", ".join(others)})' if others else ''
    return check(MISMATCH, f'No operation {label} is defined among {len(operations)} operations{note}.')


# --- API version -------------------------------------------------------------

_VERSION_LIKE = re.compile(r'v?\d+(?:\.\d+)*(?:[-.]?(?:alpha|beta|rc)\d*)?|\d{4}-\d{2}-\d{2}', re.IGNORECASE)


def _version_key(value):
    value = str(value).strip().casefold()
    return value[1:] if len(value) > 1 and value[0] == 'v' and value[1].isdigit() else value


def versions_agree(hint, value) -> bool:
    """Equal, or one is a dot-boundary prefix of the other (`2` and `2.1.0`, `2026-09-30` and `2026-09-30.x`)."""
    a, b = _version_key(hint), _version_key(value)
    return a == b or a.startswith(b + '.') or b.startswith(a + '.')


def check_api_version(target: NormalizedTarget, summary: ContractSummary, document: Mapping[str, Any],
                      source_url: str, *, mapping_api_version: str | None = None,
                      all_servers: bool = False) -> MatchCheck:
    """Does the requested provider API version fit this contract?

    Evidence in order of strength: an explicit provider-mapping version, then a version in
    a server path, then `info.version`. `info.version` is the document's own version (often a
    default such as 1.0.0), so it can only support a match when the paths say nothing and
    never contradicts. A server path can only contradict a version-like hint.
    """
    def check(outcome, description):
        return MatchCheck('api_version', outcome, description, source_url)

    hint = target.api_version
    if hint is None:
        return check(NOT_REQUESTED, 'No API version was requested.')
    shown = _text(hint, 60)
    if mapping_api_version is not None:
        if versions_agree(hint, mapping_api_version):
            return check(MATCH, f'The provider mapping declares API version {_text(mapping_api_version, 60)}, '
                                f'which fits the requested {shown}.')
        return check(MISMATCH, f'The provider mapping declares API version {_text(mapping_api_version, 60)}, '
                               f'which contradicts the requested {shown}.')
    relevant = _relevant_servers(target, document, everything=all_servers)
    segments = []
    for server in relevant:
        for segment in (server.path or '').split('/'):
            if segment and _VERSION_LIKE.fullmatch(segment) and segment not in segments:
                segments.append(segment)
    for segment in segments:
        if versions_agree(hint, segment):
            return check(MATCH, f'A server path contains version {_text(segment, 60)}, which fits the requested {shown}.')
    if segments and _VERSION_LIKE.fullmatch(hint) and not any(s.path is None for s in relevant):
        return check(MISMATCH, f'Server paths declare version(s) {", ".join(_text(s, 30) for s in segments[:5])}, '
                               f'not the requested {shown}.')
    if (not segments or not _VERSION_LIKE.fullmatch(hint)) and summary.info_version \
            and versions_agree(hint, summary.info_version):
        return check(MATCH, f'info.version {_text(summary.info_version, 60)} fits the requested {shown}; '
                            'server paths do not decide it.')
    return check(INDETERMINATE, f'Nothing in the contract states which provider API version it describes '
                                f'(info.version {_text(summary.info_version, 60)!r} is the document\'s own version), '
                                f'so {shown} cannot be confirmed.')


# --- product -----------------------------------------------------------------

MAX_PRODUCT_TEXT = 5000
MAX_TAGS = 100


def _product_tokens(value):
    """Lowercase alphanumeric words with a light plural fold (`payments` ~ `payment`)."""
    words = re.findall(r'[^\W_]+', str(value).casefold())
    return [w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w for w in words]


def check_product(target: NormalizedTarget, summary: ContractSummary, document: Mapping[str, Any],
                  source_url: str, *, mapping_product: str | None = None) -> MatchCheck:
    """Does the requested product name appear in the contract's own descriptive text?

    Whole words in the title, description, server URLs, tag names or the mapping's product
    label. Text containment is approximate: a differently worded name is a false mismatch,
    which is why this only rejects when a product hint was supplied.
    """
    def check(outcome, description):
        return MatchCheck('product', outcome, description, source_url)

    hint = target.product
    if hint is None:
        return check(NOT_REQUESTED, 'No product was requested.')
    wanted = _product_tokens(hint)
    info = document.get('info') if isinstance(document.get('info'), dict) else {}
    tags = document.get('tags') if isinstance(document.get('tags'), list) else []
    sources = [('title', summary.title), ('description', str(info.get('description', ''))[:MAX_PRODUCT_TEXT])]
    sources += [('server URL', s.raw) for s in _servers(document)[0]]
    sources += [('tag', t.get('name', '')) for t in tags[:MAX_TAGS] if isinstance(t, dict)]
    if mapping_product:
        sources.append(('provider mapping', mapping_product))
    if wanted:
        for label, text in sources:
            tokens = _product_tokens(text)
            if any(tokens[i:i + len(wanted)] == wanted for i in range(len(tokens) - len(wanted) + 1)):
                return check(MATCH, f'The requested product {_text(hint, 60)!r} appears in the contract\'s {label}.')
    return check(MISMATCH, f'The requested product {_text(hint, 60)!r} does not appear in the contract\'s title '
                           f'({_text(summary.title, 60)!r}), description, server URLs or tags.')


# --- provenance and decision -------------------------------------------------

STAGE = 'matching'
DOCUMENTATION_METHODS = frozenset({'documentation_link', 'swagger_ui_config', 'redoc_config', 'spec_url_attribute'})
REJECTION_CODES = {'server_host': 'server_host_mismatch', 'operation': 'operation_not_found',
                   'api_version': 'version_mismatch', 'product': 'product_mismatch'}
GENERAL_LIMITATION = 'Matching applies explicit rules to the contract text and its origin; it does not prove identity.'
NO_EVIDENCE = ('No check produced positive evidence that this contract belongs to the requested API; '
               'every check was indeterminate or not requested.')


@dataclass(frozen=True)
class MatchContext:
    """Everything matching needs besides the validated contract."""
    target: NormalizedTarget
    source_url: str  # final URL the contract document was retrieved from
    discovery_method: str
    discovery_source: str  # URL that led here: the probed URL, the registry's provenance page, or a docs page
    mapping_api_version: str | None = None  # from the provider mapping, if that is how it was found
    mapping_product: str | None = None


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    checks: tuple[MatchCheck, ...]
    rejection: ValidationRejection | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        if self.accepted != (self.rejection is None):
            raise ValueError('An accepted match has no rejection; a rejected one has exactly one.')

    @property
    def positive(self) -> tuple[str, ...]:
        return tuple(c.criterion for c in self.checks if c.outcome == MATCH)


def _host(url):
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def check_provenance(context: MatchContext) -> MatchCheck:
    """Where did this contract come from? Positive or unknown, never a rejection."""
    def check(outcome, description):
        return MatchCheck('provenance', outcome, description, context.source_url)

    target = context.target.hostname
    if context.discovery_method == 'direct_url':
        return check(MATCH, f'The user supplied this exact contract URL ({_text(context.source_url)}).')
    if context.discovery_method == 'provider_mapping':
        return check(MATCH, f'A provider mapping explicitly lists host {_text(target)} '
                            f'(source: {_text(context.discovery_source)}).')
    served_from = _host(context.source_url)
    relation = hosts_related(target, served_from) if served_from else None
    if relation:
        return check(MATCH, f'The contract is served from {_text(served_from)}, {_RELATION_TEXT[relation]} '
                            f'the target host {_text(target)}.')
    page = _host(context.discovery_source)
    if context.discovery_method in DOCUMENTATION_METHODS and page and hosts_related(target, page):
        return check(MATCH, f'The contract is linked from the documentation page {_text(context.discovery_source)} '
                            f'on {_text(page)}, which is related to the target host, but is hosted at '
                            f'{_text(served_from)}.')
    return check(INDETERMINATE, f'The contract is hosted at {_text(served_from)} and was found by '
                                f'{_text(context.discovery_method, 40)}, which does not establish that it belongs '
                                f'to {_text(target)}.')


def assess_match(context: MatchContext, validation: ValidationResult) -> MatchResult:
    """Run every check and decide: any mismatch rejects, otherwise accept with the evidence.

    A rejected result still carries all checks so a caller can show what passed. An accepted
    result with no `match` at all is kept but flagged: unverifiable is not the same as wrong,
    and it is never counted as a match. Pure and deterministic.
    """
    if not validation.ok:
        raise ValueError('Only a validated contract can be matched.')
    summary, document, source = validation.summary, validation.document, context.source_url
    target = context.target
    direct = context.discovery_method == 'direct_url'
    if direct:  # the URL names the document, not an endpoint of the API
        target = replace(target, path='/')
    host = check_server_host(target, document, source)
    if direct and host.outcome == MISMATCH:
        host = MatchCheck('server_host', INDETERMINATE,
                          'The contract was requested directly by its URL, so servers declared on other hosts '
                          f'are not held against it. {host.description}', source)
    checks = (
        host,
        check_operation(target, summary, document, source),
        check_api_version(target, summary, document, source, mapping_api_version=context.mapping_api_version,
                          all_servers=direct),
        check_product(target, summary, document, source, mapping_product=context.mapping_product),
        check_provenance(context),
    )
    mismatches = [c for c in checks if c.outcome == MISMATCH]
    if mismatches:
        rejection = ValidationRejection(REJECTION_CODES[mismatches[0].criterion],
                                        ' '.join(c.description for c in mismatches), None, STAGE)
        return MatchResult(False, checks, rejection, (GENERAL_LIMITATION,))
    limitations = [GENERAL_LIMITATION]
    if not any(c.outcome == MATCH for c in checks):
        limitations.append(NO_EVIDENCE)
    return MatchResult(True, checks, None, tuple(limitations))
