"""Tests for radar.comparison.structure (issue #2, plus array support)."""

import copy
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from radar.comparison import (
    ChangeKind,
    Finding,
    UnsupportedStructureError,
    compare_extracted,
    compare_structures,
    extract_structure,
)

# ---------------------------------------------------------------------------
# Example-based tests
# ---------------------------------------------------------------------------


class TestExtractStructure:
    def test_flat_object(self):
        data = {"id": 7, "name": "Ana", "active": True, "score": 9.5, "note": None}
        assert extract_structure(data) == {
            ("id",): "number",
            ("name",): "string",
            ("active",): "boolean",
            ("score",): "number",
            ("note",): "null",
        }

    def test_nested_paths(self):
        data = {"user": {"address": {"city": "Kent"}}}
        assert extract_structure(data) == {
            ("user",): "object",
            ("user", "address"): "object",
            ("user", "address", "city"): "string",
        }

    def test_empty_object(self):
        assert extract_structure({}) == {}

    def test_bool_is_not_a_number(self):
        assert extract_structure({"flag": False}) == {("flag",): "boolean"}

    def test_keys_containing_dots_stay_separate(self):
        data = {"a.b": 1, "a": {"b": "x"}}
        structure = extract_structure(data)
        assert structure[("a.b",)] == "number"
        assert structure[("a", "b")] == "string"


class TestArrays:
    def test_array_of_scalars(self):
        assert extract_structure({"tags": ["a", "b"]}) == {
            ("tags",): "array",
            ("tags", "[]"): "string",
        }

    def test_array_of_objects_merges_fields_across_elements(self):
        data = {"users": [{"id": 1}, {"id": 2, "email": "a@b.c"}]}
        assert extract_structure(data) == {
            ("users",): "array",
            ("users", "[]"): "object",
            ("users", "[]", "id"): "number",
            ("users", "[]", "email"): "string",
        }

    def test_mixed_types_become_a_sorted_union(self):
        assert extract_structure({"v": [1, "a", None]}) == {
            ("v",): "array",
            ("v", "[]"): "null|number|string",
        }

    def test_nullable_field_inside_elements(self):
        data = [{"name": "Ana"}, {"name": None}]
        assert extract_structure(data)[("[]", "name")] == "null|string"

    def test_top_level_array(self):
        assert extract_structure([{"id": 1}]) == {
            ("[]",): "object",
            ("[]", "id"): "number",
        }

    def test_nested_arrays(self):
        assert extract_structure({"m": [[1], [2, 3]]}) == {
            ("m",): "array",
            ("m", "[]"): "array",
            ("m", "[]", "[]"): "number",
        }

    def test_empty_array_has_unknown_elements(self):
        assert extract_structure({"items": []}) == {
            ("items",): "array",
            ("items", "[]"): "unknown",
        }
        assert extract_structure([]) == {("[]",): "unknown"}

    def test_empty_array_next_to_filled_array_is_ignored(self):
        assert extract_structure({"m": [[], [1]]})[("m", "[]", "[]")] == "number"

    def test_element_order_and_count_are_ignored(self):
        assert extract_structure([1, 2, 3]) == extract_structure([3])
        a = [{"x": 1}, {"y": "s"}]
        assert extract_structure(a) == extract_structure(list(reversed(a)))

    def test_element_field_changes_are_reported(self):
        old = {"users": [{"id": 1, "name": "Ana"}]}
        new = {"users": [{"id": "1"}]}
        assert compare_structures(old, new) == [
            Finding(ChangeKind.TYPE_CHANGED, ("users", "[]", "id"), "number", "string"),
            Finding(ChangeKind.ABSENT, ("users", "[]", "name"), "string", None),
        ]

    def test_object_becomes_array(self):
        findings = compare_structures({"a": {"x": 1}}, {"a": [1]})
        assert Finding(ChangeKind.TYPE_CHANGED, ("a",), "object", "array") in findings

    def test_empty_array_on_either_side_hides_element_findings(self):
        filled = {"users": [{"id": 1}]}
        empty = {"users": []}
        assert compare_structures(filled, empty) == []
        assert compare_structures(empty, filled) == []
        assert compare_structures([], [{"id": 1}]) == []

    def test_empty_array_still_reports_changes_outside_it(self):
        old = {"users": [{"id": 1}], "total": 1}
        new = {"users": [], "total": "1"}
        assert compare_structures(old, new) == [
            Finding(ChangeKind.TYPE_CHANGED, ("total",), "number", "string")
        ]

    def test_array_becoming_a_non_array_is_still_reported(self):
        findings = compare_structures({"users": []}, {"users": None})
        assert findings == [Finding(ChangeKind.TYPE_CHANGED, ("users",), "array", "null")]


class TestRejectsUnsupported:
    @pytest.mark.parametrize(
        "data",
        [
            "just a string",  # top-level scalar
            None,
            {"when": object()},  # non-JSON value
            {"pair": (1, 2)},  # tuple is not JSON
            {"list": [object()]},  # non-JSON value inside an array
            {1: "non-string key"},
            {"[]": 1},  # reserved key
            {"a": [{"[]": 1}]},
        ],
    )
    def test_raises(self, data):
        with pytest.raises(UnsupportedStructureError):
            extract_structure(data)

    def test_compare_rejects_either_side(self):
        with pytest.raises(UnsupportedStructureError):
            compare_structures({"a": 1}, {"a": object()})
        with pytest.raises(UnsupportedStructureError):
            compare_structures("x", {"a": 1})


class TestCompareExtracted:
    def test_matches_compare_structures(self):
        old = {"a": 1, "b": [{"c": 1}]}
        new = {"a": "x", "b": [{"c": 1, "d": None}]}
        assert compare_extracted(
            extract_structure(old), extract_structure(new)
        ) == compare_structures(old, new)

    def test_does_not_modify_inputs(self):
        old, new = {("a",): "number"}, {("a",): "string"}
        old_copy, new_copy = dict(old), dict(new)
        compare_extracted(old, new)
        assert (old, new) == (old_copy, new_copy)


class TestCompareStructures:
    def test_added_field(self):
        assert compare_structures({"a": 1}, {"a": 1, "b": "x"}) == [
            Finding(ChangeKind.ADDED, ("b",), None, "string")
        ]

    def test_absent_nested_field(self):
        old = {"user": {"id": 1, "email": "a@b.c"}}
        new = {"user": {"id": 1}}
        assert compare_structures(old, new) == [
            Finding(ChangeKind.ABSENT, ("user", "email"), "string", None)
        ]

    def test_type_change(self):
        assert compare_structures({"id": 1}, {"id": "1"}) == [
            Finding(ChangeKind.TYPE_CHANGED, ("id",), "number", "string")
        ]

    def test_becomes_null(self):
        assert compare_structures({"name": "Ana"}, {"name": None}) == [
            Finding(ChangeKind.TYPE_CHANGED, ("name",), "string", "null")
        ]

    def test_null_becomes_value(self):
        assert compare_structures({"name": None}, {"name": "Ana"}) == [
            Finding(ChangeKind.TYPE_CHANGED, ("name",), "null", "string")
        ]

    def test_object_becomes_null_reports_children(self):
        old = {"user": {"id": 1}}
        new = {"user": None}
        assert compare_structures(old, new) == [
            Finding(ChangeKind.TYPE_CHANGED, ("user",), "object", "null"),
            Finding(ChangeKind.ABSENT, ("user", "id"), "number", None),
        ]

    def test_int_and_float_are_same_type(self):
        assert compare_structures({"price": 10}, {"price": 10.5}) == []

    def test_value_changes_ignored(self):
        assert compare_structures({"a": 1, "b": "x"}, {"a": 2, "b": "y"}) == []

    def test_key_order_ignored(self):
        assert compare_structures({"a": 1, "b": {"c": 2, "d": 3}},
                                  {"b": {"d": 3, "c": 2}, "a": 1}) == []

    def test_formatting_ignored(self):
        compact = json.loads('{"a":1,"b":{"c":true}}')
        pretty = json.loads('{\n    "b": {\n        "c": true\n    },\n    "a": 1\n}')
        assert compare_structures(compact, pretty) == []

    def test_findings_sorted_by_path(self):
        old = {"z": 1, "a": 1}
        new = {"m": 1}
        assert [f.path for f in compare_structures(old, new)] == [("a",), ("m",), ("z",)]

    def test_inputs_not_modified(self):
        old = {"user": {"id": 1}}
        new = {"user": None, "extra": True}
        old_copy, new_copy = copy.deepcopy(old), copy.deepcopy(new)
        compare_structures(old, new)
        assert old == old_copy and new == new_copy


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

keys = st.text(max_size=5).filter(lambda key: key != "[]")
scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=10)
json_values = st.recursive(
    scalars,
    lambda children: st.dictionaries(keys, children, max_size=4)
    | st.lists(children, max_size=3),
    max_leaves=20,
)
json_objects = st.dictionaries(keys, json_values, max_size=5)
json_documents = json_objects | st.lists(json_values, max_size=3)


def _same_type_value(value, draw):
    """Draw a new value with the same JSON type (recurses into objects)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return draw(st.booleans())
    if isinstance(value, (int, float)):
        return draw(st.integers() | st.floats(allow_nan=False))
    if isinstance(value, str):
        return draw(st.text(max_size=10))
    if isinstance(value, list):
        return [_same_type_value(v, draw) for v in value]
    return {k: _same_type_value(v, draw) for k, v in value.items()}


def _reverse_key_order(value):
    if isinstance(value, dict):
        return {k: _reverse_key_order(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_key_order(v) for v in value]
    return value


def _reverse_arrays(value):
    if isinstance(value, dict):
        return {k: _reverse_arrays(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_reverse_arrays(v) for v in reversed(value)]
    return value


def _repeat_arrays(value):
    if isinstance(value, dict):
        return {k: _repeat_arrays(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_repeat_arrays(v) for v in value] * 2
    return value


@given(json_documents)
def test_comparing_with_itself_finds_nothing(data):
    assert compare_structures(data, data) == []


@given(json_documents, json_documents)
def test_inputs_are_never_modified(old, new):
    old_copy, new_copy = copy.deepcopy(old), copy.deepcopy(new)
    compare_structures(old, new)
    assert old == old_copy
    assert new == new_copy


@given(json_documents, json_documents)
def test_result_is_sorted_and_deterministic(old, new):
    first = compare_structures(old, new)
    assert first == compare_structures(copy.deepcopy(old), copy.deepcopy(new))
    paths = [f.path for f in first]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))  # at most one finding per path


@given(json_objects, json_objects)
def test_swapping_inputs_swaps_added_and_absent(old, new):
    swap = {
        ChangeKind.ADDED: ChangeKind.ABSENT,
        ChangeKind.ABSENT: ChangeKind.ADDED,
        ChangeKind.TYPE_CHANGED: ChangeKind.TYPE_CHANGED,
    }
    forward = compare_structures(old, new)
    backward = compare_structures(new, old)
    assert backward == [
        Finding(swap[f.kind], f.path, f.new_type, f.old_type) for f in forward
    ]


@given(json_objects, json_objects)
def test_findings_agree_with_extracted_structures(old, new):
    old_s, new_s = extract_structure(old), extract_structure(new)
    for f in compare_structures(old, new):
        assert old_s.get(f.path) == f.old_type
        assert new_s.get(f.path) == f.new_type


@given(json_objects, st.data())
def test_value_changes_are_ignored(data, draw_source):
    changed = _same_type_value(data, draw_source.draw)
    assert compare_structures(data, changed) == []


@given(json_documents)
def test_key_order_is_ignored(data):
    assert compare_structures(data, _reverse_key_order(data)) == []


@given(json_documents)
def test_formatting_is_ignored(data):
    reformatted = json.loads(json.dumps(data, indent=4, sort_keys=True))
    assert compare_structures(data, reformatted) == []


@given(json_documents)
def test_array_order_is_ignored(data):
    assert compare_structures(data, _reverse_arrays(data)) == []


@given(json_documents)
def test_array_length_is_ignored_when_elements_repeat(data):
    # Repeating elements adds no new shape. (Empty arrays stay empty.)
    assert compare_structures(data, _repeat_arrays(data)) == []


@given(json_documents)
def test_extract_then_compare_extracted_matches_compare_structures(data):
    other = _reverse_key_order(data)
    assert compare_extracted(
        extract_structure(data), extract_structure(other)
    ) == compare_structures(data, other)
