"""Exceptions raised by TinyMongo."""


class TinyMongoError(Exception):
    """Base class for all TinyMongo exceptions."""


class ConnectionFailure(TinyMongoError):
    """Raised when a connection to the database file cannot be made or is lost.
    """


class ConfigurationError(TinyMongoError):
    """Raised when something is incorrectly configured.
    """


class OperationFailure(TinyMongoError):
    """Raised when a database operation fails.
    """

    def __init__(self, error, code=None, details=None):
        self.__code = code
        self.__details = details
        TinyMongoError.__init__(self, error)

    @property
    def code(self):
        """The error code returned by the server, if any.
        """
        return self.__code

    @property
    def details(self):
        """The complete error document returned by the server.

        Depending on the error that occurred, the error document
        may include useful information beyond just the error
        message. When connected to a mongos the error document
        may contain one or more subdocuments if errors occurred
        on multiple shards.
        """
        return self.__details


class CursorNotFound(OperationFailure):
    """Raised while iterating query results if the cursor is
    invalidated on the server.
    """


class WriteError(OperationFailure):
    """Base exception type for errors raised during write operations."""


class DuplicateKeyError(WriteError):
    """Raised when an insert or update fails due to a duplicate key error."""


class InvalidOperation(TinyMongoError):
    """Raised when a client attempts to perform an invalid operation."""
