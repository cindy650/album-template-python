from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import json
import re
import sqlite3
from threading import Lock

from backend.orders.statuses import DEFAULT_ORDER_STATUS, ORDER_STATUSES


FIELD_LABELS = {
    "order_number_text": "订单号",
    "shop_text": "店铺",
    "shop_name_text": "店铺名",
    "product_text": "产品",
    "payment_method_text": "付款方式",
    "shipping_address_text": "邮寄地址",
    "transaction_id_text": "交易编号",
    "quantity_text": "数量",
    "price_text": "价格",
    "size_template_id_text": "关联商品模板ID",
    "product_information_text": "商品信息",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


ORDER_STATUS_SQL = ", ".join(f"'{status}'" for status in ORDER_STATUSES)

LEGACY_PRODUCT_VALUE_COLUMNS = {
    "pages_album_size": "Pages Quantity | Album Size",
    "instant_photo_size": "Instant photo size",
    "cover_colour": "Cover Colour",
    "inner_page_layout": "Inner Page Layout",
    "font_style_lettering_color": "Font Style & Lettering color",
    "cover_names_date_location": "Names/date/location for the cover",
    "delivery_phone_number": "Phone Number for Delivery",
}

LEGACY_PERSONALIZATION_COLUMNS = (
    "personalization_json",
    "personalization_text",
)

LEGACY_FIELD_LABEL_COLUMNS = (
    "order_text",
    "transaction_text",
    "specifications",
    "specifications_text",
)

LEGACY_PRODUCT_INFORMATION_COLUMNS = (
    *LEGACY_PRODUCT_VALUE_COLUMNS,
    *(f"{column}_text" for column in LEGACY_PRODUCT_VALUE_COLUMNS),
    "product_information_json",
    "product_options_json",
    "product_options_text",
)

LEGACY_GOOGLE_SHEETS_COLUMNS = (
    "google_sheets_json",
    "google_sheets_status",
    "google_sheets_attempts",
    "google_sheets_last_error",
    "google_sheets_updated_at",
)


class OrderRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def initialize(self):
        with self._lock, self.connect() as connection:
            self._ensure_shop_table(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT,
                    transaction_id TEXT,
                    shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL,
                    size_template_id INTEGER
                        REFERENCES size_templates(id) ON DELETE SET NULL,
                    shop TEXT,
                    shop_name TEXT,
                    product TEXT,
                    product_information TEXT NOT NULL DEFAULT '{}',
                    payment_method TEXT,
                    shipping_address TEXT,
                    quantity TEXT,
                    price TEXT,
                    order_number_text TEXT NOT NULL DEFAULT '订单号',
                    shop_text TEXT NOT NULL DEFAULT '店铺',
                    shop_name_text TEXT NOT NULL DEFAULT '店铺名',
                    product_text TEXT NOT NULL DEFAULT '产品',
                    payment_method_text TEXT NOT NULL DEFAULT '付款方式',
                    shipping_address_text TEXT NOT NULL DEFAULT '邮寄地址',
                    transaction_id_text TEXT NOT NULL DEFAULT '交易编号',
                    quantity_text TEXT NOT NULL DEFAULT '数量',
                    price_text TEXT NOT NULL DEFAULT '价格',
                    size_template_id_text TEXT NOT NULL
                        DEFAULT '关联商品模板ID',
                    product_information_text TEXT NOT NULL DEFAULT '商品信息',
                    status TEXT NOT NULL DEFAULT '新订单'
                        CHECK (status IN ('新订单', '确认中', '已确认', '生产中', '已发货')),
                    source TEXT,
                    uid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
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
            migrations = {
                "shop_id": "INTEGER REFERENCES shops(id) ON DELETE SET NULL",
                "size_template_id": (
                    "INTEGER REFERENCES size_templates(id) ON DELETE SET NULL"
                ),
                "shop_name": "TEXT",
                "product_information": "TEXT NOT NULL DEFAULT '{}'",
                "status": (
                    f"TEXT NOT NULL DEFAULT '{DEFAULT_ORDER_STATUS}' "
                    f"CHECK (status IN ({ORDER_STATUS_SQL}))"
                ),
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE orders ADD COLUMN {column} {definition}"
                    )
            for column in LEGACY_PERSONALIZATION_COLUMNS:
                if column in existing_columns:
                    connection.execute(f"ALTER TABLE orders DROP COLUMN {column}")
            for column in LEGACY_FIELD_LABEL_COLUMNS:
                if column in existing_columns:
                    connection.execute(f"ALTER TABLE orders DROP COLUMN {column}")
            self._migrate_legacy_product_information(
                connection,
                existing_columns,
            )
            for column in LEGACY_PRODUCT_INFORMATION_COLUMNS:
                if column in existing_columns:
                    connection.execute(f"ALTER TABLE orders DROP COLUMN {column}")
            for column in LEGACY_GOOGLE_SHEETS_COLUMNS:
                if column in existing_columns:
                    connection.execute(f"ALTER TABLE orders DROP COLUMN {column}")
            connection.execute("DROP TABLE IF EXISTS order_product_information")
            connection.execute(
                f"""
                UPDATE orders
                SET status = '{DEFAULT_ORDER_STATUS}'
                WHERE status IS NULL
                   OR status = ''
                   OR status NOT IN ({ORDER_STATUS_SQL})
                """
            )
            self._link_existing_orders_to_shops(connection)
            self._link_existing_orders_to_products(connection)
            self._refresh_all_shop_counts(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_order_number "
                "ON orders(order_number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_transaction_id "
                "ON orders(transaction_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_shop_id "
                "ON orders(shop_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_size_template_id "
                "ON orders(size_template_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_status "
                "ON orders(status)"
            )

    def upsert(
        self,
        order: dict[str, Any],
        source: str = "api",
        uid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        now = utc_now()
        product_information = order.get("商品信息") or {}
        if not isinstance(product_information, dict):
            raise ValueError("商品信息必须是字段名和值组成的对象")
        product_information = {
            str(field).strip(): " ".join(str(value or "").split())
            for field, value in product_information.items()
            if str(field).strip()
        }
        row_data = {
            "order_number": str(order.get("订单号") or ""),
            "transaction_id": str(order.get("交易编号") or ""),
            "shop": str(order.get("店铺") or ""),
            "shop_name": str(order.get("店铺名") or ""),
            "product": str(order.get("产品") or ""),
            "product_information": json.dumps(
                product_information,
                ensure_ascii=False,
            ),
            "payment_method": str(order.get("付款方式") or ""),
            "shipping_address": str(order.get("邮寄地址") or ""),
            "quantity": str(order.get("数量") or ""),
            "price": str(order.get("价格") or ""),
            **FIELD_LABELS,
            "source": source,
            "uid": uid,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        }
        with self._lock, self.connect() as connection:
            print(
                "[订单] 正在关联商品："
                f"店铺={row_data['shop'] or '空'}，"
                f"商品={row_data['product'] or '空'}",
                flush=True,
            )
            product_link = self._resolve_product_link(
                connection,
                row_data["shop"],
                row_data["product"],
                row_data["shop_name"],
            )
            if product_link is None:
                message = (
                    "商品表中没有匹配商品："
                    f"店铺={row_data['shop'] or '空'}，"
                    f"商品={row_data['product'] or '空'}"
                )
                print(f"[订单] 商品关联失败：{message}", flush=True)
                raise ValueError(message)
            row_data["shop_id"] = product_link["shop_id"]
            row_data["size_template_id"] = product_link["size_template_id"]
            row_data["shop"] = product_link["shop"]
            row_data["shop_name"] = product_link["shop_name"]
            print(
                "[订单] 商品关联成功："
                f"店铺ID={row_data['shop_id']}，"
                f"模板ID={row_data['size_template_id']}",
                flush=True,
            )
            existing = self._find_existing(connection, row_data)
            previous_shop_id = existing["shop_id"] if existing is not None else None
            print(
                "[订单] 正在写入本地数据库："
                f"订单号={row_data['order_number'] or '空'}",
                flush=True,
            )
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO orders (
                        order_number, transaction_id, shop_id,
                        size_template_id, shop, shop_name, product,
                        product_information,
                        payment_method,
                        shipping_address, quantity, price,
                        status,
                        order_number_text, shop_text, shop_name_text,
                        product_text,
                        payment_method_text, shipping_address_text,
                        transaction_id_text, quantity_text, price_text,
                        size_template_id_text,
                        product_information_text,
                        source, uid, metadata_json, created_at, updated_at
                    ) VALUES (
                        :order_number, :transaction_id, :shop_id,
                        :size_template_id, :shop, :shop_name, :product,
                        :product_information,
                        :payment_method,
                        :shipping_address, :quantity, :price,
                        :status, :order_number_text, :shop_text,
                        :shop_name_text,
                        :product_text,
                        :payment_method_text,
                        :shipping_address_text, :transaction_id_text,
                        :quantity_text, :price_text, :size_template_id_text,
                        :product_information_text,
                        :source, :uid, :metadata_json,
                        :created_at, :updated_at
                    )
                    """,
                    {
                        **row_data,
                        "status": DEFAULT_ORDER_STATUS,
                        "created_at": now,
                        "updated_at": now,
                    },
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
                        shop_id = :shop_id,
                        size_template_id = :size_template_id,
                        shop = :shop,
                        shop_name = :shop_name,
                        product = :product,
                        product_information = :product_information,
                        payment_method = :payment_method,
                        shipping_address = :shipping_address,
                        quantity = :quantity,
                        price = :price,
                        order_number_text = :order_number_text,
                        shop_text = :shop_text,
                        shop_name_text = :shop_name_text,
                        product_text = :product_text,
                        payment_method_text = :payment_method_text,
                        shipping_address_text = :shipping_address_text,
                        transaction_id_text = :transaction_id_text,
                        quantity_text = :quantity_text,
                        price_text = :price_text,
                        size_template_id_text = :size_template_id_text,
                        product_information_text = :product_information_text,
                        source = :source,
                        uid = :uid,
                        metadata_json = :metadata_json,
                        updated_at = :updated_at
                    WHERE id = :id
                    """,
                    {**row_data, "updated_at": now, "id": order_id},
                )
                created = False
            self._refresh_shop_counts(connection, previous_shop_id)
            self._refresh_shop_counts(connection, row_data["shop_id"])
            connection.commit()
            saved = self.get(order_id)
        saved["created"] = created
        print(
            "[订单] 本地数据库写入成功："
            f"订单ID={saved['id']}，订单号={saved['order_number'] or '空'}，"
            f"模板ID={saved['size_template_id']}",
            flush=True,
        )
        return saved

    def order_payload(self, order_id: int):
        saved = self.get(order_id)
        if saved is None:
            return None
        return {
            "订单号": saved["order_number"],
            "店铺": saved["shop"],
            "店铺名": saved["shop_name"],
            "产品": saved["product"],
            "商品信息": saved["product_information"],
            "付款方式": saved["payment_method"],
            "邮寄地址": saved["shipping_address"],
            "交易编号": saved["transaction_id"],
            "数量": saved["quantity"],
            "价格": saved["price"],
        }

    @staticmethod
    def _validate_status(status: str):
        if status not in ORDER_STATUSES:
            allowed = "、".join(ORDER_STATUSES)
            raise ValueError(f"订单状态必须是：{allowed}")

    @staticmethod
    def _ensure_shop_table(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop TEXT NOT NULL UNIQUE,
                shop_name TEXT NOT NULL,
                product_count INTEGER NOT NULL DEFAULT 0,
                order_count INTEGER NOT NULL DEFAULT 0,
                size_template_count INTEGER NOT NULL DEFAULT 0,
                font_template_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _migrate_legacy_product_information(connection, existing_columns):
        legacy_value_columns = [
            column
            for column in LEGACY_PRODUCT_VALUE_COLUMNS
            if column in existing_columns
        ]
        json_columns = [
            column
            for column in ("product_information_json", "product_options_json")
            if column in existing_columns
        ]
        child_table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'order_product_information'
            """
        ).fetchone() is not None
        selected_columns = ["id", "product_information"]
        selected_columns.extend(legacy_value_columns)
        selected_columns.extend(json_columns)
        rows = connection.execute(
            f"SELECT {', '.join(selected_columns)} FROM orders"
        ).fetchall()
        for row in rows:
            product_information = OrderRepository._decode_product_information(
                row["product_information"]
            )
            for column in legacy_value_columns:
                value = str(row[column] or "").strip()
                if value:
                    product_information.setdefault(
                        LEGACY_PRODUCT_VALUE_COLUMNS[column],
                        value,
                    )
            for column in json_columns:
                try:
                    product_information.update(
                        json.loads(row[column] or "{}")
                    )
                except (TypeError, ValueError):
                    pass
            if child_table_exists:
                child_rows = connection.execute(
                    """
                    SELECT field_name, field_value
                    FROM order_product_information
                    WHERE order_id = ?
                    ORDER BY position
                    """,
                    (row["id"],),
                ).fetchall()
                product_information.update(
                    {
                        child["field_name"]: child["field_value"]
                        for child in child_rows
                    }
                )
            connection.execute(
                "UPDATE orders SET product_information = ? WHERE id = ?",
                (
                    json.dumps(product_information, ensure_ascii=False),
                    row["id"],
                ),
            )

    @staticmethod
    def _decode_product_information(value):
        try:
            decoded = json.loads(str(value or "{}"))
            if isinstance(decoded, dict):
                return {
                    str(field): str(field_value or "")
                    for field, field_value in decoded.items()
                }
        except (TypeError, ValueError):
            pass
        result = {}
        current_field = None
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(
                r"^(?P<field>[^:：]{1,100})\s*[:：]\s*(?P<value>.*)$",
                line,
            )
            if match:
                current_field = match.group("field").strip()
                result[current_field] = match.group("value").strip()
            elif current_field:
                result[current_field] = (
                    f"{result[current_field]} {line}"
                ).strip()
        return result

    def _resolve_shop_id(self, connection, shop: str):
        shop = str(shop or "").strip()
        if not shop:
            return None
        candidates = [shop]
        candidates.extend(
            part.strip()
            for part in reversed(shop.split("/"))
            if part.strip() and part.strip() not in candidates
        )
        for candidate in candidates:
            row = connection.execute(
                """
                SELECT id
                FROM shops
                WHERE shop = ? COLLATE NOCASE
                   OR shop_name = ? COLLATE NOCASE
                ORDER BY CASE WHEN shop = ? COLLATE NOCASE THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (candidate, candidate, candidate),
            ).fetchone()
            if row is not None:
                return row["id"]
        now = utc_now()
        cursor = connection.execute(
            """
            INSERT INTO shops (shop, shop_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (shop, shop, now, now),
        )
        return cursor.lastrowid

    def _link_existing_orders_to_shops(self, connection):
        rows = connection.execute(
            """
            SELECT DISTINCT shop, shop_name
            FROM orders
            WHERE COALESCE(shop, '') != ''
            """
        ).fetchall()
        for row in rows:
            shop_id = self._resolve_shop_id(connection, row["shop"])
            shop_row = connection.execute(
                "SELECT shop, shop_name FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE orders
                SET shop_id = ?, shop = ?, shop_name = ?
                WHERE shop = ? AND (shop_id IS NULL OR shop_id != ?)
                """,
                (
                    shop_id,
                    shop_row["shop"],
                    shop_row["shop_name"],
                    row["shop"],
                    shop_id,
                ),
            )

    def _link_existing_orders_to_products(self, connection):
        required_tables = {"size_templates", "size_template_products"}
        available_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required_tables.issubset(available_tables):
            return
        rows = connection.execute(
            """
            SELECT id, shop, shop_name, product
            FROM orders
            WHERE size_template_id IS NULL
              AND COALESCE(shop, '') != ''
              AND COALESCE(product, '') != ''
            """
        ).fetchall()
        for row in rows:
            product_link = self._resolve_product_link(
                connection,
                row["shop"],
                row["product"],
                row["shop_name"],
            )
            if product_link is not None:
                connection.execute(
                    """
                    UPDATE orders
                    SET shop_id = ?, size_template_id = ?,
                        shop = ?, shop_name = ?
                    WHERE id = ?
                    """,
                    (
                        product_link["shop_id"],
                        product_link["size_template_id"],
                        product_link["shop"],
                        product_link["shop_name"],
                        row["id"],
                    ),
                )

    @staticmethod
    def _resolve_product_link(
        connection,
        shop: str,
        product: str,
        shop_name: str = "",
    ):
        shop = str(shop or "").strip()
        shop_name = str(shop_name or "").strip()
        product = str(product or "").strip()
        if not shop or not product:
            return None
        available = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'size_template_products'
            """
        ).fetchone()
        if available is None:
            return None
        candidates = [value for value in (shop, shop_name) if value]
        candidates.extend(
            part.strip()
            for part in reversed(shop.split("/"))
            if part.strip() and part.strip() not in candidates
        )
        for candidate in candidates:
            row = connection.execute(
                """
                SELECT st.shop_id, st.id AS size_template_id,
                       s.shop, s.shop_name
                FROM size_template_products p
                JOIN size_templates st ON st.id = p.size_template_id
                JOIN shops s ON s.id = st.shop_id
                WHERE (s.shop = ? COLLATE NOCASE
                    OR s.shop_name = ? COLLATE NOCASE)
                  AND p.product_name = ? COLLATE NOCASE
                ORDER BY st.id
                LIMIT 1
                """,
                (candidate, candidate, product),
            ).fetchone()
            if row is not None:
                return row
        return None

    def _refresh_all_shop_counts(self, connection):
        rows = connection.execute("SELECT id FROM shops").fetchall()
        for row in rows:
            self._refresh_shop_counts(connection, row["id"])

    def _refresh_shop_counts(self, connection, shop_id: int | None):
        if shop_id is None:
            return
        order_count = connection.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]
        size_template_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'size_templates'
            """
        ).fetchone()
        size_template_count = 0
        font_template_count = 0
        product_names = set()
        order_products = connection.execute(
            """
            SELECT product
            FROM orders
            WHERE shop_id = ? AND COALESCE(product, '') != ''
            """,
            (shop_id,),
        ).fetchall()
        product_names.update(row["product"] for row in order_products)
        if size_template_exists is not None:
            size_template_count = connection.execute(
                "SELECT COUNT(*) AS count FROM size_templates WHERE shop_id = ?",
                (shop_id,),
            ).fetchone()["count"]
            font_template_count = connection.execute(
                "SELECT COUNT(*) AS count FROM font_templates WHERE shop_id = ?",
                (shop_id,),
            ).fetchone()["count"]
            template_products = connection.execute(
                """
                SELECT p.product_name
                FROM size_template_products p
                JOIN size_templates st ON st.id = p.size_template_id
                WHERE st.shop_id = ?
                """,
                (shop_id,),
            ).fetchall()
            product_names.update(row["product_name"] for row in template_products)
        connection.execute(
            """
            UPDATE shops SET
                product_count = ?,
                order_count = ?,
                size_template_count = ?,
                font_template_count = ?
            WHERE id = ?
            """,
            (
                len(product_names),
                order_count,
                size_template_count,
                font_template_count,
                shop_id,
            ),
        )

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
        shop_id: int | None = None,
        status: str | None = None,
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
        if shop_id is not None:
            where.append("shop_id = ?")
            params.append(shop_id)
        if status:
            self._validate_status(status)
            where.append("status = ?")
            params.append(status)
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

    def update_status(self, order_id: int, status: str):
        self._validate_status(status)
        now = utc_now()
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("订单不存在")
            connection.execute(
                """
                UPDATE orders SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, order_id),
            )
            connection.commit()
        return self.get(order_id)

    def get(self, order_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return self.row_to_dict(row)

    def get_by_id_and_order_number(self, order_id: int, order_number: str):
        normalized_order_number = str(order_number or "").strip()
        if not normalized_order_number:
            raise ValueError("订单号不能为空")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM orders
                WHERE id = ? AND order_number = ?
                LIMIT 1
                """,
                (order_id, normalized_order_number),
            ).fetchone()
        if row is None:
            raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
        return self.row_to_dict(row)

    @staticmethod
    def row_to_dict(row):
        data = dict(row)
        data["product_information"] = OrderRepository._decode_product_information(
            data["product_information"]
        )
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data
