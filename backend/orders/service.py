from __future__ import annotations

from threading import Thread
import time
from typing import Any

from backend.events import event_bus
from backend.orders.repository import OrderRepository
from backend.orders.statuses import PREVIEW_SENT_ORDER_STATUS


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        template_image_generator=None,
        template_image_retry_attempts: int = 3,
        template_image_retry_delay_seconds: float = 5,
        wecom_notifier=None,
        order_print_image_generator=None,
        production_artifact_service=None,
    ):
        self.repository = repository
        self.template_image_generator = template_image_generator
        self.wecom_notifier = wecom_notifier
        self.order_print_image_generator = order_print_image_generator
        self.production_artifact_service = production_artifact_service
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
        saved_event_order = {
            key: saved.get(key)
            for key in (
                "id",
                "order_number",
                "shop",
                "shop_name",
                "product",
                "shop_id",
                "size_template_id",
                "status",
                "status_text",
                "status_button_text",
            )
        }
        event_bus.publish(
            "order.saved",
            {"order": saved_event_order},
            msg="新订单已入库",
        )
        return {
            "order": order,
            "local_order": saved,
        }

    def handle_mail_order(self, order, uid=None, metadata=None):
        result = self.publish_order(
            order,
            source="mail-listener",
            uid=uid,
            metadata=metadata or {},
        )
        saved_order = result.get("local_order") or {}
        if not saved_order.get("size_template_id"):
            result["template_image"] = {
                "ok": False,
                "status": "skipped",
                "reason": "size_template_not_associated",
                "message": "商品未关联模板，已跳过后续图片生成和通知",
            }
            print(
                "[订单] 商品未关联模板，跳过后续图片生成和通知："
                f"订单ID={saved_order.get('id') or '空'}，"
                f"订单号={saved_order.get('order_number') or '空'}",
                flush=True,
            )
            return result
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

    def send_preview_images(self, order_id: int, order_number: str):
        current_order = self.repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        if current_order.get("status") != 0:
            raise ValueError("只有新订单状态可以发送示意图")
        saved_order = self.repository.associate_current_template(
            order_id,
            order_number,
        )
        if self.template_image_generator is None:
            raise RuntimeError("未配置订单预览图生成器")
        if self.wecom_notifier is None:
            raise RuntimeError("未配置企业微信机器人，无法发送示意图")

        order = self.repository.order_payload(saved_order["id"])
        preview_result = self.template_image_generator.generate_for_order(
            order,
            saved_order=saved_order,
            reuse_snapshot=False,
            upload_to_oss=False,
        )
        order_info_result = self._generate_order_info_image(
            saved_order,
            preview_result,
        )
        if self.production_artifact_service is not None:
            preview_result["production_files"] = self.production_artifact_service.generate_order_artifacts(
                saved_order["id"], saved_order["order_number"],
                preview_result=preview_result,
            )
        notification = self.wecom_notifier.notify_order_image(
            order,
            preview_result,
            saved_order=saved_order,
            order_info_image_result=order_info_result,
        )
        if not notification.get("ok") or notification.get("status") != "sent":
            raise RuntimeError("企业微信示意图发送失败，订单状态未变更")

        updated_order = self.repository.update_status(
            saved_order["id"],
            PREVIEW_SENT_ORDER_STATUS,
        )
        event_bus.publish(
            "order.preview_images.sent",
            {
                "order": updated_order,
                "preview_image": preview_result,
                "order_info_image": order_info_result,
                "notification": notification,
                "source": "api",
            },
        )
        return {
            "order": updated_order,
            "preview_image": preview_result,
            "order_info_image": order_info_result,
            "wecom": notification,
        }

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
            reuse_snapshot=False,
            upload_to_oss=False,
        )
        if self.wecom_notifier is not None:
            image_result["wecom"] = self._notify_wecom(
                order,
                result.get("local_order"),
                image_result,
                uid,
            )
        if self.production_artifact_service is not None:
            local_order = result.get("local_order") or {}
            image_result["production_files"] = self.production_artifact_service.generate_order_artifacts(
                local_order["id"], local_order["order_number"],
                preview_result=image_result,
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
            order_info_image_result = self._generate_order_info_image(
                saved_order,
                image_result,
            )
            notification = self.wecom_notifier.notify_order_image(
                order,
                image_result,
                saved_order=saved_order,
                order_info_image_result=order_info_image_result,
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
            if (
                saved_order.get("status") == 0
                and notification.get("ok")
                and notification.get("status") == "sent"
            ):
                updated_order = self.repository.update_status(
                    saved_order["id"],
                    PREVIEW_SENT_ORDER_STATUS,
                )
                if isinstance(saved_order, dict):
                    saved_order.update(updated_order)
        return notification

    def _generate_order_info_image(self, saved_order, preview_result):
        if self.order_print_image_generator is None:
            raise RuntimeError("未配置订单信息图生成器")
        if not saved_order:
            raise RuntimeError("缺少本地订单，无法生成订单信息图")
        return self.order_print_image_generator.generate(
            saved_order["id"],
            saved_order["order_number"],
            preview_result=preview_result,
            upload_to_oss=False,
        )

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
