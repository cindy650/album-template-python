from __future__ import annotations

from datetime import datetime
from threading import Thread
import time
from typing import Any

from backend.events import event_bus
from backend.notifications import WeComRobotNotifier
from backend.orders.repository import OrderRepository
from backend.orders.statuses import PREVIEW_SENT_ORDER_STATUS
from backend.templates.order_resolver import FontLayoutNotConfiguredError


DEEPSEEK_EMPTY_TEXT_ERROR = "DeepSeek 未返回全部自然语言图层文字"


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        template_image_generator=None,
        template_image_retry_attempts: int = 3,
        template_image_retry_delay_seconds: float = 5,
        catalog_repository=None,
        wecom_robot_timeout_seconds: float = 10,
        order_print_image_generator=None,
        production_artifact_service=None,
    ):
        self.repository = repository
        self.template_image_generator = template_image_generator
        self.catalog_repository = catalog_repository
        self.wecom_robot_timeout_seconds = max(
            0.1,
            float(wecom_robot_timeout_seconds),
        )
        self._shop_notifier_cache = {}
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
                "order_group_id",
                "order_number",
                "transaction_id",
                "shop",
                "shop_name",
                "product",
                "shop_id",
                "size_template_id",
                "status",
                "status_text",
                "status_button_text",
                "email_sent_at",
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
        # Dispatch the order-level summary independently. Notification latency
        # must not delay product classification, template matching, or rendering.
        if not saved_order.get("size_template_id"):
            Thread(target=self._send_new_order_summary, args=(order, saved_order, metadata or {}), daemon=True).start()
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
            except FontLayoutNotConfiguredError as exc:
                result["template_image"] = {
                    "ok": False,
                    "status": "skipped",
                    "reason": "font_layout_not_configured",
                    "message": str(exc),
                }
                print(
                    "[订单] 尺寸模板未配置字体布局，跳过订单图片："
                    f"订单ID={saved_order.get('id') or '空'}，"
                    f"订单号={saved_order.get('order_number') or '空'}",
                    flush=True,
                )
                return result
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                requires_manual_intervention = self._requires_manual_intervention(exc)
                retry_scheduled = bool(
                    self.template_image_retry_attempts
                    and not requires_manual_intervention
                )
                result["template_image"] = {
                    "ok": False,
                    "status": "manual_intervention_required"
                    if requires_manual_intervention
                    else "retry_scheduled"
                    if retry_scheduled
                    else "failed",
                    "error": error,
                    "retry_attempts": (
                        self.template_image_retry_attempts
                        if retry_scheduled
                        else 0
                    ),
                }
                self._publish_template_image_failure(
                    result,
                    uid,
                    error,
                    attempt=1,
                    retry_scheduled=retry_scheduled,
                )
                if requires_manual_intervention:
                    result["automation_exception_notification"] = (
                        self._notify_automation_exception(
                            order,
                            saved_order,
                            uid,
                        )
                    )
                elif retry_scheduled:
                    self._schedule_template_image_retry(
                        order,
                        result,
                        metadata or {},
                        uid,
                    )
            else:
                result["template_image"] = image_result
                Thread(target=self._send_new_order_summary, args=(order, saved_order, metadata or {}), daemon=True).start()
        return result

    def _send_new_order_summary(self, order, saved_order, metadata):
        notifier = self._notifier_for_order(saved_order)
        print(
            "[企业微信] 新订单摘要触发检查："
            f"店铺名={saved_order.get('shop_name') or '空'}，"
            f"首次入库={saved_order.get('created')!r}，"
            f"机器人={'已配置' if notifier is not None else '未配置'}，"
            f"商品序号={metadata.get('item_index', 1) if isinstance(metadata, dict) else 1}",
            flush=True,
        )
        if (
            notifier is None
            or not saved_order.get("created")
            or int((metadata or {}).get("item_index") or 1) != 1
        ):
            return None
        try:
            stats = self.repository.order_summary_statistics(saved_order["id"])
            if not stats:
                return None
            daily = stats.get("daily") or {}
            monthly = stats.get("monthly") or {}
            currency = stats.get("settlement_currency") or stats.get("currency_code") or "CAD"
            mismatch = bool(stats.get("currency_mismatch"))
            def money(value):
                amount = float(value or 0)
                return str(int(amount))
            sent_at = str(stats.get("email_sent_at") or "")
            try:
                parsed_time = datetime.fromisoformat(sent_at)
                display_time = f"{parsed_time.month}月{parsed_time.day}号 {parsed_time.hour:02d}:{parsed_time.minute:02d}"
            except (ValueError, AttributeError):
                display_time = sent_at
            content = "\n".join([
                f"金额 : {money(stats.get('subtotal_amount'))} / {money(daily.get('total_amount'))} / {money(monthly.get('total_amount'))} {currency}（订单 / 日 / 月）",
                f"运费 : {money(stats.get('shipping_amount'))} / {money(daily.get('total_shipping'))} / {money(monthly.get('total_shipping'))} {currency}（订单 / 日 / 月）",
                f"订单数量 : {daily.get('order_count', 0)} / {monthly.get('order_count', 0)}（日 / 月）",
                f"产品数量 : {daily.get('product_count', 0)} / {monthly.get('product_count', 0)}（日 / 月）",
                f"订单编号 : {stats.get('order_number') or ''}",
                f"客户姓名 : {stats.get('customer_name') or ''}",
                f"国家 : {stats.get('country') or ''}",
                f"下单时间 : {display_time}",
                *( ["币种状态 : 订单币种与店铺结算币种不一致，未换算"] if mismatch else [] ),
            ])
            notification = notifier.notify_order_summary(content)
            print(f"[企业微信] 新订单摘要已发送：订单号={stats.get('order_number')}", flush=True)
            return notification
        except Exception as exc:
            print(f"[企业微信] 新订单摘要发送失败，不影响订单后续处理：{type(exc).__name__}: {exc}", flush=True)
            return {"ok": False, "status": "failed", "error": str(exc)}

    def send_preview_images(self, order_id: int, order_number: str):
        current_order = self.repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        if current_order.get("status") != 0:
            raise ValueError("只有新订单状态可以发送示意图")
        # Sending is a render-only operation. The automation/manual flow has
        # already copied the canonical layer JSON onto the order; never run
        # product or template matching here.
        manual_association = bool(
            isinstance(current_order.get("matched_template"), dict)
            and current_order["matched_template"].get("manual_selection")
        )
        saved_order = current_order
        if not saved_order.get("size_template_id"):
            raise ValueError("订单尚未关联尺寸模板，无法发送示意图")
        if self.template_image_generator is None:
            raise RuntimeError("未配置订单预览图生成器")
        notifier = self._notifier_for_order(saved_order)
        if notifier is None:
            raise RuntimeError("未配置企业微信机器人，无法发送示意图")

        order = self.repository.order_payload(saved_order["id"])
        try:
            preview_result = self.template_image_generator.generate_for_order(
                order,
                saved_order=saved_order,
                reuse_snapshot=True,
                upload_to_oss=False,
            )
        except Exception as exc:
            if self._requires_manual_intervention(exc):
                self._notify_automation_exception(
                    order,
                    saved_order,
                    uid=None,
                )
            raise
        order_info_result = self._generate_order_info_image(
            saved_order,
            preview_result,
        )
        notification = notifier.notify_order_image(
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

    def advance_status(self, order_id: int, order_number: str):
        current_order = self.repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        current_status = int(current_order.get("status") or 0)
        confirmation_artifacts = None
        if current_status == PREVIEW_SENT_ORDER_STATUS:
            if self.production_artifact_service is None:
                raise RuntimeError("未配置订单生产文件服务，无法处理客户确认")
            confirmation_artifacts = (
                self.production_artifact_service.regenerate_customer_confirmation_artifacts(
                    order_id,
                    order_number,
                )
            )

        updated_order = self.repository.advance_status(
            order_id,
            order_number,
            expected_status=current_status,
        )
        if confirmation_artifacts is not None:
            updated_order["customer_confirmation_artifacts"] = confirmation_artifacts
        event_bus.publish(
            "order.status.advanced",
            {
                "order": updated_order,
                "previous_status": current_status,
                "customer_confirmation_artifacts": confirmation_artifacts,
                "source": "api",
            },
        )
        return updated_order

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
        if self._notifier_for_order(result.get("local_order")) is not None:
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
        notifier = self._notifier_for_order(saved_order)
        if notifier is None:
            print(
                "[企业微信] 店铺未配置机器人，跳过订单图片通知："
                f"订单号={(saved_order or {}).get('order_number') or '未知'}",
                flush=True,
            )
            return {"ok": False, "status": "disabled", "reason": "shop_robot_not_configured"}
        try:
            order_info_image_result = self._generate_order_info_image(
                saved_order,
                image_result,
            )
            notification = notifier.notify_order_image(
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

    @staticmethod
    def _requires_manual_intervention(exc: Exception) -> bool:
        return DEEPSEEK_EMPTY_TEXT_ERROR in str(exc or "")

    def _notify_automation_exception(self, order, saved_order, uid):
        notifier = self._notifier_for_order(saved_order)
        if notifier is None:
            print(
                "[企业微信][自动化异常] 未配置异常通知机器人，已跳过发送",
                flush=True,
            )
            return {"ok": False, "status": "disabled"}
        shop = (
            (saved_order or {}).get("shop_name")
            or (saved_order or {}).get("shop")
            or order.get("店铺名")
            or order.get("店铺")
            or "未知店铺"
        )
        order_number = (
            (saved_order or {}).get("order_number")
            or order.get("订单号")
            or "未知订单"
        )
        title = f"{shop} {order_number} 自动化异常需人工处理"
        product_information = (
            order.get("商品信息")
            or order.get("product_information")
            or (saved_order or {}).get("product_information")
            or {}
        )
        try:
            notification = notifier.notify_automation_exception(
                title,
                product_information,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(
                f"[企业微信][自动化异常] 发送失败：订单号={order_number}，{error}",
                flush=True,
            )
            event_bus.publish(
                "order.wecom.failed",
                {
                    "order": saved_order,
                    "source": "mail-listener",
                    "uid": uid,
                    "message_kind": "automation_exception",
                    "error": error,
                },
            )
            return {"ok": False, "status": "failed", "error": error}
        print(
            f"[企业微信][自动化异常] 发送成功：订单号={order_number}",
            flush=True,
        )
        event_bus.publish(
            "order.wecom.sent",
            {
                "order": saved_order,
                "source": "mail-listener",
                "uid": uid,
                "message_kind": "automation_exception",
                "notification": notification,
            },
        )
        return notification

    def _notifier_for_order(self, saved_order):
        """Resolve the WeCom robot exclusively from the order's shop."""
        saved_order = saved_order or {}
        shop_id = saved_order.get("shop_id")
        if self.catalog_repository is None or shop_id is None:
            return None
        try:
            webhook_url = self.catalog_repository.get_shop_wecom_robot_webhook(
                int(shop_id)
            )
        except Exception as exc:
            print(
                f"[企业微信] 读取店铺机器人失败：店铺ID={shop_id}，{type(exc).__name__}: {exc}",
                flush=True,
            )
            return None
        if not webhook_url:
            return None
        cached = self._shop_notifier_cache.get(webhook_url)
        if cached is None:
            cached = WeComRobotNotifier(
                webhook_url,
                timeout_seconds=self.wecom_robot_timeout_seconds,
            )
            self._shop_notifier_cache[webhook_url] = cached
        return cached

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
