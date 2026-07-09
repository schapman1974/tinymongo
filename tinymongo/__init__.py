try:
    from tinymongo.tinymongo import *  # noqa
except ImportError:  # pragma: no cover - legacy import fallback
    from tinymongo import *  # noqa
