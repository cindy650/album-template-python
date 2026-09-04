import unittest

from backend.notifications.wecom import WeComRobotNotifier
from backend.orders.service import DEEPSEEK_EMPTY_TEXT_ERROR, OrderService


TEST_WEBHOOK = "https://example.invalid/shop-1"


class FakeCatalogRepository:
    @staticmethod
    def get_shop_wecom_robot_webhook(shop_id):
        return TEST_WEBHOOK if shop_id == 1 else ""


class FakeRepository:
    def __init__(self):
        self.status = 0

    def upsert(self, order, source=None, uid=None, metadata=None):
        return {
            "id": 85,
            "order_number": order["订单号"],
            "shop": "LuxeJoy",
            "shop_name": "3号店",
            "shop_id": 1,
            "product": order["产品"],
            "product_information": order["商品信息"],
            "size_template_id": 1,
            "status": 0,
            "status_text": "新订单",
            "status_button_text": "发送示意图",
        }

    def get_by_id_and_order_number(self, order_id, order_number):
        return {
            "id": order_id,
            "order_number": order_number,
            "shop": "LuxeJoy",
            "shop_name": "3号店",
            "shop_id": 1,
            "product_information": {
                "Book Size | Page Count": "10*8 | 100 sheets",
            },
            "status": self.status,
        }

    def associate_current_template(self, order_id, order_number):
        return self.get_by_id_and_order_number(order_id, order_number)

    def order_payload(self, order_id):
        return {
            "订单号": "4156445897",
            "店铺": "LuxeJoy",
            "店铺名": "3号店",
            "商品信息": {
                "Book Size | Page Count": "10*8 | 100 sheets",
            },
        }


class EmptyTextImageGenerator:
    def __init__(self):
        self.calls = 0

    def generate_for_order(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError(f"{DEEPSEEK_EMPTY_TEXT_ERROR}：layer-1")


class FakeExceptionNotifier:
    def __init__(self):
        self.calls = []

    def notify_automation_exception(self, title, product_information):
        self.calls.append((title, product_information))
        return {"ok": True, "status": "sent"}


class FakeResponse:
    @staticmethod
    def raise_for_status():
        return None

    @staticmethod
    def json():
        return {"errcode": 0, "errmsg": "ok"}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return FakeResponse()


class AutomationExceptionNotificationTests(unittest.TestCase):
    def test_empty_deepseek_text_sends_one_alert_and_does_not_retry(self):
        generator = EmptyTextImageGenerator()
        notifier = FakeExceptionNotifier()
        service = OrderService(
            FakeRepository(),
            template_image_generator=generator,
            template_image_retry_attempts=3,
            catalog_repository=FakeCatalogRepository(),
        )
        service._shop_notifier_cache[TEST_WEBHOOK] = notifier
        order = {
            "订单号": "4156445897",
            "店铺": "LuxeJoy",
            "店铺名": "3号店",
            "产品": "Luxury Linen Wedding Guest Book",
            "商品信息": {
                "Book Size | Page Count": "10*8 | 100 sheets",
                "Cover Colour": "Royal Blue",
            },
        }

        result = service.handle_mail_order(order, uid=1597)

        self.assertEqual(generator.calls, 1)
        self.assertEqual(
            result["template_image"]["status"],
            "manual_intervention_required",
        )
        self.assertEqual(result["template_image"]["retry_attempts"], 0)
        self.assertEqual(len(notifier.calls), 1)
        title, information = notifier.calls[0]
        self.assertEqual(title, "3号店 4156445897 自动化异常需人工处理")
        self.assertEqual(information, order["商品信息"])

    def test_manual_preview_empty_text_sends_alert_before_returning_error(self):
        generator = EmptyTextImageGenerator()
        notifier = FakeExceptionNotifier()
        service = OrderService(
            FakeRepository(),
            template_image_generator=generator,
            catalog_repository=FakeCatalogRepository(),
        )
        service._shop_notifier_cache[TEST_WEBHOOK] = notifier

        with self.assertRaisesRegex(RuntimeError, DEEPSEEK_EMPTY_TEXT_ERROR):
            service.send_preview_images(85, "4156445897")

        self.assertEqual(generator.calls, 1)
        self.assertEqual(len(notifier.calls), 1)
        title, information = notifier.calls[0]
        self.assertEqual(title, "3号店 4156445897 自动化异常需人工处理")
        self.assertEqual(
            information,
            {"Book Size | Page Count": "10*8 | 100 sheets"},
        )

    def test_wecom_alert_uses_markdown_title_and_product_information(self):
        session = FakeSession()
        notifier = WeComRobotNotifier(
            "https://example.invalid/webhook",
            session=session,
        )

        result = notifier.notify_automation_exception(
            "3号店 4156445897 自动化异常需人工处理",
            {
                "Book Size | Page Count": "10*8 | 100 sheets",
                "Cover Colour": "Royal Blue",
            },
        )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(session.calls), 1)
        payload = session.calls[0][1]
        self.assertEqual(payload["msgtype"], "markdown")
        content = payload["markdown"]["content"]
        self.assertIn("### 3号店 4156445897 自动化异常需人工处理", content)
        self.assertIn("Book Size | Page Count", content)
        self.assertIn("10*8 | 100 sheets", content)
        self.assertIn("Cover Colour", content)
        self.assertIn("Royal Blue", content)


if __name__ == "__main__":
    unittest.main()
