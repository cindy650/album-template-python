from __future__ import annotations

from threading import Thread
import time
from typing import Any

import qq_idleCopy as core
from backend.events import event_bus
from backend.orders.google_sheets_retry import GoogleSheetsRetryWorker
from backend.orders.repository import OrderRepository


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        template_image_generator=None,
        template_image_retry_attempts: int = 3,
        template_image_retry_delay_seconds: float = 5,
        google_sheets_publisher=None,
        google_sheets_retry_initial_seconds: float = 30,
        google_sheets_retry_max_seconds: float = 900,
        wecom_notifier=None,
    ):
        self.repository = repository
        self.template_image_generator = template_image_generator
        self.google_sheets_publisher = google_sheets_publisher or core.post_order
        self.wecom_notifier = wecom_notifier
        self.template_image_retry_attempts = max(
            0,
            int(template_image_retry_attempts),
        )
        self.template_image_retry_delay_seconds = max(
            0.0,
            float(template_image_retry_delay_seconds),
        )
        self.google_sheets_retry_worker = GoogleSheetsRetryWorker(
            repository,
            self.google_sheets_publisher,
            initial_delay_seconds=google_sheets_retry_initial_seconds,
            max_delay_seconds=google_sheets_retry_max_seconds,
        )

    def start(self):
        self.google_sheets_retry_worker.start()

    def shutdown(self):
        self.google_sheets_retry_worker.shutdown()

    def google_sheets_retry_status(self):
        return self.google_sheets_retry_worker.status()

    def publish_order(
        self,
        order: dict[str, Any],
        source: str = "api",
        uid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        saved = self.repository.upsert(
            order,
            source=source,
            uid=uid,
            metadata=metadata,
        )
        order = {
            **order,
            "店铺": saved.get("shop", order.get("店铺", "")),
            "店铺名": saved.get("shop_name", order.get("店铺名", "")),
            "商品信息": saved.get(
                "product_information",
                order.get("商品信息", {}),
            ),
        }
        try:
            google_result = self.google_sheets_publisher(order)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.google_sheets_retry_worker.schedule(saved["id"])
            google_result = {
                "ok": False,
                "status": "pending_retry",
                "attempts": 1,
                "error": error,
            }
            print(
                "[Google Sheets] 首次写入失败，已加入内存补写队列："
                f"订单ID={saved['id']}，订单号={saved['order_number']}，"
                f"{error}",
                flush=True,
            )
            event_bus.publish(
                "order.google_sheets.failed",
                {
                    "order": saved,
                    "error": error,
                    "retry_scheduled": True,
                },
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
        if self.wecom_notifier is not None:
            image_result["wecom"] = self._notify_wecom(
                order,
                result.get("local_order"),
                image_result,
                uid,
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

    def _notify_wecom(self, order, saved_order, image_result, uid):
        try:
            notification = self.wecom_notifier.notify_order_image(
                order,
                image_result,
                saved_order=saved_order,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            notification = {
                "ok": False,
                "status": "failed",
                "error": error,
            }
            print(
                "[企业微信] 订单图片通知失败："
                f"订单号={(saved_order or {}).get('order_number') or '未知'}，"
                f"{error}",
                flush=True,
            )
            event_bus.publish(
                "order.wecom.failed",
                {
                    "order": saved_order,
                    "template_image": image_result,
                    "source": "mail-listener",
                    "uid": uid,
                    "error": error,
                },
            )
        else:
            event_bus.publish(
                "order.wecom.sent",
                {
                    "order": saved_order,
                    "template_image": image_result,
                    "source": "mail-listener",
                    "uid": uid,
                    "notification": notification,
                },
            )
        return notification

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
