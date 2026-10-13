"""The APIs.guru directory: a lead generator whose entries are validated and matched like any other candidate."""

from datetime import datetime, timezone
import json
from unittest.mock import patch

from radar.discovery.directory import MAX_ENTRIES, provider_keys, search_directory
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.limits import BudgetExceeded, DiscoveryBudget, FetchLimits
from radar.discovery.orchestrator import discover
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
INDEX = 'https://api.apis.guru/v2/acme.com.json'
ORIGIN = 'https://raw.githubusercontent.com/acme/specs/main/openapi.json'
COPY = 'https://api.apis.guru/v2/specs/acme.com/1.0.0/openapi.json'


def contract(server='https://api.acme.com/v1', title='Acme'):
    return {'openapi': '3.0.3', 'info': {'title': title, 'version': '1'}, 'servers': [{'url': server}],
            'paths': {'/things': {'get': {'responses': {'200': {'description': 'ok'}}}}}}


def listing(**entries):
    return {'apis': entries}


def entry(origin=ORIGIN, copy=COPY, fmt='openapi', version='3.0', title='Acme API', ver='3.0.3'):
    info = {'title': title, 'version': '1'}
    if origin:
        info['x-origin'] = [{'format': fmt, 'url': origin, 'version': version}]
    return {'info': info, 'swaggerUrl': copy, 'openapiVer': ver}


class Network:
    def __init__(self, files):
        self.files, self.calls = files, []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        try:
            budget.claim_request()
        except BudgetExceeded as error:
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(error.code, 'limit'))
        value = self.files.get(url)
        if value is None:
            return FetchResult(url, url, 404, None, None, None, (), FetchFailure('http_error', 'HTTP status 404.'))
        return FetchResult(url, url, 200, 'application/json', json.dumps(value).encode(), NOW, (FetchAttempt(url, 200),))


def run(files, target='api.acme.com', **kwargs):
    network = Network(files)
    with patch('radar.discovery.candidates.fetch_document', side_effect=network.fetch), \
            patch('radar.discovery.capture.fetch_document', side_effect=network.fetch):
        outcome = discover(DiscoveryRequest(target), **kwargs)
    return outcome, network


def test_provider_keys_walk_up_the_host_but_never_to_a_bare_suffix():
    assert provider_keys('api.twilio.com') == ('api.twilio.com', 'twilio.com')
    assert provider_keys('twilio.com') == ('twilio.com',)
    assert provider_keys('a.b.c.example.org') == ('a.b.c.example.org', 'b.c.example.org', 'c.example.org')
    assert provider_keys('localhost') == () and provider_keys('127.0.0.1') == () and provider_keys('') == ()


def test_a_provider_listed_in_the_directory_is_found_through_its_own_source():
    outcome, network = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED
    assert outcome.package.candidate.source_url == ORIGIN and outcome.package.candidate.discovery_method == 'directory'
    assert COPY not in network.calls  # the directory's own (older) copy is not used when the source works
    provenance = next(e for e in outcome.package.candidate.evidence if e.criterion == 'provenance')
    assert provenance.outcome == 'indeterminate'  # a directory is a third party: never positive provenance


def test_the_directory_is_consulted_only_when_nothing_was_accepted_earlier():
    files = {'https://api.acme.com/openapi.json': contract(), INDEX: listing(**{'acme.com': entry()})}
    outcome, network = run(files)
    assert outcome.status is DiscoveryStatus.VALIDATED and INDEX not in network.calls


def test_it_can_be_switched_off():
    outcome, network = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: contract()}, use_directory=False)
    assert outcome.status is DiscoveryStatus.NOT_FOUND and not [c for c in network.calls if 'apis.guru' in c]


def test_a_stale_source_falls_back_to_the_directorys_copy():
    outcome, network = run({INDEX: listing(**{'acme.com': entry()}), COPY: contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED and outcome.package.candidate.source_url == COPY
    assert network.calls.count(INDEX) == 1  # the listing is read once


def test_a_source_in_an_unsupported_format_falls_back_to_the_converted_copy():
    swagger2 = {'swagger': '2.0', 'info': {'title': 'Acme', 'version': '1'}, 'host': 'api.acme.com', 'paths': {}}
    outcome, _ = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: swagger2, COPY: contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED and outcome.package.candidate.source_url == COPY


def test_an_entry_with_no_source_uses_the_copy_straight_away_and_spaces_are_encoded():
    spaced = 'https://api.apis.guru/v2/specs/acme.com/some api/1.0/openapi.json'
    outcome, network = run({INDEX: listing(**{'acme.com': entry(origin=None, copy=spaced)}),
                            spaced.replace(' ', '%20'): contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED


def test_a_listing_for_another_host_is_rejected_by_matching_not_trusted():
    other = contract(server='https://api.other.example.net/v1')
    outcome, _ = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: other})
    assert outcome.status is DiscoveryStatus.REJECTED
    assert any(r.startswith('None of the declared server hosts') or 'server host' in r.lower()
               for c in outcome.candidates for r in c.rejection_reasons)


def test_the_most_specific_provider_key_with_a_listing_wins():
    specific = 'https://api.apis.guru/v2/api.acme.com.json'
    outcome, network = run({specific: listing(**{'api.acme.com': entry()}), ORIGIN: contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED and INDEX not in network.calls


def test_listings_are_bounded_and_malformed_ones_are_ignored():
    many = listing(**{f'acme.com:s{i}': entry(origin=f'https://h.example/{i}.json', copy=None) for i in range(30)})
    network = Network({INDEX: many})
    budget = DiscoveryBudget(FetchLimits(max_requests=60))
    with patch('radar.discovery.candidates.fetch_document', side_effect=network.fetch):
        step = search_directory(DiscoveryRequest('api.acme.com'), budget)
        assert len(step.search.fetches) == MAX_ENTRIES
        copies = search_directory(DiscoveryRequest('api.acme.com'), budget, copies=True)
        assert len(copies.search.fetches) == 0  # entries without a copy have nothing to fall back to
    for junk in ({'apis': []}, {'apis': {'x': 'y'}}, {'apis': {'x': {'info': 'bad'}}}, [], 'text'):
        outcome, _ = run({INDEX: junk})
        assert outcome.status is DiscoveryStatus.NOT_FOUND


def test_ips_and_single_label_hosts_never_reach_the_directory():
    for target in ('http://127.0.0.1:9', 'localhost'):
        _, network = run({}, target=target, allow_loopback=True)
        assert not [c for c in network.calls if 'apis.guru' in c]


def test_a_directory_contract_is_confirmed_when_a_requested_operation_matches_it():
    outcome, _ = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: contract()}, target='https://api.acme.com/v1/things')
    assert outcome.status is DiscoveryStatus.VALIDATED
    assert outcome.package.candidate.discovery_method == 'directory'


def test_an_unconfirmed_outcome_keeps_its_package_and_says_why():
    outcome, _ = run({INDEX: listing(**{'acme.com': entry()}), ORIGIN: contract()})
    assert outcome.status is DiscoveryStatus.UNCONFIRMED and outcome.package is not None
    assert any('nothing confirms' in note for note in outcome.limitations)


def test_a_minor_entry_cannot_stand_in_for_the_providers_main_one():
    swagger2 = {'swagger': '2.0', 'info': {'title': 'Acme', 'version': '1'}, 'host': 'api.acme.com', 'paths': {}}
    minor = 'https://raw.githubusercontent.com/acme/specs/main/plugin.json'
    files = {INDEX: listing(**{'acme.com': entry(), 'acme.com:plugin': entry(origin=minor, copy=None, title='Plugin')}),
             ORIGIN: swagger2, minor: contract(title='Plugin'), COPY: contract(title='Acme main')}
    outcome, network = run(files)
    assert outcome.status is DiscoveryStatus.UNCONFIRMED and outcome.package.parsed_contract['info']['title'] == 'Acme main'
    assert minor not in network.calls  # the main entry's copy was enough


def test_secondary_entries_are_used_only_when_the_primary_entry_yields_nothing():
    minor = 'https://raw.githubusercontent.com/acme/specs/main/plugin.json'
    files = {INDEX: listing(**{'acme.com': entry(), 'acme.com:plugin': entry(origin=minor, copy=None, title='Plugin')}),
             minor: contract(title='Plugin')}
    outcome, _ = run(files)
    assert outcome.package.parsed_contract['info']['title'] == 'Plugin'


def test_a_listing_with_no_primary_entry_treats_every_entry_as_primary():
    files = {INDEX: listing(**{'acme.com:a': entry(origin='https://h.example/a.json', copy=None, title='A'),
                               'acme.com:b': entry(origin='https://h.example/b.json', copy=None, title='B')}),
             'https://h.example/a.json': contract(title='A'), 'https://h.example/b.json': contract(title='B')}
    outcome, _ = run(files)
    assert outcome.status is DiscoveryStatus.AMBIGUOUS and len(outcome.packages) == 2


def test_the_primary_copy_is_not_fetched_when_the_primary_source_was_accepted():
    minor = 'https://raw.githubusercontent.com/acme/specs/main/plugin.json'
    files = {INDEX: listing(**{'acme.com': entry(), 'acme.com:plugin': entry(origin=minor, copy=None, title='Plugin')}),
             ORIGIN: contract(title='Acme main'), minor: contract(title='Plugin')}
    outcome, network = run(files)
    assert COPY not in network.calls


def test_many_authentication_barriers_on_one_host_are_one_finding():
    from radar.discovery.orchestrator import _collapse_barriers
    from radar.domain.discovery import ArtifactFinding
    findings = tuple(ArtifactFinding('authentication_required', 'authentication', f'https://api.acme.com/p{i}',
                                     'The source answered HTTP 401; whether a contract exists behind it is unknown.', 'x',
                                     None, 401) for i in range(10))
    other = ArtifactFinding('authentication_required', 'authentication', 'https://docs.acme.com/x', 'd', 'x', None, 403)
    collapsed = _collapse_barriers(findings + (other,))
    assert len(collapsed) == 2 and '9 other source(s) on api.acme.com answered HTTP 401' in collapsed[0].detail
