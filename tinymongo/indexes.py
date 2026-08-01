"""Backend-independent helpers for TinyMongo index definitions and keys."""

import json
import math
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Mapping, Optional

from .errors import DuplicateKeyError, TinyMongoNotSupportedError
from .bson_types import bson_identity_key, bson_number_decimal, decimal128_type
from .warning_context import emit_warning


_Decimal128 = decimal128_type()


INDEX_METADATA_VERSION = 1
INDEX_CATALOG_TABLE = "__tinymongo_indexes"
MISSING = object()
_ALLOWED_OPTIONS = {"name", "unique"}
_MODEL_ALLOWED_OPTIONS = {
    "background",
    "expireAfterSeconds",
    "name",
    "sparse",
    "unique",
}


class TinyMongoUnsupportedWarning(UserWarning):
    """Warn that a request was accepted with explicitly reduced behavior."""


def _unsupported(message):
    raise TinyMongoNotSupportedError(message)


def _validate_field(field):
    if not isinstance(field, str) or not field:
        _unsupported("Index fields must be non-empty strings")
    if "\x00" in field or any(not part for part in field.split(".")):
        _unsupported("Index fields must use valid non-empty dotted paths")
    if any(part.startswith("$") for part in field.split(".")):
        _unsupported("Index field components cannot start with '$'")


def _parse_key(key):
    if isinstance(key, str):
        return key, 1

    pair = None
    if isinstance(key, (list, tuple)) and len(key) == 2 and isinstance(key[0], str):
        pair = key
    elif isinstance(key, (list, tuple)):
        if len(key) != 1:
            _unsupported("Only single-field indexes are supported")
        candidate = key[0]
        if isinstance(candidate, (list, tuple)) and len(candidate) == 2:
            pair = candidate

    if pair is None:
        _unsupported("Index keys must be a field string or one (field, direction) pair")

    field, direction = pair
    if not isinstance(field, str):
        _unsupported("Index fields must be non-empty strings")
    return field, direction


def _validate_index_name(name, field=None):
    if not isinstance(name, str) or not name or "\x00" in name:
        _unsupported("Index names must be non-empty strings")
    if field != "_id" and name in ("_id", "_id_"):
        _unsupported("The built-in _id index names are reserved")


def _default_index_name(keys):
    return "_".join("{0}_{1}".format(field, direction) for field, direction in keys)


@dataclass(frozen=True)
class IndexSpec:
    """A normalized index definition supported by TinyMongo."""

    field: str
    direction: int = 1
    unique: bool = False
    name: Optional[str] = None

    def __post_init__(self):
        _validate_field(self.field)
        if self.direction != 1 or isinstance(self.direction, bool):
            _unsupported("Only ascending index direction 1 is supported")
        if not isinstance(self.unique, bool):
            _unsupported("The unique index option must be a boolean")

        name = self.name
        if name is None:
            name = "{0}_1".format(self.field)
        _validate_index_name(name, self.field)
        object.__setattr__(self, "name", name)

    def to_metadata(self):
        """Return a JSON-safe durable representation of this index."""
        return {
            "v": INDEX_METADATA_VERSION,
            "name": self.name,
            "key": [[self.field, self.direction]],
            "unique": self.unique,
        }

    @classmethod
    def from_metadata(cls, metadata):
        """Restore and validate an index from durable metadata."""
        if not isinstance(metadata, Mapping):
            raise ValueError("Index metadata must be a mapping")
        if metadata.get("v") != INDEX_METADATA_VERSION:
            raise ValueError("Unsupported index metadata version")
        required = {"v", "name", "key", "unique"}
        if set(metadata) != required:
            raise ValueError("Index metadata has missing or unknown fields")
        return parse_index_spec(
            metadata["key"],
            name=metadata["name"],
            unique=metadata["unique"],
        )


def index_spec_signature(spec):
    """Return the key-and-options identity used to detect equivalent indexes."""
    if not isinstance(spec, IndexSpec):
        raise TypeError("Index signatures require an IndexSpec")
    return (spec.field, spec.direction, spec.unique)


def parse_index_spec(key, **options):
    """Normalize a supported single-field ascending index definition."""
    unknown = sorted(set(options) - _ALLOWED_OPTIONS)
    if unknown:
        _unsupported("Unsupported index option(s): {0}".format(", ".join(unknown)))

    field, direction = _parse_key(key)
    return IndexSpec(
        field=field,
        direction=direction,
        unique=options.get("unique", False),
        name=options.get("name"),
    )


@dataclass(frozen=True)
class IndexModelPlan:
    """One normalized outcome from a PyMongo-style index declaration.

    ``spec`` is the effective index TinyMongo can create. Compound indexes use
    their ascending leading-field prefix, matching MongoDB's leftmost-prefix
    behavior as closely as a single-field backend can. It is ``None`` when no
    useful safe fallback exists, such as a text index. ``degraded_features``
    records every requested feature that is not honored.
    """

    name: str
    requested_keys: tuple
    requested_options: tuple
    spec: Optional[IndexSpec]
    degraded_features: tuple = ()
    warning: Optional[str] = None

    @property
    def outcome(self):
        """Return ``create``, ``create_degraded``, or ``skip``."""
        if self.spec is None:
            return "skip"
        if self.degraded_features:
            return "create_degraded"
        return "create"

    def to_metadata(self):
        """Return JSON-safe planning metadata for diagnostics and tests."""
        return {
            "name": self.name,
            "requested_key": [list(pair) for pair in self.requested_keys],
            "requested_options": dict(self.requested_options),
            "outcome": self.outcome,
            "degraded_features": list(self.degraded_features),
            "effective_spec": None if self.spec is None else self.spec.to_metadata(),
        }


@dataclass(frozen=True)
class IndexBatchPlan:
    """Ordered index-model outcomes suitable for ``create_indexes`` wiring."""

    entries: tuple

    def __iter__(self):
        return iter(self.entries)

    @property
    def names(self):
        """Return requested index names in input order."""
        return tuple(entry.name for entry in self.entries)

    @property
    def specs(self):
        """Return only effective index specs that should be created."""
        return tuple(entry.spec for entry in self.entries if entry.spec is not None)

    @property
    def warnings(self):
        """Return warning messages in input order."""
        return tuple(entry.warning for entry in self.entries if entry.warning)


def _index_model_document(model):
    if isinstance(model, Mapping):
        document = model
    else:
        try:
            document = model.document
        except AttributeError:
            raise TypeError(
                "Index models must expose a mapping-valued document attribute"
            ) from None
    if not isinstance(document, Mapping):
        raise TypeError("Index model document must be a mapping")
    return document


def _index_model_keys(key_document):
    if isinstance(key_document, Mapping):
        keys = tuple(key_document.items())
    elif isinstance(key_document, (list, tuple)):
        keys = tuple(key_document)
    else:
        _unsupported("IndexModel key must be a mapping or sequence of pairs")
    if not keys:
        _unsupported("IndexModel key must contain at least one field")

    normalized = []
    for pair in keys:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _unsupported("IndexModel keys must contain (field, direction) pairs")
        field, direction = pair
        _validate_field(field)
        if isinstance(direction, bool) or direction not in (1, -1, "hashed", "text"):
            _unsupported(
                "IndexModel directions must be 1, -1, 'hashed', or 'text'; got "
                "{0!r}".format(direction)
            )
        normalized.append((field, direction))
    return tuple(normalized)


def _validate_model_options(options):
    unknown = sorted(set(options) - _MODEL_ALLOWED_OPTIONS)
    if unknown:
        _unsupported("Unsupported index option(s): {0}".format(", ".join(unknown)))

    unique = options.get("unique", False)
    if not isinstance(unique, bool):
        _unsupported("The unique index option must be a boolean")

    for option in ("sparse", "background"):
        if option in options and not isinstance(options[option], bool):
            _unsupported("The {0} index option must be a boolean".format(option))

    if "expireAfterSeconds" in options:
        seconds = options["expireAfterSeconds"]
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            _unsupported("The expireAfterSeconds index option must be non-negative")


def _degraded_feature_message(feature):
    messages = {
        "background": "background creation is ignored",
        "compound": "compound indexing is reduced to its ascending leading field",
        "descending": "descending direction is treated as ascending",
        "hashed": "hashed indexing is replaced by ascending equality indexing",
        "sparse": "sparse membership is not honored",
        "text": "text indexing is ignored because $text queries are not supported",
        "ttl": "TTL expiration is not performed",
    }
    return messages[feature]


def _plan_warning(name, features):
    details = "; ".join(_degraded_feature_message(item) for item in features)
    return "Index {0!r} was created with reduced behavior: {1}.".format(name, details)


def degraded_index_reuse_warning(plan, existing):
    """Describe one degraded declaration being satisfied by an existing index."""
    if not isinstance(plan, IndexModelPlan) or plan.spec is None:
        raise TypeError("Degraded index reuse requires an effective index plan")
    if not isinstance(existing, IndexSpec):
        raise TypeError("Degraded index reuse requires an existing IndexSpec")
    if not plan.degraded_features:
        raise ValueError("Only degraded index plans may reuse an equivalent index")
    if index_spec_signature(plan.spec) != index_spec_signature(existing):
        raise ValueError("A degraded index may only reuse an equivalent index")

    details = "; ".join(
        _degraded_feature_message(item) for item in plan.degraded_features
    )
    return (
        "Index {0!r} was accepted with reduced behavior: {1}. Its effective "
        "specification matches existing index {2!r}, so TinyMongo reused that "
        "index instead of creating a duplicate."
    ).format(plan.name, details, existing.name)


def plan_index_model(model):
    """Plan one duck-typed PyMongo ``IndexModel`` declaration.

    PyMongo is deliberately not imported. Any object whose ``document``
    attribute is a mapping with ``key`` and index options is accepted. A plain
    mapping is also accepted for adapters and contract tests.
    """
    if isinstance(model, IndexSpec):
        return IndexModelPlan(
            name=model.name,
            requested_keys=((model.field, model.direction),),
            requested_options=(("name", model.name), ("unique", model.unique)),
            spec=model,
        )

    document = _index_model_document(model)
    if "key" not in document:
        _unsupported("IndexModel document must contain a key definition")
    keys = _index_model_keys(document["key"])
    options = {key: value for key, value in document.items() if key != "key"}
    _validate_model_options(options)

    name = options.get("name")
    if name is None:
        name = _default_index_name(keys)
    first_field = keys[0][0] if len(keys) == 1 else None
    _validate_index_name(name, first_field)

    unique = options.get("unique", False)
    features = []
    if len(keys) > 1:
        features.append("compound")
    if any(direction == -1 for _, direction in keys):
        features.append("descending")
    if any(direction == "hashed" for _, direction in keys):
        features.append("hashed")
    if any(direction == "text" for _, direction in keys):
        features.append("text")
    if options.get("sparse", False):
        features.append("sparse")
    if "expireAfterSeconds" in options:
        features.append("ttl")
    if options.get("background", False):
        features.append("background")

    unsafe_unique_features = {"compound", "hashed", "sparse", "text", "ttl"}
    unsafe = [item for item in features if item in unsafe_unique_features]
    if unique and unsafe:
        _unsupported(
            "Unique index {0!r} cannot be degraded because {1} would change its "
            "semantics".format(name, ", ".join(unsafe))
        )

    spec = None
    if "text" not in features:
        spec = IndexSpec(
            field=keys[0][0],
            direction=1,
            unique=unique,
            name=name,
        )
    degraded = tuple(features)
    warning = _plan_warning(name, degraded) if degraded else None
    return IndexModelPlan(
        name=name,
        requested_keys=keys,
        requested_options=tuple(options.items()),
        spec=spec,
        degraded_features=degraded,
        warning=warning,
    )


def plan_index_models(models):
    """Return ordered outcomes for a ``create_indexes`` model iterable."""
    try:
        entries = tuple(plan_index_model(model) for model in models)
    except TypeError as exc:
        if "not iterable" not in str(exc):
            raise
        raise TypeError("Index models must be an iterable") from None
    return IndexBatchPlan(entries)


def emit_index_plan_warnings(plan, stacklevel=2):
    """Emit dedicated warnings for every degraded batch entry."""
    if not isinstance(plan, IndexBatchPlan):
        raise TypeError("Index warning emission requires an IndexBatchPlan")
    for message in plan.warnings:
        emit_warning(
            message,
            TinyMongoUnsupportedWarning,
            stacklevel=stacklevel,
        )


def index_catalog_id(collection, index_name):
    """Encode an unambiguous durable identity for a collection index."""
    return json.dumps(
        [str(collection), str(index_name)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _nested_value(document, path):
    current = document
    for part in path.split("."):
        if isinstance(current, (list, tuple)):
            _unsupported("Array traversal inside dotted index paths is not supported")
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


def _exact_decimal_text(value):
    """Normalize a Decimal without rounding its coefficient to 28 digits."""

    with localcontext() as context:
        context.prec = max(1, len(value.as_tuple().digits))
        return str(value.normalize())


def _float_token(value):
    """Return a token for the double's exact numeric value."""

    return "number:{0}".format(_exact_decimal_text(Decimal.from_float(value)))


def _scalar_token(value):
    if value is MISSING or value is None:
        return "null:"
    if isinstance(value, bool):
        return "bool:{0}".format("true" if value else "false")
    if isinstance(value, int):
        if value == 0:
            return "number:0"
        return "number:{0}".format(_exact_decimal_text(Decimal(value)))
    if isinstance(value, float):
        if not math.isfinite(value):
            _unsupported("Non-finite numbers cannot be indexed")
        if value == 0:
            return "number:0"
        return _float_token(value)
    if _Decimal128 is not None and isinstance(value, _Decimal128):
        decimal_value = bson_number_decimal(value)
        if not decimal_value.is_finite():
            _unsupported("Non-finite numbers cannot be indexed")
        if decimal_value == 0:
            return "number:0"
        return "number:{0}".format(_exact_decimal_text(decimal_value))
    if isinstance(value, str):
        return "string:{0}".format(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
    identity = bson_identity_key(value)
    if identity is not None and identity[0] == "binary":
        subtype, raw = identity[1]
        return "binary:{0}:{1}".format(subtype, raw.hex())
    if identity is not None and identity[0] == "regex":
        pattern, options = identity[1]
        return "regex:{0}".format(
            json.dumps(
                [pattern, options],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    if isinstance(value, Mapping):
        _unsupported("Object values cannot be indexed")
    if isinstance(value, (list, tuple)):
        _unsupported("Nested array values cannot be indexed")
    _unsupported("Unsupported indexed value type: {0}".format(type(value).__name__))


def index_tokens(document, field):
    """Return deterministic index tokens for a document's nested field."""
    if not isinstance(document, Mapping):
        raise TypeError("Indexed documents must be mappings")
    _validate_field(field)

    value = _nested_value(document, field)
    values = value if isinstance(value, list) else [value]
    if not values:
        return ("undefined:",)
    tokens = []
    seen = set()
    for item in values:
        token = _scalar_token(item)
        if token not in seen:
            tokens.append(token)
            seen.add(token)
    return tuple(tokens)


def validate_unique_documents(documents, indexes):
    """Raise when post-image documents conflict on a unique index."""
    docs = list(documents)
    specs = list(indexes)
    for spec in specs:
        if not isinstance(spec, IndexSpec):
            raise TypeError("Unique index validation requires IndexSpec values")
        if not spec.unique:
            continue

        owners = {}
        for position, document in enumerate(docs):
            if not isinstance(document, Mapping):
                raise TypeError("Indexed documents must be mappings")
            identity = document.get("_id", "position {0}".format(position))
            for token in index_tokens(document, spec.field):
                if token in owners:
                    raise DuplicateKeyError(
                        "duplicate key for unique index {0}: documents {1!r} and "
                        "{2!r}".format(spec.name, owners[token], identity)
                    )
                owners[token] = identity


__all__ = [
    "INDEX_METADATA_VERSION",
    "INDEX_CATALOG_TABLE",
    "IndexBatchPlan",
    "IndexModelPlan",
    "IndexSpec",
    "MISSING",
    "TinyMongoUnsupportedWarning",
    "degraded_index_reuse_warning",
    "emit_index_plan_warnings",
    "index_catalog_id",
    "index_spec_signature",
    "index_tokens",
    "parse_index_spec",
    "plan_index_model",
    "plan_index_models",
    "validate_unique_documents",
]
