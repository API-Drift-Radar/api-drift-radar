"""Sample discovery outcomes for the API and web-interface owners, generated from the real code.

The files in docs/examples/discovery-outcomes are produced by running `discover` against a fake
network and serialising the result, so they cannot drift from the implementation: this test fails
when they differ. Regenerate them deliberately with `UPDATE_SAMPLES=1 python -m pytest ...`.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from radar.discovery.limits import BudgetExceeded
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.limits import FetchLimits
from radar.discovery.orchestrator import discover, select_candidate
from radar.discovery.serialization import outcome_to_dict, rejection_from_text
from radar.domain.discovery import DiscoveryRequest

SAMPLES = Path(__file__).resolve().parents[4] / 'docs' / 'examples' / 'discovery-outcomes'
HOST = 'https://api.acme.com'
RETRIEVED = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def contract(title='Acme Pets API', server=HOST + '/v1', **extra):
    return {'openapi': '3.0.3', 'info': {'title': title, 'version': '1.0.0'}, 'servers': [{'url': server}],
            'paths': {'/pets': {'get': {}, 'post': {}}, '/pets/{id}': {'get': {}, 'delete': {}}}, **extra}


def body(value):
    return value if isinstance(value, bytes) else json.dumps(value, indent=2).encode()


class Network:
    """URL -> bytes/dict (200) | ('status', code) | ('html', text)."""

    def __init__(self, files):
        self.files = {k if '://' in k else HOST + k: v for k, v in files.items()}

    def fetch(self, url, budget, **options):
        try:
            budget.claim_request()
        except BudgetExceeded as error:  # as the real fetcher does: a refused request is a failed fetch
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(error.code, 'limit'))
        value = self.files.get(url, ('status', 404))
        if isinstance(value, tuple) and value[0] == 'status':
            return FetchResult(url, url, value[1], None, None, None, (FetchAttempt(url, value[1]),),
                               FetchFailure('http_error', f'HTTP status {value[1]}.'))
        if isinstance(value, tuple) and value[0] == 'js':
            media, content = 'application/javascript', value[1].encode()
        elif isinstance(value, tuple):
            media, content = 'text/html', value[1].encode()
        else:
            media, content = 'application/json', body(value)
        return FetchResult(url, url, 200, media, content, RETRIEVED, (FetchAttempt(url, 200),))


def run(files, target='api.acme.com', registry=None, tmp_path=None, discover_options=None, **hints):
    options = dict(discover_options or {})
    if registry:
        path = tmp_path / 'providers.json'
        path.write_text(json.dumps({'schema_version': 1, 'providers': registry}))
        options['registry_path'] = path
    network = Network(files)
    with patch('radar.discovery.candidates.fetch_document', side_effect=network.fetch), \
            patch('radar.discovery.capture.fetch_document', side_effect=network.fetch):
        return discover(DiscoveryRequest(target, **hints), **options)


def mapping(id, url, version):
    return {'id': id, 'hosts': ['api.acme.com'], 'api_version': version, 'spec_url': url,
            'provenance_url': 'https://github.com/acme/openapi'}


SWAGGER2 = {'swagger': '2.0', 'info': {'title': 'Legacy', 'version': '1'}, 'paths': {}}
WITH_REFS = contract(paths={'/pets': {'get': {'responses': {'200': {'description': 'ok', 'content': {
    'application/json': {'schema': {'$ref': 'schemas/pet.json#/Pet'}}}}}}}})
PET = {'Pet': {'type': 'object', 'properties': {'owner': {'$ref': 'owner.json'}}}}
OWNER = {'type': 'object', 'properties': {'name': {'type': 'string'}}}
SPEC_V1, SPEC_V2 = 'https://specs.acme.com/2022-11-28.json', 'https://specs.acme.com/2026-03-10.json'
TWO_VERSIONS = [mapping('acme-2022-11-28', SPEC_V1, '2022-11-28'), mapping('acme-2026-03-10', SPEC_V2, '2026-03-10')]
TWO_FILES = {SPEC_V1: contract(), SPEC_V2: contract(paths={'/pets': {'get': {}}, '/owners': {'get': {}}})}


SWAGGER_SITE = {
    '/docs': ('html', '<script src="/docs/swagger-initializer.js"></script>'),
    '/docs/swagger-initializer.js': ('js', 'SwaggerUIBundle({configUrl: "/docs/swagger-config"})'),
    '/docs/swagger-config': {'urls': [{'url': '/v1/openapi.json', 'name': 'v1'}, {'url': '/v2/openapi.json', 'name': 'v2'}]},
    '/v1/openapi.json': contract(server=HOST + '/v1'),
    '/v2/openapi.json': contract(server=HOST + '/v2', paths={'/pets': {'get': {}}, '/owners': {'get': {}}}),
}
APIDOC_SITE = {
    '/doc/index.html': ('html', '<script src="vendor/require.min.js" data-main="main.js"></script>'),
    '/doc/main.js': ('js', "require(['./api_project.js', './api_data.js'], function () {});"),
}
GOOGLE_DISCOVERY = {'kind': 'discovery#restDescription', 'name': 'pets', 'version': 'v1', 'resources': {}}
CATALOGUE = {'linkset': [{'anchor': HOST + '/v1', 'service-desc': [{'href': '/specs/pets.json', 'type': 'application/openapi+json'}]},
                         {'anchor': 'https://unrelated.example/api', 'service-desc': [{'href': 'https://unrelated.example/s.json'}]}]}


def scenarios(tmp_path):
    """name -> (what it shows, request, outcome)"""
    def request(target, **hints):
        return {'target': target, **hints}

    ambiguous = run({'/openapi.json': contract(), '/swagger.json': contract(title='Acme Admin API', paths={'/admin': {'get': {}}})})
    yield ('validated-common-location',
           'One contract found at a guessed location, checked against an operation (GET /v1/pets).',
           request('https://api.acme.com/v1/pets', method='GET'),
           run({'/openapi.json': contract()}, 'https://api.acme.com/v1/pets', method='GET'))
    yield ('validated-direct-url',
           'The target is itself a specification file. It is fetched directly, nothing else is searched, and '
           'servers declared on other hosts are not held against it.',
           request('https://cdn.acme.com/specs/pets.json'),
           run({'https://cdn.acme.com/specs/pets.json': contract(), '/openapi.json': contract(title='Unrelated')},
               'https://cdn.acme.com/specs/pets.json'))
    yield ('validated-with-referenced-documents',
           'A multi-file contract: the root plus the two files it depends on, each listed with size and SHA-256.',
           request('api.acme.com'),
           run({'/openapi.json': WITH_REFS, '/schemas/pet.json': PET, '/schemas/owner.json': OWNER}))
    yield ('validated-provider-version-selected',
           'A provider publishes one contract per API version. Supplying api_version selects one; the other is '
           'listed in candidates with the reason it was rejected.',
           request('api.acme.com', api_version='2026-03-10'),
           run(TWO_FILES, registry=TWO_VERSIONS, tmp_path=tmp_path, api_version='2026-03-10'))
    yield ('ambiguous-two-contracts',
           'Two distinct contracts fit. Nothing is chosen; both are in alternatives.',
           request('api.acme.com'), ambiguous)
    yield ('ambiguous-resolved-by-selection',
           'The same result after the user picks one alternative (select_candidate: no re-fetch).',
           request('api.acme.com'), select_candidate(ambiguous, HOST + '/swagger.json'))
    yield ('ambiguous-provider-versions',
           'No version requested, so each dated provider version is an alternative.',
           request('api.acme.com'), run(TWO_FILES, registry=TWO_VERSIONS, tmp_path=tmp_path))
    yield ('rejected-at-each-stage',
           'Documents were found but none qualified: one fails validation, one fails matching, one has an '
           'incomplete reference. Each rejection names its stage and code.',
           request('https://api.acme.com/v1/pets', method='GET'),
           run({'/openapi.json': contract(server='https://api.other.test/v1'), '/swagger.json': SWAGGER2,
                '/openapi.yaml': contract(paths={'/pets': {'get': {'x': {'$ref': 'gone.json'}}}})},
               'https://api.acme.com/v1/pets', method='GET'))
    yield ('validated-via-api-catalog',
           'Found through an RFC 9727 catalogue. The trail records each step with its parent and mechanism; an '
           'unrelated catalogue entry is skipped and recorded.',
           request('api.acme.com'), run({'/.well-known/api-catalog': CATALOGUE, '/specs/pets.json': contract()}))
    yield ('ambiguous-swagger-config-versions',
           'A Swagger UI initializer points at a configuration file listing two versions. Both are alternatives.',
           request('api.acme.com'), run(SWAGGER_SITE))
    yield ('not-found-apidoc-documentation-only',
           'Documentation data (apiDoc) was found but it is not an OpenAPI contract: documentation only, nothing '
           'accepted. Not proof that no contract exists.',
           request('https://api.acme.com/api/fruit/apple', method='GET'),
           run(APIDOC_SITE, 'https://api.acme.com/api/fruit/apple', method='GET',
               discover_options={'documentation_urls': [HOST + '/doc/index.html']}))
    yield ('rejected-unsupported-format',
           'A formal description was found in a format Radar recognises but does not support (Google Discovery). '
           'It is reported as such, not as a malformed OpenAPI file.',
           request('api.acme.com'), run({'/openapi.json': GOOGLE_DISCOVERY}))
    yield ('inaccessible-authentication-required',
           'A conventional description location answered 401. Whether a contract exists behind it is unknown.',
           request('api.acme.com'), run({'/v3/api-docs': ('status', 401)}))
    yield ('not-found-search-cut-short',
           'The search ran out of its navigation budget. `coverage` says which limit and what was left unexamined.',
           request('api.acme.com'),
           run({'/': ('html', ''.join(f'<a href="/developers/{i}">Developer documentation {i}</a>' for i in range(6)))},
               discover_options={'limits': FetchLimits(max_requests=12)}))
    yield ('inaccessible',
           'Nothing qualified and a source answered with an error, so a contract may exist behind it.',
           request('api.acme.com'), run({'/openapi.json': ('status', 503), '/swagger.json': ('status', 403)}))
    yield ('not-found',
           'Every place tried answered with a clean miss. This is not proof that no contract is published.',
           request('api.acme.com'),
           run({'/openapi.json': ('html', '<!doctype html><html><body>App</body></html>')}))


def render(description, request, outcome):
    return json.dumps({'description': description, 'request': request, 'outcome': outcome_to_dict(outcome)},
                      indent=2, ensure_ascii=False) + '\n'


def test_sample_files_match_what_the_code_produces(tmp_path):
    update = os.environ.get('UPDATE_SAMPLES') == '1'
    names = set()
    for name, description, request, outcome in scenarios(tmp_path):
        names.add(f'{name}.json')
        path = SAMPLES / f'{name}.json'
        text = render(description, request, outcome)
        if update:
            SAMPLES.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        assert path.exists(), f'{path.name} is missing; run with UPDATE_SAMPLES=1'
        assert path.read_text(encoding='utf-8') == text, f'{path.name} is stale; run with UPDATE_SAMPLES=1'
    on_disk = {p.name for p in SAMPLES.glob('*.json')}
    assert on_disk == names, f'unexpected or missing sample files: {sorted(on_disk ^ names)}'


# --- the serialised shape ------------------------------------------------------

@pytest.fixture(scope='module')
def rendered(tmp_path_factory):
    return {name: json.loads(render(d, r, o)) for name, d, r, o in scenarios(tmp_path_factory.mktemp('s'))}


def test_every_status_is_represented(rendered):
    assert {s['outcome']['status'] for s in rendered.values()} == {
        'validated', 'ambiguous', 'rejected', 'inaccessible', 'not_found'}


def test_only_validated_has_a_package_and_only_ambiguous_has_alternatives(rendered):
    for name, sample in rendered.items():
        outcome = sample['outcome']
        assert (outcome['package'] is not None) == (outcome['status'] == 'validated'), name
        assert bool(outcome['alternatives']) == (outcome['status'] == 'ambiguous'), name


def test_no_contract_bytes_are_inlined_and_documents_are_described(rendered):
    package = rendered['validated-with-referenced-documents']['outcome']['package']
    assert package['root_document'].keys() == {'url', 'media_type', 'size_bytes', 'sha256', 'retrieved_at'}
    assert package['root_document']['retrieved_at'] == '2026-10-09T12:00:00Z'
    assert [d['url'] for d in package['referenced_documents']] == [
        HOST + '/schemas/pet.json', HOST + '/schemas/owner.json']
    assert len(package['content_fingerprint']) == 64 and len(package['root_document']['sha256']) == 64
    assert (package['title'], package['info_version'], package['path_count'], package['operation_count']) == (
        'Acme Pets API', '1.0.0', 1, 1)

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert 'content' not in set(keys(package)) and 'parsed_contract' not in set(keys(package))
    assert '/pets' not in json.dumps(package)  # the contract's own paths are not inlined


def test_rejections_are_structured_by_stage(rendered):
    candidates = rendered['rejected-at-each-stage']['outcome']['candidates']
    stages = sorted((c['rejections'][0]['stage'], c['rejections'][0]['code']) for c in candidates)
    assert stages == [('matching', 'server_host_mismatch'), ('reference_capture', 'reference_unavailable'),
                      ('validation', 'unsupported_version')]
    assert all(c['accepted'] is False and c['rejections'][0]['reason'] for c in candidates)
    accepted = rendered['validated-common-location']['outcome']['candidates']
    assert [c['accepted'] for c in accepted] == [True] and accepted[0]['rejections'] == []


def test_evidence_carries_the_outcome_of_each_check(rendered):
    evidence = rendered['validated-common-location']['outcome']['package']['evidence']
    assert {e['criterion']: e['outcome'] for e in evidence if e['outcome']} == {
        'server_host': 'match', 'operation': 'match', 'api_version': 'not_requested', 'product': 'not_requested',
        'provenance': 'match'}


def test_selection_keeps_the_candidates_and_notes_the_choice(rendered):
    ambiguous = rendered['ambiguous-two-contracts']['outcome']
    chosen = rendered['ambiguous-resolved-by-selection']['outcome']
    assert chosen['status'] == 'validated' and chosen['alternatives'] == []
    assert chosen['candidates'] == ambiguous['candidates']
    assert any('Selected explicitly' in note for note in chosen['limitations'])


@pytest.mark.parametrize('text,expected', [
    ('matching:server_host_mismatch: No host related.', {'stage': 'matching', 'code': 'server_host_mismatch', 'reason': 'No host related.'}),
    ('validation:html_document: It: has a colon.', {'stage': 'validation', 'code': 'html_document', 'reason': 'It: has a colon.'}),
    ('something unexpected', {'stage': None, 'code': None, 'reason': 'something unexpected'}),
    ('', {'stage': None, 'code': None, 'reason': ''}),
])
def test_rejection_text_parsing(text, expected):
    assert rejection_from_text(text) == expected


def test_the_optional_details_are_present_and_shaped_for_clients(rendered):
    for name, sample in rendered.items():
        outcome = sample['outcome']
        assert set(outcome) >= {'artifacts', 'trail', 'coverage'}, name
        assert isinstance(outcome['artifacts'], list) and isinstance(outcome['trail'], list), name
        c = outcome['coverage']
        assert set(c) == {'complete', 'limits_reached', 'leads_examined', 'leads_unexamined', 'unexamined', 'requests_used',
                          'requests_limit', 'reserved_for_references', 'hosts_contacted'}, name
        assert all(set(step) == {'url', 'parent_url', 'mechanism', 'kind', 'depth', 'outcome'} for step in outcome['trail'])
        assert all(set(a) == {'category', 'kind', 'url', 'detail', 'discovery_method', 'parent_url', 'status'}
                   for a in outcome['artifacts'])


def test_apidoc_is_documentation_only_and_never_a_package(rendered):
    outcome = rendered['not-found-apidoc-documentation-only']['outcome']
    assert outcome['status'] == 'not_found' and outcome['package'] is None and outcome['alternatives'] == []
    assert {a['kind'] for a in outcome['artifacts']} == {'apidoc'} and {a['category'] for a in outcome['artifacts']} == {'documentation_only'}


def test_unsupported_format_and_authentication_barriers_are_structured(rendered):
    google = rendered['rejected-unsupported-format']['outcome']
    assert google['status'] == 'rejected' and [(a['category'], a['kind']) for a in google['artifacts']] == [
        ('unsupported_description', 'google_discovery')]
    assert google['candidates'][0]['rejections'][0]['code'] == 'unsupported_format'
    barrier = rendered['inaccessible-authentication-required']['outcome']
    assert barrier['status'] == 'inaccessible' and [(a['category'], a['status']) for a in barrier['artifacts']] == [
        ('authentication_required', 401)]


def test_a_cut_short_search_says_which_limit_and_what_was_left(rendered):
    coverage = rendered['not-found-search-cut-short']['outcome']['coverage']
    assert coverage['complete'] is False and 'navigation_limit' in coverage['limits_reached']
    assert coverage['leads_unexamined'] > 0 and coverage['unexamined'] and coverage['reserved_for_references'] == 3
    complete = rendered['validated-common-location']['outcome']['coverage']
    assert complete['complete'] is True and complete['limits_reached'] == []


def test_the_catalogue_sample_shows_each_step_and_the_skipped_unrelated_entry(rendered):
    outcome = rendered['validated-via-api-catalog']['outcome']
    steps = {(s['mechanism'], s['outcome']) for s in outcome['trail']}
    assert ('well_known_api_catalog', 'fetched') in steps and ('api_catalog', 'fetched') in steps
    assert ('catalog_entry', 'skipped_unrelated') in steps
    assert outcome['package']['discovery_method'] == 'api_catalog'
