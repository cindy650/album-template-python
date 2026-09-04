import unittest

from backend.orders.repository import OrderRepository
from backend.orders.service import OrderService
from backend.templates.order_resolver import (
    FontLayoutNotConfiguredError,
    OrderTemplateResolver,
)


def product_information(value):
    return {"Font Style & Lettering color": value}


def template_row(template_id, name):
    return {
        "shop_id": 1,
        "shop": "LuxeJoy",
        "shop_name": "3号店",
        "size_template_id": template_id,
        "template_name": name,
    }


def valid_snapshot(template_id=12, layout_id=7):
    workarea = {
        "id": "workarea",
        "type": "rect",
        "left": 0,
        "top": 0,
        "width": 100,
        "height": 100,
    }
    return {
        "size_template_id": template_id,
        "matched_template": {
            "template_id": template_id,
            "size_unit": "in",
            "selected_size": "10x12",
            "single_side_width": 10,
            "single_side_height": 12,
            "bleed": 0.125,
            "spine_width": 1,
            "spine_bleed": 0.0625,
        },
        "resolved_layers": {
            "id": layout_id,
            "name": "封面布局",
            "objects": [workarea],
        },
    }


class SizeTemplateStyleMatchingTests(unittest.TestCase):
    def test_hash_style_matches_template_name_suffix(self):
        rows = [
            template_row(11, "婚礼签到册-#3"),
            template_row(12, "婚礼签到册-#2"),
        ]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("#2 and Black"),
        )

        self.assertEqual(matched["size_template_id"], 12)

    def test_plain_number_matches_hash_template_name_suffix(self):
        rows = [template_row(12, "婚礼签到册-#2")]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("2"),
        )

        self.assertEqual(matched["size_template_id"], 12)

    def test_plain_number_with_color_matches_template_name_suffix(self):
        rows = [template_row(12, "婚礼签到册-#2")]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("2 and Black"),
        )

        self.assertEqual(matched["size_template_id"], 12)

    def test_explicit_hash_style_takes_priority_over_other_numbers(self):
        rows = [
            template_row(11, "婚礼签到册-#2026"),
            template_row(12, "婚礼签到册-#2"),
        ]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("2026 edition, #2 and Black"),
        )

        self.assertEqual(matched["size_template_id"], 12)

    def test_only_the_final_template_name_segment_is_used(self):
        rows = [
            template_row(20, "10x8 婚礼签到册-#3"),
            template_row(21, "10x8 婚礼签到册-#2"),
        ]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("2 and Black"),
        )

        self.assertEqual(matched["size_template_id"], 21)

    def test_unmatched_style_keeps_product_but_does_not_associate_template(self):
        rows = [template_row(11, "婚礼签到册-#3")]

        matched = OrderRepository._select_template_for_style(
            rows,
            product_information("#2 and Black"),
        )

        self.assertIsNotNone(matched)
        self.assertIsNone(matched["size_template_id"])


class SelectedFontLayoutTests(unittest.TestCase):
    def test_empty_deepseek_text_removes_rule_layer_and_keeps_other_layers(self):
        class DeepSeekStub:
            enabled = True

            @staticmethod
            def resolve(*_args, **_kwargs):
                return {
                    "text_values": {
                        "keep-layer": "Emmett",
                        "drop-layer": "",
                    }
                }

        resolver = OrderTemplateResolver(DeepSeekStub())
        template = {
            "size_spec": {"selected": None, "options": []},
            "font_layout_templates": [
                {
                    "id": 7,
                    "name": "#1",
                    "objects": [
                        {
                            "id": "keep-layer",
                            "type": "IText",
                            "left": 0,
                            "top": 0,
                            "width": 100,
                            "rules": "取姓名",
                            "text": "示例",
                        },
                        {
                            "id": "drop-layer",
                            "type": "IText",
                            "left": 0,
                            "top": 0,
                            "width": 100,
                            "rules": "没有匹配到地点时置空",
                            "text": "示例地点",
                        },
                        {
                            "id": "static-layer",
                            "type": "IText",
                            "left": 0,
                            "top": 0,
                            "width": 100,
                            "text": "固定文字",
                        },
                    ],
                }
            ],
        }

        resolved = resolver.resolve(
            template,
            {
                "order_number": "test-order",
                "product_information": product_information("#1 and Gold"),
            },
        )

        objects = resolved["_selected_font_layout"]["objects"]
        self.assertEqual(
            {item["id"] for item in objects},
            {"keep-layer", "static-layer"},
        )
        self.assertEqual(
            next(item["text"] for item in objects if item["id"] == "keep-layer"),
            "Emmett",
        )

    def test_empty_deepseek_text_without_clear_instruction_keeps_error(self):
        class DeepSeekStub:
            enabled = True

            @staticmethod
            def resolve(*_args, **_kwargs):
                return {"text_values": {"name-layer": ""}}

        resolver = OrderTemplateResolver(DeepSeekStub())
        template = {
            "size_spec": {"selected": None, "options": []},
            "font_layout_templates": [
                {
                    "id": 7,
                    "name": "#1",
                    "objects": [
                        {
                            "id": "name-layer",
                            "type": "IText",
                            "left": 0,
                            "top": 0,
                            "width": 100,
                            "rules": "从订单信息取姓名",
                            "text": "示例",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "DeepSeek 未返回全部自然语言图层文字：name-layer",
        ):
            resolver.resolve(
                template,
                {
                    "order_number": "test-order",
                    "product_information": product_information("#1 and Gold"),
                },
            )

    def test_missing_deepseek_text_keeps_error_even_when_rule_allows_empty(self):
        class DeepSeekStub:
            enabled = True

            @staticmethod
            def resolve(*_args, **_kwargs):
                return {"text_values": {}}

        resolver = OrderTemplateResolver(DeepSeekStub())
        template = {
            "size_spec": {"selected": None, "options": []},
            "font_layout_templates": [
                {
                    "id": 7,
                    "name": "#1",
                    "objects": [
                        {
                            "id": "location-layer",
                            "type": "IText",
                            "left": 0,
                            "top": 0,
                            "width": 100,
                            "rules": "没有匹配到地点时置空",
                            "text": "示例地点",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "DeepSeek 未返回全部自然语言图层文字：location-layer",
        ):
            resolver.resolve(
                template,
                {
                    "order_number": "test-order",
                    "product_information": product_information("#1 and Gold"),
                },
            )

    def test_resolver_uses_the_size_templates_selected_layout_without_name_matching(self):
        resolver = OrderTemplateResolver()
        template = {
            "size_spec": {"selected": None, "options": []},
            "font_layout_templates": [
                {"id": 7, "name": "任意布局名称", "objects": []}
            ],
        }

        resolved = resolver.resolve(template, {
            "order_number": "test-order",
            "product_information": product_information("#2 and Black"),
        })

        self.assertEqual(resolved["_selected_font_layout"]["id"], 7)

    def test_resolver_skips_when_size_template_has_no_selected_font_layout(self):
        resolver = OrderTemplateResolver()
        template = {
            "size_spec": {"selected": None, "options": []},
            "font_layout_templates": [],
        }

        with self.assertRaisesRegex(
            FontLayoutNotConfiguredError,
            "未配置字体布局",
        ):
            resolver.resolve(template, {
                "order_number": "test-order",
                "product_information": product_information("#2 and Black"),
            })

    def test_snapshot_is_reused_when_size_template_and_layout_are_unchanged(self):
        snapshot = valid_snapshot()

        restored = OrderTemplateResolver.restore_order_snapshot(
            snapshot,
            {"id": 12, "selected_font_layout_id": 7},
        )

        self.assertIsNotNone(restored)

    def test_snapshot_is_rejected_when_size_template_changed(self):
        snapshot = valid_snapshot()

        restored = OrderTemplateResolver.restore_order_snapshot(
            snapshot,
            {"id": 13, "selected_font_layout_id": 7},
        )

        self.assertIsNone(restored)

    def test_snapshot_is_rejected_when_selected_font_layout_changed(self):
        snapshot = valid_snapshot()

        restored = OrderTemplateResolver.restore_order_snapshot(
            snapshot,
            {"id": 12, "selected_font_layout_id": 8},
        )

        self.assertIsNone(restored)


class _OrderRepositoryStub:
    @staticmethod
    def upsert(order, **_kwargs):
        return {
            "id": 83,
            "order_number": order["订单号"],
            "shop": order["店铺"],
            "shop_name": order.get("店铺名", ""),
            "product": order["产品"],
            "product_information": order["商品信息"],
            "shop_id": 1,
            "size_template_id": 12,
            "status": 0,
        }


class _NoFontLayoutGenerator:
    def __init__(self):
        self.calls = 0

    def generate_for_order(self, *_args, **_kwargs):
        self.calls += 1
        raise FontLayoutNotConfiguredError("命中的尺寸模板未配置字体布局")


class MissingFontLayoutOrderServiceTests(unittest.TestCase):
    def test_automatic_order_image_is_skipped_without_retry(self):
        generator = _NoFontLayoutGenerator()
        service = OrderService(
            _OrderRepositoryStub(),
            template_image_generator=generator,
            template_image_retry_attempts=3,
        )

        result = service.handle_mail_order({
            "订单号": "test-order",
            "店铺": "LuxeJoy",
            "店铺名": "3号店",
            "产品": "Wedding Guest Book",
            "商品信息": product_information("#2 and Black"),
        })

        self.assertEqual(generator.calls, 1)
        self.assertEqual(result["template_image"]["status"], "skipped")
        self.assertEqual(
            result["template_image"]["reason"],
            "font_layout_not_configured",
        )


if __name__ == "__main__":
    unittest.main()
