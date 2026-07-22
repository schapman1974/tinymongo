"""Exceptions raised by TinyMongo.

When PyMongo is installed, TinyMongo exceptions also inherit from the matching
PyMongo exception classes.  PyMongo remains optional, while applications that
already catch ``PyMongoError`` continue to catch TinyMongo failures.
"""

try:  # Keep PyMongo an optional runtime integration.
    from pymongo import errors as _pymongo_errors
except ImportError:  # pragma: no cover - exercised in dependency-free installs

    class _PyMongoError(Exception):
        """Dependency-free stand-in for ``pymongo.errors.PyMongoError``."""

    class _ConnectionFailure(_PyMongoError):
        pass

    class _ConfigurationError(_PyMongoError):
        pass

    class _OperationFailure(_PyMongoError):
        def __init__(self, error, code=None, details=None, max_wire_version=None):
            super(_OperationFailure, self).__init__(error)
            self.__code = code
            self.__details = details
            self.__max_wire_version = max_wire_version

        @property
        def code(self):
            return self.__code

        @property
        def details(self):
            return self.__details

        @property
        def max_wire_version(self):
            return self.__max_wire_version

    class _CursorNotFound(_OperationFailure):
        pass

    class _WriteError(_OperationFailure):
        pass

    class _DuplicateKeyError(_WriteError):
        pass

    class _InvalidOperation(_PyMongoError):
        pass

else:
    _PyMongoError = _pymongo_errors.PyMongoError  # type: ignore[misc,assignment]
    _ConnectionFailure = _pymongo_errors.ConnectionFailure  # type: ignore[misc,assignment]
    _ConfigurationError = _pymongo_errors.ConfigurationError  # type: ignore[misc,assignment]
    _OperationFailure = _pymongo_errors.OperationFailure  # type: ignore[misc,assignment]
    _CursorNotFound = _pymongo_errors.CursorNotFound  # type: ignore[misc,assignment]
    _WriteError = _pymongo_errors.WriteError  # type: ignore[misc,assignment]
    _DuplicateKeyError = _pymongo_errors.DuplicateKeyError  # type: ignore[misc,assignment]
    _InvalidOperation = _pymongo_errors.InvalidOperation  # type: ignore[misc,assignment]


class TinyMongoError(_PyMongoError):
    """Base class for all TinyMongo exceptions."""


class ConnectionFailure(_ConnectionFailure, TinyMongoError):
    """Raised when a connection to storage cannot be made or is lost."""


class ConfigurationError(_ConfigurationError, TinyMongoError):
    """Raised when something is incorrectly configured."""


class OperationFailure(_OperationFailure, TinyMongoError):
    """Raised when a database operation fails."""


class CursorNotFound(_CursorNotFound, OperationFailure):
    """Raised while iterating results after a cursor is invalidated."""


class WriteError(_WriteError, OperationFailure):
    """Base exception type for errors raised during write operations."""


class DuplicateKeyError(_DuplicateKeyError, WriteError):
    """Raised when a write violates a unique key constraint."""


class InvalidOperation(_InvalidOperation, TinyMongoError):
    """Raised when a client attempts an invalid operation."""


class TinyMongoNotSupportedError(TinyMongoError, NotImplementedError):
    """Raised when TinyMongo cannot honor requested database semantics."""


class StorageError(TinyMongoError):
    """Raised when persistent storage cannot be read or written safely."""


class StorageCorruptionError(StorageError):
    """Raised when an existing database file cannot be decoded."""


class LockError(StorageError):
    """Raised when a storage lock cannot be acquired or released safely."""
