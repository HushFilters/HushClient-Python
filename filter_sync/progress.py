"""Structured progress scoped to the sync thread; CLI use needs no listener."""
from contextlib import contextmanager
from contextvars import ContextVar

_listener = ContextVar("sync_progress_listener", default=None)
_segment = ContextVar("sync_progress_segment", default=None)


@contextmanager
def listen(callback):
    token = _listener.set(callback)
    try:
        yield
    finally:
        _listener.reset(token)


@contextmanager
def segment(phase, index, total, detail):
    token = _segment.set((phase, index, total, detail))
    try:
        yield
    finally:
        _segment.reset(token)


def report(phase, completed=0, total=None, detail="", status="running"):
    listener = _listener.get()
    if listener is None:
        return
    current = _segment.get()
    if current and current[0] == phase:
        _, index, count, label = current
        detail = f"{label}: {detail}" if detail else label
        if total:
            completed, total = index + completed / total, count
        else:
            completed, total = index, None
    listener(phase, completed, total, detail, status)
