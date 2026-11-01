"""Single background inventory scan with inexpensive, thread-safe status reads."""
import logging
import threading
from datetime import datetime, timezone


class InventoryJob:
    def __init__(self, on_change=None):
        self._lock = threading.Lock()
        self._on_change = on_change or (lambda: None)
        self._thread = None
        self._active = False
        self._inventory = None
        self._error = None
        self._checked_at = None
        self._pending_scan = None

    def snapshot(self):
        with self._lock:
            return {"active": self._active, "inventory": self._inventory,
                    "error": self._error, "checked_at": self._checked_at}

    def start(self, scan, *, rerun=False):
        with self._lock:
            if self._active:
                if rerun:
                    self._pending_scan = scan
                return
            self._active = True
            self._error = None
            self._thread = threading.Thread(target=self._run, args=(scan,),
                                            name="filter-inventory", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._active = False
                self._thread = None
                raise
        self._on_change()

    def _run(self, scan):
        while True:
            try:
                inventory = scan()
                with self._lock:
                    self._inventory = inventory
                    self._checked_at = datetime.now(timezone.utc).isoformat()
                    self._error = None
            except Exception as exc:
                logging.getLogger("hushclient.sync").exception("Filter inventory scan failed")
                with self._lock:
                    self._error = str(getattr(exc, "detail", "Could not refresh file totals; check diagnostic logs."))
            with self._lock:
                finished = self._pending_scan is None
                if finished:
                    self._active = False
                else:
                    scan, self._pending_scan = self._pending_scan, None
            # Notify outside the lock so subscribers may safely read a snapshot.
            self._on_change()
            if finished:
                return

    def join(self):
        if self._thread is not None:
            self._thread.join()
