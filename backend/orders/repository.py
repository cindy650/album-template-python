from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
from threading import Lock


FIELD_LABELS = {
    "order_text": "订单号",
    "shop_text": "店铺",
    "product_text": "产品",
    "specifications_text": "规格/尺寸",
    "personalization_text": "定制信息",
    "payment_method_text": "付款方式",
    "shipping_address_text": "邮寄地址",
    "transaction_text": "交易编号",
    "quantity_text": "数量",
    "price_text": "价格",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class OrderRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT,
                    transaction_id TEXT,
                    shop TEXT,
                    product TEXT,
                    specifications TEXT,
                    personalization_json TEXT NOT NULL DEFAULT '{}',
                    payment_method TEXT,
                    shipping_address TEXT,
                    quantity TEXT,
                    price TEXT,
                    order_text TEXT NOT NULL DEFAULT '订单号',
                    shop_text TEXT NOT NULL DEFAULT '店铺',
                    product_text TEXT NOT NULL DEFAULT '产品',
                    specifications_text TEXT NOT NULL DEFAULT '规格/尺寸',
                    personalization_text TEXT NOT NULL DEFAULT '定制信息',
                    payment_method_text TEXT NOT NULL DEFAULT '付款方式',
                    shipping_address_text TEXT NOT NULL DEFAULT '邮寄地址',
                    transaction_text TEXT NOT NULL DEFAULT '交易编号',
                    quantity_text TEXT NOT NULL DEFAULT '数量',
                    price_text TEXT NOT NULL DEFAULT '价格',
                    google_sheets_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT,
                    uid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_order_number "
                "ON orders(order_number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_transaction_id "
                "ON orders(transaction_id)"
            )
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(orders)")
            }
            for column, label in FIELD_LABELS.items():
                if column not in existing_columns:
                    escaped_label = label.replace("'", "''")
                    connection.execute(
                        f"ALTER TABLE orders ADD COLUMN {column} "
                        f"TEXT NOT NULL DEFAULT '{escaped_label}'"
                    )

    def upsert(
        self,
        order: dict[str, Any],
        google_sheets: dict[str, Any] | None = None,
        source: str = "api",
        uid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        now = utc_now()
        row_data = {
            "order_number": str(order.get("订单号") or ""),
            "transaction_id": str(order.get("交易编号") or ""),
            "shop": str(order.get("店铺") or ""),
            "product": str(order.get("产品") or ""),
            "specifications": str(order.get("规格/尺寸") or ""),
            "personalization_json": json.dumps(
                order.get("定制信息") or {},
                ensure_ascii=False,
            ),
            "payment_method": str(order.get("付款方式") or ""),
            "shipping_address": str(order.get("邮寄地址") or ""),
            "quantity": str(order.get("数量") or ""),
            "price": str(order.get("价格") or ""),
            **FIELD_LABELS,
            "google_sheets_json": json.dumps(
                google_sheets or {},
                ensure_ascii=False,
            ),
            "source": source,
            "uid": uid,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        }
        with self._lock, self.connect() as connection:
            existing = self._find_existing(connection, row_data)
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO orders (
                        order_number, transaction_id, shop, product,
                        specifications, personalization_json, payment_method,
                        shipping_address, quantity, price, google_sheets_json,
                        order_text, shop_text, product_text,
                        specifications_text, personalization_text,
                        payment_method_text, shipping_address_text,
                        transaction_text, quantity_text, price_text,
                        source, uid, metadata_json, created_at, updated_at
                    ) VALUES (
                        :order_number, :transaction_id, :shop, :product,
                        :specifications, :personalization_json, :payment_method,
                        :shipping_address, :quantity, :price,
                        :google_sheets_json, :order_text, :shop_text,
                        :product_text, :specifications_text,
                        :personalization_text, :payment_method_text,
                        :shipping_address_text, :transaction_text,
                        :quantity_text, :price_text,
                        :source, :uid, :metadata_json,
                        :created_at, :updated_at
                    )
                    """,
                    {**row_data, "created_at": now, "updated_at": now},
                )
                order_id = cursor.lastrowid
                created = True
            else:
                order_id = existing["id"]
                connection.execute(
                    """
                    UPDATE orders SET
                        order_number = :order_number,
                        transaction_id = :transaction_id,
                        shop = :shop,
                        product = :product,
                        specifications = :specifications,
                        personalization_json = :personalization_json,
                        payment_method = :payment_method,
                        shipping_address = :shipping_address,
                        quantity = :quantity,
                        price = :price,
                        order_text = :order_text,
                        shop_text = :shop_text,
                        product_text = :product_text,
                        specifications_text = :specifications_text,
                        personalization_text = :personalization_text,
                        payment_method_text = :payment_method_text,
                        shipping_address_text = :shipping_address_text,
                        transaction_text = :transaction_text,
                        quantity_text = :quantity_text,
                        price_text = :price_text,
                        google_sheets_json = :google_sheets_json,
                        source = :source,
                        uid = :uid,
                        metadata_json = :metadata_json,
                        updated_at = :updated_at
                    WHERE id = :id
                    """,
                    {**row_data, "updated_at": now, "id": order_id},
                )
                created = False
            connection.commit()
            saved = self.get(order_id)
        saved["created"] = created
        return saved

    def _find_existing(self, connection, row_data):
        transaction_id = row_data.get("transaction_id")
        order_number = row_data.get("order_number")
        if order_number:
            return connection.execute(
                "SELECT * FROM orders WHERE order_number = ? LIMIT 1",
                (order_number,),
            ).fetchone()
        if transaction_id:
            return connection.execute(
                "SELECT * FROM orders WHERE transaction_id = ? LIMIT 1",
                (transaction_id,),
            ).fetchone()
        return None

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        order_number: str | None = None,
        transaction_id: str | None = None,
        shop: str | None = None,
    ):
        where = []
        params: list[Any] = []
        if order_number:
            where.append("order_number = ?")
            params.append(order_number)
        if transaction_id:
            where.append("transaction_id = ?")
            params.append(transaction_id)
        if shop:
            where.append("shop LIKE ?")
            params.append(f"%{shop}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM orders {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT * FROM orders
                {where_sql}
                ORDER BY datetime(updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self.row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get(self, order_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return self.row_to_dict(row)

    @staticmethod
    def row_to_dict(row):
        data = dict(row)
        data["personalization"] = json.loads(
            data.pop("personalization_json") or "{}"
        )
        data["google_sheets"] = json.loads(
            data.pop("google_sheets_json") or "{}"
        )
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data
