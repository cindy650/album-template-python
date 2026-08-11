from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import qq_idleCopy as core
from backend.catalog.defaults import DEFAULT_SIZE_TEMPLATE_FIELDS
from backend.catalog.repository import CatalogRepository
from backend.orders.repository import OrderRepository
from backend.schemas import OrderData


EMAIL_BODY = """\
Personalized Wedding Guest Book – Linen Photo Album, Instax
Polaroid Compatible
Pages Quantity | Album Size: 50 printed | 11x10"
Instant photo size: Instax Mini
Cover Colour: Mauve Pink
Inner Page Layout: 11 inchx10inch - M4
Font Style & Lettering color: #4 and black
Names/date/location for the cover: “Mikayla &
Matt
LaGrow”
“September 6th, 2026”

*I’ll also be sending you a logo to use if possible.
if logo is doable, we can exclude our names but
leave date at bottoms
Phone Number for Delivery: 224-318-9856
Shop: LuxeJoy
Transaction ID: 5173830272
Quantity: 1
Price: CA$198.00
"""

NEW_OPTION_EMAIL_BODY = """\
Modern Wedding Album
Landscape Guest Book
Album Orientation: Landscape
Cover Material: Velvet
Custom cover message: Alice &
Bob
October 10, 2027
Foil shade: Rose Gold
Shop: LuxeJoy
Transaction ID: dynamic-1
Quantity: 1
Price: CA$210.00
"""


class IndependentOrderFieldTests(unittest.TestCase):
    def test_multiline_cover_message_remains_one_value(self):
        fields = core.extract_product_options(EMAIL_BODY)

        self.assertEqual(fields["Cover Colour"], "Mauve Pink")
        self.assertEqual(fields["Inner Page Layout"], "11 inchx10inch - M4")
        self.assertEqual(
            fields["Font Style & Lettering color"],
            "#4 and black",
        )
        self.assertEqual(
            fields["Names/date/location for the cover"],
            "“Mikayla & Matt LaGrow” “September 6th, 2026” "
            "*I’ll also be sending you a logo to use if possible. "
            "if logo is doable, we can exclude our names but "
            "leave date at bottoms",
        )
        self.assertNotIn(
            "\n",
            fields["Names/date/location for the cover"],
        )
        self.assertEqual(fields["Phone Number for Delivery"], "224-318-9856")

    def test_order_parse_does_not_use_rules_or_deepseek(self):
        with (
            patch.object(
                core,
                "read_personalization_config",
                side_effect=AssertionError("rules must not be read"),
            ),
            patch.object(
                core,
                "classify_personalization_with_deepseek",
                side_effect=AssertionError("DeepSeek must not be called"),
            ),
        ):
            order = core.parse_order_fields(
                subject="You made a sale - Order #123456",
                body=EMAIL_BODY,
                email_date="",
                shop_lookup=lambda original, resolved: True,
            )

        self.assertEqual(order["商品信息"]["Cover Colour"], "Mauve Pink")
        self.assertEqual(order["店铺"], "LuxeJoy")
        self.assertEqual(order["店铺名"], "3号店")
        self.assertEqual(
            order["产品"],
            "Personalized Wedding Guest Book – Linen Photo Album, "
            "Instax Polaroid Compatible",
        )
        self.assertNotIn("规格/尺寸", order)
        self.assertEqual(
            order["商品信息"]["Names/date/location for the cover"],
            core.extract_product_options(EMAIL_BODY)[
                "Names/date/location for the cover"
            ],
        )
        self.assertNotIn("定制信息", order)
        validated = OrderData.model_validate(order)
        self.assertEqual(validated.product_information, order["商品信息"])

    def test_new_product_option_labels_are_extracted_without_rules(self):
        order = core.parse_order_fields(
            subject="You made a sale - Order #654321",
            body=NEW_OPTION_EMAIL_BODY,
            email_date="",
            shop_lookup=lambda original, resolved: True,
        )

        self.assertEqual(order["产品"], "Modern Wedding Album Landscape Guest Book")
        self.assertEqual(order["商品信息"]["Album Orientation"], "Landscape")
        self.assertEqual(order["商品信息"]["Cover Material"], "Velvet")
        self.assertEqual(
            order["商品信息"]["Custom cover message"],
            "Alice & Bob October 10, 2027",
        )
        self.assertEqual(order["商品信息"]["Foil shade"], "Rose Gold")
        validated = OrderData.model_validate(order)
        self.assertEqual(
            validated.product_information,
            order["商品信息"],
        )

    def test_repository_stores_independent_fields(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "orders.db"
            catalog = CatalogRepository(db_path)
            template = catalog.ensure_size_template(
                shop="LuxeJoy",
                shop_name="3号店",
                product_name="Guest Book",
                fields=DEFAULT_SIZE_TEMPLATE_FIELDS,
            )
            repository = OrderRepository(db_path)
            product_information = core.extract_product_options(EMAIL_BODY)
            saved = repository.upsert(
                {
                    "订单号": "123456",
                    "店铺": "LuxeJoy",
                    "店铺名": "3号店",
                    "产品": "Guest Book",
                    "商品信息": product_information,
                }
            )

            self.assertEqual(saved["size_template_id"], template["id"])
            self.assertEqual(saved["product_information"], product_information)
            self.assertEqual(saved["shop"], "LuxeJoy")
            self.assertEqual(saved["shop_name"], "3号店")
            self.assertEqual(saved["shop_name_text"], "店铺名")
            self.assertEqual(saved["product_information_text"], "商品信息")
            self.assertNotIn("cover_colour", saved)
            self.assertNotIn("cover_colour_text", saved)
            self.assertFalse(
                any(key.startswith("google_sheets_") for key in saved)
            )
            self.assertEqual(saved["status"], "新订单")
            self.assertEqual(saved["order_number_text"], "订单号")
            self.assertEqual(saved["transaction_id_text"], "交易编号")
            self.assertNotIn("order_text", saved)
            self.assertNotIn("transaction_text", saved)
            self.assertNotIn("specifications", saved)
            self.assertNotIn("specifications_text", saved)
            self.assertNotIn("personalization", saved)
            self.assertNotIn("personalization_json", saved)
            self.assertNotIn("personalization_text", saved)
            payload = repository.order_payload(saved["id"])
            self.assertEqual(payload["商品信息"], product_information)


if __name__ == "__main__":
    unittest.main()
