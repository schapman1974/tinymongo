"""Backend-independent aggregation pipeline execution.

The production-driven slice intentionally stays small: ``$match``, ``$project``,
and ``$group`` with the ``$ifNull`` and ``$size`` projection expressions and the
``$min``, ``$max``, and ``$sum`` accumulators. Keeping the pipeline engine
separate from storage lets every TinyMongo backend share validation, field-path,
missing-value, and BSON comparison behavior.
"""

from __future__ import absolute_import

import copy
from collections.abc import Mapping
from numbers import Number

from .bson_types import bson_value_identity_key, bson_value_sort_key
from .errors import OperationFailure, TinyMongoNotSupportedError
from .projection import normalize_projection, project_document
from .table_backends import matches_filter, validate_filter_operators


_MISSING = object()
_ALLOWED_DOLLAR_PREFIXED_FIELDS = frozenset(
    (
        "$db",
        "$id",
        "$recordId",
        "$ref",
        "$searchRootDocumentId",
        "$searchScore",
        "$searchSortValues",
        "$sortKey",
    )
)
_SUPPORTED_ACCUMULATORS = ("$max", "$min", "$sum")
_SUPPORTED_EXPRESSIONS = ("$ifNull", "$size")
_SUPPORTED_STAGES = ("$match", "$project", "$group")


def _sum_value(value):
    """Return the numeric contribution MongoDB's group ``$sum`` would use."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    return 0


def aggregation_capabilities():
    """Return a fresh description of the currently supported pipeline slice."""

    return {
        "stages": _SUPPORTED_STAGES,
        "accumulators": _SUPPORTED_ACCUMULATORS,
        "expressions": _SUPPORTED_EXPRESSIONS,
    }


def _resolve_parts(value, parts):
    if not parts:
        return copy.deepcopy(value)

    part = parts[0]
    remaining = parts[1:]
    if isinstance(value, Mapping):
        if part not in value:
            return _MISSING
        return _resolve_parts(value[part], remaining)

    if isinstance(value, (list, tuple)):
        resolved = []
        for item in value:
            candidate = _resolve_parts(item, parts)
            if candidate is not _MISSING:
                resolved.append(candidate)
        # Once a path traverses an array, MongoDB preserves an empty traversal
        # as an empty array. Only a missing field before array traversal is
        # represented by the missing sentinel.
        return resolved

    return _MISSING


class AggregationContext(object):
    """Evaluate the field paths and literals shared by pipeline stages."""

    @staticmethod
    def validate_field_path(expression):
        if isinstance(expression, str) and expression.startswith("$$"):
            raise TinyMongoNotSupportedError(
                "Aggregation variable {0} is not supported by TinyMongo".format(
                    expression
                )
            )
        if (
            not isinstance(expression, str)
            or not expression.startswith("$")
            or len(expression) == 1
        ):
            raise TinyMongoNotSupportedError(
                "Aggregation field paths must be strings such as '$field'"
            )

        path = expression[1:]
        if path.endswith("."):
            raise OperationFailure(
                "Aggregation field paths must not end with '.'", code=40353
            )
        parts = path.split(".")
        if any(not part for part in parts):
            raise OperationFailure(
                "Aggregation field path components cannot be empty", code=15998
            )
        for part in parts:
            if part.startswith("$") and part not in _ALLOWED_DOLLAR_PREFIXED_FIELDS:
                raise OperationFailure(
                    "Aggregation field path components may not start with '$'",
                    code=16410,
                )
            if "\x00" in part:
                raise OperationFailure(
                    "Aggregation field path components may not contain null bytes",
                    code=16411,
                )

    @staticmethod
    def _expression_operator(expression):
        operators = [key for key in expression if str(key).startswith("$")]
        if not operators:
            return None
        if len(expression) != 1 or len(operators) != 1:
            raise OperationFailure(
                "Aggregation expression documents must contain one operator"
            )
        return operators[0]

    @staticmethod
    def _size_operand(operand):
        if isinstance(operand, (list, tuple)):
            if len(operand) != 1:
                raise OperationFailure("$size requires exactly one expression")
            return operand[0]
        return operand

    def validate_expression(self, expression, allowed_operators=()):
        """Reject unsupported expression operators before reading documents."""

        if isinstance(expression, str) and expression.startswith("$"):
            self.validate_field_path(expression)
            return
        if isinstance(expression, (list, tuple)):
            for item in expression:
                self.validate_expression(item, allowed_operators)
            return
        if isinstance(expression, Mapping):
            operator = self._expression_operator(expression)
            if operator is not None:
                if operator not in allowed_operators:
                    raise TinyMongoNotSupportedError(
                        "Aggregation expression {0} is not supported by TinyMongo".format(
                            operator
                        )
                    )
                operand = expression[operator]
                if operator == "$ifNull":
                    if not isinstance(operand, (list, tuple)) or len(operand) < 2:
                        raise OperationFailure(
                            "$ifNull requires at least two expressions"
                        )
                    for item in operand:
                        self.validate_expression(item, allowed_operators)
                    return
                self.validate_expression(self._size_operand(operand), allowed_operators)
                return
            for value in expression.values():
                self.validate_expression(value, allowed_operators)

    def resolve_field_path(self, document, expression):
        self.validate_field_path(expression)
        return _resolve_parts(document, expression[1:].split("."))

    def evaluate(self, document, expression, allowed_operators=()):
        if isinstance(expression, str) and expression.startswith("$"):
            return self.resolve_field_path(document, expression)
        if isinstance(expression, list):
            values = [
                self.evaluate(document, item, allowed_operators) for item in expression
            ]
            return [None if value is _MISSING else value for value in values]
        if isinstance(expression, tuple):
            values = [
                self.evaluate(document, item, allowed_operators) for item in expression
            ]
            return [None if value is _MISSING else value for value in values]
        if isinstance(expression, Mapping):
            operator = self._expression_operator(expression)
            if operator is not None:
                if operator not in allowed_operators:
                    raise TinyMongoNotSupportedError(
                        "Aggregation expression {0} is not supported by TinyMongo".format(
                            operator
                        )
                    )
                operand = expression[operator]
                if operator == "$ifNull":
                    if not isinstance(operand, (list, tuple)) or len(operand) < 2:
                        raise OperationFailure(
                            "$ifNull requires at least two expressions"
                        )
                    for item in operand[:-1]:
                        value = self.evaluate(document, item, allowed_operators)
                        if value is not _MISSING and value is not None:
                            return value
                    return self.evaluate(document, operand[-1], allowed_operators)

                value = self.evaluate(
                    document, self._size_operand(operand), allowed_operators
                )
                if not isinstance(value, list):
                    raise OperationFailure("$size requires an array input")
                return len(value)

            result = {}
            for key, value in expression.items():
                evaluated = self.evaluate(document, value, allowed_operators)
                if evaluated is not _MISSING:
                    result[key] = evaluated
            return result
        return copy.deepcopy(expression)


class AggregationEngine(object):
    """Validate and execute supported pipeline stages over document iterables."""

    def __init__(self):
        self.context = AggregationContext()
        self.stage_registry = {
            "$match": (self._validate_match, self._match),
            "$project": (self._validate_project, self._project),
            "$group": (self._validate_group, self._group),
        }

    def prepare(self, pipeline):
        """Validate a pipeline before any storage is opened or read."""

        if not isinstance(pipeline, list):
            raise TypeError("pipeline must be a list")

        prepared = []
        for index, stage in enumerate(pipeline):
            name, argument = self._validate_stage(stage, index)
            stage_entry = self.stage_registry.get(name)
            if stage_entry is None:
                raise TinyMongoNotSupportedError(
                    "Aggregation stage {0} is not supported by TinyMongo".format(name)
                )
            validator, handler = stage_entry
            prepared_argument = validator(argument)
            prepared.append((name, handler, prepared_argument))
        return prepared

    @staticmethod
    def run_prepared(documents, prepared):
        current = documents
        for _name, handler, argument in prepared:
            current = handler(current, argument)
        return list(current)

    def run(self, documents, pipeline):
        return self.run_prepared(documents, self.prepare(pipeline))

    @staticmethod
    def _validate_stage(stage, index):
        if not isinstance(stage, Mapping) or len(stage) != 1:
            raise OperationFailure(
                "Aggregation pipeline stage {0} must contain exactly one field".format(
                    index
                )
            )
        name, argument = next(iter(stage.items()))
        if not isinstance(name, str) or not name.startswith("$"):
            raise OperationFailure(
                "Aggregation pipeline stage {0} must use a $-prefixed name".format(
                    index
                )
            )
        return name, argument

    @staticmethod
    def _validate_match(specification):
        if not isinstance(specification, Mapping):
            raise OperationFailure("$match stage must contain a query document")
        validate_filter_operators(specification)
        return copy.deepcopy(dict(specification))

    @staticmethod
    def _match(documents, specification):
        return (
            document
            for document in documents
            if matches_filter(document, specification)
        )

    @staticmethod
    def _is_projection_flag(value):
        return isinstance(value, Number) or (
            isinstance(value, Mapping)
            and not any(str(key).startswith("$") for key in value)
        )

    @classmethod
    def _has_non_id_exclusion(cls, specification, prefix=""):
        for field, value in specification.items():
            path = ".".join(part for part in (prefix, str(field)) if part)
            if isinstance(value, Mapping) and not any(
                str(key).startswith("$") for key in value
            ):
                if cls._has_non_id_exclusion(value, path):
                    return True
            elif isinstance(value, Number) and not bool(value) and path != "_id":
                return True
        return False

    def _validate_project(self, specification):
        if not isinstance(specification, Mapping) or not specification:
            raise OperationFailure("$project stage must contain a non-empty document")

        if self._has_non_id_exclusion(specification):
            raise TinyMongoNotSupportedError(
                "$project exclusion of fields other than _id is not supported"
            )

        shape = {}
        computed = []
        for output_field, expression in specification.items():
            if self._is_projection_flag(expression):
                shape[output_field] = copy.deepcopy(expression)
                continue
            self.context.validate_expression(expression, _SUPPORTED_EXPRESSIONS)
            shape[output_field] = 1
            computed.append((output_field, copy.deepcopy(expression)))

        projection = normalize_projection(shape)
        if any(
            isinstance(output_field, str) and "." in output_field
            for output_field, _expression in computed
        ):
            raise TinyMongoNotSupportedError(
                "$project computed dotted output paths are not supported"
            )
        return projection, tuple(computed)

    def _project(self, documents, prepared_project):
        projection, computed = prepared_project

        def project_one(document):
            projected = project_document(document, projection)
            for output_field, expression in computed:
                value = self.context.evaluate(
                    document, expression, _SUPPORTED_EXPRESSIONS
                )
                if value is _MISSING:
                    projected.pop(output_field, None)
                else:
                    projected[output_field] = copy.deepcopy(value)
            return projected

        return (project_one(document) for document in documents)

    def _group(self, documents, prepared_group):
        group_expression, accumulators = prepared_group
        groups = []
        groups_by_key = {}

        for document in documents:
            group_value = (
                None
                if group_expression is None
                else self.context.resolve_field_path(document, group_expression)
            )
            if group_value is _MISSING:
                group_value = None

            group_key = bson_value_identity_key(group_value)
            if group_key is None:
                raise TinyMongoNotSupportedError(
                    "$group cannot use values of type {0} as keys".format(
                        type(group_value).__name__
                    )
                )
            group = groups_by_key.get(group_key)
            if group is None:
                group = {
                    "key": copy.deepcopy(group_value),
                    "states": {
                        output_field: 0 if operator == "$sum" else _MISSING
                        for output_field, operator, _operand in accumulators
                    },
                }
                groups.append(group)
                groups_by_key[group_key] = group

            for output_field, operator, operand in accumulators:
                value = self.context.evaluate(document, operand)
                if operator == "$sum":
                    group["states"][output_field] += _sum_value(value)
                    continue

                # MongoDB ignores both null and missing values for min/max when
                # any comparable value exists, and returns null when none does.
                if value is _MISSING or value is None:
                    continue
                if bson_value_sort_key(value) is None:
                    raise TinyMongoNotSupportedError(
                        "Aggregation cannot compare values of type {0}".format(
                            type(value).__name__
                        )
                    )
                current = group["states"][output_field]
                if current is _MISSING or self._is_better(operator, value, current):
                    group["states"][output_field] = copy.deepcopy(value)

        results = []
        for group in groups:
            result = {"_id": copy.deepcopy(group["key"])}
            for output_field, operator, _operand in accumulators:
                value = group["states"][output_field]
                result[output_field] = (
                    None
                    if operator != "$sum" and value is _MISSING
                    else copy.deepcopy(value)
                )
            results.append(result)
        return results

    @staticmethod
    def _is_better(operator, candidate, current):
        candidate_key = bson_value_sort_key(candidate)
        current_key = bson_value_sort_key(current)
        if operator == "$min":
            return candidate_key < current_key
        return candidate_key > current_key

    def _validate_group(self, specification):
        if not isinstance(specification, Mapping):
            raise OperationFailure("$group stage must contain a document")
        if "_id" not in specification:
            raise OperationFailure("$group stage requires an _id expression")

        group_expression = specification["_id"]
        if group_expression is not None and (
            not isinstance(group_expression, str)
            or not group_expression.startswith("$")
            or group_expression.startswith("$$")
            or len(group_expression) == 1
        ):
            raise TinyMongoNotSupportedError(
                "$group _id currently supports a field path or None"
            )
        if group_expression is not None:
            self.context.validate_field_path(group_expression)

        accumulators = []
        for output_field, accumulator in specification.items():
            if output_field == "_id":
                continue
            if (
                not isinstance(output_field, str)
                or output_field.startswith("$")
                or "." in output_field
            ):
                raise OperationFailure(
                    "$group output field names must be plain strings"
                )
            if not isinstance(accumulator, Mapping) or len(accumulator) != 1:
                raise OperationFailure(
                    "$group output field {0!r} must contain one accumulator".format(
                        output_field
                    )
                )
            operator, operand = next(iter(accumulator.items()))
            if operator not in _SUPPORTED_ACCUMULATORS:
                raise TinyMongoNotSupportedError(
                    "Aggregation accumulator {0} is not supported by TinyMongo".format(
                        operator
                    )
                )
            self.context.validate_expression(operand)
            accumulators.append((output_field, operator, operand))
        return group_expression, accumulators
