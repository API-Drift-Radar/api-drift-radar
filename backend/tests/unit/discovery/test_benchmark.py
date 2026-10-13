from radar.discovery.benchmark import Verdict, judge, run, summarize
from radar.domain.discovery import DiscoveryStatus


def entry(expect='found', accept=('good',), reject=()):
    return {'target': 'https://t.example', 'expect': expect, 'accept': list(accept), 'reject': list(reject)}


def test_a_found_target_is_a_hit_only_when_the_source_is_accepted():
    assert judge(entry(), 'validated', ('https://x/good.json',)) == 'hit'
    assert judge(entry(), 'unconfirmed', ('https://x/good.json',)) == 'hit'
    assert judge(entry(), 'validated', ('https://x/other.json',)) == 'false_positive'
    assert judge(entry(), 'not_found', ()) == 'miss'


def test_a_listed_wrong_source_is_a_false_positive_even_if_it_also_looks_accepted():
    assert judge(entry(reject=('plugin',)), 'validated', ('https://x/good-plugin.json',)) == 'false_positive'


def test_ambiguity_counts_when_every_alternative_is_right_and_flags_a_wrong_one():
    assert judge(entry(), 'ambiguous', ('https://x/good-1', 'https://x/good-2')) == 'ambiguous_hit'
    assert judge(entry(), 'ambiguous', ('https://x/good-1', 'https://x/bad')) == 'wrong_alternative'


def test_none_and_optional_targets():
    assert judge(entry('none'), 'not_found', ()) == 'correct_empty'
    assert judge(entry('none'), 'validated', ('https://x/good',)) == 'false_positive'
    assert judge(entry('optional'), 'rejected', ()) == 'correct_empty'
    assert judge(entry('optional'), 'validated', ('https://x/good',)) == 'hit'


def test_recall_precision_and_the_lists_are_computed_from_the_verdicts():
    v = [Verdict('a', 'found', 'validated', ('good',), 'hit'), Verdict('b', 'found', 'not_found', (), 'miss'),
         Verdict('c', 'none', 'validated', ('x',), 'false_positive'), Verdict('d', 'found', 'unconfirmed', ('good',), 'hit')]
    summary = summarize(v)
    assert summary['recall'] == 0.667 and summary['precision'] == 0.667
    assert summary['misses'] == ['b'] and summary['false_positives'] == ['c'] and summary['unconfirmed_hits'] == 1
    assert summary['validated_hits'] == 1 and summary['recall_validated'] == 0.333


def test_a_crashing_target_is_recorded_and_the_run_continues():
    class Outcome:
        status, package, packages = DiscoveryStatus.NOT_FOUND, None, ()

    def fake(request, use_directory):
        if 'boom' in request.target:
            raise RuntimeError('x')
        return Outcome()

    verdicts = run([{'target': 'https://boom.example', 'expect': 'found', 'accept': ['a']},
                    {'target': 'https://ok.example', 'expect': 'none'}], discover_fn=fake)
    assert [v.status for v in verdicts] == ['error: RuntimeError', 'not_found'] and verdicts[1].outcome == 'correct_empty'
