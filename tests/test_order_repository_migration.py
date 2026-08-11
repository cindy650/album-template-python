import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.orders.repository import OrderRepository


class OrderRepositoryMigrationTests(unittest.TestCase):
    def test_legacy_orders_table_migrates_before_new_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "orders.db"
            with closing(sqlite3.connect(db_path)) as connection:
                with connection:
                    connection.execute(
                        """
                        CREATE TABLE orders (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            order_number TEXT,
                            transaction_id TEXT,
                            shop TEXT,
                            product TEXT,
                            cover_colour TEXT NOT NULL DEFAULT '',
                            cover_colour_text TEXT NOT NULL DEFAULT '封面颜色',
                            product_options_json TEXT NOT NULL DEFAULT '{}',
                            product_options_text TEXT NOT NULL DEFAULT '商品选项',
                            personalization_json TEXT NOT NULL DEFAULT '{}',
                            personalization_text TEXT NOT NULL DEFAULT '',
                            order_text TEXT NOT NULL DEFAULT '订单号',
                            transaction_text TEXT NOT NULL DEFAULT '交易编号',
                            google_sheets_json TEXT NOT NULL DEFAULT '{}',
                            google_sheets_status TEXT NOT NULL DEFAULT 'pending',
                            google_sheets_attempts INTEGER NOT NULL DEFAULT 0,
                            google_sheets_last_error TEXT,
                            google_sheets_updated_at TEXT,
                            metadata_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO orders (
                            order_number, transaction_id, shop, product,
                            cover_colour, product_options_json,
                            created_at, updated_at
                        ) VALUES (
                            'legacy-1', 'tx-1', 'LegacyShop', 'Album',
                            'Mauve Pink', '{"Custom Field":"Custom Value"}',
                            'now', 'now'
                        )
                        """
                    )

            repository = OrderRepository(db_path)
            saved = repository.get(1)

            self.assertEqual(saved["status"], "新订单")
            self.assertIsNotNone(saved["shop_id"])
            self.assertNotIn("personalization_json", saved)
            self.assertNotIn("personalization_text", saved)
            self.assertEqual(saved["order_number_text"], "订单号")
            self.assertEqual(saved["transaction_id_text"], "交易编号")
            self.assertIsNone(saved["size_template_id"])
            self.assertEqual(
                saved["size_template_id_text"],
                "关联商品模板ID",
            )
            self.assertEqual(
                saved["product_information"],
                {
                    "Cover Colour": "Mauve Pink",
                    "Custom Field": "Custom Value",
                },
            )
            self.assertEqual(saved["product_information_text"], "商品信息")
            self.assertNotIn("order_text", saved)
            self.assertNotIn("transaction_text", saved)
            self.assertNotIn("specifications", saved)
            self.assertNotIn("specifications_text", saved)
            self.assertNotIn("cover_colour", saved)
            self.assertNotIn("cover_colour_text", saved)
            self.assertNotIn("product_options_json", saved)
            self.assertNotIn("product_options_text", saved)
            self.assertFalse(
                any(key.startswith("google_sheets_") for key in saved)
            )
            self.assertNotIn(
                "personalization_json",
                self._columns(db_path),
            )
            self.assertNotIn("personalization_text", self._columns(db_path))
            self.assertNotIn("order_text", self._columns(db_path))
            self.assertNotIn("transaction_text", self._columns(db_path))
            self.assertNotIn("specifications", self._columns(db_path))
            self.assertNotIn("specifications_text", self._columns(db_path))
            self.assertNotIn("cover_colour", self._columns(db_path))
            self.assertNotIn("cover_colour_text", self._columns(db_path))
            self.assertNotIn("product_options_json", self._columns(db_path))
            self.assertNotIn("product_options_text", self._columns(db_path))
            self.assertIn("product_information", self._columns(db_path))
            self.assertNotIn("product_information_json", self._columns(db_path))
            for column in (
                "google_sheets_json",
                "google_sheets_status",
                "google_sheets_attempts",
                "google_sheets_last_error",
                "google_sheets_updated_at",
            ):
                self.assertNotIn(column, self._columns(db_path))
            self.assertIn("idx_orders_shop_id", self._indexes(db_path))
            self.assertIn(
                "idx_orders_size_template_id",
                self._indexes(db_path),
            )
            self.assertIn("idx_orders_status", self._indexes(db_path))
            self.assertNotIn(
                "order_product_information",
                self._tables(db_path),
            )

    @staticmethod
    def _indexes(db_path: Path):
        with closing(sqlite3.connect(db_path)) as connection:
            return {
                row[1]
                for row in connection.execute("PRAGMA index_list('orders')")
            }

    @staticmethod
    def _columns(db_path: Path):
        with closing(sqlite3.connect(db_path)) as connection:
            return {
                row[1]
                for row in connection.execute("PRAGMA table_info('orders')")
            }

    @staticmethod
    def _tables(db_path: Path):
        with closing(sqlite3.connect(db_path)) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }


if __name__ == "__main__":
    unittest.main()
