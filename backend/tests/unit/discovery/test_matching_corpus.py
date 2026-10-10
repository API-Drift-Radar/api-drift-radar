"""Labeled corpus for relevance matching: does it accept the right contracts and reject look-alikes?

Every case has an IDEAL label (what a careful human would decide) and the verdict the rules give.
The corpus asserts zero false accepts and zero false rejects among the ordinary cases, and pins the
KNOWN LIMITATIONS: cases where the explicit rules deliberately differ from the ideal. If a rule
changes, a pinned limitation flips and this file has to be updated on purpose. Run with `-s` to see
the table.
"""

import json
from dataclasses import dataclass, field

import pytest

from radar.discovery.input import normalize_target
from radar.discovery.matching import MatchContext, assess_match
from radar.discovery.validation import validate_document
from radar.domain.discovery import DiscoveryRequest

ACCEPT, REJECT = 'accept', 'reject'
STANDARD_PATHS = {'/pets': {'get': {}, 'post': {}}, '/pets/{id}': {'get': {}, 'delete': {}}}


@dataclass
class Case:
    name: str
    ideal: str
    target: str = 'https://api.acme.com'
    method: str | None = None
    version: str | None = None
    product: str | None = None
    servers: list = field(default_factory=lambda: [{'url': 'https://api.acme.com/v1'}])
    paths: dict = field(default_factory=lambda: STANDARD_PATHS)
    title: str = 'Acme Pets API'
    info_version: str = '1.0.0'
    tags: list | None = None
    how: str = 'common_location'
    source: str = 'https://api.acme.com/openapi.json'
    via: str | None = None
    mapping_version: str | None = None
    extra: dict = field(default_factory=dict)
    note: str = ''


def build(case):
    document = {'openapi': '3.0.3', 'info': {'title': case.title, 'version': case.info_version},
                'paths': case.paths, **case.extra}
    if case.servers is not None:
        document['servers'] = case.servers
    if case.tags is not None:
        document['tags'] = case.tags
    validation = validate_document(json.dumps(document).encode())
    assert validation.ok, (case.name, validation.rejection)
    target = normalize_target(DiscoveryRequest(case.target, method=case.method, api_version=case.version,
                                               product=case.product))
    context = MatchContext(target, case.source, case.how, case.via or case.source, case.mapping_version)
    return assess_match(context, validation)


ORDINARY = [
    # --- right contracts: must be accepted -------------------------------------------------------------
    Case('same host', ACCEPT),
    Case('provider domain, api subdomain declared', ACCEPT, target='https://acme.com', source='https://acme.com/openapi.json'),
    Case('api host, parent domain declared', ACCEPT, servers=[{'url': 'https://acme.com/api'}]),
    Case('production and sandbox servers', ACCEPT, servers=[{'url': 'https://sandbox.example.test'}, {'url': 'https://api.acme.com'}]),
    Case('region variable with enum', ACCEPT, target='https://eu.acme.com', source='https://eu.acme.com/openapi.json',
         servers=[{'url': 'https://{r}.acme.com', 'variables': {'r': {'default': 'us', 'enum': ['us', 'eu']}}}]),
    Case('relative server, served from the target host', ACCEPT, servers=[{'url': '/v1'}]),
    Case('method and templated path', ACCEPT, target='https://api.acme.com/v1/pets/42', method='GET'),
    Case('base path stripped', ACCEPT, target='https://api.acme.com/v1/pets', method='POST'),
    Case('path without method that exists', ACCEPT, target='https://api.acme.com/v1/pets'),
    Case('docs URL path without a method', ACCEPT, target='https://api.acme.com/docs/guide'),
    Case('version from server path', ACCEPT, version='2', servers=[{'url': 'https://api.acme.com/v2'}]),
    Case('version from provider mapping', ACCEPT, version='2022-11-28', mapping_version='2022-11-28', how='provider_mapping',
         source='https://raw.example.test/s.json', via='https://github.com/acme'),
    Case('dated version prefix in info.version', ACCEPT, version='2026-09-30', info_version='2026-09-30.endive',
         servers=[{'url': 'https://api.acme.com'}]),
    Case('product in title, plural folded', ACCEPT, product='pet', title='Acme Pets API'),
    Case('product in a tag', ACCEPT, product='adoption', tags=[{'name': 'Adoption'}]),
    Case('ip address and port', ACCEPT, target='http://127.0.0.1:8765', source='http://127.0.0.1:8765/openapi.json',
         servers=[{'url': 'http://127.0.0.1:8765/v1'}]),
    Case('operation hidden behind a $ref path item', ACCEPT, target='https://api.acme.com/orders', method='GET',
         paths={**STANDARD_PATHS, '/orders': {'$ref': 'orders.yaml'}}, servers=[{'url': 'https://api.acme.com'}]),
    Case('contract on a cdn with the right declared host', ACCEPT, source='https://cdn.example.test/acme.json',
         how='documentation_link', via='https://api.acme.com/docs'),
    Case('internationalised host', ACCEPT, target='https://münchen.example', source='https://münchen.example/o.json',
         servers=[{'url': 'https://xn--mnchen-3ya.example'}]),
    Case('version requested but the contract is silent', ACCEPT, version='7', servers=[{'url': 'https://api.acme.com'}]),
    Case('non-version hint with matching info.version', ACCEPT, version='latest', info_version='latest',
         servers=[{'url': 'https://api.acme.com/v1'}]),
    # --- look-alikes and wrong contracts: must be rejected ---------------------------------------------
    Case('different provider', REJECT, servers=[{'url': 'https://api.other.test/v1'}]),
    Case('sibling subdomain', REJECT, servers=[{'url': 'https://billing.acme.com'}]),
    Case('string suffix, not a subdomain', REJECT, servers=[{'url': 'https://evilacme.com'}]),
    Case('target ends with the declared host, without a dot', REJECT, target='https://notacme.com',
         source='https://notacme.com/o.json', servers=[{'url': 'https://acme.com'}]),
    Case('declared host ends with the target, without a dot', REJECT, target='https://acme.com',
         source='https://acme.com/o.json', servers=[{'url': 'https://evilacme.com'}]),
    Case('target embedded in an attacker domain', REJECT, servers=[{'url': 'https://api.acme.com.evil.test'}]),
    Case('same name, different tld', REJECT, servers=[{'url': 'https://api.acme.org'}]),
    Case('different ip address', REJECT, target='http://127.0.0.1:8765', source='http://127.0.0.1:8765/o.json',
         servers=[{'url': 'http://127.0.0.2:8765'}]),
    Case('same host, different port', REJECT, target='http://api.acme.com:8080', source='http://api.acme.com:8080/o.json',
         servers=[{'url': 'http://api.acme.com:9000'}]),
    Case('method not defined for the path', REJECT, target='https://api.acme.com/v1/pets', method='DELETE'),
    Case('path not defined', REJECT, target='https://api.acme.com/v1/orders', method='GET'),
    Case('only under the wrong base path', REJECT, target='https://api.acme.com/v2/pets', method='GET'),
    Case('version contradicted by server path', REJECT, version='v3', servers=[{'url': 'https://api.acme.com/v1'}]),
    Case('version contradicted by provider mapping', REJECT, version='2026-03-10', mapping_version='2022-11-28',
         how='provider_mapping', source='https://raw.example.test/s.json', via='https://github.com/acme'),
    Case('info.version coincidence must not rescue /v10', REJECT, version='1', info_version='1.0.0',
         servers=[{'url': 'https://api.acme.com/v10'}]),
    Case('product absent from the contract', REJECT, product='shipping'),
    Case('operation only under another host\'s base path', REJECT, target='https://api.acme.com/pets-api/pets', method='GET',
         servers=[{'url': 'https://api.other.test/pets-api'}, {'url': 'https://api.acme.com'}]),
    Case('webhook is not an endpoint', REJECT, target='https://api.acme.com/hook', method='POST',
         servers=[{'url': 'https://api.acme.com'}], paths=STANDARD_PATHS,
         extra={'openapi': '3.1.0', 'webhooks': {'/hook': {'post': {}}}}),
]

# Known limitations. `rules` is what the explicit rules decide today; `ideal` is the better answer.
LIMITATIONS = [
    (Case('sub-product hint absent from a multi-product spec', ACCEPT, product='billing', title='Acme API',
          note='the contract text never names the sub-product'), REJECT),
    (Case('canonical API domain differs from the provider domain', ACCEPT, target='https://acme.com',
          source='https://acme.com/openapi.json', servers=[{'url': 'https://api.acme-cloud.net'}],
          note='needs a mapping to declare expected API hosts'), REJECT),
    (Case('product written without the hyphen', ACCEPT, product='ecommerce', title='E-Commerce Orders',
          note='whole-word text match'), REJECT),
    (Case('shared hosting suffix as the target', REJECT, target='https://github.io', source='https://github.io/o.json',
          servers=[{'url': 'https://someone-else.github.io'}], note='no public-suffix list'), ACCEPT),
]


def verdict(result):
    return ACCEPT if result.accepted else REJECT


def test_corpus_has_no_false_accepts_and_no_false_rejects(capsys):
    rows, false_accepts, false_rejects = [], [], []
    for case in ORDINARY:
        result = build(case)
        got = verdict(result)
        rows.append((case.name, case.ideal, got, ','.join(result.positive) or '-'))
        if got != case.ideal:
            (false_accepts if got == ACCEPT else false_rejects).append(case.name)
    accepts = [r for r in rows if r[1] == ACCEPT]
    rejects = [r for r in rows if r[1] == REJECT]
    print(f'\n{"case":58} {"ideal":7} {"rules":7} positive evidence')
    for name, ideal, got, positive in rows:
        print(f'{name:58} {ideal:7} {got:7} {positive}{"   <-- WRONG" if ideal != got else ""}')
    print(f'\n{len(ORDINARY)} cases | should accept: {len(accepts)} | should reject: {len(rejects)} | '
          f'false accepts: {len(false_accepts)} | false rejects: {len(false_rejects)}')
    assert false_accepts == [] and false_rejects == []
    assert len(accepts) >= 18 and len(rejects) >= 17


@pytest.mark.parametrize('case,rules', LIMITATIONS, ids=[c.name for c, _ in LIMITATIONS])
def test_known_limitations_stay_as_documented(case, rules):
    assert case.ideal != rules  # a limitation, by definition, differs from the ideal
    assert verdict(build(case)) == rules


def test_accepted_contracts_always_say_what_was_and_was_not_established():
    for case in ORDINARY:
        result = build(case)
        if result.accepted:
            assert result.limitations and len(result.checks) == 5
            assert all(c.description for c in result.checks)


def test_every_rejection_names_a_cause_a_user_can_act_on():
    for case in ORDINARY:
        result = build(case)
        if not result.accepted:
            assert result.rejection.code in {'server_host_mismatch', 'operation_not_found', 'version_mismatch',
                                             'product_mismatch'}
            assert result.rejection.reason and result.rejection.stage == 'matching'
