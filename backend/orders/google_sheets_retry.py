from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Thread
import time
from typing import Any

from backend.events import event_bus
from backend.orders.repository import OrderRepository


class GoogleSheetsRetryWorker:
    """Retry failed Google Sheets writes in memory while the process runs."""

    def __init__(
        self,
        repository: OrderRepository,
        publisher: Callable[[dict[str, Any]], dict[str, Any]],
        initial_delay_seconds: float = 30,
        max_delay_seconds: float = 900,
    ):
        self.repository = repository
        self.publisher = publisher
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self.max_delay_seconds = max(
            self.initial_delay_seconds,
            float(max_delay_seconds),
        )
        self._condition = Condition()
        self._scheduled: dict[int, float] = {}
        self._attempts: dict[int, int] = {}
        self._thread: Thread | None = None
        self._stop_requested = False

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_requested = False
            self._thread = Thread(
                target=self._run,
                name="google-sheets-retry",
                daemon=True,
            )
            self._thread.start()

        print(
            "[Google Sheets] 内存补写任务已启动",
            flush=True,
        )
        return True

    def schedule(self, order_id: int, delay_seconds: float | None = None):
        self.start()
        delay = (
            self.initial_delay_seconds
            if delay_seconds is None
            else max(0.0, float(delay_seconds))
        )
        due_at = time.monotonic() + delay
        with self._condition:
            self._attempts.setdefault(order_id, 1)
            current_due_at = self._scheduled.get(order_id)
            if current_due_at is None or due_at < current_due_at:
                self._scheduled[order_id] = due_at
            self._condition.notify_all()

    def _run(self):
        while True:
            order_id = self._next_order_id()
            if order_id is None:
                return
            try:
                self._retry_order(order_id)
            except Exception as exc:
                print(
                    f"[Google Sheets] 后台补写任务异常：订单ID={order_id}，"
                    f"{type(exc).__name__}: {exc}；"
                    f"{self.max_delay_seconds:g}秒后重试",
                    flush=True,
                )
                self.schedule(
                    order_id,
                    delay_seconds=self.max_delay_seconds,
                )

    def _next_order_id(self):
        with self._condition:
            while not self._stop_requested:
                if not self._scheduled:
                    self._condition.wait()
                    continue

                order_id, due_at = min(
                    self._scheduled.items(),
                    key=lambda item: item[1],
                )
                remaining = due_at - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue

                self._scheduled.pop(order_id, None)
                return order_id
            return None

    def _retry_order(self, order_id: int):
        saved = self.repository.get(order_id)
        if saved is None:
            with self._condition:
                self._attempts.pop(order_id, None)
            return

        order = self.repository.order_payload(order_id)
        if order is None:
            return

        with self._condition:
            attempt = self._attempts.get(order_id, 1) + 1
            self._attempts[order_id] = attempt
        print(
            f"[Google Sheets] 开始后台补写：订单ID={order_id}，"
            f"订单号={saved['order_number']}，第{attempt}次尝试",
            flush=True,
        )
        event_bus.publish(
            "order.google_sheets.retrying",
            {
                "order_id": order_id,
                "order_number": saved["order_number"],
                "attempt": attempt,
            },
        )
        try:
            result = self.publisher(order)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            delay = self._retry_delay(attempt)
            print(
                f"[Google Sheets] 后台补写失败：订单ID={order_id}，"
                f"{error}；{delay:g}秒后重试",
                flush=True,
            )
            event_bus.publish(
                "order.google_sheets.failed",
                {
                    "order": saved,
                    "error": error,
                    "retry_scheduled": True,
                    "retry_delay_seconds": delay,
                },
            )
            self.schedule(order_id, delay_seconds=delay)
            return

        with self._condition:
            self._attempts.pop(order_id, None)
        print(
            f"[Google Sheets] 后台补写成功：订单ID={order_id}，"
            f"订单号={saved['order_number']}，结果={result}",
            flush=True,
        )
        event_bus.publish(
            "order.google_sheets.succeeded",
            {"order": saved, "google_sheets": result},
        )

    def _retry_delay(self, attempts: int):
        exponent = max(0, min(int(attempts) - 1, 20))
        return min(
            self.initial_delay_seconds * (2**exponent),
            self.max_delay_seconds,
        )

    def shutdown(self):
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=1)

    def status(self):
        with self._condition:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "queued": len(self._scheduled),
                "queued_order_ids": sorted(self._scheduled),
                "attempts": dict(self._attempts),
            }
