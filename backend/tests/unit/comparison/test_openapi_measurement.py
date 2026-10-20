"""The measurement report, the held-out set, grouping, the command, scale, and the storage round trip."""

import io
import json
import time
from contextlib import redirect_stdout

import pytest

from radar.comparison.openapi import compare_contracts, result_to_dict
from radar.comparison.openapi.__main__ import main as compare_main
from radar.comparison.openapi.evaluation import (
    CONFUSION_COLUMNS, CONFUSION_ROWS, FIXTURES, HOLDOUT, Report, build_package, evaluate, load_cases, main as evaluate_main,
)
from radar.comparison.openapi.serialization import group_findings
from radar.discovery.validation import parse_document
from radar.domain.discovery import CapturedDocument, ContractCandidate, ValidatedContractPackage

PB, NB, UN, DOC = 'potentially_breaking', 'non_breaking_under_rules', 'uncertain', 'documentation_only'


# --- the held-out set and the report ---------------------------------------------------------------

def test_the_held_out_set_matches_its_labels():
    """Labeled before the engine was run on them (see the rationale in each meta.json). Same author as the rules, so
    this is a blind check by one person, not independent validation."""
    cases = load_cases(HOLDOUT)
    report = evaluate(cases)
    assert report.failures == []
    assert (report.precision, report.recall, report.classification_accuracy) == (1.0, 1.0, 1.0)
    assert len(cases) >= 22 and report.expected >= 30
    assert {c.name for c in cases}.isdisjoint({c.name for c in load_cases(FIXTURES)})


def test_the_report_is_machine_readable_and_has_a_confusion_matrix():
    report = evaluate(load_cases(HOLDOUT))
    document = json.loads(json.dumps(report.as_dict()))
    assert set(document['confusion_matrix']) == set(CONFUSION_ROWS)
    assert all(set(row) == set(CONFUSION_COLUMNS) for row in document['confusion_matrix'].values())
    assert document['recall'] == 1.0 and document['uncertain_returned'] > 0
    assert document['limitations_by_code']['scope_not_assessed'] == document['cases']
    assert document['per_rule']['operation.removed']['recall'] == 1.0


def test_missed_unexpected_and_misclassified_findings_land_in_the_right_cells():
    cases = load_cases(FIXTURES)

    def silent(before, after):
        return compare_contracts(build_package({'openapi': '3.0.3', 'info': {}, 'paths': {}}), build_package({'openapi': '3.0.3', 'info': {}, 'paths': {}}))

    report = evaluate(cases, compare=silent)
    assert report.recall == 0.0 and sum(row['missed'] for row in report.confusion.values()) == report.expected
    assert report.precision is None  # nothing returned: N/A, not 1.0

    assert evaluate([]).as_dict()['precision'] is None and Report().as_dict()['recall'] is None


def test_the_evaluation_command_prints_both_sets_and_signals_failure(capsys, tmp_path):
    assert evaluate_main([]) == 0
    printed = capsys.readouterr().out
    assert '[comparison]' in printed and '[comparison_holdout]' in printed and 'confusion matrix' in printed
    broken = tmp_path / 'broken'
    broken.mkdir()
    case = broken / 'c'
    case.mkdir()
    for name, value in (('before', {'openapi': '3.0.3', 'info': {}, 'paths': {}}), ('after', {'openapi': '3.0.3', 'info': {}, 'paths': {'/a': {'get': {}}}}),
                        ('expected', {'findings': []})):
        (case / f'{name}.json').write_text(json.dumps(value))
    assert evaluate_main(['--fixtures', str(broken), '--json']) == 1
    assert json.loads(capsys.readouterr().out)['broken']['failures']


# --- grouping --------------------------------------------------------------------------------------

def shared(kind, operations=3):
    schema = {'type': 'object', 'properties': {'a': {'type': kind}}}
    paths = {f'/p{i}': {'get': {'responses': {'200': {'description': 'ok', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/S'}}}}}}}
             for i in range(operations)}
    return {'openapi': '3.0.3', 'info': {}, 'components': {'schemas': {'S': schema}}, 'paths': paths}


def test_one_change_to_a_shared_schema_is_one_group_with_all_its_operations():
    result = compare_contracts(build_package(shared('string')), build_package(shared('integer')))
    assert len(result.findings) == 3
    (group,) = group_findings(result.findings)
    assert (group['count'], group['operation_count'], group['category']) == (3, 3, PB)
    assert group['operations'] == ['GET /p0', 'GET /p1', 'GET /p2']
    document = result_to_dict(result)
    assert len(document['findings']) == 3 and len(document['finding_groups']) == 1


def test_different_changes_are_never_merged():
    before, after = shared('string'), shared('string')
    before['components']['schemas']['S']['properties']['b'] = {'type': 'string', 'maxLength': 5}
    after['components']['schemas']['S']['properties']['b'] = {'type': 'string', 'maxLength': 3, 'minLength': 1}
    groups = group_findings(compare_contracts(build_package(before), build_package(after)).findings)
    assert sorted(g['example_key'].rsplit('|', 1)[-1] for g in groups) == ['maxLength', 'minLength']
    assert all(g['count'] == 3 for g in groups)


# --- the command -----------------------------------------------------------------------------------

def write(path, contract):
    path.write_text(json.dumps(contract))
    return str(path)


def run_cli(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        status = compare_main(list(args))
    return status, out.getvalue()


def test_the_command_compares_two_files(tmp_path):
    before = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {'/pets': {'get': {}, 'delete': {}}}}
    after = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {'/pets': {'get': {}}, '/owners': {'get': {}}}}
    status, text = run_cli(write(tmp_path / 'a.json', before), write(tmp_path / 'b.json', after))
    assert status == 1 and '2 finding(s), 2 distinct change(s)' in text and 'operation.removed' in text
    assert 'Not compared:' in text
    status, text = run_cli(write(tmp_path / 'a.json', before), write(tmp_path / 'b.json', after), '--json')
    assert status == 1 and len(json.loads(text)['finding_groups']) == 2


def test_the_command_exit_codes(tmp_path, capsys):
    contract = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {}}
    same = write(tmp_path / 'same.json', contract)
    assert run_cli(same, same)[0] == 0
    assert compare_main([same, str(tmp_path / 'missing.json')]) == 2
    (tmp_path / 'swagger.json').write_text(json.dumps({'swagger': '2.0', 'info': {}, 'paths': {}}))
    assert compare_main([same, str(tmp_path / 'swagger.json')]) == 2
    errors = capsys.readouterr().err
    assert 'Cannot read' in errors and 'not a supported OpenAPI 3.x document' in errors


def test_the_command_reads_yaml_and_reports_a_limitation_for_other_files(tmp_path):
    (tmp_path / 'a.yaml').write_text('openapi: 3.0.3\ninfo: {title: T, version: "1"}\npaths:\n  /a:\n    $ref: other.yaml#/A\n')
    (tmp_path / 'b.yaml').write_text('openapi: 3.0.3\ninfo: {title: T, version: "1"}\npaths:\n  /a:\n    get: {}\n')
    status, text = run_cli(str(tmp_path / 'a.yaml'), str(tmp_path / 'b.yaml'))
    assert status == 0 and 'path_item_unresolved' in text  # not read, so nothing is claimed about /a


# --- scale -----------------------------------------------------------------------------------------

def big(version):
    schemas = {f'S{i}': {'type': 'object', 'properties': {f'f{j}': {'type': 'integer' if j % 7 else 'string', 'description': f'd{version}'} for j in range(20)}}
               for i in range(100)}
    paths = {}
    for i in range(1500):
        ref = {'$ref': f'#/components/schemas/S{i % 100}'}
        paths[f'/r{i}/{{id}}'] = {
            'get': {'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                    'responses': {'200': {'description': 'ok', 'content': {'application/json': {'schema': ref}}}}},
            'post': {'requestBody': {'content': {'application/json': {'schema': ref}}}, 'responses': {'201': {'description': 'ok'}}}}
    if version == 2:
        schemas['S5']['properties']['f1'] = {'type': 'string'}
        del paths['/r7/{id}']['post']
    return {'openapi': '3.0.3', 'info': {'title': 'Big', 'version': str(version)}, 'components': {'schemas': schemas}, 'paths': paths}


def test_a_contract_with_thousands_of_operations_is_compared_in_reasonable_time():
    """Smoke test, not a benchmark: a generous bound so only a pathological slowdown fails it."""
    before, after = build_package(big(1)), build_package(big(2))
    started = time.perf_counter()
    result = compare_contracts(before, after)
    elapsed = time.perf_counter() - started
    assert elapsed < 60, f'{elapsed:.1f}s'
    rules = {f.rule_id for f in result.findings}
    assert 'operation.removed' in rules and 'schema.type_changed' in rules and 'documentation.changed' in rules
    assert len(group_findings(result.findings)) < len(result.findings)  # shared schemas collapse


# --- storage round trip ----------------------------------------------------------------------------

def rebuilt(package: ValidatedContractPackage) -> ValidatedContractPackage:
    """What storage would do: keep the exact bytes, the URLs, the provenance and the evidence, and re-parse on load."""
    documents = lambda d: CapturedDocument(d.source_url, bytes(d.content), d.retrieved_at, d.media_type)  # noqa: E731
    candidate = ContractCandidate(package.candidate.source_url, package.candidate.discovery_method,
                                  package.candidate.discovery_source, tuple(package.candidate.evidence),
                                  tuple(package.candidate.rejection_reasons), tuple(package.candidate.limitations),
                                  package.candidate.source_kind)
    root = documents(package.root_document)
    return ValidatedContractPackage(candidate, root, tuple(documents(d) for d in package.referenced_documents),
                                    parse_document(root.content).value, package.openapi_version, package.limitations)


@pytest.mark.parametrize('root', [FIXTURES, HOLDOUT], ids=['development', 'held-out'])
def test_a_package_rebuilt_from_its_stored_parts_compares_identically(root):
    """The contract with storage: keep each document's exact bytes and URL, `source_kind` and the matching evidence, and
    comparing the reloaded packages gives exactly the same result as comparing the originals."""
    checked = 0
    for case in load_cases(root):
        original = result_to_dict(compare_contracts(case.before, case.after))
        reloaded = result_to_dict(compare_contracts(rebuilt(case.before), rebuilt(case.after)))
        assert original == reloaded, case.name
        checked += 1
    assert checked >= 20
