from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from typing import Any
import json
import re
from threading import Lock

from backend.database import (
    connection_scope,
    connect,
    foreign_keys,
    json_array_contains_sql,
    table_columns,
    table_exists,
    table_names,
)

from backend.orders.statuses import (
    DEFAULT_ORDER_STATUS,
    ORDER_STATUS_SEED,
)


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

class OrderRepository:
    def __init__(self, database_url):
        self.database_url = database_url
        self._lock = Lock()
        self.initialize()

    @contextmanager
    def connect(self):
        with connection_scope(self.database_url) as connection:
            yield connection

    def initialize(self):
        with self._lock, self.connect() as connection:
            self._ensure_shop_table(connection)
            self._ensure_order_status_table(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number VARCHAR(255) NOT NULL,
                    shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL,
                    shop VARCHAR(255),
                    shop_name VARCHAR(255),
                    payment_method TEXT,
                    shipping_address TEXT,
                    totals_json TEXT NOT NULL DEFAULT '{}',
                    email_sent_at TEXT,
                    customer_name VARCHAR(255),
                    country VARCHAR(100),
                    currency_code VARCHAR(10),
                    subtotal_amount DECIMAL(14, 2),
                    shipping_amount DECIMAL(14, 2),
                    product_quantity INTEGER NOT NULL DEFAULT 0,
                    source TEXT,
                    uid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_number, shop_id)
                )
                """
            )
            group_columns = {row["name"] for row in table_columns(connection, "order_groups")}
            if "totals_json" not in group_columns:
                connection.execute(
                    "ALTER TABLE order_groups ADD COLUMN totals_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "email_sent_at" not in group_columns:
                connection.execute(
                    "ALTER TABLE order_groups ADD COLUMN email_sent_at TEXT"
                )
            group_migrations = {
                "customer_name": "VARCHAR(255)",
                "country": "VARCHAR(100)",
                "currency_code": "VARCHAR(10)",
                "subtotal_amount": "DECIMAL(14, 2)",
                "shipping_amount": "DECIMAL(14, 2)",
                "product_quantity": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in group_migrations.items():
                if column not in group_columns:
                    connection.execute(
                        f"ALTER TABLE order_groups ADD COLUMN {column} {definition}"
                    )
            group_rows = connection.execute(
                "SELECT id, metadata_json FROM order_groups WHERE email_sent_at IS NULL OR email_sent_at = ''"
            ).fetchall()
            for group_row in group_rows:
                try:
                    group_metadata = json.loads(group_row["metadata_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    group_metadata = {}
                # Only backfill values that were explicitly stored under the
                # dedicated field.  The legacy ``邮件日期`` key could contain
                # an Etsy body/order date rather than the RFC email header.
                email_sent_at = (
                    ((group_metadata.get("order_group") or {}).get("email_sent_at"))
                    if isinstance(group_metadata, dict)
                    else None
                )
                if email_sent_at:
                    connection.execute(
                        "UPDATE order_groups SET email_sent_at = ? WHERE id = ?",
                        (str(email_sent_at), group_row["id"]),
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_monthly_statistics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                    stat_month DATE NOT NULL,
                    currency_code VARCHAR(10) NOT NULL DEFAULT 'CAD',
                    total_amount DECIMAL(14, 2) NOT NULL DEFAULT 0,
                    total_shipping DECIMAL(14, 2) NOT NULL DEFAULT 0,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    product_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(shop_id, stat_month, currency_code)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_daily_statistics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                    stat_date DATE NOT NULL,
                    currency_code VARCHAR(10) NOT NULL DEFAULT 'CAD',
                    total_amount DECIMAL(14, 2) NOT NULL DEFAULT 0,
                    total_shipping DECIMAL(14, 2) NOT NULL DEFAULT 0,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    product_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(shop_id, stat_date, currency_code)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_group_id INTEGER REFERENCES order_groups(id) ON DELETE SET NULL,
                    order_number TEXT,
                    transaction_id TEXT,
                    shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL,
                    size_template_id INTEGER
                        REFERENCES size_templates(id) ON DELETE SET NULL,
                    shop TEXT,
                    shop_name TEXT,
                    product TEXT,
                    product_information TEXT NOT NULL DEFAULT '{}',
                    matched_template_json TEXT NOT NULL DEFAULT '{}',
                    resolved_layers_json TEXT NOT NULL DEFAULT '{}',
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
                    status INTEGER NOT NULL DEFAULT 0
                        REFERENCES order_statuses(status),
                    source TEXT,
                    uid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    artifact_type VARCHAR(64) NOT NULL,
                    file_format VARCHAR(16) NOT NULL,
                    filename VARCHAR(255) NOT NULL,
                    local_path TEXT NOT NULL,
                    oss_url TEXT,
                    oss_object_key TEXT,
                    oss_status TEXT NOT NULL DEFAULT 'pending',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_id, artifact_type, file_format)
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in table_columns(connection, "orders")
            }
            for column, label in FIELD_LABELS.items():
                if column not in existing_columns:
                    escaped_label = label.replace("'", "''")
                    connection.execute(
                        f"ALTER TABLE orders ADD COLUMN {column} "
                        f"TEXT NOT NULL DEFAULT '{escaped_label}'"
                    )
            migrations = {
                "order_group_id": "INTEGER REFERENCES order_groups(id) ON DELETE SET NULL",
                "shop_id": "INTEGER REFERENCES shops(id) ON DELETE SET NULL",
                "size_template_id": (
                    "INTEGER REFERENCES size_templates(id) ON DELETE SET NULL"
                ),
                "shop_name": "TEXT",
                "product_information": "TEXT NOT NULL DEFAULT '{}'",
                "matched_template_json": "TEXT NOT NULL DEFAULT '{}'",
                "resolved_layers_json": "TEXT NOT NULL DEFAULT '{}'",
                "status": f"INTEGER NOT NULL DEFAULT {DEFAULT_ORDER_STATUS}",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE orders ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                UPDATE orders
                SET resolved_layers_json = '{}'
                WHERE resolved_layers_json IS NULL
                   OR resolved_layers_json = ''
                   OR resolved_layers_json = '[]'
                """
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
            connection.execute("DROP TABLE IF EXISTS order_product_information")
            self._migrate_order_status_schema(connection)
            connection.execute(
                """
                UPDATE orders
                SET status = ?
                WHERE status IS NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM order_statuses
                       WHERE order_statuses.status = orders.status
                   )
                """,
                (DEFAULT_ORDER_STATUS,),
            )
            self._link_existing_orders_to_shops(connection)
            self._link_existing_orders_to_products(connection)
            # Backfill the parent group for orders created before multi-item
            # support was introduced.
            legacy_orders = connection.execute(
                "SELECT id, order_number, shop_id, shop, shop_name, payment_method, shipping_address, source, uid, metadata_json FROM orders WHERE order_group_id IS NULL AND order_number IS NOT NULL AND order_number <> ''"
            ).fetchall()
            for legacy in legacy_orders:
                group_id = self._ensure_order_group(
                    connection,
                    legacy,
                    {},
                    legacy.get("source") or "legacy",
                    legacy.get("uid"),
                    {},
                )
                if group_id:
                    connection.execute(
                        "UPDATE orders SET order_group_id = ? WHERE id = ?",
                        (group_id, legacy["id"]),
                    )
            self._refresh_all_shop_counts(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_order_number "
                "ON orders(order_number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_order_group_id "
                "ON orders(order_group_id)"
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
        group_metadata = (metadata or {}).get("order_group") if isinstance(metadata, dict) else None
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
                product_information,
            )
            if product_link is None:
                product_link = self._resolve_unlinked_shop(
                    connection,
                    row_data["shop"],
                    row_data["shop_name"],
                )
                if product_link is None:
                    message = (
                        "系统没有匹配店铺："
                        f"店铺={row_data['shop'] or row_data['shop_name'] or '空'}"
                    )
                    print(f"[订单] 店铺关联失败：{message}", flush=True)
                    raise ValueError(message)
                self._append_shop_product_name(
                    connection,
                    product_link["shop_id"],
                    row_data["product"],
                )
                print(
                    "[订单] 商品未关联产品或模板，订单仍将入库："
                    f"店铺ID={product_link['shop_id']}，"
                    f"商品={row_data['product'] or '空'}",
                    flush=True,
                )
            row_data["shop_id"] = product_link["shop_id"]
            row_data["size_template_id"] = product_link["size_template_id"]
            row_data["shop"] = product_link["shop"]
            row_data["shop_name"] = product_link["shop_name"]
            row_data["order_group_id"] = self._ensure_order_group(
                connection, row_data, group_metadata, source, uid, metadata
            )
            print(
                "[订单] 商品关联成功："
                f"店铺ID={row_data['shop_id']}，"
                f"模板ID={row_data['size_template_id'] or '未关联'}",
                flush=True,
            )
            existing = self._find_existing(connection, row_data)
            previous_shop_id = existing["shop_id"] if existing is not None else None
            print(
                "[订单] 正在写入 MySQL 订单库："
                f"订单号={row_data['order_number'] or '空'}",
                flush=True,
            )
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO orders (
                        order_group_id, order_number, transaction_id, shop_id,
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
                        :order_group_id, :order_number, :transaction_id, :shop_id,
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
                template_changed = (
                    existing["size_template_id"] != row_data["size_template_id"]
                )
                connection.execute(
                    """
                    UPDATE orders SET
                        order_group_id = :order_group_id,
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
                if template_changed:
                    connection.execute(
                        """
                        UPDATE orders
                        SET matched_template_json = '{}',
                            resolved_layers_json = '{}'
                        WHERE id = ?
                        """,
                        (order_id,),
                    )
                created = False
            self._refresh_shop_counts(connection, previous_shop_id)
            self._refresh_shop_counts(connection, row_data["shop_id"])
            connection.commit()
            saved = self.get(order_id)
        saved["created"] = created
        print(
            "[订单] MySQL 订单库写入成功："
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
            "order_group_id": saved.get("order_group_id"),
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
            "订单总计": saved.get("order_totals", {}),
            "matched_template": saved.get("matched_template"),
            "resolved_layers": saved.get("resolved_layers"),
        }

    def save_template_resolution(
        self,
        order_id: int,
        matched_template: dict[str, Any],
        resolved_layers: dict[str, Any],
    ):
        """Persist the exact template and layer snapshot used for rendering."""
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET matched_template_json = ?,
                    resolved_layers_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(matched_template or {}, ensure_ascii=False),
                    json.dumps(resolved_layers or {}, ensure_ascii=False),
                    utc_now(),
                    order_id,
                ),
            )
            connection.commit()
        print(
            f"[订单] 模板解析快照已保存：订单ID={order_id}，"
            f"尺寸模板={bool(matched_template)}，图层={len((resolved_layers or {}).get('objects') or (resolved_layers or {}).get('elements') or [])}",
            flush=True,
        )
        return self.get(order_id)

    def save_template_json(
        self,
        order_id: int,
        order_number: str,
        template_json: dict[str, Any],
    ):
        """Save the frontend-editable Fabric document on an order."""
        normalized_order_number = str(order_number or "").strip()
        if not normalized_order_number:
            raise ValueError("订单号不能为空")
        if not isinstance(template_json, dict):
            raise ValueError("template_json 必须是对象")
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM orders
                WHERE id = ? AND order_number = ?
                LIMIT 1
                """,
                (order_id, normalized_order_number),
            ).fetchone()
            if existing is None:
                raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
            connection.execute(
                """
                UPDATE orders
                SET resolved_layers_json = ?, updated_at = ?
                WHERE id = ? AND order_number = ?
                """,
                (
                    json.dumps(template_json, ensure_ascii=False),
                    utc_now(),
                    order_id,
                    normalized_order_number,
                ),
            )
            connection.commit()
        return self.get(order_id)

    def save_artifact(self, order_id: int, artifact: dict[str, Any]):
        """Upsert one generated order file and its OSS result."""
        now = utc_now()
        oss = artifact.get("oss") if isinstance(artifact.get("oss"), dict) else {}
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO order_artifacts
                    (order_id, artifact_type, file_format, filename, local_path,
                     oss_url, oss_object_key, oss_status, file_size, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id, artifact_type, file_format) DO UPDATE SET
                    filename = excluded.filename,
                    local_path = excluded.local_path,
                    oss_url = excluded.oss_url,
                    oss_object_key = excluded.oss_object_key,
                    oss_status = excluded.oss_status,
                    file_size = excluded.file_size,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    str(artifact.get("artifact_type") or "artifact"),
                    str(artifact.get("file_format") or ""),
                    str(artifact.get("filename") or ""),
                    str(artifact.get("local_path") or ""),
                    str(oss.get("url") or "") or None,
                    str(oss.get("object_key") or "") or None,
                    str(oss.get("status") or "pending"),
                    int(artifact.get("file_size") or 0),
                    now,
                    now,
                ),
            )
        return artifact

    def delete_artifacts_by_type(self, order_id: int, artifact_type: str) -> int:
        """Delete obsolete artifact records for one order and artifact type."""
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM order_artifacts "
                "WHERE order_id = ? AND artifact_type = ?",
                (order_id, str(artifact_type)),
            ).fetchone()["count"]
            connection.execute(
                "DELETE FROM order_artifacts WHERE order_id = ? AND artifact_type = ?",
                (order_id, str(artifact_type)),
            )
            connection.commit()
        deleted = max(0, int(existing or 0))
        if deleted:
            print(
                f"[订单] 已删除旧产物记录：订单ID={order_id}，"
                f"类型={artifact_type}，数量={deleted}",
                flush=True,
            )
        return deleted

    def list_artifacts(self, order_id: int, order_number: str | None = None):
        suffix = "WHERE oa.order_id = ?"
        params: list[Any] = [order_id]
        if order_number is not None:
            suffix += " AND o.order_number = ?"
            params.append(str(order_number))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT oa.*
                FROM order_artifacts oa
                JOIN orders o ON o.id = oa.order_id
                {suffix}
                ORDER BY oa.id
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def _validate_status(self, status: int):
        try:
            normalized = int(status)
        except (TypeError, ValueError):
            normalized = None
        with self.connect() as connection:
            definition = self._status_definition(connection, normalized)
            if definition is not None:
                return definition["status"]
            allowed = "、".join(
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM order_statuses ORDER BY status"
                )
            )
        raise ValueError(f"订单状态值必须是：{allowed}")

    @staticmethod
    def _status_definition(connection, status):
        if status is None:
            return None
        return connection.execute(
            """
            SELECT status, status_text, status_button_text
            FROM order_statuses
            WHERE status = ?
            """,
            (status,),
        ).fetchone()

    @staticmethod
    def _ensure_order_status_table(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_statuses (
                status INTEGER PRIMARY KEY,
                status_text TEXT NOT NULL,
                status_button_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {
            row["name"] for row in table_columns(connection, "shops")
        }
        if "product_names_json" not in columns:
            connection.execute(
                "ALTER TABLE shops ADD COLUMN product_names_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        connection.executemany(
            """
            INSERT INTO order_statuses (
                status, status_text, status_button_text
            ) VALUES (?, ?, ?)
            ON DUPLICATE KEY UPDATE
                status_text = VALUES(status_text),
                status_button_text = VALUES(status_button_text)
            """,
            ORDER_STATUS_SEED,
        )

    @staticmethod
    def _migrate_order_status_schema(connection):
        columns = {
            row["name"]: row
            for row in table_columns(connection, "orders")
        }
        status_column = columns.get("status")
        status_has_foreign_key = any(
            row["from"] == "status" and row["table"] == "order_statuses"
                for row in foreign_keys(connection, "orders")
        )
        if (
            status_column is not None
            and "status_text" not in columns
            # A numeric status column with no legacy status_text column is
            # already normalized.
            and (status_has_foreign_key or getattr(connection, "mysql", False))
        ):
            return

        legacy_table = "orders_status_legacy"
        connection.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        connection.execute(f"ALTER TABLE orders RENAME TO {legacy_table}")
        size_template_reference = (
            "REFERENCES size_templates(id) ON DELETE SET NULL"
            if table_exists(connection, "size_templates")
            else ""
        )
        connection.execute(
            f"""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_group_id INTEGER REFERENCES order_groups(id) ON DELETE SET NULL,
                order_number TEXT,
                transaction_id TEXT,
                shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL,
                size_template_id INTEGER
                    {size_template_reference},
                shop TEXT,
                shop_name TEXT,
                product TEXT,
                product_information TEXT NOT NULL DEFAULT '{{}}',
                matched_template_json TEXT NOT NULL DEFAULT '{{}}',
                resolved_layers_json TEXT NOT NULL DEFAULT '{{}}',
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
                size_template_id_text TEXT NOT NULL DEFAULT '关联商品模板ID',
                product_information_text TEXT NOT NULL DEFAULT '商品信息',
                status INTEGER NOT NULL DEFAULT 0
                    REFERENCES order_statuses(status),
                source TEXT,
                uid INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy_columns = {
            row["name"]
            for row in connection.execute(
                f"SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{legacy_table}'"
            )
        }
        target_defaults = {
            "id": "NULL",
            "order_group_id": "NULL",
            "order_number": "NULL",
            "transaction_id": "NULL",
            "shop_id": "NULL",
            "size_template_id": "NULL",
            "shop": "NULL",
            "shop_name": "NULL",
            "product": "NULL",
            "product_information": "'{}'",
            "matched_template_json": "'{}'",
            "resolved_layers_json": "'{}'",
            "payment_method": "NULL",
            "shipping_address": "NULL",
            "quantity": "NULL",
            "price": "NULL",
            "order_number_text": "'订单号'",
            "shop_text": "'店铺'",
            "shop_name_text": "'店铺名'",
            "product_text": "'产品'",
            "payment_method_text": "'付款方式'",
            "shipping_address_text": "'邮寄地址'",
            "transaction_id_text": "'交易编号'",
            "quantity_text": "'数量'",
            "price_text": "'价格'",
            "size_template_id_text": "'关联商品模板ID'",
            "product_information_text": "'商品信息'",
            "source": "NULL",
            "uid": "NULL",
            "metadata_json": "'{}'",
            "created_at": "''",
            "updated_at": "''",
        }
        target_columns = list(target_defaults)
        select_values = [
            column if column in legacy_columns else default
            for column, default in target_defaults.items()
        ]
        if "status" in legacy_columns:
            status_expression = """
                CASE CAST(status AS TEXT)
                    WHEN '0' THEN 0
                    WHEN '1' THEN 1
                    WHEN '2' THEN 2
                    WHEN '3' THEN 3
                    WHEN '4' THEN 4
                    WHEN '5' THEN 5
                    WHEN '新订单' THEN 0
                    WHEN '确认中' THEN 1
                    WHEN '客户确认中' THEN 1
                    WHEN '已确认' THEN 2
                    WHEN '待生产' THEN 2
                    WHEN '生产中' THEN 3
                    WHEN '已完成' THEN 5
                    WHEN '未发货' THEN 4
                    WHEN '待发货' THEN 4
                    WHEN '已发货' THEN 5
                    WHEN '订单已完成' THEN 5
                    ELSE 0
                END
            """
        else:
            status_expression = "0"
        target_columns.append("status")
        select_values.append(status_expression)
        connection.execute(
            f"""
            INSERT INTO orders ({', '.join(target_columns)})
            SELECT {', '.join(select_values)} FROM {legacy_table}
            """
        )
        connection.execute(f"DROP TABLE {legacy_table}")

    @staticmethod
    def _ensure_shop_table(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop TEXT NOT NULL UNIQUE,
                shop_name TEXT NOT NULL,
                product_names_json TEXT NOT NULL DEFAULT '[]',
                product_count INTEGER NOT NULL DEFAULT 0,
                order_count INTEGER NOT NULL DEFAULT 0,
                new_order_count INTEGER NOT NULL DEFAULT 0,
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                pending_production_count INTEGER NOT NULL DEFAULT 0,
                in_production_count INTEGER NOT NULL DEFAULT 0,
                pending_shipment_count INTEGER NOT NULL DEFAULT 0,
                completed_order_count INTEGER NOT NULL DEFAULT 0,
                size_template_count INTEGER NOT NULL DEFAULT 0,
                font_template_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {row["name"] for row in table_columns(connection, "shops")}
        for column in (
            "new_order_count",
            "confirmation_count",
            "pending_production_count",
            "in_production_count",
            "pending_shipment_count",
            "completed_order_count",
        ):
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE shops ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
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
        child_table_exists = table_exists(connection, "order_product_information")
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
        size_columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        product_column = (
            "product_names_json"
            if "product_names_json" in size_columns
            else "product_names"
        )
        for candidate in candidates:
            row = connection.execute(
                f"""
                SELECT id
                FROM shops
                WHERE shop = ?
                   OR shop_name = ?
                ORDER BY CASE WHEN shop = ? THEN 0 ELSE 1 END, id
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
        required_tables = {"size_templates"}
        available_tables = set(table_names(connection))
        if not required_tables.issubset(available_tables):
            return
        rows = connection.execute(
            """
            SELECT id, shop, shop_name, product, product_information
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
                self._json_object(row["product_information"]),
            )
            if product_link is not None:
                connection.execute(
                    """
                    UPDATE orders
                    SET shop_id = ?, size_template_id = ?,
                        shop = ?, shop_name = ?,
                        matched_template_json = '{}',
                        resolved_layers_json = '{}'
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
    def _resolve_unlinked_shop(connection, shop: str, shop_name: str = ""):
        candidates = []
        for value in (shop, shop_name):
            value = str(value or "").strip()
            if value and value.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(value)
        if not candidates:
            return None
        placeholders = ", ".join("?" for _ in candidates)
        row = connection.execute(
            f"""
            SELECT id AS shop_id, shop, shop_name
            FROM shops
            WHERE shop IN ({placeholders})
               OR shop_name IN ({placeholders})
            ORDER BY id
            LIMIT 1
            """,
            [*candidates, *candidates],
        ).fetchone()
        if row is None:
            return None
        return {
            "shop_id": row["shop_id"],
            "shop": row["shop"],
            "shop_name": row["shop_name"],
            "size_template_id": None,
            "template_name": None,
        }

    @staticmethod
    def _append_shop_product_name(connection, shop_id: int, product: str):
        product = str(product or "").strip()
        if not product:
            return False
        row = connection.execute(
            "SELECT product_names_json FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            names = json.loads(row["product_names_json"] or "[]")
        except (TypeError, ValueError):
            names = []
        if not isinstance(names, list):
            names = []
        names = [str(name).strip() for name in names if str(name).strip()]
        if product.casefold() in {name.casefold() for name in names}:
            return False
        names.append(product)
        connection.execute(
            """
            UPDATE shops
            SET product_names_json = ?, product_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(names, ensure_ascii=False), len(names), utc_now(), shop_id),
        )
        return True

    @staticmethod
    def _resolve_product_link(
        connection,
        shop: str,
        product: str,
        shop_name: str = "",
        product_information: dict[str, Any] | None = None,
    ):
        shop = str(shop or "").strip()
        shop_name = str(shop_name or "").strip()
        product = str(product or "").strip()
        if not shop or not product:
            return None
        candidates = [value for value in (shop, shop_name) if value]
        candidates.extend(
            part.strip()
            for part in reversed(shop.split("/"))
            if part.strip() and part.strip() not in candidates
        )
        size_columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        product_column = (
            "product_names_json"
            if "product_names_json" in size_columns
            else "product_names"
        )
        available_tables = set(table_names(connection))
        normalized_tables = {
            "products",
            "product_shops",
            "product_names",
        }
        has_normalized_products = normalized_tables.issubset(available_tables)
        has_template_product_id = "product_id" in size_columns
        template_name_expression = (
            "st.template_name" if "template_name" in size_columns
            else "st.name" if "name" in size_columns
            else "NULL"
        )
        template_join_condition = (
            "st.product_id = p.id AND st.shop_id = s.id"
            if has_template_product_id
            else "1 = 0"
        )
        for candidate in candidates:
            if has_normalized_products:
                rows = connection.execute(
                    f"""
                    SELECT s.id AS shop_id, s.shop, s.shop_name,
                           st.id AS size_template_id,
                           {template_name_expression} AS template_name
                    FROM shops s
                    JOIN product_shops ps ON ps.shop_id = s.id
                    JOIN products p ON p.id = ps.product_id
                    JOIN product_names pn ON pn.product_id = p.id
                    LEFT JOIN size_templates st ON {template_join_condition}
                    WHERE (s.shop = ? OR s.shop_name = ?)
                      AND LOWER(TRIM(pn.name)) = LOWER(TRIM(?))
                      AND p.enabled = 1
                    ORDER BY p.id, s.id, st.id
                    """,
                    (candidate, candidate, product),
                ).fetchall()
                selected = OrderRepository._select_template_for_style(
                    rows,
                    product_information,
                )
                if selected is not None:
                    return selected

            # Compatibility fallback for product associations stored in JSON.
            rows = connection.execute(
                f"""
                SELECT s.id AS shop_id, s.shop, s.shop_name,
                       st.id AS size_template_id,
                       {template_name_expression} AS template_name
                FROM shops s
                LEFT JOIN size_templates st
                    ON st.shop_id = s.id
                   AND {json_array_contains_sql(connection, f'st.{product_column}')}
                WHERE (s.shop = ?
                    OR s.shop_name = ?)
                  AND {json_array_contains_sql(connection, 's.product_names_json')}
                ORDER BY s.id, st.id
                """,
                (product, candidate, candidate, product),
            ).fetchall()
            selected = OrderRepository._select_template_for_style(
                rows,
                product_information,
            )
            if selected is not None:
                return selected
        return None

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _style_variant(value: Any) -> int | None:
        text = str(value or "")
        hash_match = re.search(r"#\s*(\d+)", text)
        if hash_match:
            return int(hash_match.group(1))
        number_match = re.search(r"\b(\d+)\b", text)
        return int(number_match.group(1)) if number_match else None

    @classmethod
    def _select_template_for_style(cls, rows, product_information):
        variant = None
        for key, value in (product_information or {}).items():
            normalized = re.sub(r"[^a-z0-9]+", "", str(key or "").casefold())
            if normalized == "fontstyleletteringcolor" or (
                "font" in normalized and "lettering" in normalized
            ):
                variant = cls._style_variant(value)
                break
        fallback = dict(rows[0]) if rows else None
        if fallback is not None:
            fallback["size_template_id"] = None
            fallback["template_name"] = None
        if variant is None:
            return fallback
        for row in rows:
            if row["size_template_id"] is None:
                continue
            match = re.search(
                r"(?:^|[-–—_/])\s*#?\s*(\d+)\s*$",
                str(row["template_name"] or ""),
            )
            template_variant = int(match.group(1)) if match else None
            if template_variant == variant:
                return dict(row)
        return fallback

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
        status_counts = {status: 0 for status in range(6)}
        order_columns = {row["name"] for row in table_columns(connection, "orders")}
        if "status" in order_columns:
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM orders WHERE shop_id = ? GROUP BY status",
                (shop_id,),
            ).fetchall():
                try:
                    status = int(row["status"])
                except (TypeError, ValueError):
                    continue
                if status in status_counts:
                    status_counts[status] = int(row["count"] or 0)
        size_template_exists = table_exists(connection, "size_templates")
        size_template_count = 0
        font_template_count = 0
        shop_row = connection.execute(
            "SELECT product_names_json FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone()
        try:
            product_count = len(json.loads(shop_row["product_names_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            product_count = 0
        if size_template_exists:
            size_template_count = connection.execute(
                "SELECT COUNT(*) AS count FROM size_templates WHERE shop_id = ?",
                (shop_id,),
            ).fetchone()["count"]
            if table_exists(connection, "font_layout_library"):
                font_template_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM font_layout_library WHERE shop_id = ?",
                    (shop_id,),
                ).fetchone()["count"]
        connection.execute(
            """
            UPDATE shops SET
                product_count = ?,
                order_count = ?,
                new_order_count = ?,
                confirmation_count = ?,
                pending_production_count = ?,
                in_production_count = ?,
                pending_shipment_count = ?,
                completed_order_count = ?,
                size_template_count = ?,
                font_template_count = ?
            WHERE id = ?
            """,
            (
                product_count,
                order_count,
                status_counts[0],
                status_counts[1],
                status_counts[2],
                status_counts[3],
                status_counts[4],
                status_counts[5],
                size_template_count,
                font_template_count,
                shop_id,
            ),
        )

    def _find_existing(self, connection, row_data):
        transaction_id = row_data.get("transaction_id")
        order_number = row_data.get("order_number")
        if transaction_id:
            return connection.execute(
                "SELECT * FROM orders WHERE transaction_id = ? LIMIT 1",
                (transaction_id,),
            ).fetchone()
        if order_number:
            return connection.execute(
                "SELECT * FROM orders WHERE order_number = ? LIMIT 1",
                (order_number,),
            ).fetchone()
        return None

    @staticmethod
    def _ensure_order_group(connection, row_data, group_metadata, source, uid, metadata):
        order_number = str(row_data.get("order_number") or "").strip()
        if not order_number:
            return None
        shop_id = row_data.get("shop_id")
        group = group_metadata if isinstance(group_metadata, dict) else {}
        totals = group.get("订单总计") or group.get("totals") or {}
        email_sent_at = str(group.get("email_sent_at") or "").strip() or None
        customer_name = str(group.get("客户姓名") or "").strip() or None
        country = str(group.get("国家") or "").strip() or None
        product_quantity = int(group.get("产品数量") or 0)
        currency_code, subtotal_amount = OrderRepository._money_value(
            totals.get("subtotal") if isinstance(totals, dict) else None
        )
        _, shipping_amount = OrderRepository._money_value(
            totals.get("shipping") if isinstance(totals, dict) else None,
            currency_code=currency_code,
        )
        if not isinstance(totals, dict):
            totals = {}
        existing = connection.execute(
            "SELECT id FROM order_groups WHERE order_number = ? AND (shop_id = ? OR (shop_id IS NULL AND ? IS NULL)) LIMIT 1",
            (order_number, shop_id, shop_id),
        ).fetchone()
        now = utc_now()
        if existing:
            totals_clause = ", totals_json = ?" if totals else ""
            totals_params = [json.dumps(totals, ensure_ascii=False)] if totals else []
            connection.execute(
                f"UPDATE order_groups SET shop = ?, shop_name = ?, payment_method = ?, shipping_address = ?{totals_clause}, email_sent_at = COALESCE(?, email_sent_at), customer_name = COALESCE(?, customer_name), country = COALESCE(?, country), currency_code = COALESCE(?, currency_code), subtotal_amount = COALESCE(?, subtotal_amount), shipping_amount = COALESCE(?, shipping_amount), updated_at = ? WHERE id = ?",
                (row_data.get("shop"), row_data.get("shop_name"), row_data.get("payment_method"), row_data.get("shipping_address"), *totals_params, email_sent_at, customer_name, country, currency_code, subtotal_amount, shipping_amount, now, existing["id"]),
            )
            return existing["id"]
        cursor = connection.execute(
            """
            INSERT INTO order_groups
                (order_number, shop_id, shop, shop_name, payment_method,
                 shipping_address, totals_json, email_sent_at, customer_name,
                 country, currency_code, subtotal_amount, shipping_amount,
                 product_quantity, source, uid, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (order_number, shop_id, row_data.get("shop"), row_data.get("shop_name"),
             group.get("付款方式") or row_data.get("payment_method"),
             group.get("邮寄地址") or row_data.get("shipping_address"), json.dumps(totals, ensure_ascii=False), email_sent_at, customer_name,
             country, currency_code, subtotal_amount, shipping_amount, product_quantity, source, uid,
             json.dumps(metadata or {}, ensure_ascii=False), now, now),
        )
        OrderRepository._record_monthly_stat(
            connection,
            shop_id=shop_id,
            email_sent_at=email_sent_at,
            currency_code=currency_code,
            subtotal_amount=subtotal_amount,
            shipping_amount=shipping_amount,
            order_count=1,
            product_count=product_quantity,
        )
        return cursor.lastrowid

    @staticmethod
    def _money_value(value, currency_code=None):
        raw = str(value or "").strip()
        if not raw:
            return currency_code, None
        match = re.search(r"(?i)(CA\$|CAD|C\$|US\$|USD|AU\$|AUD|£|€|¥|\$)", raw)
        token = (match.group(1).upper() if match else "")
        detected = "CAD" if token in {"CA$", "CAD", "C$", "$"} else (
            "USD" if token in {"US$", "USD"} else token
        )
        if currency_code and detected and detected != currency_code:
            return detected, None
        currency_code = currency_code or detected or "CAD"
        number = re.sub(r"[^0-9,.-]", "", raw).replace(",", "")
        try:
            amount = Decimal(number).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return currency_code, None
        return currency_code, amount if currency_code == "CAD" else None

    @staticmethod
    def _record_monthly_stat(
        connection,
        *,
        shop_id,
        email_sent_at,
        currency_code,
        subtotal_amount,
        shipping_amount,
        order_count,
        product_count,
    ):
        if not shop_id or currency_code != "CAD":
            return
        try:
            stat_date = datetime.fromisoformat(str(email_sent_at)).date()
        except (TypeError, ValueError):
            stat_date = datetime.now(timezone.utc).date()
        month = stat_date.replace(day=1).isoformat()
        existing = connection.execute(
            "SELECT id FROM order_monthly_statistics WHERE shop_id = ? AND stat_month = ? AND currency_code = ? LIMIT 1",
            (shop_id, month, currency_code),
        ).fetchone()
        now = utc_now()
        if existing:
            connection.execute(
                "UPDATE order_monthly_statistics SET total_amount = total_amount + ?, total_shipping = total_shipping + ?, order_count = order_count + ?, product_count = product_count + ?, updated_at = ? WHERE id = ?",
                (subtotal_amount or 0, shipping_amount or 0, order_count, product_count, now, existing["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO order_monthly_statistics (shop_id, stat_month, currency_code, total_amount, total_shipping, order_count, product_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (shop_id, month, currency_code, subtotal_amount or 0, shipping_amount or 0, order_count, product_count, now, now),
            )
        daily = connection.execute(
            "SELECT id FROM order_daily_statistics WHERE shop_id = ? AND stat_date = ? AND currency_code = ? LIMIT 1",
            (shop_id, stat_date.isoformat(), currency_code),
        ).fetchone()
        if daily:
            connection.execute(
                "UPDATE order_daily_statistics SET total_amount = total_amount + ?, total_shipping = total_shipping + ?, order_count = order_count + ?, product_count = product_count + ?, updated_at = ? WHERE id = ?",
                (subtotal_amount or 0, shipping_amount or 0, order_count, product_count, now, daily["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO order_daily_statistics (shop_id, stat_date, currency_code, total_amount, total_shipping, order_count, product_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (shop_id, stat_date.isoformat(), currency_code, subtotal_amount or 0, shipping_amount or 0, order_count, product_count, now, now),
            )

    def list(
        self,
        limit: int = 50,
        pages: int = 1,
        order_number: str | None = None,
        transaction_id: str | None = None,
        shop: str | None = None,
        shop_id: int | None = None,
        status: int | None = None,
    ):
        if pages < 1:
            raise ValueError("页码必须从 1 开始")
        if limit < 1:
            raise ValueError("每页订单数量必须大于 0")
        offset = (pages - 1) * limit
        where = []
        params: list[Any] = []
        if order_number:
            where.append("orders.order_number = ?")
            params.append(order_number)
        if transaction_id:
            where.append("orders.transaction_id = ?")
            params.append(transaction_id)
        if shop:
            where.append("orders.shop LIKE ?")
            params.append(f"%{shop}%")
        if shop_id is not None:
            where.append("orders.shop_id = ?")
            params.append(shop_id)
        if status is not None:
            status = self._validate_status(status)
            where.append("orders.status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM orders {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                self._order_select(
                    f"{where_sql} ORDER BY CAST(orders.updated_at AS DATETIME) DESC, "
                    "orders.id DESC LIMIT ? OFFSET ?"
                ),
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self.row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "pages": pages,
            "total_pages": (total + limit - 1) // limit,
        }

    def monthly_statistics(self, shop_id: int | None = None, stat_month: str | None = None):
        """Return write-once monthly CAD counters grouped by shop."""
        where = ["currency_code = 'CAD'"]
        params: list[Any] = []
        if shop_id is not None:
            where.append("shop_id = ?")
            params.append(shop_id)
        if stat_month:
            value = str(stat_month).strip()
            if not re.fullmatch(r"\d{4}-\d{2}", value):
                raise ValueError("stat_month 必须使用 YYYY-MM 格式")
            where.append("stat_month = ?")
            params.append(f"{value}-01")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM order_monthly_statistics WHERE {' AND '.join(where)} ORDER BY stat_month DESC, shop_id",
                params,
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                shop = connection.execute(
                    "SELECT shop, shop_name FROM shops WHERE id = ? LIMIT 1",
                    (row["shop_id"],),
                ).fetchone()
                item["shop"] = shop.get("shop") if shop else ""
                item["shop_name"] = shop.get("shop_name") if shop else ""
                items.append(item)
        return {"items": items, "total": len(items)}

    def daily_statistics(self, shop_id: int | None = None, stat_date: str | None = None):
        where = ["currency_code = 'CAD'"]
        params: list[Any] = []
        if shop_id is not None:
            where.append("shop_id = ?")
            params.append(shop_id)
        if stat_date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(stat_date).strip()):
                raise ValueError("stat_date 必须使用 YYYY-MM-DD 格式")
            where.append("stat_date = ?")
            params.append(str(stat_date).strip())
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM order_daily_statistics WHERE {' AND '.join(where)} ORDER BY stat_date DESC, shop_id",
                params,
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": len(rows)}

    def order_summary_statistics(self, order_id: int):
        """Return order, daily, and monthly counters for the first WeCom notice."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT orders.id, orders.order_group_id, orders.order_number, orders.shop_id,
                       order_groups.email_sent_at, order_groups.customer_name,
                       order_groups.country, order_groups.currency_code,
                       order_groups.subtotal_amount, order_groups.shipping_amount,
                       order_groups.product_quantity
                FROM orders
                LEFT JOIN order_groups ON order_groups.id = orders.order_group_id
                WHERE orders.id = ?
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
            if row is None:
                return None
            currency = row.get("currency_code") or "CAD"
            try:
                stat_date = datetime.fromisoformat(str(row.get("email_sent_at"))).date()
            except (TypeError, ValueError):
                stat_date = datetime.now(timezone.utc).date()
            month = stat_date.replace(day=1).isoformat()
            daily = connection.execute(
                "SELECT * FROM order_daily_statistics WHERE shop_id = ? AND stat_date = ? AND currency_code = ? LIMIT 1",
                (row.get("shop_id"), stat_date.isoformat(), currency),
            ).fetchone()
            monthly = connection.execute(
                "SELECT * FROM order_monthly_statistics WHERE shop_id = ? AND stat_month = ? AND currency_code = ? LIMIT 1",
                (row.get("shop_id"), month, currency),
            ).fetchone()
        return {
            **dict(row),
            "product_quantity": int(float(row.get("product_quantity") or 0)),
            "daily": dict(daily) if daily else {},
            "monthly": dict(monthly) if monthly else {},
        }

    def list_statuses(self):
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, status_text, status_button_text
                FROM order_statuses
                ORDER BY status
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, order_id: int, status: int):
        status = self._validate_status(status)
        now = utc_now()
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT id, shop_id FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("订单不存在")
            connection.execute(
                """
                UPDATE orders
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, order_id),
            )
            self._refresh_shop_counts(connection, existing["shop_id"])
            connection.commit()
        return self.get(order_id)

    def associate_current_template(self, order_id: int, order_number: str):
        normalized_order_number = str(order_number or "").strip()
        if not normalized_order_number:
            raise ValueError("订单号不能为空")
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id, shop, shop_name, product, product_information,
                       size_template_id, shop_id
                FROM orders
                WHERE id = ? AND order_number = ?
                LIMIT 1
                """,
                (order_id, normalized_order_number),
            ).fetchone()
            if existing is None:
                raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
            product_link = self._resolve_product_link(
                connection,
                existing["shop"],
                existing["product"],
                existing["shop_name"],
                self._json_object(existing["product_information"]),
            )
            if product_link is None:
                raise ValueError(
                    "订单商品尚未关联产品，无法生成并发送示意图："
                    f"店铺={existing['shop'] or existing['shop_name'] or '空'}，"
                    f"商品={existing['product'] or '空'}"
                )
            template_changed = (
                existing["size_template_id"] != product_link["size_template_id"]
            )
            connection.execute(
                """
                UPDATE orders
                SET shop_id = ?, size_template_id = ?,
                    shop = ?, shop_name = ?,
                    matched_template_json = CASE WHEN ? THEN '{}' ELSE matched_template_json END,
                    resolved_layers_json = CASE WHEN ? THEN '{}' ELSE resolved_layers_json END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    product_link["shop_id"],
                    product_link["size_template_id"],
                    product_link["shop"],
                    product_link["shop_name"],
                    template_changed,
                    template_changed,
                    utc_now(),
                    order_id,
                ),
            )
            self._refresh_shop_counts(connection, existing["shop_id"])
            self._refresh_shop_counts(connection, product_link["shop_id"])
            connection.commit()
            if not product_link["size_template_id"]:
                raise ValueError(
                    "订单样式未匹配到尺寸模板，已跳过订单图片："
                    f"店铺={existing['shop'] or existing['shop_name'] or '空'}，"
                    f"商品={existing['product'] or '空'}"
                )
        return self.get(order_id)

    def advance_status(
        self,
        order_id: int,
        order_number: str,
        expected_status: int | None = None,
    ):
        normalized_order_number = str(order_number or "").strip()
        if not normalized_order_number:
            raise ValueError("订单号不能为空")
        now = utc_now()
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id, status, shop_id FROM orders
                WHERE id = ? AND order_number = ?
                LIMIT 1
                """,
                (order_id, normalized_order_number),
            ).fetchone()
            if existing is None:
                raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
            current_status = self._validate_status(existing["status"])
            if expected_status is not None and current_status != self._validate_status(
                expected_status
            ):
                raise ValueError(
                    "订单状态已变化，请刷新订单后重试："
                    f"预期状态={expected_status}，当前状态={current_status}"
                )
            if current_status == DEFAULT_ORDER_STATUS:
                raise ValueError(
                    "新订单必须先调用发送示意图接口，不能直接推进状态"
                )
            next_definition = connection.execute(
                """
                SELECT status FROM order_statuses
                WHERE status > ?
                ORDER BY status
                LIMIT 1
                """,
                (current_status,),
            ).fetchone()
            if next_definition is None:
                raise ValueError("订单已经是订单已完成状态，不能继续推进")
            next_status = next_definition["status"]
            connection.execute(
                """
                UPDATE orders
                SET status = ?, updated_at = ?
                WHERE id = ? AND order_number = ?
                """,
                (
                    next_status,
                    now,
                    order_id,
                    normalized_order_number,
                ),
            )
            self._refresh_shop_counts(connection, existing["shop_id"])
            connection.commit()
        return self.get(order_id)

    def get(self, order_id: int):
        with self.connect() as connection:
            row = connection.execute(
                self._order_select("WHERE orders.id = ?"),
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
                self._order_select(
                    "WHERE orders.id = ? AND orders.order_number = ? LIMIT 1"
                ),
                (order_id, normalized_order_number),
            ).fetchone()
        if row is None:
            raise LookupError("订单 ID 与订单号不匹配，未找到对应订单")
        return self.row_to_dict(row)

    @staticmethod
    def _order_select(suffix=""):
        return f"""
            SELECT orders.*,
                   order_statuses.status_text,
                   order_statuses.status_button_text,
                   order_groups.totals_json AS order_totals_json,
                   order_groups.email_sent_at,
                   order_groups.customer_name,
                   order_groups.country,
                   order_groups.currency_code,
                   order_groups.subtotal_amount,
                   order_groups.shipping_amount,
                   order_groups.product_quantity AS order_product_quantity
            FROM orders
            JOIN order_statuses USING (status)
            LEFT JOIN order_groups ON order_groups.id = orders.order_group_id
            {suffix}
        """

    @staticmethod
    def row_to_dict(row):
        data = dict(row)
        data["product_information"] = OrderRepository._decode_product_information(
            data["product_information"]
        )
        data["matched_template"] = OrderRepository._decode_json_object(
            data.pop("matched_template_json", "{}")
        )
        data["resolved_layers"] = OrderRepository._decode_json_object(
            data.pop("resolved_layers_json", "{}")
        )
        data["order_totals"] = OrderRepository._decode_json_object(
            data.pop("order_totals_json", "{}")
        )
        data["template_json"] = data["resolved_layers"]
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    @staticmethod
    def _decode_json_object(value):
        try:
            decoded = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
