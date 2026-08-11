import base64
from hashlib import md5
import tempfile
import unittest
from pathlib import Path

from backend.notifications.wecom import WeComRobotError, WeComRobotNotifier
from backend.orders.service import OrderService


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {"errcode": 0, "errmsg": "ok"}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()


class FakeRepository:
    def upsert(self, order, **kwargs):
        return {
            "id": 7,
            "order_number": order["订单号"],
            "shop": "LuxeJoy",
            "shop_name": "3号店",
            "product": order["产品"],
            "product_information": {},
        }


class FakeImageGenerator:
    def __init__(self, path):
        self.path = path

    def generate_for_order(self, order, saved_order=None, metadata=None):
        return {"ok": True, "path": str(self.path), "format": "jpg"}


class FailingNotifier:
    def notify_order_image(self, order, image_result, saved_order=None):
        raise RuntimeError("network unavailable")


class WeComNotificationTests(unittest.TestCase):
    def test_sends_base64_image_then_order_and_product_text(self):
        with tempfile.TemporaryDirectory() as directory:
            image_bytes = b"generated-jpeg-bytes"
            image_path = Path(directory) / "order.jpg"
            image_path.write_bytes(image_bytes)
            session = FakeSession()
            notifier = WeComRobotNotifier(
                "https://example.test/webhook",
                timeout_seconds=3,
                session=session,
            )

            result = notifier.notify_order_image(
                {"订单号": "ORDER-100", "产品": "Wedding Album"},
                {"path": str(image_path)},
            )

            self.assertTrue(result["ok"])
            self.assertEqual(len(session.calls), 2)
            image_payload = session.calls[0]["json"]
            self.assertEqual(image_payload["msgtype"], "image")
            self.assertEqual(
                image_payload["image"]["base64"],
                base64.b64encode(image_bytes).decode("ascii"),
            )
            self.assertEqual(
                image_payload["image"]["md5"], md5(image_bytes).hexdigest()
            )
            self.assertEqual(session.calls[1]["json"], {
                "msgtype": "text",
                "text": {"content": "订单号：ORDER-100；产品：Wedding Album"},
            })

    def test_notification_failure_does_not_mark_image_generation_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "order.jpg"
            image_path.write_bytes(b"jpeg")
            service = OrderService(
                FakeRepository(),
                FakeImageGenerator(image_path),
                google_sheets_publisher=lambda order: {"ok": True},
                wecom_notifier=FailingNotifier(),
            )

            result = service.handle_mail_order(
                {"订单号": "ORDER-101", "产品": "Photo Album"}
            )

            self.assertTrue(result["template_image"]["ok"])
            self.assertEqual(
                result["template_image"]["wecom"]["status"], "failed"
            )
            self.assertIn(
                "network unavailable",
                result["template_image"]["wecom"]["error"],
            )

    def test_nonzero_wecom_errcode_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "order.jpg"
            image_path.write_bytes(b"generated-jpeg-bytes")
            session = FakeSession(
                [FakeResponse({"errcode": 93000, "errmsg": "invalid webhook"})]
            )
            notifier = WeComRobotNotifier(
                "https://example.test/webhook",
                session=session,
            )

            with self.assertRaisesRegex(WeComRobotError, "93000"):
                notifier.notify_order_image(
                    {"订单号": "ORDER-102", "产品": "Guest Book"},
                    {"path": str(image_path)},
                )


if __name__ == "__main__":
    unittest.main()
