import pytest

from radar.discovery.limits import BUDGET_STOP_CODES, BudgetExceeded, DiscoveryBudget, FetchLimits


def claim(budget, host=None):
    try:
        budget.claim_request(host)
        return None
    except BudgetExceeded as error:
        return error.code


def test_a_quarter_of_the_requests_is_reserved_for_references_by_default():
    limits = FetchLimits(max_requests=20)
    assert limits.reserve == 5
    budget = DiscoveryBudget(limits)
    assert [claim(budget) for _ in range(15)] == [None] * 15 and budget.navigation_exhausted()
    assert claim(budget) == 'navigation_limit'  # navigation cannot touch the reserve
    with budget.reference_phase():
        assert [claim(budget) for _ in range(5)] == [None] * 5
        assert claim(budget) == 'request_limit'  # the total still holds
    assert budget.requests_used == 20


def test_small_budgets_reserve_nothing_and_an_explicit_reserve_is_honoured():
    assert FetchLimits(max_requests=3).reserve == 0 and FetchLimits(max_requests=40).reserve == 10
    budget = DiscoveryBudget(FetchLimits(max_requests=10, reference_reserve=4))
    assert budget.navigation_remaining() == 6
    for _ in range(6):
        budget.claim_request()
    assert budget.navigation_remaining() == 0 and claim(budget) == 'navigation_limit'


def test_the_reference_phase_is_scoped_and_nests():
    budget = DiscoveryBudget(FetchLimits(max_requests=4, reference_reserve=2))
    for _ in range(2):
        budget.claim_request()
    assert claim(budget) == 'navigation_limit'
    with budget.reference_phase():
        with budget.reference_phase():
            assert claim(budget) is None
        assert claim(budget) is None
    assert claim(budget) == 'request_limit'  # nothing left, and the flag did not leak out of the block


def test_the_host_limit_applies_to_navigation_but_a_known_host_is_always_allowed():
    budget = DiscoveryBudget(FetchLimits(max_requests=30, max_hosts=2))
    assert [claim(budget, 'a.test:443'), claim(budget, 'b.test:443')] == [None, None]
    assert claim(budget, 'c.test:443') == 'host_limit' and budget.hosts == {'a.test:443', 'b.test:443'}
    assert claim(budget, 'a.test:443') is None  # revisiting a contacted host costs no new slot
    assert claim(budget) is None  # no host given: not counted


def test_the_deadline_and_byte_limits_are_unchanged():
    budget = DiscoveryBudget(FetchLimits(max_total_bytes=10))
    budget.record_bytes(10)
    with pytest.raises(BudgetExceeded):
        budget.record_bytes(1)


def test_a_failed_claim_spends_nothing():
    budget = DiscoveryBudget(FetchLimits(max_requests=10, max_hosts=1))
    budget.claim_request('a.test:443')
    before = budget.requests_used
    assert claim(budget, 'b.test:443') == 'host_limit' and budget.requests_used == before and 'b.test:443' not in budget.hosts


def test_the_deep_preset_matches_the_research_proposal_and_can_be_adjusted():
    deep = FetchLimits.deep()
    assert (deep.max_requests, deep.discovery_timeout, deep.reserve) == (60, 120.0, 15)
    assert FetchLimits.deep(max_hosts=3).max_hosts == 3 and FetchLimits.deep(max_requests=80).max_requests == 80


@pytest.mark.parametrize('kwargs', [{'max_hosts': 0}, {'max_hosts': True}, {'reference_reserve': -1},
                                    {'max_requests': 5, 'reference_reserve': 5}, {'reference_reserve': 1.5}])
def test_invalid_new_limits(kwargs):
    with pytest.raises(ValueError):
        FetchLimits(**kwargs)


def test_every_stop_code_is_known_in_one_place():
    assert BUDGET_STOP_CODES == {'request_limit', 'navigation_limit', 'host_limit', 'deadline_exceeded', 'total_size_limit'}


def test_default_budget_has_room_for_navigation_and_reference_capture():
    from radar.discovery.navigation import NavigationLimits
    limits = FetchLimits()
    assert (limits.max_requests, limits.discovery_timeout, limits.max_hosts) == (40, 90, 8)
    budget = DiscoveryBudget(limits)
    assert budget.navigation_remaining() == 30 and limits.reserve == 10
    assert (NavigationLimits().max_pages, NavigationLimits().max_depth) == (16, 4)
