"""Shared MongoDB-style document sorting helpers.

Cursor sorting and aggregation ``$sort`` both use this module so BSON value
ordering, dotted-path traversal, and array sort-key selection cannot drift.
"""

from __future__ import absolute_import

from collections.abc import Mapping
from functools import cmp_to_key

from .bson_types import bson_value_sort_key
from .errors import OperationFailure


_MISSING_SORT_KEY = (0, None)
# MongoDB gives an empty array a special field-sort position below null but
# above MinKey. A fractional internal rank keeps it distinct from both BSON
# families while remaining comparable with the integer registry ranks.
_EMPTY_ARRAY_SORT_KEY = (-0.5, ())
_MISSING = object()


class _AmbiguousArrayField(object):
    """Defer numeric-path errors until parallel-array checks have run."""

    def __init__(self, field):
        self.field = field


def _plain_bson_sort_key(value, sort_field, unsupported_value_callback):
    value_key = bson_value_sort_key(value)
    if value_key is None:
        if unsupported_value_callback is not None and sort_field is not None:
            unsupported_value_callback(sort_field, value)
        return _MISSING_SORT_KEY
    return value_key


def bson_document_sort_value_key(
    value,
    descending=False,
    sort_field=None,
    unsupported_value_callback=None,
):
    """Return the BSON key used when ``value`` is a document sort value.

    MongoDB compares an array-valued sort field by its smallest member for an
    ascending sort and its largest member for a descending sort. Empty arrays
    sort before missing/null in ascending order and after them in descending
    order. Unsupported values retain TinyMongo's established null-like order
    and can be reported by the caller.
    """

    if isinstance(value, (list, tuple)):
        member_keys = (
            [
                _plain_bson_sort_key(
                    member,
                    sort_field=sort_field,
                    unsupported_value_callback=unsupported_value_callback,
                )
                for member in value
            ]
            if value
            else [_EMPTY_ARRAY_SORT_KEY]
        )
        return max(member_keys) if descending else min(member_keys)

    return _plain_bson_sort_key(value, sort_field, unsupported_value_callback)


def _is_canonical_array_index(part):
    return part == "0" or (
        part
        and part[0] in "123456789"
        and all("0" <= character <= "9" for character in part)
    )


def _sort_path_candidates(
    value,
    parts,
    traversed=(),
    provenance=(),
    array_sources=frozenset(),
    positional_endpoint=False,
):
    """Return endpoint values plus the array elements that produced them."""

    if not parts:
        if isinstance(value, (list, tuple)):
            array_sources = array_sources.union((".".join(traversed),))
        return [(value, provenance, array_sources, positional_endpoint)]

    if isinstance(value, Mapping):
        field = parts[0]
        if field not in value:
            return [(_MISSING, provenance, array_sources, False)]
        return _sort_path_candidates(
            value[field],
            parts[1:],
            traversed + (field,),
            provenance,
            array_sources,
            False,
        )

    if isinstance(value, (list, tuple)):
        field = parts[0]
        if _is_canonical_array_index(field):
            index = int(field)
            source = ".".join(traversed)
            expanded_sources = array_sources.union((source,))
            named_field_exists = any(
                isinstance(member, Mapping) and field in member for member in value
            )
            if index < len(value) and named_field_exists:
                return [
                    (
                        _AmbiguousArrayField(field),
                        provenance,
                        expanded_sources,
                        False,
                    )
                ]
            if index < len(value):
                return _sort_path_candidates(
                    value[index],
                    parts[1:],
                    traversed + (field,),
                    provenance,
                    expanded_sources,
                    True,
                )
            if not named_field_exists:
                return [(_MISSING, provenance, expanded_sources, False)]
            # When the numeric index is out of range, MongoDB falls back to
            # treating the component as a literal field name in each embedded
            # array document. The ordinary fan-out path below provides that
            # behavior.

        source = ".".join(traversed)
        expanded_sources = array_sources.union((source,))
        if not value:
            return [(_MISSING, provenance, expanded_sources, False)]
        resolved = []
        for index, member in enumerate(value):
            member_provenance = provenance + ((source, index),)
            # Field-path traversal fans out across arrays of documents, but a
            # raw array nested directly inside another array is a boundary.
            # Canonical numeric path components above can still select either
            # array explicitly.
            if isinstance(member, Mapping):
                resolved.extend(
                    _sort_path_candidates(
                        member,
                        parts,
                        traversed,
                        member_provenance,
                        expanded_sources,
                        False,
                    )
                )
            else:
                resolved.append((_MISSING, member_provenance, expanded_sources, False))
        return resolved

    return [(_MISSING, provenance, array_sources, False)]


def _candidate_key(
    candidate,
    field,
    descending,
    unsupported_value_callback,
):
    value = candidate[0]
    if value is _MISSING:
        return _MISSING_SORT_KEY
    if isinstance(value, _AmbiguousArrayField):
        raise OperationFailure(
            "Ambiguous field name found in array (do not use numeric "
            "field names in embedded elements in an array), field: "
            "'{0}'".format(value.field),
            code=16746,
        )
    if candidate[3] and isinstance(value, (list, tuple)) and value:
        return _plain_bson_sort_key(
            value,
            sort_field=field,
            unsupported_value_callback=unsupported_value_callback,
        )
    return bson_document_sort_value_key(
        value,
        descending=descending,
        sort_field=field,
        unsupported_value_callback=unsupported_value_callback,
    )


def document_sort_key(
    document,
    field,
    descending=False,
    unsupported_value_callback=None,
):
    """Return one document's key for a field in a MongoDB sort specification."""

    candidates = _sort_path_candidates(document, field.split("."))
    keys = [
        _candidate_key(
            candidate,
            field,
            descending,
            unsupported_value_callback,
        )
        for candidate in candidates
    ]
    return max(keys) if descending else min(keys)


def _compare_compound_keys(left, right, sort_specification):
    for index, (_field, direction) in enumerate(sort_specification):
        if left[index] == right[index]:
            continue
        if left[index] < right[index]:
            return -1 if direction > 0 else 1
        return 1 if direction > 0 else -1
    return 0


def _sources_are_one_chain(sources):
    def is_prefix(left, right):
        return left == right or right.startswith(left + ".")

    return all(
        is_prefix(left, right) or is_prefix(right, left)
        for left in sources
        for right in sources
    )


def _candidate_lookup(candidates):
    """Index candidates by array provenance for efficient compound joins."""

    by_source = {}
    for candidate_index, candidate in enumerate(candidates):
        for source, index in candidate[1]:
            by_source.setdefault(source, {}).setdefault(index, []).append(
                candidate_index
            )

    all_indexes = tuple(range(len(candidates)))
    without_source = {}
    for source, groups in by_source.items():
        with_source = {
            candidate_index
            for indexes in groups.values()
            for candidate_index in indexes
        }
        without_source[source] = tuple(
            candidate_index
            for candidate_index in all_indexes
            if candidate_index not in with_source
        )
    return candidates, by_source, without_source


def _compatible_candidates(lookup, selected_provenance):
    """Return candidates that can share array elements with selected values."""

    candidates, by_source, without_source = lookup
    shared_sources = [source for source in selected_provenance if source in by_source]
    if not shared_sources:
        return candidates

    source = min(
        shared_sources,
        key=lambda item: len(by_source[item].get(selected_provenance[item], ()))
        + len(without_source[item]),
    )
    candidate_indexes = (
        tuple(by_source[source].get(selected_provenance[source], ()))
        + without_source[source]
    )
    return [
        candidates[candidate_index]
        for candidate_index in candidate_indexes
        if all(
            selected_provenance.get(candidate_source, candidate_index_value)
            == candidate_index_value
            for candidate_source, candidate_index_value in candidates[candidate_index][
                1
            ]
        )
    ]


def _compound_document_sort_key(
    document,
    sort_specification,
    unsupported_value_callback,
):
    candidate_lookups = [
        _candidate_lookup(_sort_path_candidates(document, field.split(".")))
        for field, _direction in sort_specification
    ]

    # Build only combinations whose values came from the same element whenever
    # fields fan out through a shared array. This preserves the compound key a
    # real MongoDB multikey index would produce instead of independently taking
    # each field's minimum or maximum.
    combinations = [((), {}, frozenset())]
    for candidate_lookup in candidate_lookups:
        next_combinations = []
        for selected_candidates, selected_provenance, selected_sources in combinations:
            for candidate in _compatible_candidates(
                candidate_lookup, selected_provenance
            ):
                candidate_provenance = dict(candidate[1])
                merged_provenance = dict(selected_provenance)
                merged_provenance.update(candidate_provenance)
                merged_sources = selected_sources.union(candidate[2])
                if not _sources_are_one_chain(merged_sources):
                    raise OperationFailure(
                        "cannot sort with keys that are parallel arrays", code=2
                    )
                next_combinations.append(
                    (
                        selected_candidates + (candidate,),
                        merged_provenance,
                        merged_sources,
                    )
                )
        combinations = next_combinations

    keys = [
        tuple(
            _candidate_key(
                candidate,
                field,
                direction < 0,
                unsupported_value_callback,
            )
            for candidate, (field, direction) in zip(
                selected_candidates, sort_specification
            )
        )
        for selected_candidates, _provenance, _sources in combinations
    ]
    selected = keys[0]
    for candidate in keys[1:]:
        if _compare_compound_keys(candidate, selected, sort_specification) < 0:
            selected = candidate
    return selected


def sort_documents(documents, sort_specification, unsupported_value_callback=None):
    """Return documents ordered by an already validated sort specification."""

    specification = tuple(sort_specification)
    keyed = [
        (
            document,
            _compound_document_sort_key(
                document,
                specification,
                unsupported_value_callback,
            ),
        )
        for document in documents
    ]
    keyed.sort(
        key=cmp_to_key(
            lambda left, right: _compare_compound_keys(left[1], right[1], specification)
        )
    )
    return [document for document, _key in keyed]
