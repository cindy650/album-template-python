from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
import json
import sqlite3


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_dump(value: Any):
    return json.dumps(value, ensure_ascii=False)


def json_load(value: str | None, fallback: Any):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def compact_string(value: Any):
    return str(value or "").strip()


def normalize_string_list(value: Any):
    if value is None:
        return []
    if isinstance(value, str):
        parts = (
            value.replace("，", ",")
            .replace("\n", ",")
            .replace("\r", ",")
            .split(",")
        )
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            text = compact_string(item)
            if text:
                items.append(text)
        return items
    text = compact_string(value)
    return [text] if text else []


def unique_preserve_order(values: list[str]):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def placeholders(values: list[Any]):
    return ", ".join("?" for _ in values)


class CatalogRepository:
    """SQLite storage for shops, size templates, font templates, and fonts."""

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
            self._create_schema(connection)
            self._refresh_all_shop_counts(connection)
            connection.commit()

    def _create_schema(self, connection):
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
        self._ensure_columns(
            connection,
            "shops",
            {
                "shop_name": "TEXT NOT NULL DEFAULT ''",
                "product_count": "INTEGER NOT NULL DEFAULT 0",
                "order_count": "INTEGER NOT NULL DEFAULT 0",
                "size_template_count": "INTEGER NOT NULL DEFAULT 0",
                "font_template_count": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fonts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                font_name TEXT NOT NULL UNIQUE,
                font_family TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS size_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                product_names_json TEXT NOT NULL DEFAULT '[]',
                fields_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS size_template_products (
                size_template_id INTEGER NOT NULL
                    REFERENCES size_templates(id) ON DELETE CASCADE,
                product_name TEXT NOT NULL,
                PRIMARY KEY (size_template_id, product_name)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS font_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                size_template_id INTEGER NOT NULL
                    REFERENCES size_templates(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '',
                font_id INTEGER REFERENCES fonts(id) ON DELETE SET NULL,
                layout_json TEXT NOT NULL DEFAULT '{}',
                options_json TEXT NOT NULL DEFAULT '{}',
                fields_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_size_templates_shop_id "
            "ON size_templates(shop_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_size_template_products_name "
            "ON size_template_products(product_name)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_templates_shop_id "
            "ON font_templates(shop_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_templates_size_template_id "
            "ON font_templates(size_template_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_fonts_enabled ON fonts(enabled)"
        )

    @staticmethod
    def _ensure_columns(connection, table: str, columns: dict[str, str]):
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def create_shop(self, payload: dict[str, Any]):
        shop = compact_string(payload.get("shop") or payload.get("店铺"))
        if not shop:
            raise ValueError("店铺不能为空")
        shop_name = compact_string(
            payload.get("shop_name") or payload.get("店铺名") or shop
        )
        now = utc_now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shops (shop, shop_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (shop, shop_name, now, now),
            )
            shop_id = cursor.lastrowid
            self._link_orders_to_shop(connection, shop_id, shop)
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_shop(shop_id)

    def list_shops(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
    ):
        where = []
        params: list[Any] = []
        if search:
            where.append("(shop LIKE ? OR shop_name LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock, self.connect() as connection:
            self._refresh_all_shop_counts(connection)
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM shops {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT *
                FROM shops
                {where_sql}
                ORDER BY shop COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            connection.commit()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_shop(self, shop_id: int):
        with self._lock, self.connect() as connection:
            self._refresh_shop_counts(connection, shop_id)
            row = connection.execute(
                "SELECT * FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise LookupError("店铺不存在")
        return dict(row)

    def find_shop_by_names(self, *names: str):
        candidates = []
        for name in names:
            normalized = compact_string(name)
            if normalized and normalized.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(normalized)
        if not candidates:
            return None

        placeholders = ", ".join("?" for _ in candidates)
        with self._lock, self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM shops
                WHERE shop COLLATE NOCASE IN ({placeholders})
                   OR shop_name COLLATE NOCASE IN ({placeholders})
                ORDER BY id
                LIMIT 1
                """,
                [*candidates, *candidates],
            ).fetchone()
        return dict(row) if row is not None else None

    def update_shop(self, shop_id: int, payload: dict[str, Any]):
        updates = {}
        if "shop" in payload or "店铺" in payload:
            shop = compact_string(payload.get("shop") or payload.get("店铺"))
            if not shop:
                raise ValueError("店铺不能为空")
            updates["shop"] = shop
        if "shop_name" in payload or "店铺名" in payload:
            shop_name = compact_string(
                payload.get("shop_name") or payload.get("店铺名")
            )
            if not shop_name:
                raise ValueError("店铺名不能为空")
            updates["shop_name"] = shop_name
        if not updates:
            return self.get_shop(shop_id)
        updates["updated_at"] = utc_now()
        set_sql = ", ".join(f"{column} = :{column}" for column in updates)
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("店铺不存在")
            connection.execute(
                f"UPDATE shops SET {set_sql} WHERE id = :id",
                {**updates, "id": shop_id},
            )
            if "shop" in updates:
                self._link_orders_to_shop(connection, shop_id, updates["shop"])
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_shop(shop_id)

    def delete_shop(self, shop_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("店铺不存在")
            order_count = self._orders_count(connection, shop_id)
            if order_count:
                raise ValueError("店铺已有订单，不能删除")
            connection.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
            connection.commit()
        return {"deleted": True, "id": shop_id}

    def create_size_template(self, payload: dict[str, Any]):
        shop_id = self._required_int(payload, "shop_id", "店铺id")
        product_names = self._payload_product_names(payload)
        if not product_names:
            raise ValueError("商品名不能为空")
        values = self._size_template_values(payload, product_names)
        now = utc_now()
        with self._lock, self.connect() as connection:
            self._require_shop(connection, shop_id)
            cursor = connection.execute(
                """
                INSERT INTO size_templates (
                    shop_id, name, product_name, product_names_json,
                    fields_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    values["name"],
                    values["product_name"],
                    json_dump(product_names),
                    json_dump(values["fields"]),
                    now,
                    now,
                ),
            )
            template_id = cursor.lastrowid
            self._replace_size_template_products(
                connection,
                template_id,
                product_names,
            )
            self._replace_size_template_font_links(
                connection,
                template_id,
                shop_id,
                self._payload_id_list(payload, "font_template_ids"),
            )
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_size_template(template_id)

    def list_size_templates(
        self,
        limit: int = 50,
        offset: int = 0,
        shop_id: int | None = None,
        product_name: str | None = None,
    ):
        where = []
        params: list[Any] = []
        if shop_id is not None:
            where.append("st.shop_id = ?")
            params.append(shop_id)
        if product_name:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM size_template_products p
                    WHERE p.size_template_id = st.id
                      AND p.product_name LIKE ?
                )
                """
            )
            params.append(f"%{product_name}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM size_templates st
                {where_sql}
                """,
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT st.*, s.shop, s.shop_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                {where_sql}
                ORDER BY datetime(st.updated_at) DESC, st.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._size_template_row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_size_template(self, template_id: int):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT st.*, s.shop, s.shop_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                WHERE st.id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise LookupError("尺寸模板不存在")
        return self._size_template_row_to_dict(row)

    def ensure_size_template(
        self,
        shop: str,
        shop_name: str,
        product_name: str,
        fields: dict[str, Any],
        name: str | None = None,
    ):
        shop = compact_string(shop)
        product_name = compact_string(product_name)
        if not shop or not product_name:
            raise ValueError("店铺和商品名不能为空")
        now = utc_now()
        with self._lock, self.connect() as connection:
            shop_row = connection.execute(
                "SELECT id FROM shops WHERE shop = ? COLLATE NOCASE",
                (shop,),
            ).fetchone()
            if shop_row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO shops (shop, shop_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (shop, compact_string(shop_name) or shop, now, now),
                )
                shop_id = cursor.lastrowid
            else:
                shop_id = shop_row["id"]

            existing = connection.execute(
                """
                SELECT st.id
                FROM size_templates st
                JOIN size_template_products p ON p.size_template_id = st.id
                WHERE st.shop_id = ?
                  AND p.product_name = ? COLLATE NOCASE
                ORDER BY st.id
                LIMIT 1
                """,
                (shop_id, product_name),
            ).fetchone()
            if existing is not None:
                template_id = existing["id"]
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO size_templates (
                        shop_id, name, product_name, product_names_json,
                        fields_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shop_id,
                        compact_string(name) or product_name,
                        product_name,
                        json_dump([product_name]),
                        json_dump(fields),
                        now,
                        now,
                    ),
                )
                template_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO size_template_products (
                        size_template_id, product_name
                    ) VALUES (?, ?)
                    """,
                    (template_id, product_name),
                )
            self._refresh_shop_counts(connection, shop_id)
        return self.get_size_template(template_id)

    def find_render_template(
        self,
        product_name: str,
        shop_id: int | None = None,
        shop: str | None = None,
        template_id: int | None = None,
    ):
        if template_id is not None:
            try:
                template = self.get_size_template(template_id)
            except LookupError:
                return None
            return self._flatten_render_template(template)

        product_name = compact_string(product_name)
        if not product_name:
            return None
        candidate_shop_ids = []
        with self.connect() as connection:
            if shop_id is not None:
                row = connection.execute(
                    "SELECT id FROM shops WHERE id = ?",
                    (shop_id,),
                ).fetchone()
                if row is not None:
                    candidate_shop_ids.append(row["id"])

            shop_candidates = self._shop_name_candidates(shop)
            if shop_candidates:
                sql_placeholders = ", ".join("?" for _ in shop_candidates)
                rows = connection.execute(
                    f"""
                    SELECT id
                    FROM shops
                    WHERE shop COLLATE NOCASE IN ({sql_placeholders})
                       OR shop_name COLLATE NOCASE IN ({sql_placeholders})
                    ORDER BY id
                    """,
                    [*shop_candidates, *shop_candidates],
                ).fetchall()
                candidate_shop_ids.extend(row["id"] for row in rows)

            candidate_shop_ids = list(dict.fromkeys(candidate_shop_ids))
            if not candidate_shop_ids:
                return None
            id_placeholders = ", ".join("?" for _ in candidate_shop_ids)
            row = connection.execute(
                f"""
                SELECT st.id
                FROM size_templates st
                JOIN size_template_products p ON p.size_template_id = st.id
                WHERE st.shop_id IN ({id_placeholders})
                  AND p.product_name = ? COLLATE NOCASE
                ORDER BY CASE WHEN st.shop_id = ? THEN 0 ELSE 1 END, st.id
                LIMIT 1
                """,
                [*candidate_shop_ids, product_name, shop_id or -1],
            ).fetchone()
        if row is None:
            return None
        return self._flatten_render_template(self.get_size_template(row["id"]))

    def update_size_template(self, template_id: int, payload: dict[str, Any]):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("尺寸模板不存在")
            shop_id = int(payload.get("shop_id") or existing["shop_id"])
            self._require_shop(connection, shop_id)
            product_names = (
                self._payload_product_names(payload)
                if self._payload_has_product_names(payload)
                else json_load(existing["product_names_json"], [])
            )
            if not product_names:
                raise ValueError("商品名不能为空")
            existing_fields = json_load(existing["fields_json"], {})
            values = self._size_template_values(
                payload,
                product_names,
                fallback_name=existing["name"],
                fallback_fields=existing_fields,
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE size_templates SET
                    shop_id = ?,
                    name = ?,
                    product_name = ?,
                    product_names_json = ?,
                    fields_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    shop_id,
                    values["name"],
                    values["product_name"],
                    json_dump(product_names),
                    json_dump(values["fields"]),
                    now,
                    template_id,
                ),
            )
            self._replace_size_template_products(
                connection,
                template_id,
                product_names,
            )
            if "font_template_ids" in payload:
                self._replace_size_template_font_links(
                    connection,
                    template_id,
                    shop_id,
                    self._payload_id_list(payload, "font_template_ids"),
                )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_size_template(template_id)

    def delete_size_template(self, template_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT shop_id FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("尺寸模板不存在")
            connection.execute(
                "DELETE FROM size_templates WHERE id = ?",
                (template_id,),
            )
            self._refresh_shop_counts(connection, existing["shop_id"])
            connection.commit()
        return {"deleted": True, "id": template_id}

    def create_font_template(self, payload: dict[str, Any]):
        shop_id = self._required_int(payload, "shop_id", "店铺id")
        size_template_id = self._required_int(
            payload,
            "size_template_id",
            "尺寸模板id",
        )
        values = self._font_template_values(payload)
        now = utc_now()
        with self._lock, self.connect() as connection:
            self._require_size_template(connection, size_template_id, shop_id)
            font_id = self._resolve_font_id(connection, payload)
            cursor = connection.execute(
                """
                INSERT INTO font_templates (
                    shop_id, size_template_id, name, font_id, layout_json,
                    options_json, fields_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    size_template_id,
                    values["name"],
                    font_id,
                    json_dump(values["layout"]),
                    json_dump(values["options"]),
                    json_dump(values["fields"]),
                    now,
                    now,
                ),
            )
            template_id = cursor.lastrowid
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_font_template(template_id)

    def list_font_templates(
        self,
        limit: int = 50,
        offset: int = 0,
        shop_id: int | None = None,
        size_template_id: int | None = None,
    ):
        where = []
        params: list[Any] = []
        if shop_id is not None:
            where.append("ft.shop_id = ?")
            params.append(shop_id)
        if size_template_id is not None:
            where.append("ft.size_template_id = ?")
            params.append(size_template_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM font_templates ft
                {where_sql}
                """,
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT ft.*, s.shop, s.shop_name, st.name AS size_template_name,
                       f.font_name, f.font_family, f.file_path
                FROM font_templates ft
                JOIN shops s ON s.id = ft.shop_id
                JOIN size_templates st ON st.id = ft.size_template_id
                LEFT JOIN fonts f ON f.id = ft.font_id
                {where_sql}
                ORDER BY datetime(ft.updated_at) DESC, ft.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._font_template_row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_font_template(self, template_id: int):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT ft.*, s.shop, s.shop_name, st.name AS size_template_name,
                       f.font_name, f.font_family, f.file_path
                FROM font_templates ft
                JOIN shops s ON s.id = ft.shop_id
                JOIN size_templates st ON st.id = ft.size_template_id
                LEFT JOIN fonts f ON f.id = ft.font_id
                WHERE ft.id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise LookupError("字体模板不存在")
        return self._font_template_row_to_dict(row)

    def update_font_template(self, template_id: int, payload: dict[str, Any]):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM font_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体模板不存在")
            shop_id = int(payload.get("shop_id") or existing["shop_id"])
            size_template_id = int(
                payload.get("size_template_id") or existing["size_template_id"]
            )
            self._require_size_template(connection, size_template_id, shop_id)
            existing_payload = {
                "name": existing["name"],
                "layout": json_load(existing["layout_json"], {}),
                "options": json_load(existing["options_json"], {}),
                "fields": json_load(existing["fields_json"], {}),
            }
            values = self._font_template_values(payload, existing_payload)
            font_id = (
                self._resolve_font_id(connection, payload)
                if "font_id" in payload or "font_name" in payload or "字体" in payload
                else existing["font_id"]
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE font_templates SET
                    shop_id = ?,
                    size_template_id = ?,
                    name = ?,
                    font_id = ?,
                    layout_json = ?,
                    options_json = ?,
                    fields_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    shop_id,
                    size_template_id,
                    values["name"],
                    font_id,
                    json_dump(values["layout"]),
                    json_dump(values["options"]),
                    json_dump(values["fields"]),
                    now,
                    template_id,
                ),
            )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_font_template(template_id)

    def delete_font_template(self, template_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT shop_id FROM font_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体模板不存在")
            connection.execute(
                "DELETE FROM font_templates WHERE id = ?",
                (template_id,),
            )
            self._refresh_shop_counts(connection, existing["shop_id"])
            connection.commit()
        return {"deleted": True, "id": template_id}

    def create_font(self, payload: dict[str, Any]):
        values = self._font_values(payload)
        now = utc_now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO fonts (
                    font_name, font_family, file_path, source, enabled,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["font_name"],
                    values["font_family"],
                    values["file_path"],
                    values["source"],
                    values["enabled"],
                    json_dump(values["metadata"]),
                    now,
                    now,
                ),
            )
            font_id = cursor.lastrowid
            connection.commit()
        return self.get_font(font_id)

    def list_fonts(
        self,
        limit: int = 100,
        offset: int = 0,
        enabled: bool | None = None,
        search: str | None = None,
    ):
        where = []
        params: list[Any] = []
        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        if search:
            where.append("(font_name LIKE ? OR font_family LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM fonts {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT *
                FROM fonts
                {where_sql}
                ORDER BY enabled DESC, font_name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._font_row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_font(self, font_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fonts WHERE id = ?",
                (font_id,),
            ).fetchone()
        if row is None:
            raise LookupError("字体不存在")
        return self._font_row_to_dict(row)

    def update_font(self, font_id: int, payload: dict[str, Any]):
        existing = self.get_font(font_id)
        values = self._font_values(payload, existing)
        now = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE fonts SET
                    font_name = ?,
                    font_family = ?,
                    file_path = ?,
                    source = ?,
                    enabled = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    values["font_name"],
                    values["font_family"],
                    values["file_path"],
                    values["source"],
                    values["enabled"],
                    json_dump(values["metadata"]),
                    now,
                    font_id,
                ),
            )
            connection.commit()
        return self.get_font(font_id)

    def delete_font(self, font_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM fonts WHERE id = ?",
                (font_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体不存在")
            connection.execute("DELETE FROM fonts WHERE id = ?", (font_id,))
            connection.commit()
        return {"deleted": True, "id": font_id}

    @staticmethod
    def _required_int(payload: dict[str, Any], key: str, label: str):
        value = payload.get(key) or payload.get(label)
        if value in (None, ""):
            raise ValueError(f"{label}不能为空")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字") from exc

    @staticmethod
    def _payload_id_list(payload: dict[str, Any], key: str):
        ids = []
        for value in normalize_string_list(payload.get(key)):
            try:
                ids.append(int(value))
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字列表") from exc
        return ids

    @staticmethod
    def _payload_has_product_names(payload: dict[str, Any]):
        return any(
            key in payload
            for key in ("product_name", "product_names", "商品名", "商品名列表")
        )

    def _payload_product_names(self, payload: dict[str, Any]):
        product_names = []
        product_names.extend(
            normalize_string_list(
                payload.get("product_names") or payload.get("商品名列表")
            )
        )
        product_names.extend(
            normalize_string_list(payload.get("product_name") or payload.get("商品名"))
        )
        return unique_preserve_order(product_names)

    @staticmethod
    def _dynamic_fields(
        payload: dict[str, Any],
        reserved: set[str],
        fallback: dict[str, Any] | None = None,
    ):
        fields = dict(fallback or {})
        explicit = payload.get("fields") or payload.get("字段")
        if isinstance(explicit, dict):
            fields.update(explicit)
        extra = {
            key: value
            for key, value in payload.items()
            if key not in reserved
        }
        fields.update(extra)
        return fields

    def _size_template_values(
        self,
        payload: dict[str, Any],
        product_names: list[str],
        fallback_name: str = "",
        fallback_fields: dict[str, Any] | None = None,
    ):
        name = compact_string(
            payload.get("name")
            or payload.get("template_name")
            or payload.get("模板名")
            or fallback_name
            or product_names[0]
        )
        reserved = {
            "shop_id",
            "店铺id",
            "name",
            "template_name",
            "模板名",
            "product_name",
            "product_names",
            "商品名",
            "商品名列表",
            "fields",
            "字段",
            "font_template_ids",
        }
        return {
            "name": name,
            "product_name": product_names[0],
            "fields": self._dynamic_fields(payload, reserved, fallback_fields),
        }

    def _font_template_values(
        self,
        payload: dict[str, Any],
        fallback: dict[str, Any] | None = None,
    ):
        fallback = fallback or {}
        name = compact_string(
            payload.get("name")
            or payload.get("template_name")
            or payload.get("模板名")
            or fallback.get("name")
            or "字体模板"
        )
        layout = payload.get("layout")
        if layout is None:
            layout = payload.get("positions") or payload.get("位置")
        if layout is None:
            layout = fallback.get("layout", {})
        options = payload.get("options")
        if options is None:
            options = payload.get("排版选项")
        if options is None:
            options = fallback.get("options", {})
        reserved = {
            "shop_id",
            "店铺id",
            "size_template_id",
            "尺寸模板id",
            "name",
            "template_name",
            "模板名",
            "font_id",
            "font_name",
            "字体",
            "layout",
            "positions",
            "位置",
            "options",
            "排版选项",
            "fields",
            "字段",
        }
        return {
            "name": name,
            "layout": layout,
            "options": options,
            "fields": self._dynamic_fields(
                payload,
                reserved,
                fallback.get("fields", {}),
            ),
        }

    def _font_values(
        self,
        payload: dict[str, Any],
        fallback: dict[str, Any] | None = None,
    ):
        fallback = fallback or {}
        font_name = compact_string(
            payload.get("font_name") or payload.get("字体") or fallback.get("font_name")
        )
        if not font_name:
            raise ValueError("字体名称不能为空")
        enabled = payload.get("enabled", fallback.get("enabled", True))
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off", "否"}
        metadata = payload.get("metadata") or payload.get("metadata_json")
        if metadata is None:
            metadata = fallback.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须是对象")
        return {
            "font_name": font_name,
            "font_family": compact_string(
                payload.get("font_family") or payload.get("字体族") or fallback.get("font_family")
            ),
            "file_path": compact_string(
                payload.get("file_path") or payload.get("文件路径") or fallback.get("file_path")
            ),
            "source": compact_string(payload.get("source") or fallback.get("source")),
            "enabled": 1 if enabled else 0,
            "metadata": metadata,
        }

    def _resolve_font_id(self, connection, payload: dict[str, Any]):
        font_id = payload.get("font_id")
        if font_id not in (None, ""):
            row = connection.execute(
                "SELECT id FROM fonts WHERE id = ?",
                (int(font_id),),
            ).fetchone()
            if row is None:
                raise ValueError("字体不存在")
            return int(font_id)
        font_name = compact_string(payload.get("font_name") or payload.get("字体"))
        if not font_name:
            return None
        row = connection.execute(
            "SELECT id FROM fonts WHERE font_name = ?",
            (font_name,),
        ).fetchone()
        if row is not None:
            return row["id"]
        now = utc_now()
        cursor = connection.execute(
            """
            INSERT INTO fonts (font_name, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (font_name, now, now),
        )
        return cursor.lastrowid

    def _replace_size_template_products(
        self,
        connection,
        template_id: int,
        product_names: list[str],
    ):
        connection.execute(
            "DELETE FROM size_template_products WHERE size_template_id = ?",
            (template_id,),
        )
        connection.executemany(
            """
            INSERT INTO size_template_products (size_template_id, product_name)
            VALUES (?, ?)
            """,
            [(template_id, product_name) for product_name in product_names],
        )

    def _replace_size_template_font_links(
        self,
        connection,
        template_id: int,
        shop_id: int,
        font_template_ids: list[int],
    ):
        if not font_template_ids:
            return
        rows = connection.execute(
            f"""
            SELECT id, shop_id
            FROM font_templates
            WHERE id IN ({placeholders(font_template_ids)})
            """,
            font_template_ids,
        ).fetchall()
        if len(rows) != len(set(font_template_ids)):
            raise ValueError("部分字体模板不存在")
        if any(row["shop_id"] != shop_id for row in rows):
            raise ValueError("字体模板必须属于同一个店铺")
        connection.execute(
            f"""
            UPDATE font_templates
            SET size_template_id = ?
            WHERE id IN ({placeholders(font_template_ids)})
            """,
            [template_id, *font_template_ids],
        )

    @staticmethod
    def _require_shop(connection, shop_id: int):
        row = connection.execute(
            "SELECT id FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone()
        if row is None:
            raise ValueError("店铺不存在")

    @staticmethod
    def _require_size_template(connection, template_id: int, shop_id: int):
        row = connection.execute(
            """
            SELECT id
            FROM size_templates
            WHERE id = ? AND shop_id = ?
            """,
            (template_id, shop_id),
        ).fetchone()
        if row is None:
            raise ValueError("尺寸模板不存在或不属于该店铺")

    def _link_orders_to_shop(self, connection, shop_id: int, shop: str):
        orders_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'orders'
            """
        ).fetchone()
        if orders_exists is None:
            return
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(orders)")
        }
        if "shop_id" not in columns:
            return
        connection.execute(
            """
            UPDATE orders
            SET shop_id = ?
            WHERE shop = ? AND (shop_id IS NULL OR shop_id != ?)
            """,
            (shop_id, shop, shop_id),
        )

    def _orders_count(self, connection, shop_id: int):
        orders_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'orders'
            """
        ).fetchone()
        if orders_exists is None:
            return 0
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(orders)")
        }
        if "shop_id" not in columns:
            return 0
        return connection.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]

    def _refresh_all_shop_counts(self, connection):
        rows = connection.execute("SELECT id FROM shops").fetchall()
        for row in rows:
            self._refresh_shop_counts(connection, row["id"])

    def _refresh_shop_counts(self, connection, shop_id: int | None):
        if shop_id is None:
            return
        order_count = self._orders_count(connection, shop_id)
        size_template_count = connection.execute(
            "SELECT COUNT(*) AS count FROM size_templates WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]
        font_template_count = connection.execute(
            "SELECT COUNT(*) AS count FROM font_templates WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]
        product_names = set()
        if order_count:
            order_rows = connection.execute(
                """
                SELECT product
                FROM orders
                WHERE shop_id = ? AND COALESCE(product, '') != ''
                """,
                (shop_id,),
            ).fetchall()
            product_names.update(row["product"] for row in order_rows)
        product_rows = connection.execute(
            """
            SELECT p.product_name
            FROM size_template_products p
            JOIN size_templates st ON st.id = p.size_template_id
            WHERE st.shop_id = ?
            """,
            (shop_id,),
        ).fetchall()
        product_names.update(row["product_name"] for row in product_rows)
        connection.execute(
            """
            UPDATE shops SET
                product_count = ?,
                order_count = ?,
                size_template_count = ?,
                font_template_count = ?,
                updated_at = CASE WHEN updated_at = '' THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (
                len(product_names),
                order_count,
                size_template_count,
                font_template_count,
                utc_now(),
                shop_id,
            ),
        )

    def _size_template_row_to_dict(self, row):
        data = dict(row)
        data["product_names"] = json_load(
            data.pop("product_names_json"),
            [],
        )
        data["fields"] = json_load(data.pop("fields_json"), {})
        data["font_templates"] = self._font_templates_for_size_template(data["id"])
        return data

    @staticmethod
    def _flatten_render_template(template: dict[str, Any]):
        return {
            **template["fields"],
            "id": template["id"],
            "shop_id": template["shop_id"],
            "name": template["name"],
            "product_name": template["product_name"],
            "product_names": template["product_names"],
        }

    @staticmethod
    def _shop_name_candidates(shop: str | None):
        normalized = compact_string(shop)
        if not normalized:
            return []
        candidates = [normalized]
        for part in reversed(normalized.split("/")):
            part = part.strip()
            if part and part.casefold() not in {
                candidate.casefold() for candidate in candidates
            }:
                candidates.append(part)
        return candidates

    def _font_templates_for_size_template(self, template_id: int):
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, font_id
                FROM font_templates
                WHERE size_template_id = ?
                ORDER BY id
                """,
                (template_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _font_template_row_to_dict(row):
        data = dict(row)
        data["layout"] = json_load(data.pop("layout_json"), {})
        data["options"] = json_load(data.pop("options_json"), {})
        data["fields"] = json_load(data.pop("fields_json"), {})
        data["font"] = (
            {
                "id": data["font_id"],
                "font_name": data.pop("font_name"),
                "font_family": data.pop("font_family"),
                "file_path": data.pop("file_path"),
            }
            if data.get("font_id")
            else None
        )
        if data.get("font_id") is None:
            data.pop("font_name", None)
            data.pop("font_family", None)
            data.pop("file_path", None)
        return data

    @staticmethod
    def _font_row_to_dict(row):
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data
