"""Bounded documentation navigation: a prioritised queue of leads that can reach a published description.

A lead is a URL with a role, the URL it was found on (its parent), and the mechanism that produced it. Leads
backed by explicit publication (typed `service-desc`/`service-doc`/`api-catalog` links, an RFC 9727 catalogue,
a link or viewer configuration that names a description) outrank leads inferred from page navigation, and
those outrank framework-convention probes, which are guesses. Everything shares the run's budget (requests,
bytes, time, hosts), a depth limit and a lead limit, and a share of the request budget stays reserved for
capturing the chosen contract's references.

Nothing fetched is trusted. A link, however explicit, only creates a lead: it never proves who published a
contract or that it applies to the target. Cross-origin links are followed through the same protected fetcher
(private addresses refused, redirects re-checked) and the connection is kept in the trail. The target's
HTTP method is never used: this module only issues GET requests for documentation and metadata.

Candidates it finds are returned unvalidated; the caller judges them exactly as it judges any other candidate.
"""

from dataclasses import dataclass
import heapq
import itertools
import re
from typing import Sequence
from urllib.parse import urljoin, urlsplit

from radar.discovery.candidates import RetrievedCandidate, fetch_cached
from radar.discovery.catalog import parse_linkset
from radar.discovery.documentation import DocumentationLimits, extract_document_links
from radar.discovery.fetch import FetchResult
from radar.discovery.formats import DOCUMENTATION_ONLY, identify_description, recognize_artifact
from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.limits import BUDGET_STOP_CODES, DiscoveryBudget
from radar.discovery.link_scoring import MIN_CROSS_ORIGIN_SCORE, MIN_SCORE, score_link
from radar.discovery.links import html_typed_links, parse_link_header
from radar.discovery.matching import hosts_related
from radar.discovery.viewer_config import (
    PageScan, ViewerScan, is_initializer, scan_page, scan_requirejs_main, scan_swagger_config_json, scan_viewer_script,
)
from radar.domain.discovery import ArtifactFinding, ContractCandidate, DiscoveryRequest, LeadRecord, MatchingEvidence

FRAMEWORK_PROBES = ('/v3/api-docs', '/openapi/v1.json', '/swagger/v1/swagger.json', '/v2/api-docs')
# Why navigation stopped completely, versus why one lead was skipped.
STOP_ALL = frozenset({'request_limit', 'navigation_limit', 'deadline_exceeded', 'total_size_limit'})
HTML_TYPES = frozenset({'text/html', 'application/xhtml+xml'})
# Trail outcomes that mean a lead was found but never looked at.
UNEXAMINED = frozenset({'not_examined', 'skipped_depth', 'skipped_limit'})

# Lead priorities: lower is examined first. Explicit publication beats navigation beats guessing.
P_CATALOG, P_DESCRIPTION_TYPED, P_CATALOG_ITEM, P_DESCRIPTION_LINK, P_VIEWER_DESCRIPTION = 4, 5, 6, 6, 7
P_VIEWER_CONFIG, P_SCRIPT, P_LLM, P_DOCUMENTATION_TYPED, P_ORIGIN_ROOT, P_NAVIGATION, P_PROBE = 8, 9, 12, 15, 20, 30, 90


@dataclass(frozen=True)
class NavigationLimits:
    max_pages: int = 8  # pages, scripts and configuration files fetched while navigating (not the contracts themselves)
    max_depth: int = 3  # hops from the start for pages; contracts, scripts and configuration may be one hop further
    max_links_per_page: int = 6  # navigation links followed from any one page
    max_leads: int = 80  # leads ever queued in one run
    max_catalog_entries: int = 20
    max_catalogs: int = 4
    max_initializers_per_page: int = 2
    max_asset_bytes: int = 256 * 1024  # size cap for scripts and configuration files
    max_probe_contexts: int = 2  # the origin root, plus at most one prefix taken from the target's own path
    framework_probes: tuple[str, ...] = FRAMEWORK_PROBES

    def __post_init__(self):
        for name in ('max_pages', 'max_depth', 'max_links_per_page', 'max_leads', 'max_catalog_entries', 'max_catalogs',
                     'max_initializers_per_page', 'max_asset_bytes', 'max_probe_contexts'):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name in ('max_links_per_page', 'max_initializers_per_page') else 1):
                raise ValueError(f'Invalid {name}.')
        if not isinstance(self.framework_probes, tuple) or not all(isinstance(p, str) and p.startswith('/') for p in self.framework_probes):
            raise ValueError('Invalid framework_probes.')


@dataclass
class Lead:
    url: str
    kind: str  # page, description, catalog, config, script
    mechanism: str  # how it was found; for descriptions this becomes the candidate's discovery method
    parent: str | None
    depth: int
    priority: int
    base: str | None = None  # the page whose base URL relative URLs inside a script or configuration resolve against


@dataclass(frozen=True)
class PageRecord:
    fetch: FetchResult
    scan: PageScan
    depth: int


@dataclass(frozen=True)
class NavigationResult:
    candidates: tuple[RetrievedCandidate, ...] = ()
    pages: tuple[PageRecord, ...] = ()
    artifacts: tuple[ArtifactFinding, ...] = ()
    trail: tuple[LeadRecord, ...] = ()
    fetch_log: tuple[tuple[str, FetchResult], ...] = ()  # (mechanism, result) for every fetch made
    limits_reached: tuple[str, ...] = ()
    unexamined: tuple[LeadRecord, ...] = ()


def _key(url):
    try:
        return normalize_target(DiscoveryRequest(url)).normalized_url
    except DiscoveryInputError:
        return None


def _host(url):
    try:
        parts = urlsplit(url)
        return f'{parts.hostname}:{parts.port or (443 if parts.scheme == "https" else 80)}'
    except ValueError:
        return None


class Navigator:
    """One navigation session. Call `seed_*`, then `run()`; `add_leads` + `run()` again for further leads."""

    def __init__(self, request: DiscoveryRequest, budget: DiscoveryBudget, *, limits: NavigationLimits | None = None,
                 allow_loopback: bool = False, cache: dict | None = None, explore: bool = True):
        self.target = normalize_target(request)
        self.budget, self.limits, self.allow_loopback = budget, limits or NavigationLimits(), allow_loopback
        self.cache = {} if cache is None else cache
        self.explore = explore
        parts = urlsplit(self.target.normalized_url)
        self.origin = f'{parts.scheme}://{parts.netloc}'
        self._heap, self._counter = [], itertools.count()
        self._visited, self._leads = set(), {}
        self._candidates, self._returned = [], 0
        self._pages, self._artifacts, self._trail, self._fetch_log = [], [], [], []
        self._limits_reached = []
        self._documents = 0
        self._catalog_leads = 0
        self._catalog_entries = 0
        self._stopped = False
        self._record_cache = {}

    # -- queue -----------------------------------------------------------------------------------
    def _note_limit(self, code):
        if code not in self._limits_reached:
            self._limits_reached.append(code)

    def _trail_add(self, lead, outcome):
        self._trail.append(LeadRecord(lead.url, lead.parent, lead.mechanism, lead.kind, lead.depth, outcome))

    def add(self, url, kind, mechanism, parent, depth, priority, base=None) -> bool:
        key = _key(url)
        if key is None or key in self._visited or key in self._leads:
            return False
        lead = Lead(key, kind, mechanism, parent, depth, priority, base)
        limit = self.limits.max_depth if kind == 'page' else self.limits.max_depth + 1
        if depth > limit:
            self._note_limit('depth_limit')
            self._trail_add(lead, 'skipped_depth')
            return False
        if len(self._leads) >= self.limits.max_leads:
            self._note_limit('lead_limit')
            self._trail_add(lead, 'skipped_limit')
            return False
        if kind == 'catalog':
            if self._catalog_leads >= self.limits.max_catalogs:
                self._note_limit('catalog_limit')
                self._trail_add(lead, 'skipped_limit')
                return False
            self._catalog_leads += 1
        self._leads[key] = lead
        heapq.heappush(self._heap, (priority, next(self._counter), key))
        return True

    def add_leads(self, leads: Sequence[Lead]):
        for lead in leads:
            self.add(lead.url, lead.kind, lead.mechanism, lead.parent, lead.depth, lead.priority, lead.base)

    def depth_of(self, page_url: str) -> int:
        record = next((p for p in self._pages if p.fetch.final_url == page_url), None)
        return record.depth if record else 0

    # -- seeding ---------------------------------------------------------------------------------
    def seed(self, seed_pages: Sequence[FetchResult] = ()):
        """Ingest pages already fetched (no new request), then queue the standard starting leads."""
        for page in seed_pages:
            key = _key(page.final_url)
            if page.ok and key and key not in self._visited:
                self._visited.add(key)
                lead = Lead(key, 'page', 'documentation_seed', None, 0, 0)
                self._leads[key] = lead
                self._trail_add(lead, 'fetched')
                self._ingest(page, lead)
        self.add(f'{self.origin}/.well-known/api-catalog', 'catalog', 'well_known_api_catalog', None, 0, P_CATALOG)
        if self.explore:
            self.add(f'{self.origin}/', 'page', 'origin_root', None, 0, P_ORIGIN_ROOT)
            self._seed_probes()

    def _seed_probes(self):
        segments = [s for s in urlsplit(self.target.normalized_url).path.split('/') if s]
        contexts = [''] + ([f'/{segments[0]}'] if segments else [])  # a prefix only where the target's own path gives one
        for context in contexts[:self.limits.max_probe_contexts]:
            for index, probe in enumerate(self.limits.framework_probes):
                self.add(f'{self.origin}{context}{probe}', 'description', 'framework_probe', None, 0, P_PROBE + index)

    # -- running ---------------------------------------------------------------------------------
    def run(self):
        while self._heap and not self._stopped:
            _, _, key = heapq.heappop(self._heap)
            lead = self._leads[key]
            if key in self._visited:
                continue
            if lead.kind != 'description' and self._documents >= self.limits.max_pages:
                self._note_limit('page_limit')
                self._trail_add(lead, 'skipped_limit')
                continue
            self._visited.add(key)
            self._process(lead)
        if self._stopped:
            for _, _, key in sorted(self._heap):
                if key not in self._visited:
                    self._trail_add(self._leads[key], 'not_examined')
                    self._visited.add(key)  # recorded once; the run is over
            self._heap.clear()

    def _process(self, lead: Lead):
        limit = self.limits.max_asset_bytes if lead.kind in ('script', 'config') else None
        fetch = self._fetch(lead.url, limit)
        self._fetch_log.append((lead.mechanism, fetch))
        if not fetch.ok:
            code = fetch.failure.code
            self._trail_add(lead, 'failed')
            if code in STOP_ALL:
                self._note_limit(code)
                self._stopped = True
            elif code in BUDGET_STOP_CODES:
                self._note_limit(code)  # host_limit: only this lead is refused
            elif fetch.status in (401, 403):
                self._artifacts.append(ArtifactFinding(
                    'authentication_required', 'authentication', lead.url,
                    f'The source answered HTTP {fetch.status}; whether a contract exists behind it is unknown.',
                    lead.mechanism, lead.parent, fetch.status))
            return
        if lead.kind != 'description':
            self._documents += 1
        self._trail_add(lead, 'fetched')
        self._ingest(fetch, lead)

    def _fetch(self, url, document_byte_limit):
        return fetch_cached(url, self.budget, cache=self.cache, allow_loopback=self.allow_loopback,
                            document_byte_limit=document_byte_limit)

    # -- reading what was fetched ------------------------------------------------------------------
    def _path_to(self, lead: Lead) -> str:
        chain, seen, current = [], set(), lead
        while current is not None and current.url not in seen:
            seen.add(current.url)
            host_path = urlsplit(current.url)
            chain.append(f'{current.mechanism} {host_path.netloc}{host_path.path}')
            current = self._leads.get(_key(current.parent)) if current.parent else None
        return ' → '.join(reversed(chain))

    def _candidate(self, fetch: FetchResult, lead: Lead, path: str | None = None):
        candidate = ContractCandidate(
            source_url=fetch.final_url, discovery_method=lead.mechanism, discovery_source=lead.parent or fetch.final_url,
            evidence=(MatchingEvidence('navigation_path', path or self._path_to(lead), lead.parent or fetch.final_url),),
            limitations=('OpenAPI validity and relevance have not been assessed.',
                         'Found by following published links; a link does not establish ownership or applicability.'))
        self._candidates.append(RetrievedCandidate(candidate, fetch))

    def _embedded(self, content: bytes, container: FetchResult, lead: Lead):
        """A specification embedded in a script or configuration: the container is its source; its bytes are kept."""
        synthetic = FetchResult(container.final_url, container.final_url, 200, 'application/json', content,
                                container.retrieved_at, container.attempts)
        embedded = Lead(container.final_url, 'description', 'embedded_spec', container.final_url, lead.depth + 1,
                        P_VIEWER_DESCRIPTION)
        self._candidate(synthetic, embedded, f'{self._path_to(lead)} → embedded_spec in {container.final_url}')

    def _ingest(self, fetch: FetchResult, lead: Lead):
        final = fetch.final_url
        for link in parse_link_header(fetch.link_header, final):
            self._typed(link, final, lead.depth + 1)
        if lead.kind == 'description':
            self._candidate(fetch, lead)
        elif lead.kind == 'catalog':
            self._ingest_catalog(fetch, lead)
        elif lead.kind == 'config':
            self._ingest_config(fetch, lead)
        elif lead.kind == 'script':
            self._ingest_script(fetch, lead)
        else:
            self._ingest_unknown(fetch, lead)

    def _text(self, fetch):
        return fetch.content.decode('utf-8-sig', errors='replace')

    def _ingest_unknown(self, fetch: FetchResult, lead: Lead):
        """A lead whose role is not known in advance (a page, a model-selected link): look at what it is."""
        media = (fetch.content_type or '').split(';')[0].strip().lower()
        head = fetch.content[:512].lstrip()[:1]
        if media in HTML_TYPES or head == b'<':
            self._ingest_page(fetch, lead)
            return
        if parse_linkset(fetch.content, fetch.final_url) is not None:
            self._ingest_catalog(fetch, lead)
            return
        artifact = recognize_artifact(fetch.content)
        if artifact is not None and artifact.category == DOCUMENTATION_ONLY:
            self._artifact(artifact, fetch.final_url, lead)
            return
        if head in (b'{', b'[') or media.endswith(('json', 'yaml', 'yml')):
            self._candidate(fetch, lead)  # a description-like body reached by navigation: judge it like any candidate

    def _artifact(self, info, url, lead):
        self._artifacts.append(ArtifactFinding(
            info.category, info.kind, url, f'{info.label} found; it is not a supported OpenAPI 3.0/3.1 contract.',
            lead.mechanism, lead.parent))

    def _typed(self, link, parent, depth):
        if link.rel == 'service-desc':
            self.add(link.href, 'description', 'service_desc_link', parent, depth, P_DESCRIPTION_TYPED)
        elif link.rel == 'service-doc':
            self.add(link.href, 'page', 'service_doc_link', parent, depth, P_DOCUMENTATION_TYPED)
        elif link.rel == 'api-catalog':
            self.add(link.href, 'catalog', 'api_catalog_link', parent, depth, P_CATALOG_ITEM)

    def _ingest_catalog(self, fetch: FetchResult, lead: Lead):
        catalog = parse_linkset(fetch.content, fetch.final_url)
        if catalog is None:
            return
        target_path = urlsplit(self.target.normalized_url).path
        for entry in catalog.entries:
            if self._catalog_entries >= self.limits.max_catalog_entries:
                self._note_limit('catalog_entry_limit')
                break
            self._catalog_entries += 1
            related, penalty = True, 0
            if entry.anchor:
                anchor = urlsplit(entry.anchor)
                related = bool(anchor.hostname and hosts_related(self.target.hostname, anchor.hostname))
                if related and anchor.path.strip('/') and target_path.strip('/') and not target_path.startswith(anchor.path.rstrip('/')):
                    penalty = 10  # an API at another path on the same host: possible, but a weaker lead
            if not related:
                self._trail_add(Lead(entry.anchor or fetch.final_url, 'catalog_entry', 'catalog_entry', fetch.final_url,
                                     lead.depth + 1, 0), 'skipped_unrelated')
                continue
            for link in entry.descriptions:
                self.add(link.href, 'description', 'api_catalog', fetch.final_url, lead.depth + 1, P_DESCRIPTION_TYPED + penalty)
            for link in entry.documentation:
                self.add(link.href, 'page', 'service_doc_link', fetch.final_url, lead.depth + 1, P_DOCUMENTATION_TYPED + penalty)
            for link in entry.items:
                self.add(link.href, 'catalog', 'catalog_item', fetch.final_url, lead.depth + 1, P_CATALOG_ITEM + penalty)

    def _handle_viewer(self, scan: ViewerScan, fetch: FetchResult, lead: Lead, base: str):
        depth = lead.depth + 1
        for ref in scan.description_urls:
            self.add(urljoin(base, ref.url), 'description', ref.mechanism, fetch.final_url, depth, P_VIEWER_DESCRIPTION)
        for config in scan.config_urls:
            self.add(urljoin(base, config), 'config', 'swagger_config_url', fetch.final_url, depth, P_VIEWER_CONFIG, base)
        for spec in scan.embedded_specs:
            self._embedded(spec, fetch, lead)
        if scan.unsupported:  # one finding per source, with every reason
            self._artifacts.append(ArtifactFinding(
                'unsupported_dynamic_configuration', 'swagger_ui_configuration', fetch.final_url,
                '; '.join(f'{code}: {detail}' for code, detail in scan.unsupported), lead.mechanism, lead.parent))

    def _ingest_config(self, fetch: FetchResult, lead: Lead):
        scan = scan_swagger_config_json(fetch.content)
        if scan is None:
            return
        self._handle_viewer(scan, fetch, lead, lead.base or fetch.final_url)

    def _ingest_script(self, fetch: FetchResult, lead: Lead):
        text = self._text(fetch)
        if lead.mechanism == 'requirejs_data_main':
            for url in scan_requirejs_main(text, fetch.final_url):
                self._artifacts.append(ArtifactFinding(
                    DOCUMENTATION_ONLY, 'apidoc', url,
                    'apiDoc documentation data, referenced by a RequireJS data-main script. It is not an OpenAPI '
                    'contract and no apiDoc adapter exists.', lead.mechanism, fetch.final_url))
            return
        self._handle_viewer(scan_viewer_script(text), fetch, lead, lead.base or fetch.final_url)

    def _ingest_page(self, fetch: FetchResult, lead: Lead):
        text = self._text(fetch)
        final, depth = fetch.final_url, lead.depth + 1
        scan = scan_page(text, final)
        self._pages.append(PageRecord(fetch, scan, lead.depth))
        for link in html_typed_links(text, final):
            self._typed(link, final, depth)
        spec_links, _ = extract_document_links(fetch, DocumentationLimits())
        for candidate in spec_links:
            if candidate.discovery_method == 'documentation_link':
                self.add(candidate.source_url, 'description', 'documentation_link', final, depth, P_DESCRIPTION_LINK)
        for url in scan.spec_attributes:
            self.add(url, 'description', 'spec_url_attribute', final, depth, P_VIEWER_DESCRIPTION)
        for script in scan.inline_scripts:
            self._handle_viewer(scan_viewer_script(script), fetch, lead, scan.base_url)
        initializers = [u for u in scan.script_srcs if is_initializer(u)][:self.limits.max_initializers_per_page]
        for url in initializers:
            self.add(url, 'script', 'viewer_initializer', final, depth, P_SCRIPT, scan.base_url)
        for url in scan.data_main:
            self.add(url, 'script', 'requirejs_data_main', final, depth, P_SCRIPT, scan.base_url)
        if self.explore:
            self._follow_navigation(scan, final, depth)

    def _follow_navigation(self, scan: PageScan, page_url: str, depth: int):
        own = _host(page_url)
        scored = []
        for url, label in scan.anchors:
            key = _key(url)
            if key is None or key == _key(page_url) or key in self._visited or key in self._leads:
                continue
            score = score_link(url, label)
            cross = _host(url) != own
            if score >= (MIN_CROSS_ORIGIN_SCORE if cross else MIN_SCORE):
                scored.append((-score, len(scored), url))
        for negative_score, _, url in sorted(scored)[:self.limits.max_links_per_page]:
            self.add(url, 'page', 'documentation_navigation', page_url, depth, P_NAVIGATION + negative_score)

    # -- results ---------------------------------------------------------------------------------
    def take_candidates(self) -> tuple[RetrievedCandidate, ...]:
        """Candidates found since the last call."""
        new = tuple(self._candidates[self._returned:])
        self._returned = len(self._candidates)
        return new

    def result(self) -> NavigationResult:
        unexamined = tuple(r for r in self._trail if r.outcome in UNEXAMINED)
        return NavigationResult(
            tuple(self._candidates), tuple(self._pages), tuple(self._artifacts), tuple(self._trail),
            tuple(self._fetch_log), tuple(self._limits_reached), unexamined)


def navigate(request: DiscoveryRequest, budget: DiscoveryBudget, *, seed_pages: Sequence[FetchResult] = (),
             limits: NavigationLimits | None = None, allow_loopback: bool = False, cache: dict | None = None,
             explore: bool = True) -> NavigationResult:
    """Run one navigation pass. With explore=False only explicit evidence is followed (seed pages, catalogue,
    typed links, viewer configuration); with explore=True the origin's pages and framework probes are tried too."""
    navigator = Navigator(request, budget, limits=limits, allow_loopback=allow_loopback, cache=cache, explore=explore)
    navigator.seed(seed_pages)
    navigator.run()
    return navigator.result()
