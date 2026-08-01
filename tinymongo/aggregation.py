"""Backend-independent aggregation pipeline execution.

The supported slice stays deliberately smaller than MongoDB's full aggregation
language. Keeping it separate from storage lets every TinyMongo backend share
stage validation, field-path behavior, missing values, and BSON comparisons.
"""

from __future__ import absolute_import

import copy
from collections.abc import Mapping
from numbers import Number

from .bson_types import (
    binary_components,
    binary_type,
    bson_value_identity_key,
    bson_value_sort_key,
)
from .errors import OperationFailure, TinyMongoNotSupportedError
from .projection import normalize_projection, project_document
from .table_backends import matches_filter, validate_filter_operators


_MISSING = object()
_BINARY = binary_type()
_PROJECT_LEAF = object()
_PROJECT_INCLUDE = object()
_PROJECT_COMPUTED = object()
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
_SUPPORTED_EXPRESSIONS = ("$ifNull", "$literal", "$size")
_SUPPORTED_STAGES = (
    "$match",
    "$project",
    "$set",
    "$addFields",
    "$unset",
    "$group",
)


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
            # Field references traverse arrays of documents, but do not cross
            # a raw array nested directly inside another array at the same
            # path component. Projection output traversal has different rules
            # and is handled by the stage-specific renderer below.
            if not isinstance(item, Mapping):
                continue
            candidate = _resolve_parts(item, parts)
            if candidate is not _MISSING:
                resolved.append(candidate)
        # Once a path traverses an array, MongoDB preserves an empty traversal
        # as an empty array. Only a missing field before array traversal is
        # represented by the missing sentinel.
        return resolved

    return _MISSING


def _literal_value(value):
    """Copy a ``$literal`` operand while normalizing BSON arrays to lists."""

    if isinstance(value, Mapping):
        return {key: _literal_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_literal_value(item) for item in value]
    if _BINARY is not None and isinstance(value, _BINARY):
        raw, subtype = binary_components(value)
        if subtype == 0:
            return raw
    return copy.deepcopy(value)


def _contains_dollar_operator(value):
    return isinstance(value, Mapping) and any(str(key).startswith("$") for key in value)


def _flatten_stage_specification(specification, project, prefix=None):
    """Flatten MongoDB's nested projection syntax without losing order."""

    flattened = []
    for field, value in specification.items():
        if not isinstance(field, str):
            raise TypeError("projection field names must be strings")
        if project and not field:
            raise OperationFailure("Projection field names cannot be empty", code=40352)
        path = ".".join(part for part in (prefix, field) if part is not None)
        if isinstance(value, Mapping) and not _contains_dollar_operator(value):
            if value:
                flattened.extend(
                    _flatten_stage_specification(value, project, prefix=path)
                )
                continue
            if project:
                raise OperationFailure(
                    "An empty sub-projection is not a valid $project expression",
                    code=51270,
                )
        if project and isinstance(value, Number):
            flattened.append((path, "flag", bool(value)))
        else:
            flattened.append((path, "computed", copy.deepcopy(value)))
    return flattened


def _ensure_unique_paths(items):
    seen = set()
    for path, _kind, _value in items:
        if path in seen:
            raise OperationFailure("Path collision at {0}".format(path), code=31250)
        seen.add(path)


def _projection_shape(items):
    return {path: (value if kind == "flag" else 1) for path, kind, value in items}


def _validate_stage_paths(items, collision_code=None):
    """Validate path syntax before duplicates, collisions, or stage modes."""

    for path, _kind, _value in items:
        if path.endswith("."):
            raise OperationFailure(
                "Aggregation output paths must not end with '.'", code=40353
            )
        if any(part.startswith("$") for part in path.split(".")):
            raise OperationFailure(
                "Aggregation output path components may not start with '$'",
                code=16410,
            )
        normalize_projection({path: 1})

    try:
        _ensure_unique_paths(items)
        normalize_projection({path: 1 for path, _kind, _value in items})
    except OperationFailure as error:
        if collision_code is not None and error.code in (31249, 31250):
            raise OperationFailure(
                "Conflicting paths in $set or $addFields", code=collision_code
            )
        raise


def _add_project_path(tree, path, leaf):
    node = tree
    for part in path.split("."):
        node = node.setdefault(part, {})
    node[_PROJECT_LEAF] = leaf


def _project_tree_has_computed(node):
    leaf = node.get(_PROJECT_LEAF)
    if leaf is not None:
        return leaf[0] is _PROJECT_COMPUTED
    return any(_project_tree_has_computed(child) for child in node.values())


def _render_project_node(source, node, computed_values):
    """Render one compiled inclusion/computed projection tree node."""

    leaf = node.get(_PROJECT_LEAF)
    if leaf is not None:
        kind, path = leaf
        value = source if kind is _PROJECT_INCLUDE else computed_values[path]
        return _MISSING if value is _MISSING else copy.deepcopy(value)

    if isinstance(source, Mapping):
        result = {}
        traversed = set()
        for key, child_source in source.items():
            child = node.get(key)
            if child is None:
                continue
            leaf = child.get(_PROJECT_LEAF)
            if leaf is not None and leaf[0] is _PROJECT_COMPUTED:
                continue
            traversed.add(key)
            rendered = _render_project_node(child_source, child, computed_values)
            if rendered is not _MISSING:
                result[key] = rendered

        # Direct computed fields do not retain their source position. MongoDB
        # appends them in projection-spec order after every retained/traversed
        # source field. Missing parents created by dotted computations append
        # in that same second pass.
        for key, child in node.items():
            if key in traversed:
                continue
            rendered = _render_project_node(
                source.get(key, _MISSING), child, computed_values
            )
            if rendered is not _MISSING:
                result[key] = rendered
        return result

    if isinstance(source, (list, tuple)):
        result = []
        has_computed = _project_tree_has_computed(node)
        for item in source:
            if not has_computed and not isinstance(item, (Mapping, list, tuple)):
                continue
            rendered = _render_project_node(item, node, computed_values)
            result.append(rendered)
        return result

    if not _project_tree_has_computed(node):
        return _MISSING

    # A computed descendant creates an object when its source parent is
    # missing, null, or scalar. Missing leaves are omitted but the structural
    # shell remains, matching MongoDB's dotted computed-output behavior.
    result = {}
    for key, child in node.items():
        rendered = _render_project_node(_MISSING, child, computed_values)
        if rendered is not _MISSING:
            result[key] = rendered
    return result


def _assign_aggregation_path(container, parts, value):
    """Apply one dotted add-fields assignment, broadcasting through arrays."""

    if isinstance(container, list):
        for index, item in enumerate(container):
            if isinstance(item, tuple):
                item = list(item)
                container[index] = item
            elif not isinstance(item, (Mapping, list)):
                item = {}
                container[index] = item
            _assign_aggregation_path(item, parts, value)
        return

    field = parts[0]
    if len(parts) == 1:
        if value is _MISSING:
            container.pop(field, None)
        else:
            container[field] = copy.deepcopy(value)
        return

    child = container.get(field, _MISSING)
    if isinstance(child, tuple):
        child = list(child)
        container[field] = child
    elif not isinstance(child, (Mapping, list)):
        child = {}
        container[field] = child
    _assign_aggregation_path(child, parts[1:], value)


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
                raise OperationFailure(
                    "$size requires exactly one expression", code=16020
                )
            return operand[0]
        return operand

    def _is_remove_reference(self, expression):
        if expression == "$$REMOVE":
            return True
        if not isinstance(expression, str) or not expression.startswith("$$REMOVE."):
            return False
        suffix = expression[len("$$REMOVE.") :]
        self.validate_field_path("$placeholder." + suffix)
        return True

    def validate_expression(self, expression, allowed_operators=(), allow_remove=False):
        """Reject unsupported expression operators before reading documents."""

        if allow_remove and self._is_remove_reference(expression):
            return
        if isinstance(expression, str) and expression.startswith("$"):
            self.validate_field_path(expression)
            return
        if isinstance(expression, (list, tuple)):
            for item in expression:
                self.validate_expression(item, allowed_operators, allow_remove)
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
                if operator == "$literal":
                    return
                if operator == "$ifNull":
                    if not isinstance(operand, (list, tuple)) or len(operand) < 2:
                        raise OperationFailure(
                            "$ifNull requires at least two expressions", code=1257300
                        )
                    for item in operand:
                        self.validate_expression(item, allowed_operators, allow_remove)
                    return
                self.validate_expression(
                    self._size_operand(operand), allowed_operators, allow_remove
                )
                return
            for value in expression.values():
                self.validate_expression(value, allowed_operators, allow_remove)

    def resolve_field_path(self, document, expression):
        self.validate_field_path(expression)
        return _resolve_parts(document, expression[1:].split("."))

    def evaluate(self, document, expression, allowed_operators=(), allow_remove=False):
        if allow_remove and self._is_remove_reference(expression):
            return _MISSING
        if isinstance(expression, str) and expression.startswith("$"):
            return self.resolve_field_path(document, expression)
        if isinstance(expression, list):
            values = [
                self.evaluate(document, item, allowed_operators, allow_remove)
                for item in expression
            ]
            return [None if value is _MISSING else value for value in values]
        if isinstance(expression, tuple):
            values = [
                self.evaluate(document, item, allowed_operators, allow_remove)
                for item in expression
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
                if operator == "$literal":
                    return _literal_value(operand)
                if operator == "$ifNull":
                    if not isinstance(operand, (list, tuple)) or len(operand) < 2:
                        raise OperationFailure(
                            "$ifNull requires at least two expressions", code=1257300
                        )
                    for item in operand[:-1]:
                        value = self.evaluate(
                            document, item, allowed_operators, allow_remove
                        )
                        if value is not _MISSING and value is not None:
                            return value
                    return self.evaluate(
                        document, operand[-1], allowed_operators, allow_remove
                    )

                value = self.evaluate(
                    document,
                    self._size_operand(operand),
                    allowed_operators,
                    allow_remove,
                )
                if not isinstance(value, list):
                    raise OperationFailure("$size requires an array input", code=17124)
                return len(value)

            result = {}
            for key, value in expression.items():
                evaluated = self.evaluate(
                    document, value, allowed_operators, allow_remove
                )
                if evaluated is not _MISSING:
                    result[key] = evaluated
            return result
        return _literal_value(expression)


class AggregationEngine(object):
    """Validate and execute supported pipeline stages over document iterables."""

    def __init__(self):
        self.context = AggregationContext()
        self.stage_registry = {
            "$match": (self._validate_match, self._match),
            "$project": (self._validate_project, self._project),
            "$set": (self._validate_add_fields, self._add_fields),
            "$addFields": (self._validate_add_fields, self._add_fields),
            "$unset": (self._validate_unset, self._unset),
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

    def _validate_project(self, specification):
        if not isinstance(specification, Mapping):
            raise OperationFailure("$project stage must contain a document", code=15969)
        if not specification:
            raise OperationFailure(
                "$project stage must contain a non-empty document", code=51272
            )

        items = _flatten_stage_specification(specification, project=True)
        _validate_stage_paths(items)

        # MongoDB chooses the mixed-mode error from the order in which paths
        # establish the projection mode. Exact _id flags remain the one special
        # inclusion/exclusion exception.
        mode = None
        computed = []
        for path, kind, value in items:
            if kind == "flag" and path == "_id":
                continue
            if kind == "computed":
                if mode == "exclude" and _contains_dollar_operator(value):
                    raise OperationFailure(
                        "Cannot use an expression in an exclusion projection",
                        code=31252,
                    )
                self.context.validate_expression(
                    value, _SUPPORTED_EXPRESSIONS, allow_remove=True
                )
                if mode == "exclude":
                    raise OperationFailure(
                        "Cannot use an expression in an exclusion projection",
                        code=31310,
                    )
                mode = "include"
                computed.append((path, value))
                continue
            candidate_mode = "include" if value else "exclude"
            if mode is not None and mode != candidate_mode:
                code = 31254 if mode == "include" else 31253
                raise OperationFailure(
                    "Cannot mix inclusion and exclusion in a projection", code=code
                )
            mode = candidate_mode

        projection = normalize_projection(_projection_shape(items))

        if not computed:
            return "basic", projection

        tree = {}
        if not any(
            path == "_id" or path.startswith("_id.") for path, _kind, _value in items
        ):
            _add_project_path(tree, "_id", (_PROJECT_INCLUDE, "_id"))
        for path, kind, value in items:
            if kind == "flag":
                if not value:
                    # In computed/inclusion mode only exact _id exclusion can
                    # reach this branch; mixed non-id exclusions failed above.
                    continue
                leaf = (_PROJECT_INCLUDE, path)
            else:
                leaf = (_PROJECT_COMPUTED, path)
            _add_project_path(tree, path, leaf)
        return "computed", tree, tuple(computed)

    def _project(self, documents, prepared_project):
        if prepared_project[0] == "basic":
            projection = prepared_project[1]
            return (project_document(document, projection) for document in documents)

        _mode, tree, computed = prepared_project

        def project_one(document):
            computed_values = {
                path: self.context.evaluate(
                    document,
                    expression,
                    _SUPPORTED_EXPRESSIONS,
                    allow_remove=True,
                )
                for path, expression in computed
            }
            return _render_project_node(document, tree, computed_values)

        return (project_one(document) for document in documents)

    def _validate_add_fields(self, specification):
        if not isinstance(specification, Mapping):
            raise OperationFailure(
                "$set and $addFields stages must contain a document", code=40272
            )

        items = _flatten_stage_specification(specification, project=False)
        _validate_stage_paths(items, collision_code=40176)

        for _path, _kind, expression in items:
            self.context.validate_expression(
                expression, _SUPPORTED_EXPRESSIONS, allow_remove=True
            )
        return tuple((path, expression) for path, _kind, expression in items)

    def _add_fields(self, documents, assignments):
        def add_fields_one(document):
            evaluated = [
                (
                    path,
                    self.context.evaluate(
                        document,
                        expression,
                        _SUPPORTED_EXPRESSIONS,
                        allow_remove=True,
                    ),
                )
                for path, expression in assignments
            ]
            result = copy.deepcopy(document)
            for path, value in evaluated:
                _assign_aggregation_path(result, path.split("."), value)
            return result

        return (add_fields_one(document) for document in documents)

    @staticmethod
    def _validate_unset(specification):
        if isinstance(specification, str):
            paths = [specification]
        elif isinstance(specification, (list, tuple)):
            if not specification:
                raise OperationFailure("$unset requires at least one field", code=31119)
            if any(not isinstance(path, str) for path in specification):
                raise OperationFailure("$unset field names must be strings", code=31120)
            paths = list(specification)
        else:
            raise OperationFailure(
                "$unset requires a string or an array of strings", code=31002
            )

        items = [(path, "flag", False) for path in paths]
        _validate_stage_paths(items)
        return normalize_projection({path: 0 for path in paths})

    @staticmethod
    def _unset(documents, projection):
        return (project_document(document, projection) for document in documents)

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
