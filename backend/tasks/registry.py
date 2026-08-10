from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from backend.events import event_bus


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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
        event_bus.publish("task.queued", record)
        self._executor.submit(self._run, task_id, function)
        return dict(record)

    def _run(self, task_id, function):
        with self._lock:
            self._tasks[task_id]["status"] = "running"
            self._tasks[task_id]["started_at"] = utc_now()
            running_record = dict(self._tasks[task_id])
        event_bus.publish("task.running", running_record)
        try:
            result = function()
        except Exception as exc:
            with self._lock:
                record = self._tasks[task_id]
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["finished_at"] = utc_now()
                failed_record = dict(record)
            event_bus.publish("task.failed", failed_record)
        else:
            with self._lock:
                record = self._tasks[task_id]
                record["status"] = "succeeded"
                record["result"] = result
                record["finished_at"] = utc_now()
                succeeded_record = dict(record)
            event_bus.publish("task.succeeded", succeeded_record)

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
