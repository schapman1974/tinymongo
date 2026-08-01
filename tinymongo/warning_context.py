"""Preserve application warning locations across async worker threads."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
import sys
from typing import Iterator, Optional, Type
import warnings


@dataclass(frozen=True)
class WarningOrigin:
    """The external source location responsible for a TinyMongo operation."""

    filename: str
    lineno: int
    module: Optional[str]


_WARNING_ORIGIN: ContextVar[Optional[WarningOrigin]] = ContextVar(
    "tinymongo_warning_origin",
    default=None,
)


def capture_warning_origin() -> Optional[WarningOrigin]:
    """Return the first caller frame outside the :mod:`tinymongo` package."""

    frame = inspect.currentframe()
    if frame is None:  # pragma: no cover - supported interpreters provide frames
        return None
    frame = frame.f_back
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__")
            if not isinstance(module, str) or not (
                module == "tinymongo" or module.startswith("tinymongo.")
            ):
                return WarningOrigin(
                    filename=frame.f_code.co_filename,
                    lineno=frame.f_lineno,
                    module=module if isinstance(module, str) else None,
                )
            frame = frame.f_back
        return None
    finally:
        # Frames retain their locals and callers, so never keep this reference
        # alive after reducing it to immutable source metadata.
        del frame


@contextmanager
def use_warning_origin(origin: Optional[WarningOrigin]) -> Iterator[None]:
    """Make ``origin`` available to warning emitters in the current context."""

    if origin is None:
        yield
        return
    token = _WARNING_ORIGIN.set(origin)
    try:
        yield
    finally:
        _WARNING_ORIGIN.reset(token)


def emit_warning(message: str, category: Type[Warning], stacklevel: int = 2) -> None:
    """Emit a warning at a captured async origin or the synchronous caller."""

    origin = _WARNING_ORIGIN.get() or capture_warning_origin()
    if origin is None:
        # This is only a fallback for runtimes which do not expose frames.
        # Account for this helper so a supplied legacy stacklevel keeps the
        # same meaning it had when the call site used ``warnings.warn``.
        warnings.warn(message, category, stacklevel=stacklevel + 1)
        return

    registry = None
    if origin.module is not None:
        loaded_module = sys.modules.get(origin.module)
        if loaded_module is not None:
            registry = loaded_module.__dict__.setdefault("__warningregistry__", {})
    if origin.module is None:
        warnings.warn_explicit(
            message,
            category,
            filename=origin.filename,
            lineno=origin.lineno,
            registry=registry,
        )
    else:
        warnings.warn_explicit(
            message,
            category,
            filename=origin.filename,
            lineno=origin.lineno,
            module=origin.module,
            registry=registry,
        )


__all__ = [
    "WarningOrigin",
    "capture_warning_origin",
    "emit_warning",
    "use_warning_origin",
]
