from base64 import b64decode
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.orders.print_image import (
    DeepSeekProductInformationTranslator,
    OrderPrintImageGenerator,
)


class FakeOrderRepository:
    def __init__(self, order):
        self.order = order

    def get_by_id_and_order_number(self, order_id, order_number):
        if (
            order_id != self.order["id"]
            or order_number != self.order["order_number"]
        ):
            raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
        return self.order

    def order_payload(self, order_id):
        return {
            "订单号": self.order["order_number"],
            "店铺": self.order["shop"],
            "店铺名": self.order["shop_name"],
            "产品": self.order["product"],
            "商品信息": self.order["product_information"],
        }


class FakeTemplateImageGenerator:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.calls = 0

    def generate_for_order(self, payload, saved_order):
        self.calls += 1
        path = self.output_dir / (
            f"{saved_order['order_number']}-{saved_order['id']}-generated.jpg"
        )
        Image.new("RGB", (800, 400), "#dbe9f2").save(path, "JPEG")
        return {"path": str(path)}


class FakeTranslator:
    def __init__(self):
        self.received = None

    def translate(self, product_information):
        self.received = product_information
        return [
            "页数和相册尺寸：50页，11x10英寸",
            "封面颜色：自然亚麻色",
        ]


class OrderPrintImageTests(unittest.TestCase):
    def setUp(self):
        self.order = {
            "id": 12,
            "order_number": "A-4134685628",
            "shop": "LuxeJoy",
            "shop_name": "3号店",
            "product": "Personalized Linen Wedding Guest Book Photo Album",
            "product_information": {
                "Book Size / Page Count": "11x10 / 50 sheets",
                "Cover Colour": "Natural Linen",
            },
            "shipping_address": "HANNAH SAWCYHN\n7 HEATHER LANE\nCanada",
            "payment_method": "Paid via Etsy Payments",
            "quantity": "1",
            "created_at": "2026-08-03T10:20:30+00:00",
        }

    def test_generates_decodable_a4_jpeg_base64(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = FakeOrderRepository(self.order)
            template_generator = FakeTemplateImageGenerator(directory)
            translator = FakeTranslator()
            generator = OrderPrintImageGenerator(
                repository,
                template_generator,
                translator,
                dpi=72,
            )

            result = generator.generate(12, "A-4134685628")

            self.assertEqual(result["mime_type"], "image/jpeg")
            self.assertEqual(result["encoding"], "base64")
            self.assertEqual(result["pixel_width"], round(210 / 25.4 * 72))
            self.assertEqual(result["pixel_height"], round(297 / 25.4 * 72))
            with Image.open(BytesIO(b64decode(result["image_base64"]))) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (result["pixel_width"], result["pixel_height"]))
            self.assertEqual(translator.received, self.order["product_information"])
            self.assertEqual(template_generator.calls, 1)

    def test_reuses_latest_existing_order_template_image(self):
        with tempfile.TemporaryDirectory() as directory:
            preview_path = Path(directory) / "A-4134685628-12-existing.jpg"
            Image.new("RGB", (600, 300), "white").save(preview_path, "JPEG")
            template_generator = FakeTemplateImageGenerator(directory)
            generator = OrderPrintImageGenerator(
                FakeOrderRepository(self.order),
                template_generator,
                FakeTranslator(),
                dpi=72,
            )

            generator.generate(12, "A-4134685628")

            self.assertEqual(template_generator.calls, 0)

    def test_rejects_mismatched_order_id_and_number(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = OrderPrintImageGenerator(
                FakeOrderRepository(self.order),
                FakeTemplateImageGenerator(directory),
                FakeTranslator(),
                dpi=72,
            )

            with self.assertRaisesRegex(LookupError, "不匹配"):
                generator.generate(12, "wrong-order-number")


class DeepSeekProductInformationTranslatorTests(unittest.TestCase):
    @patch("backend.orders.print_image.requests.post")
    def test_translates_each_product_information_line_in_order(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"translations": ['
                            '"封面颜色：自然亚麻色", '
                            '"内页：50张"]}'
                        )
                    }
                }
            ]
        }
        post.return_value = response
        translator = DeepSeekProductInformationTranslator(
            "https://example.test/chat/completions",
            "test-key",
            "deepseek-chat",
        )

        result = translator.translate(
            {"Cover Colour": "Natural Linen", "Inside Pages": "50 sheets"}
        )

        self.assertEqual(result, ["封面颜色：自然亚麻色", "内页：50张"])
        response.raise_for_status.assert_called_once_with()
        request_json = post.call_args.kwargs["json"]
        self.assertEqual(request_json["response_format"], {"type": "json_object"})
        self.assertEqual(request_json["temperature"], 0)

    @patch("backend.orders.print_image.requests.post")
    def test_chinese_information_does_not_call_deepseek(self, post):
        translator = DeepSeekProductInformationTranslator(
            "https://example.test/chat/completions",
            "",
            "deepseek-chat",
        )

        result = translator.translate({"封面颜色": "红色"})

        self.assertEqual(result, ["封面颜色: 红色"])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
