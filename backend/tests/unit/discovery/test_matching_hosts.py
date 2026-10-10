import pytest

from radar.discovery.input import normalize_target
from radar.discovery.matching import (
    INDETERMINATE, MATCH, MAX_SERVERS, MISMATCH, check_server_host, hosts_related,
)
from radar.domain.discovery import DiscoveryRequest


SOURCE = 'https://specs.acme.com/openapi.json'


def target(text='api.acme.com'):
    return normalize_target(DiscoveryRequest(text))


def check(servers, text='api.acme.com', **document):
    return check_server_host(target(text), {'servers': servers, **document}, SOURCE)


# --- host relation -----------------------------------------------------------

@pytest.mark.parametrize('target_host,server_host,expected', [
    ('api.acme.com', 'api.acme.com', 'same'),
    ('API.Acme.COM', 'api.acme.com.', 'same'),
    ('api.acme.com', 'acme.com', 'target_under_server'),
    ('acme.com', 'api.acme.com', 'server_under_target'),
    ('eu.api.acme.com', 'acme.com', 'target_under_server'),
    ('münchen.example', 'xn--mnchen-3ya.example', 'same'),
    ('api.münchen.example', 'münchen.example', 'target_under_server'),
    ('127.0.0.1', '127.0.0.1', 'same'),
    ('::1', '[::1]', 'same'),
    ('localhost', 'localhost', 'same'),
])
def test_related_hosts(target_host, server_host, expected):
    assert hosts_related(target_host, server_host) == expected


@pytest.mark.parametrize('target_host,server_host', [
    ('a.acme.com', 'b.acme.com'),            # siblings
    ('evilacme.com', 'acme.com'),            # string suffix, not a label boundary
    ('acme.com', 'evilacme.com'),
    ('acme.com.evil.test', 'acme.com'),
    ('com', 'acme.com'), ('acme.com', 'com'),  # single-label side never relates
    ('localhost', 'a.localhost'), ('a.localhost', 'localhost'),
    ('127.0.0.1', '127.0.0.2'), ('127.0.0.1', 'acme.com'), ('10.0.0.1', '0.0.0.1'),
    ('acme.com', ''), ('', 'acme.com'), ('acme.com', 'bad host'), ('acme.com', 'a..com'), ('acme.com', None),
    ('acme.com', 'x' * 300 + '.com'),
])
def test_unrelated_hosts(target_host, server_host):
    assert hosts_related(target_host, server_host) is None


# --- server host check -------------------------------------------------------

def test_absolute_server_with_the_same_host_matches():
    result = check([{'url': 'https://api.acme.com/v1'}])
    assert (result.criterion, result.outcome, result.source_url) == ('server_host', MATCH, SOURCE)
    assert 'the same host as' in result.description


def test_parent_and_subdomain_servers_match_with_the_relation_stated():
    assert 'a parent domain of' in check([{'url': 'https://acme.com'}]).description
    assert 'a subdomain of' in check([{'url': 'https://api.acme.com'}], 'acme.com').description


def test_any_related_server_is_enough():
    assert check([{'url': 'https://sandbox.other.test'}, {'url': 'https://api.acme.com'}, {'url': '/v1'}]).outcome == MATCH


def test_unrelated_absolute_servers_are_a_mismatch():
    result = check([{'url': 'https://api.other.test/v1'}, {'url': 'https://eu.other.test'}])
    assert result.outcome == MISMATCH and 'api.other.test' in result.description


def test_lookalike_hosts_do_not_match():
    for url in ('https://api.acme.com.evil.test', 'https://evilacme.com', 'https://b.acme.com', 'https://acme.org'):
        assert check([{'url': url}]).outcome == MISMATCH, url


def test_relative_or_missing_servers_cannot_be_compared():
    for servers in ([{'url': '/v1'}], [{'url': '/'}, {'url': 'v2'}], [], None):
        assert check(servers).outcome == INDETERMINATE
    assert check_server_host(target(), {}, SOURCE).outcome == INDETERMINATE


def test_a_relative_server_keeps_unrelated_absolute_servers_from_being_a_mismatch():
    assert check([{'url': 'https://other.test'}, {'url': '/v1'}]).outcome == INDETERMINATE


def test_variable_defaults_and_enum_values_are_expanded():
    servers = [{'url': 'https://{region}.acme.com/v1', 'variables': {'region': {'default': 'us', 'enum': ['us', 'eu']}}}]
    assert check(servers, 'eu.acme.com').outcome == MATCH
    assert check(servers, 'us.acme.com').outcome == MATCH
    assert check(servers, 'ap.acme.com').outcome == MISMATCH  # a sibling region is not related
    assert check(servers, 'acme.com').outcome == MATCH        # the parent domain is
    assert check(servers, 'ap.other.test').outcome == MISMATCH
    only_default = [{'url': 'https://{env}.acme.org', 'variables': {'env': {'default': 'api'}}}]
    assert check(only_default, 'api.acme.org').outcome == MATCH and check(only_default, 'eu.acme.org').outcome == MISMATCH


def test_variables_without_a_usable_value_are_indeterminate():
    for servers in (
        [{'url': 'https://{host}/v1'}],
        [{'url': 'https://{host}/v1', 'variables': {'host': {}}}],
        [{'url': 'https://{host}/v1', 'variables': {'host': 'x'}}],
        [{'url': 'https://{host}/v1', 'variables': {'host': {'default': 3}}}],
        [{'url': 'https://{host}/v1', 'variables': {'host': {'enum': []}}}],
    ):
        assert check(servers, 'api.other.test').outcome == INDETERMINATE, servers


def test_variables_only_in_the_path_still_allow_a_host_decision():
    servers = [{'url': 'https://api.other.test/{version}'}]
    assert check(servers).outcome == INDETERMINATE  # variable has no value, so the server cannot be ruled out
    assert check([{'url': 'https://api.other.test/{v}', 'variables': {'v': {'default': 'v1'}}}]).outcome == MISMATCH


def test_too_many_variable_combinations_are_not_expanded():
    values = [str(i) for i in range(10)]
    servers = [{'url': 'https://{a}{b}.other.test', 'variables': {'a': {'enum': values, 'default': '0'},
                                                                  'b': {'enum': values, 'default': '0'}}}]
    assert check(servers, 'api.acme.com').outcome == INDETERMINATE


def test_ports_must_agree_when_both_sides_state_one():
    assert check([{'url': 'http://api.acme.com:8080'}], 'http://api.acme.com:8080').outcome == MATCH
    assert check([{'url': 'http://api.acme.com:9000'}], 'http://api.acme.com:8080').outcome == MISMATCH
    assert check([{'url': 'http://api.acme.com:9000'}], 'api.acme.com').outcome == MATCH  # target gives no port
    assert check([{'url': 'http://api.acme.com'}], 'http://api.acme.com:8080').outcome == MATCH


def test_ip_literal_servers():
    assert check([{'url': 'http://127.0.0.1:8765/v1'}], 'http://127.0.0.1:8765').outcome == MATCH
    assert check([{'url': 'http://127.0.0.2'}], 'http://127.0.0.1:8765').outcome == MISMATCH
    assert check([{'url': 'http://[::1]:8765'}], 'http://[::1]:8765').outcome == MATCH


def test_malformed_server_entries_cannot_be_ruled_out():
    for servers in ([{'url': 3}], ['x'], [{}], [{'url': 'http://[bad'}], [{'url': 'https://api.other.test:abc'}]):
        assert check(servers, 'api.acme.com').outcome == INDETERMINATE, servers


def test_only_the_first_servers_are_compared_and_truncation_blocks_a_mismatch():
    servers = [{'url': f'https://h{i}.other.test'} for i in range(MAX_SERVERS)] + [{'url': 'https://api.acme.com'}]
    result = check(servers)
    assert result.outcome == INDETERMINATE and str(MAX_SERVERS) in result.description


def test_hostile_text_is_bounded_in_descriptions():
    result = check([{'url': 'https://' + 'a' * 5000 + '.other.test/\n\x00'}])
    assert len(result.description) < 400 and '\n' not in result.description


def test_check_is_deterministic():
    servers = [{'url': 'https://api.acme.com'}, {'url': '/x'}]
    assert check(servers) == check(servers)
