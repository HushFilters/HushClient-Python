"""Single background inventory scan with inexpensive, thread-safe status reads."""
import logging
import threading


class InventoryJob:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._active = False
        self._inventory = None
        self._error = None

    def snapshot(self):
        with self._lock:
            return {"active": self._active, "inventory": self._inventory,
                    "error": self._error}

    def start(self, scan):
        with self._lock:
            if self._active:
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

    def _run(self, scan):
        try:
            inventory = scan()
            with self._lock:
                self._inventory = inventory
        except Exception as exc:
            logging.getLogger("hushclient.sync").exception("Filter inventory scan failed")
            with self._lock:
                self._error = str(getattr(exc, "detail", "Could not refresh file totals; check diagnostic logs."))
        finally:
            with self._lock:
                self._active = False

    def join(self):
        if self._thread is not None:
            self._thread.join()
