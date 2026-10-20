"""Cooperative stop for the render thread.

Cancelled is a BaseException so a search's `except Exception` does not treat
Stop as a failed origin and keep going. The render thread binds an event;
POST /api/jobs/<id>/cancel sets that event.
"""
from __future__ import annotations

import contextvars
import threading

MESSAGE = "Stopped."

_current: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("nc_cancel", default=None)


class Cancelled(BaseException):
    """The person who started this Perform stopped it."""


def bind(event: threading.Event):
    return _current.set(event)


def reset(token) -> None:
    _current.reset(token)


def check() -> None:
    event = _current.get()
    if event is not None and event.is_set():
        raise Cancelled(MESSAGE)
