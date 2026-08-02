"""Mongo-style inclusion and exclusion projections."""

import copy
from collections.abc import Mapping, MutableMapping, Sequence, Set

from .bson_types import bson_number_truth, is_bson_number
from .errors import OperationFailure, TinyMongoNotSupportedError


_LEAF = object()
_OMIT = object()


class ProjectionSpec(object):
    """Normalized basic projection used by every storage backend."""

    __slots__ = ("mode", "tree", "include_id")

    def __init__(self, mode, tree, include_id):
        self.mode = mode
        self.tree = tree
        self.include_id = include_id


def _projection_path(path):
    if not isinstance(path, str):
        raise TypeError("projection field names must be strings")
    if not path or "\x00" in path:
        raise OperationFailure("Projection field names cannot be empty", code=40352)

    parts = path.split(".")
    if any(not part for part in parts):
        raise OperationFailure(
            "Projection field paths cannot contain empty components", code=15998
        )
    if any(part.startswith("$") for part in parts):
        raise TinyMongoNotSupportedError(
            "Projection operators and positional projection are not supported"
        )
    if any(part.lstrip("-").isdigit() for part in parts):
        raise TinyMongoNotSupportedError(
            "Projection through numeric array indexes is not supported"
        )
    return parts


def _flatten_mapping(mapping, prefix=None):
    flattened = []
    for key, value in mapping.items():
        parts = _projection_path(key)
        path = ".".join(([prefix] if prefix else []) + parts)
        if isinstance(value, Mapping):
            if not value:
                raise TinyMongoNotSupportedError(
                    "Empty and expression projection mappings are not supported"
                )
            flattened.extend(_flatten_mapping(value, path))
        elif isinstance(value, bool) or is_bson_number(value):
            include = (
                bool(value) if isinstance(value, bool) else bson_number_truth(value)
            )
            flattened.append((path, include))
        else:
            raise TinyMongoNotSupportedError(
                "Only numeric inclusion and exclusion projection flags are supported"
            )
    return flattened


def _projection_items(projection):
    if isinstance(projection, Mapping):
        return _flatten_mapping(projection)
    if isinstance(projection, (Sequence, Set)) and not isinstance(
        projection, (str, bytes, bytearray)
    ):
        paths = (".".join(_projection_path(path)) for path in projection)
        return [(path, True) for path in dict.fromkeys(paths)]
    raise TypeError("projection must be a mapping or list of field names")


def _add_path(tree, path):
    node = tree
    for part in path.split("."):
        if _LEAF in node:
            raise OperationFailure("Path collision at {0}".format(path), code=31249)
        node = node.setdefault(part, {})
    if node:
        raise OperationFailure("Path collision at {0}".format(path), code=31250)
    node[_LEAF] = True


def normalize_projection(projection):
    """Validate and normalize a basic MongoDB projection document."""
    if projection is None:
        return None

    items = _projection_items(projection)
    if not items:
        return None

    mode = None
    include_id = True
    paths = []
    collision_tree = {}
    for path, include in items:
        _add_path(collision_tree, path)
        if path == "_id":
            include_id = include
            continue
        if mode is None:
            mode = "include" if include else "exclude"
        elif (mode == "include") != include:
            code = 31254 if mode == "include" else 31253
            raise OperationFailure(
                "Cannot mix inclusion and exclusion in a projection", code=code
            )
        paths.append(path)

    if mode is None:
        mode = "include" if include_id else "exclude"

    tree = {}
    for path in paths:
        _add_path(tree, path)
    return ProjectionSpec(mode, tree, include_id)


def _include_value(value, tree):
    if _LEAF in tree:
        return copy.deepcopy(value)
    if isinstance(value, Mapping):
        result = {}
        for key, child_value in value.items():
            if key not in tree:
                continue
            child = _include_value(child_value, tree[key])
            if child is not _OMIT:
                result[key] = child
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            if not isinstance(item, (Mapping, list)):
                continue
            result.append(_include_value(item, tree))
        return result
    return _OMIT


def _exclude_value(value, tree):
    if isinstance(value, MutableMapping):
        for key, child_tree in tree.items():
            if key not in value:
                continue
            if _LEAF in child_tree:
                value.pop(key, None)
            else:
                _exclude_value(value[key], child_tree)
    elif isinstance(value, list):
        for item in value:
            _exclude_value(item, tree)


def project_document(document, projection):
    """Return a projected copy of *document* using a normalized spec."""
    if document is None or projection is None:
        return document

    if projection.mode == "exclude":
        result = copy.deepcopy(document)
        _exclude_value(result, projection.tree)
        if not projection.include_id:
            result.pop("_id", None)
        return result

    result = {}
    for key, value in document.items():
        if key == "_id" and key not in projection.tree:
            included = copy.deepcopy(value) if projection.include_id else _OMIT
        elif key in projection.tree:
            included = _include_value(value, projection.tree[key])
        else:
            included = _OMIT
        if included is not _OMIT:
            result[key] = included
    return result
