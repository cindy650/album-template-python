from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable
from uuid import uuid4

import qq_idleCopy as core


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class MailListenerManager:
    def __init__(self):
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
            return True

    def _run(self):
        try:
            core.listen_forever(self._stop_event)
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._stopped_at = utc_now()

    def stop(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._stop_event.set()
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


class TaskRegistry:
    def __init__(self, max_workers=3):
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="backend-task",
        )
        self._tasks: dict[str, dict[str, Any]] = {}

    def submit(self, name: str, function: Callable[[], Any]):
        task_id = uuid4().hex
        record = {
            "task_id": task_id,
            "name": name,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._tasks[task_id] = record
        self._executor.submit(self._run, task_id, function)
        return dict(record)

    def _run(self, task_id, function):
        with self._lock:
            self._tasks[task_id]["status"] = "running"
            self._tasks[task_id]["started_at"] = utc_now()
        try:
            result = function()
        except Exception as exc:
            with self._lock:
                record = self._tasks[task_id]
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["finished_at"] = utc_now()
        else:
            with self._lock:
                record = self._tasks[task_id]
                record["status"] = "succeeded"
                record["result"] = result
                record["finished_at"] = utc_now()

    def get(self, task_id):
        with self._lock:
            record = self._tasks.get(task_id)
            return dict(record) if record is not None else None

    def list(self):
        with self._lock:
            records = [dict(record) for record in self._tasks.values()]
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def has_active(self, name):
        with self._lock:
            return any(
                record["name"] == name
                and record["status"] in {"queued", "running"}
                for record in self._tasks.values()
            )

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
