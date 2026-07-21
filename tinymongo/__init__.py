try:
    from tinymongo.tinymongo import *  # noqa
except ImportError:  # pragma: no cover - legacy import fallback
    from tinymongo import *  # noqa

from tinymongo.patching import patch  # noqa: E402,F401
from tinymongo.indexes import TinyMongoUnsupportedWarning  # noqa: E402,F401
from tinymongo.asyncio import (  # noqa: E402,F401
    AsyncCollection,
    AsyncCursor,
    AsyncDatabase,
    AsyncMongoClient,
    AsyncTinyMongoClient,
    AsyncTinyMongoCollection,
    AsyncTinyMongoCursor,
    AsyncTinyMongoDatabase,
)
