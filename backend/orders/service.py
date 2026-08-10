from __future__ import annotations

from threading import Thread
import time
from typing import Any

import qq_idleCopy as core
from backend.events import event_bus
from backend.orders.repository import OrderRepository


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        template_image_generator=None,
        template_image_retry_attempts: int = 3,
        template_image_retry_delay_seconds: float = 5,
    ):
        self.repository = repository
        self.template_image_generator = template_image_generator
        self.template_image_retry_attempts = max(
            0,
            int(template_image_retry_attempts),
        )
        self.template_image_retry_delay_seconds = max(
            0.0,
            float(template_image_retry_delay_seconds),
        )

    def publish_order(
        self,
        order: dict[str, Any],
        source: str = "api",
        uid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        google_result = core.post_order(order)
        saved = self.repository.upsert(
            order,
            google_sheets=google_result,
            source=source,
            uid=uid,
            metadata=metadata,
        )
        event_bus.publish(
            "order.saved",
            {
                "order": saved,
                "google_sheets": google_result,
                "source": source,
            },
        )
        return {
            "order": order,
            "google_sheets": google_result,
            "local_order": saved,
        }

    def handle_mail_order(self, order, uid=None, metadata=None):
        result = self.publish_order(
            order,
            source="mail-listener",
            uid=uid,
            metadata=metadata or {},
        )
        if self.template_image_generator is not None:
            try:
                image_result = self._generate_template_image_once(
                    order,
                    result,
                    metadata or {},
                    uid,
                    attempt=1,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                result["template_image"] = {
                    "ok": False,
                    "status": "retry_scheduled"
                    if self.template_image_retry_attempts
                    else "failed",
                    "error": error,
                    "retry_attempts": self.template_image_retry_attempts,
                }
                self._publish_template_image_failure(
                    result,
                    uid,
                    error,
                    attempt=1,
                    retry_scheduled=bool(self.template_image_retry_attempts),
                )
                if self.template_image_retry_attempts:
                    self._schedule_template_image_retry(
                        order,
                        result,
                        metadata or {},
                        uid,
                    )
            else:
                result["template_image"] = image_result
        return result

    def _generate_template_image_once(
        self,
        order,
        result,
        metadata,
        uid,
        attempt: int,
    ):
        image_result = self.template_image_generator.generate_for_order(
            order,
            saved_order=result.get("local_order"),
            metadata=metadata,
        )
        event_bus.publish(
            "order.template_image.generated",
            {
                "order": result.get("local_order"),
                "template_image": image_result,
                "source": "mail-listener",
                "uid": uid,
                "attempt": attempt,
            },
        )
        return image_result

    def _publish_template_image_failure(
        self,
        result,
        uid,
        error: str,
        attempt: int,
        retry_scheduled: bool,
    ):
        event_bus.publish(
            "order.template_image.failed",
            {
                "order": result.get("local_order"),
                "source": "mail-listener",
                "uid": uid,
                "attempt": attempt,
                "retry_scheduled": retry_scheduled,
                "error": error,
            },
        )

    def _schedule_template_image_retry(self, order, result, metadata, uid):
        thread = Thread(
            target=self._retry_template_image_generation,
            args=(order, result, metadata, uid),
            name=f"template-image-retry-{uid or 'unknown'}",
            daemon=True,
        )
        thread.start()

    def _retry_template_image_generation(self, order, result, metadata, uid):
        for retry_index in range(1, self.template_image_retry_attempts + 1):
            if self.template_image_retry_delay_seconds:
                time.sleep(self.template_image_retry_delay_seconds)
            attempt = retry_index + 1
            event_bus.publish(
                "order.template_image.retrying",
                {
                    "order": result.get("local_order"),
                    "source": "mail-listener",
                    "uid": uid,
                    "attempt": attempt,
                },
            )
            try:
                self._generate_template_image_once(
                    order,
                    result,
                    metadata,
                    uid,
                    attempt=attempt,
                )
                return
            except Exception as exc:
                self._publish_template_image_failure(
                    result,
                    uid,
                    f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                    retry_scheduled=retry_index
                    < self.template_image_retry_attempts,
                )
