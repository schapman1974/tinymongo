"""Small reusable helpers for behavioral compatibility contracts."""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from tinymongo.errors import DuplicateKeyError as TinyMongoDuplicateKeyError
from tinymongo.errors import OperationFailure as TinyMongoOperationFailure

try:
    from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError
    from pymongo.errors import OperationFailure as PyMongoOperationFailure
except ImportError:  # pragma: no cover - development dependency guard
    PyMongoDuplicateKeyError = ()  # type: ignore[assignment,misc]
    PyMongoOperationFailure = ()  # type: ignore[assignment,misc]


@dataclass(frozen=True)
class Outcome:
    """Normalized result of one operation against a contract target."""

    value: Any = None
    error: Optional[str] = None


@dataclass
class ContractTarget:
    """Objects and metadata exposed to each shared contract."""

    name: str
    client: Any
    database: Any
    collection: Any


def error_category(error: Exception) -> str:
    """Map backend-specific exceptions to a small compatibility vocabulary."""

    duplicate_errors = (TinyMongoDuplicateKeyError,)
    if PyMongoDuplicateKeyError:
        duplicate_errors = duplicate_errors + (PyMongoDuplicateKeyError,)
    if isinstance(error, duplicate_errors):
        return "duplicate_key"
    operation_errors = (TinyMongoOperationFailure,)
    if PyMongoOperationFailure:
        operation_errors = operation_errors + (PyMongoOperationFailure,)
    if isinstance(error, operation_errors):
        return "operation_failure"
    return "{0}.{1}".format(type(error).__module__, type(error).__name__)


def observe(operation: Callable[[], Any]) -> Outcome:
    """Run an operation and retain either its value or normalized error."""

    try:
        return Outcome(value=operation())
    except Exception as error:  # noqa: BLE001 - exceptions are contract output
        return Outcome(error=error_category(error))
