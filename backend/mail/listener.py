from __future__ import annotations

from threading import Event, Lock, Thread
from typing import Callable

import qq_idleCopy as core
from backend.events import event_bus
from backend.tasks.registry import utc_now


class MailListenerManager:
    def __init__(
        self,
        order_handler: Callable | None = None,
        shop_lookup: Callable | None = None,
        product_lookup: Callable | None = None,
    ):
        self._order_handler = order_handler
        self._shop_lookup = shop_lookup
        self._product_lookup = product_lookup
        self._lock = Lock()
        self._thread: Thread | None = None
        self._stop_event: Event | None = None
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._last_error: str | None = None

    def start(self):
        core.validate_config()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event = Event()
            self._started_at = utc_now()
            self._stopped_at = None
            self._last_error = None
            self._thread = Thread(
                target=self._run,
                name="qq-imap-listener",
                daemon=True,
            )
            self._thread.start()
        event_bus.publish("listener.started", self.status())
        return True

    def _run(self):
        try:
            core.listen_forever(
                self._stop_event,
                order_handler=self._order_handler,
                shop_lookup=self._shop_lookup,
                product_lookup=self._product_lookup,
            )
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            event_bus.publish("listener.error", self.status())
        finally:
            with self._lock:
                self._stopped_at = utc_now()
            event_bus.publish("listener.stopped", self.status())

    def stop(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._stop_event.set()
        event_bus.publish("listener.stop_requested", self.status())
        return True

    def status(self):
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            stop_requested = (
                self._stop_event is not None
                and self._stop_event.is_set()
            )
            if running and stop_requested:
                status = "stopping"
            elif running:
                status = "running"
            else:
                status = "stopped"
            return {
                "status": status,
                "running": running,
                "stop_requested": stop_requested,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_error": self._last_error,
                "last_uid": core.load_last_uid(),
            }

    def shutdown(self):
        self.stop()
