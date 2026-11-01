"""Thread-safe change notifications for the Sync UI's SSE connections."""
import asyncio
import threading
from contextlib import contextmanager


class SyncEvents:
    """Keep only dirty topics per subscriber, never an unbounded event queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = {}

    @contextmanager
    def subscribe(self):
        event = asyncio.Event()
        with self._lock:
            self._subscribers[event] = (asyncio.get_running_loop(), set())
        try:
            yield event
        finally:
            with self._lock:
                self._subscribers.pop(event, None)

    def publish(self, *topics):
        with self._lock:
            for event, (loop, dirty) in self._subscribers.items():
                wake = not dirty
                dirty.update(topics)
                if wake:
                    # Mutations originate in both the ASGI loop and worker threads.
                    try:
                        loop.call_soon_threadsafe(event.set)
                    except RuntimeError:
                        pass  # The connection's event loop has already closed.

    def take(self, event):
        with self._lock:
            _, dirty = self._subscribers[event]
            topics = set(dirty)
            dirty.clear()
            event.clear()
            return topics
