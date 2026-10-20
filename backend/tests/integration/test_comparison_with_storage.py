"""The comparison (#11) and storage (#10) working together, through the runner shown in the storage documentation.

Every test reads its results back through a newly opened store, as the API would.
"""

import copy
from datetime import timedelta

import pytest

from radar.comparison.openapi.evaluation import build_package
from radar.comparison.openapi.stored import compare_stored, normalize_contract
from radar.discovery.evaluation import package_fingerprint
from radar.storage.errors import ConflictError, StoreError
from radar.storage.records import ABSENT, CHANGED, FAILED, INITIAL, UNCHANGED
from radar.storage.repository import Store

LOCATOR = "https://api.acme.com"
MAX_ATTEMPTS = 3


def record_check(store, source_id, package, compare=compare_stored):
    """The runner from docs/component-interfaces.md (storage section), for a validated package."""
    check = store.begin_check(source_id)

    def failed(stage, error):
        return store.fail_check(check.id, stage=stage, reason=f"{stage}_failed", error=str(error) or type(error).__name__)

    try:
        normalized, version = normalize_contract(package)
    except Exception as error:
        return failed("normalization", error)
    for _ in range(MAX_ATTEMPTS):
        before = store.latest_snapshot(source_id)
        if before is None:
            result, changes, limitations = INITIAL, [], []
        else:
            try:
                reference = store.read_contract(before.id)
                kinds = {"snapshot_documents": store.snapshot_documents(before.id),
                         "before_source_kind": before.provenance.get("source_kind", "provider"),
                         "after_source_kind": package.candidate.source_kind}
                changes, limitations = compare(reference, normalized, version, **kinds)
            except Exception as error:
                return failed("comparison", error)
            same = reference.normalized == normalized and before.normalization_version == version
            result = UNCHANGED if same else CHANGED
        try:
            return store.complete_check(
                check.id, outcome=result, package=package, normalized=normalized, normalization_version=version,
                before_snapshot_id=before.id if before else None, changes=changes, limitations=limitations,
                content_fingerprint=package_fingerprint(package))
        except ConflictError:
            if store.get_check(check.id).finished:
                raise
        except StoreError as error:
            return failed("storage", error)
    raise AssertionError("the reference kept moving")


def pets(version=1):
    pet = {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}, "required": ["id"]}
    contract = {
        "openapi": "3.0.3", "info": {"title": "Acme", "version": str(version)},
        "servers": [{"url": "https://api.acme.com/v1"}],
        "paths": {
            "/pets": {"get": {"summary": "List pets", "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                              "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": pet}}}}}},
            "/pets/{id}": {"delete": {"responses": {"204": {"description": "gone"}}}}}}
    if version == 2:
        contract["paths"]["/pets"]["get"]["parameters"][0]["required"] = True
        contract["paths"]["/pets"]["get"]["summary"] = "List all pets"
        contract["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["nickname"] = {"type": "string"}
        del contract["paths"]["/pets/{id}"]["delete"]
        contract["paths"]["/pets/{id}"]["get"] = {"responses": {"200": {"description": "ok"}}}
    return contract


def reordered(contract):
    """The same contract with every mapping's keys in reverse order."""
    if isinstance(contract, dict):
        return {k: reordered(v) for k, v in reversed(list(contract.items()))}
    if isinstance(contract, list):
        return [reordered(v) for v in contract]
    return contract


@pytest.fixture
def data(tmp_path):
    return tmp_path / "radar-data"


def new_source(data):
    with Store(data) as store:
        return store.add_source(LOCATOR).id


def test_the_first_capture_is_initial_and_records_no_changes(data):
    source = new_source(data)
    with Store(data) as store:
        check = record_check(store, source, build_package(pets(1)))
    with Store(data) as store:
        assert (store.get_check(check.id).status, store.list_changes(check_id=check.id)) == (INITIAL, [])
        assert store.latest_snapshot(source).id == check.snapshot_id


def test_a_reformatted_capture_is_unchanged_but_still_gets_its_own_snapshot(data):
    source = new_source(data)
    with Store(data) as store:
        first = record_check(store, source, build_package(pets(1)))
        second = record_check(store, source, build_package(reordered(pets(1))))
    with Store(data) as store:
        assert store.get_check(second.id).status == UNCHANGED
        assert second.snapshot_id != first.snapshot_id and store.list_changes(check_id=second.id) == []
        assert store.read_contract(first.snapshot_id).normalized == store.read_contract(second.snapshot_id).normalized
        assert store.read_contract(first.snapshot_id).original != store.read_contract(second.snapshot_id).original  # bytes kept as captured


def test_a_changed_capture_records_classified_changes_between_both_snapshots(data):
    source = new_source(data)
    with Store(data) as store:
        first = record_check(store, source, build_package(pets(1)))
        second = record_check(store, source, build_package(pets(2)))
    with Store(data) as store:
        check = store.get_check(second.id)
        changes = store.list_changes(check_id=second.id)
        assert check.status == CHANGED and check.before_snapshot_id == first.snapshot_id and check.snapshot_id == second.snapshot_id
        assert [c.ordinal for c in changes] == list(range(len(changes))) and len(changes) >= 6
        assert all(c.before_snapshot_id == first.snapshot_id and c.after_snapshot_id == second.snapshot_id for c in changes)
        by_type = {c.change_type: c for c in changes}
        removed = by_type["operation_removed"]
        assert (removed.location, removed.classification, removed.direction) == ("DELETE /pets/{id}", "potentially_breaking", None)
        assert removed.before_value == {"method": "DELETE", "path": "/pets/{id}"} and removed.after_value is ABSENT
        added = by_type["operation_added"]
        assert (added.classification, added.before_value) == ("non_breaking", ABSENT)
        required = by_type["parameter_required_changed"]
        assert (required.classification, required.direction) == ("potentially_breaking", "request")
        assert required.location == "GET /pets > query parameter limit"
        assert by_type["schema_property_added"].classification == "non_breaking" and by_type["schema_property_added"].direction == "response"
        assert by_type["documentation_changed"].classification == "documentation_only"
        assert {c.classification for c in changes} <= {"potentially_breaking", "non_breaking", "documentation_only", "uncertain"}
        assert all(c.summary and c.details["rule"] and c.details["key"] for c in changes)
        assert any(l["code"] == "scope_not_assessed" for l in check.limitations)


def test_the_comparison_works_after_the_store_is_closed_and_reopened(data):
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(pets(1)))
    with Store(data) as store:  # a new process, a new connection: everything is read back from disk
        second = record_check(store, source, build_package(pets(2)))
        assert second.status == CHANGED and store.list_changes(check_id=second.id)


def test_a_failed_check_never_moves_the_reference_and_the_next_check_compares_with_the_right_one(data):
    source = new_source(data)
    with Store(data) as store:
        first = record_check(store, source, build_package(pets(1)))
        failure = store.begin_check(source)
        store.fail_check(failure.id, stage="discovery", reason="inaccessible", error="Could not reach the API.")
        assert store.latest_snapshot(source).id == first.snapshot_id
        third = record_check(store, source, build_package(pets(2)))
        assert store.get_check(failure.id).status == FAILED and third.before_snapshot_id == first.snapshot_id


def test_a_change_the_comparison_does_not_report_is_changed_with_no_change_records(data):
    """Reordering a `required` list changes the normalized bytes but is not a reported difference."""
    base = pets(1)
    other = copy.deepcopy(base)
    other["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["required"] = ["id", "name"]
    swapped = copy.deepcopy(other)
    swapped["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["required"] = ["name", "id"]
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(other))
        second = record_check(store, source, build_package(swapped))
    with Store(data) as store:
        assert store.get_check(second.id).status == CHANGED and store.list_changes(check_id=second.id) == []


def test_a_reference_with_an_unknown_version_is_rebuilt_from_its_originals(data):
    source = new_source(data)
    with Store(data) as store:
        old = store.begin_check(source)
        legacy = store.complete_check(old.id, outcome=INITIAL, package=build_package(pets(1)), normalized=b"legacy bytes",
                                      normalization_version="legacy-1")
        second = record_check(store, source, build_package(pets(2)))
        assert second.status == CHANGED and store.list_changes(check_id=second.id)
        assert store.get_check(second.id).before_snapshot_id == legacy.snapshot_id


def test_a_directory_copy_reference_is_noted_in_the_stored_limitations(data):
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(pets(1), source_kind="directory_copy"))
        second = record_check(store, source, build_package(pets(2)))
        assert any(l["code"] == "directory_copy_source" and l["scope"] == "before" for l in store.get_check(second.id).limitations)


def test_a_slow_clock_does_not_matter(data):
    """Retrieval times are not part of the normalized form, so a later capture of the same contract is unchanged."""
    source = new_source(data)
    first, later = build_package(pets(1)), build_package(pets(1))
    later = type(later)(later.candidate, type(later.root_document)(later.root_document.source_url, later.root_document.content,
                        later.root_document.retrieved_at + timedelta(days=30), later.root_document.media_type),
                        later.referenced_documents, later.parsed_contract, later.openapi_version)
    with Store(data) as store:
        record_check(store, source, first)
        assert record_check(store, source, later).status == UNCHANGED


def test_the_same_contract_from_another_address_is_unchanged(data):
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(pets(1), url="https://api.acme.com/openapi.json"))
        second = record_check(store, source, build_package(pets(1), url="https://cdn.acme.net/specs/openapi.yaml"))
    with Store(data) as store:
        assert (store.get_check(second.id).status, store.list_changes(check_id=second.id)) == (UNCHANGED, [])


def test_the_same_contract_with_a_referenced_document_from_another_address_is_unchanged(data):
    contract = pets(1)
    contract["paths"]["/owners"] = {"$ref": "paths/owners.json"}
    owners = {"get": {"responses": {"200": {"description": "ok"}}}}
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(
            contract, url="https://api.acme.com/openapi.json", referenced={"https://api.acme.com/paths/owners.json": owners}))
        second = record_check(store, source, build_package(
            contract, url="https://cdn.acme.net/v1/spec.json", referenced={"https://cdn.acme.net/v1/paths/owners.json": owners}))
    with Store(data) as store:
        assert (store.get_check(second.id).status, store.list_changes(check_id=second.id)) == (UNCHANGED, [])


def test_a_normalization_upgrade_rebuilds_the_reference_from_the_originals(data, monkeypatch):
    from radar.comparison.openapi import stored
    source = new_source(data)
    with Store(data) as store:
        record_check(store, source, build_package(pets(1)))
    monkeypatch.setattr(stored, "NORMALIZATION_VERSION", "openapi-canonical-2")
    monkeypatch.setattr(stored, "SUPPORTED_VERSIONS", frozenset({"openapi-canonical-2"}))
    with Store(data) as store:
        same = record_check(store, source, build_package(pets(1)))
        changed = record_check(store, source, build_package(pets(2)))
    with Store(data) as store:
        assert store.get_check(same.id).status == CHANGED  # the version differs, so the bytes are new; nothing failed
        assert store.list_changes(check_id=same.id) == []
        assert store.get_check(changed.id).status == CHANGED and store.list_changes(check_id=changed.id)
        assert store.latest_snapshot(source).id == changed.snapshot_id


def test_an_upgrade_without_the_originals_fails_the_check_and_keeps_the_reference(data, monkeypatch):
    from radar.comparison.openapi import stored
    source = new_source(data)
    with Store(data) as store:
        first = record_check(store, source, build_package(pets(1)))
    monkeypatch.setattr(stored, "NORMALIZATION_VERSION", "openapi-canonical-2")
    monkeypatch.setattr(stored, "SUPPORTED_VERSIONS", frozenset({"openapi-canonical-2"}))
    with Store(data) as store:
        failed = record_check(store, source, build_package(pets(2)),
                              compare=lambda reference, normalized, version, **kinds: stored.compare_stored(
                                  reference, normalized, version, **{k: v for k, v in kinds.items() if k != "snapshot_documents"}))
    with Store(data) as store:
        assert store.get_check(failed.id).status == FAILED and store.latest_snapshot(source).id == first.snapshot_id
