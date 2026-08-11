import tempfile
import unittest
from pathlib import Path

from backend.catalog.defaults import (
    DEFAULT_SIZE_TEMPLATE_FIELDS,
    DEFAULT_SIZE_TEMPLATE_PRODUCT,
)
from backend.catalog.repository import CatalogRepository
from backend.orders.repository import OrderRepository
from backend.templates.image_generator import TemplateImageGenerator


class NewSizeTemplateRenderingTests(unittest.TestCase):
    def test_order_uses_shop_and_product_template_from_catalog_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "orders.db"
            catalog = CatalogRepository(db_path)
            template = catalog.ensure_size_template(
                shop="LuxeJoy",
                shop_name="3号店",
                product_name=DEFAULT_SIZE_TEMPLATE_PRODUCT,
                fields=DEFAULT_SIZE_TEMPLATE_FIELDS,
            )
            self.assertEqual(
                catalog.ensure_size_template(
                    shop="LuxeJoy",
                    shop_name="3号店",
                    product_name=DEFAULT_SIZE_TEMPLATE_PRODUCT,
                    fields=DEFAULT_SIZE_TEMPLATE_FIELDS,
                )["id"],
                template["id"],
            )

            orders = OrderRepository(db_path)
            payload = {
                "订单号": "template-test-1",
                "店铺": "LuxeJoy",
                "店铺名": "3号店",
                "产品": DEFAULT_SIZE_TEMPLATE_PRODUCT,
                "定制信息": {},
                "付款方式": "",
                "邮寄地址": "",
                "交易编号": "template-test-tx-1",
                "数量": "1",
                "价格": "1",
            }
            saved_order = orders.upsert(payload)
            self.assertEqual(saved_order["shop_id"], template["shop_id"])
            self.assertEqual(saved_order["size_template_id"], template["id"])
            self.assertEqual(
                saved_order["size_template_id_text"],
                "关联商品模板ID",
            )

            render_template = catalog.find_render_template(
                product_name=DEFAULT_SIZE_TEMPLATE_PRODUCT,
                shop_id=saved_order["shop_id"],
                shop=saved_order["shop"],
            )
            self.assertEqual(render_template["id"], template["id"])
            self.assertEqual(render_template["single_side_width"], 12.01)

            generator = TemplateImageGenerator(
                catalog,
                output_dir=root / "images",
                dpi=10,
            )
            result = generator.generate_for_order(payload, saved_order=saved_order)
            self.assertEqual(result["template_id"], template["id"])
            self.assertTrue(Path(result["path"]).exists())

    def test_order_without_catalog_product_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "orders.db"
            catalog = CatalogRepository(db_path)
            catalog.ensure_size_template(
                shop="LuxeJoy",
                shop_name="3号店",
                product_name="Known Product",
                fields=DEFAULT_SIZE_TEMPLATE_FIELDS,
            )
            orders = OrderRepository(db_path)

            with self.assertRaisesRegex(ValueError, "商品表中没有匹配商品"):
                orders.upsert(
                    {
                        "订单号": "missing-product-1",
                        "店铺": "LuxeJoy",
                        "店铺名": "3号店",
                        "产品": "Unknown Product",
                    }
                )

            self.assertEqual(orders.list()["total"], 0)


if __name__ == "__main__":
    unittest.main()
