from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4
import json
from backend.database import (
    connection_scope,
    foreign_keys,
    json_array_contains_any_sql,
    json_array_contains_sql,
    table_columns,
    table_exists,
    table_names,
)


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


def normalize_preview_url(value: Any):
    value = compact_string(value)
    if value and not value.startswith(("http://", "https://")):
        raise ValueError("preview_image 必须是 OSS 访问链接，请先上传预览图")
    return value


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


def normalize_common_spec_values(value: Any) -> list[dict[str, Any]]:
    """Keep frontend-provided common size presets as JSON objects unchanged."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("common_spec_values 必须是数组")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"common_spec_values[{index}] 必须是对象")
        result.append(deepcopy(item))
    return result


def placeholders(values: list[Any]):
    return ", ".join("?" for _ in values)


DEFAULT_TEXT_GENERATION_RULES = (
    (
        "背脊",
        "从订单商品信息取Spine Text字段，去掉多余符号,字母全部大写,字段为空不替换原本内容",
    ),
    (
        "地点",
        "从订单的商品信息取Names/date/location for the cover字段中的地点，字母全部大写，没有匹配到地点的话给这个字段文字置空",
    ),
    (
        "日期转数字",
        "从订单的商品信息取Names/date/location for the cover字段中取日期，是英语的需要翻译成数字并补零",
    ),
    (
        "原文日期",
        "从订单的商品信息取Names/date/location for the cover字段中取日期，",
    ),
    (
        "年转数字",
        "从订单的商品信息取Names/date/location for the cover字段中取日期年，是英语的需要翻译成数字",
    ),
    (
        "月/日转数字",
        "从订单的商品信息取Names/date/location for the cover字段中取日期月和日，是英语的需要翻译成数字并补零",
    ),
    (
        "姓氏字母全大写",
        "从订单的商品信息取Names/date/location for the cover字段中的姓氏，，字母全部大写，没的话置空",
    ),
    (
        "姓氏用原文",
        "从订单的商品信息取Names/date/location for the cover字段中的姓氏，没的话置空",
    ),
    (
        "第一个名",
        "从订单的商品信息取Names/date/location for the cover字段中的名字，第一个名",
    ),
    (
        "第二个名",
        "从订单的商品信息取Names/date/location for the cover字段中的名字，第二个名",
    ),
    (
        "第一个名首字母",
        "取订单信息Names/date/location for the cover 字段中的名字，取第一个姓名的首字母并大写",
    ),
    (
        "第二个名首字母",
        "取订单信息Names/date/location for the cover 字段中的名字，取第二个姓名的首字母并大写",
    ),
    (
        "两名字首字母",
        "取订单信息Names/date/location for the cover 字段中的名字，取第一个姓名和第二个名的首字母并小写",
    ),
    (
        "全部名字",
        "取订单信息Names/date/location for the cover 字段中的名字，名字之间的连接符需要保留",
    ),
    (
        "全部名字大写",
        "取订单信息Names/date/location for the cover 字段中的名字 ，名字之间的连接符需要保留，字母全部大写",
    ),
)


class CatalogRepository:
    """Shared storage for shops, size templates, font templates, and fonts."""

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
            existing_tables = set(table_names(connection))
            required_tables = {
                "shops",
                "size_templates",
                "fonts",
            }
            if not required_tables.issubset(existing_tables):
                self._create_schema(connection)
            self._ensure_columns(
                connection,
                "shops",
                {
                    "shop_name": "TEXT NOT NULL DEFAULT ''",
                    "product_names_json": "TEXT NOT NULL DEFAULT '[]'",
                    "wecom_robot_webhook_url": "TEXT NOT NULL DEFAULT ''",
                    "product_count": "INTEGER NOT NULL DEFAULT 0",
                    "order_count": "INTEGER NOT NULL DEFAULT 0",
                    "new_order_count": "INTEGER NOT NULL DEFAULT 0",
                    "confirmation_count": "INTEGER NOT NULL DEFAULT 0",
                    "pending_production_count": "INTEGER NOT NULL DEFAULT 0",
                    "in_production_count": "INTEGER NOT NULL DEFAULT 0",
                    "pending_shipment_count": "INTEGER NOT NULL DEFAULT 0",
                    "completed_order_count": "INTEGER NOT NULL DEFAULT 0",
                    "size_template_count": "INTEGER NOT NULL DEFAULT 0",
                    "font_template_count": "INTEGER NOT NULL DEFAULT 0",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                connection,
                "size_templates",
                {
                    "template_name": "TEXT NOT NULL DEFAULT ''",
                    "background_color": "TEXT NOT NULL DEFAULT ''",
                    "paper_thickness_mm": "REAL NOT NULL DEFAULT 0",
                    "min_spine_width": "REAL NOT NULL DEFAULT 0",
                    "max_spine_width": "REAL NOT NULL DEFAULT 0",
                    "spine_width_basis": "INTEGER NOT NULL DEFAULT 0",
                    "cover_safe_distance_json": "TEXT NOT NULL DEFAULT '{}'",
                    "spine_safe_distance_json": "TEXT NOT NULL DEFAULT '{}'",
                    "back_cover_safe_distance_json": "TEXT NOT NULL DEFAULT '{}'",
                    "max_spine_bleed": "REAL NOT NULL DEFAULT 0",
                    "min_spine_bleed": "REAL NOT NULL DEFAULT 0",
                    "size_template_info_json": "TEXT NOT NULL DEFAULT '[]'",
                    "preview_image_path": "TEXT NOT NULL DEFAULT ''",
                    "selected_size_option_id": "VARCHAR(255) NOT NULL DEFAULT ''",
                    "display_unit": "VARCHAR(10) NOT NULL DEFAULT 'in'",
                    "page_count": "INTEGER",
                    "page_count_options_json": "TEXT NOT NULL DEFAULT '[]'",
                    "selected_font_layout_id": "INTEGER",
                },
            )
            self._create_size_template_option_schema(connection)
            self._migrate_size_template_fields(connection)
            self._create_font_layout_library_schema(connection)
            self._create_product_schema(connection)
            self._create_inner_page_template_schema(connection)
            self._create_text_generation_rule_schema(connection)
            self._seed_text_generation_rules(connection)
            self._create_font_layout_size_variant_schema(connection)
            self._remove_size_template_font_layout_schema(connection)
            connection.execute("DROP TABLE IF EXISTS font_templates")
            # Product categories and their product_names are user-managed.
            # Do not repopulate them from legacy shop/template JSON on startup.
            self._link_existing_products_to_size_templates(connection)
            self._migrate_product_safe_distances(connection)
            self._refresh_all_shop_counts(connection)
            connection.commit()

    def _create_schema(self, connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop TEXT NOT NULL UNIQUE,
                shop_name TEXT NOT NULL,
                product_names_json TEXT NOT NULL DEFAULT '[]',
                wecom_robot_webhook_url TEXT NOT NULL DEFAULT '',
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
        shop_columns = {
            row["name"] for row in table_columns(connection, "shops")
        }
        self._ensure_columns(
            connection,
            "shops",
            {
                "shop_name": "TEXT NOT NULL DEFAULT ''",
                "product_names_json": "TEXT NOT NULL DEFAULT '[]'",
                "wecom_robot_webhook_url": "TEXT NOT NULL DEFAULT ''",
                "product_count": "INTEGER NOT NULL DEFAULT 0",
                "order_count": "INTEGER NOT NULL DEFAULT 0",
                "new_order_count": "INTEGER NOT NULL DEFAULT 0",
                "confirmation_count": "INTEGER NOT NULL DEFAULT 0",
                "pending_production_count": "INTEGER NOT NULL DEFAULT 0",
                "in_production_count": "INTEGER NOT NULL DEFAULT 0",
                "pending_shipment_count": "INTEGER NOT NULL DEFAULT 0",
                "completed_order_count": "INTEGER NOT NULL DEFAULT 0",
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
                font_preferred TEXT NOT NULL DEFAULT '',
                font_en TEXT NOT NULL DEFAULT '',
                font_all_name TEXT NOT NULL DEFAULT '',
                post_script_name TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._ensure_columns(
            connection,
            "fonts",
            {
                "font_family": "TEXT NOT NULL DEFAULT ''",
                "font_preferred": "TEXT NOT NULL DEFAULT ''",
                "font_en": "TEXT NOT NULL DEFAULT ''",
                "font_all_name": "TEXT NOT NULL DEFAULT ''",
                "post_script_name": "TEXT NOT NULL DEFAULT ''",
                "file_path": "TEXT NOT NULL DEFAULT ''",
                "source": "TEXT NOT NULL DEFAULT ''",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS size_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                template_name TEXT NOT NULL DEFAULT '',
                product_names_json TEXT NOT NULL DEFAULT '[]',
                background_color TEXT NOT NULL DEFAULT '',
                paper_thickness_mm REAL NOT NULL DEFAULT 0,
                min_spine_width REAL NOT NULL DEFAULT 0,
                max_spine_width REAL NOT NULL DEFAULT 0,
                spine_width_basis INTEGER NOT NULL DEFAULT 0,
                cover_safe_distance_json TEXT NOT NULL DEFAULT '{}',
                spine_safe_distance_json TEXT NOT NULL DEFAULT '{}',
                back_cover_safe_distance_json TEXT NOT NULL DEFAULT '{}',
                max_spine_bleed REAL NOT NULL DEFAULT 0,
                min_spine_bleed REAL NOT NULL DEFAULT 0,
                size_template_info_json TEXT NOT NULL DEFAULT '[]',
                preview_image_path TEXT NOT NULL DEFAULT '',
                selected_size_option_id VARCHAR(255) NOT NULL DEFAULT '',
                display_unit VARCHAR(10) NOT NULL DEFAULT 'in',
                page_count INTEGER,
                page_count_options_json TEXT NOT NULL DEFAULT '[]',
                selected_font_layout_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._migrate_size_template_product_storage(connection)
        self._create_size_template_option_schema(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_size_templates_shop_id "
            "ON size_templates(shop_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_fonts_enabled ON fonts(enabled)"
        )
        self._migrate_shop_product_storage(
            connection,
            include_legacy_sources="product_names_json" not in shop_columns,
        )

    @staticmethod
    def _create_font_layout_library_schema(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS font_layout_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sort_key TEXT NOT NULL DEFAULT '',
                preview_image_path TEXT NOT NULL DEFAULT '',
                layers_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_layout_library_shop_id "
            "ON font_layout_library(shop_id)"
        )
        columns = {
            row["name"]
            for row in table_columns(connection, "font_layout_library")
        }
        if "sort_key" not in columns:
            connection.execute(
                "ALTER TABLE font_layout_library "
                "ADD COLUMN sort_key TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_layout_library_sort_key "
            "ON font_layout_library(shop_id, sort_key)"
        )

    @staticmethod
    def _create_inner_page_template_schema(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inner_page_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                preview_image_path TEXT NOT NULL DEFAULT '',
                layers_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        CatalogRepository._ensure_columns(
            connection,
            "inner_page_templates",
            {
                "product_id": "INTEGER NOT NULL",
                "description": "TEXT NOT NULL DEFAULT ''",
                "preview_image_path": "TEXT NOT NULL DEFAULT ''",
                "layers_json": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_inner_page_templates_shop_id "
            "ON inner_page_templates(shop_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_inner_page_templates_product_id "
            "ON inner_page_templates(product_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inner_page_template_options (
                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                inner_page_template_id INTEGER NOT NULL,
                size_option_id VARCHAR(255) NOT NULL,
                label VARCHAR(255) NOT NULL,
                size_unit VARCHAR(10) NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                layers_json LONGTEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                UNIQUE KEY uq_inner_page_template_option (inner_page_template_id, size_option_id),
                CONSTRAINT fk_inner_page_template_options_template
                    FOREIGN KEY (inner_page_template_id)
                    REFERENCES inner_page_templates(id) ON DELETE CASCADE
            )
            """
        )
        CatalogRepository._ensure_columns(
            connection,
            "inner_page_template_options",
            {"size_unit": "VARCHAR(10) NOT NULL DEFAULT ''"},
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_inner_page_template_options_template_id "
            "ON inner_page_template_options(inner_page_template_id)"
        )

    @staticmethod
    def _create_text_generation_rule_schema(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS template_text_generation_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        CatalogRepository._ensure_columns(
            connection,
            "template_text_generation_rules",
            {
                "name": "VARCHAR(255) NOT NULL DEFAULT ''",
                "description": "TEXT NOT NULL DEFAULT ''",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_template_text_generation_rules_name "
            "ON template_text_generation_rules(name)"
        )

    @staticmethod
    def _seed_text_generation_rules(connection):
        now = utc_now()
        for name, description in DEFAULT_TEXT_GENERATION_RULES:
            connection.execute(
                """
                INSERT OR IGNORE INTO template_text_generation_rules
                    (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, description, now, now),
            )

    @staticmethod
    def _create_font_layout_size_variant_schema(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS font_layout_size_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                font_layout_id INTEGER NOT NULL,
                size_template_id INTEGER NOT NULL,
                size_template_option_id INTEGER NOT NULL,
                layers_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(font_layout_id, size_template_option_id),
                CONSTRAINT fk_font_layout_size_variants_option
                    FOREIGN KEY (size_template_option_id)
                    REFERENCES size_template_options(id) ON DELETE CASCADE
            )
            """
        )
        columns = {
            row["name"]
            for row in table_columns(connection, "font_layout_size_variants")
        }
        if "size_template_id" not in columns:
            connection.execute(
                "ALTER TABLE font_layout_size_variants "
                "ADD COLUMN size_template_id INTEGER NULL AFTER font_layout_id"
            )
        # Backfill rows created before the direct template reference existed.
        connection.execute(
            """
            UPDATE font_layout_size_variants flsv
            JOIN size_template_options sto
              ON sto.id = flsv.size_template_option_id
            SET flsv.size_template_id = sto.size_template_id
            WHERE flsv.size_template_id IS NULL
            """
        )
        connection.execute(
            "ALTER TABLE font_layout_size_variants "
            "MODIFY COLUMN size_template_id INTEGER NOT NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_layout_size_variants_option_id "
            "ON font_layout_size_variants(size_template_option_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_layout_size_variants_template_id "
            "ON font_layout_size_variants(size_template_id)"
        )
        if getattr(connection, "mysql", False):
            existing_foreign_key_columns = {
                row["from"]
                for row in foreign_keys(connection, "font_layout_size_variants")
            }
            # The variant JSON is an independent snapshot. Do not keep a
            # cascading FK to the library row: deleting a library template
            # must leave already-applied size variants intact.
            layout_fk_names = connection.execute(
                """
                SELECT DISTINCT kcu.CONSTRAINT_NAME AS constraint_name
                FROM information_schema.KEY_COLUMN_USAGE kcu
                WHERE kcu.TABLE_SCHEMA = DATABASE()
                  AND kcu.TABLE_NAME = 'font_layout_size_variants'
                  AND kcu.COLUMN_NAME = 'font_layout_id'
                  AND kcu.REFERENCED_TABLE_NAME = 'font_layout_library'
                """
            ).fetchall()
            for fk_row in layout_fk_names:
                constraint_name = compact_string(fk_row.get("constraint_name"))
                if constraint_name:
                    connection.execute(
                        f"ALTER TABLE font_layout_size_variants "
                        f"DROP FOREIGN KEY `{constraint_name}`"
                    )
            if "size_template_option_id" not in existing_foreign_key_columns:
                connection.execute(
                    "ALTER TABLE font_layout_size_variants "
                    "ADD CONSTRAINT fk_font_layout_size_variants_option "
                    "FOREIGN KEY (size_template_option_id) "
                    "REFERENCES size_template_options(id) ON DELETE CASCADE"
                )
            if "size_template_id" not in existing_foreign_key_columns:
                connection.execute(
                    "ALTER TABLE font_layout_size_variants "
                    "ADD CONSTRAINT fk_font_layout_size_variants_template "
                    "FOREIGN KEY (size_template_id) "
                    "REFERENCES size_templates(id) ON DELETE CASCADE"
                )

    @staticmethod
    def _create_product_schema(connection):
        """Create the product-centred catalog relations.

        The original application stored product names as JSON on ``shops`` and
        ``size_templates``.  Those columns remain for backwards compatibility,
        while these tables provide proper foreign-key relationships for the new
        product-first editor.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                specifications_json TEXT NOT NULL DEFAULT '[]',
                specification_field VARCHAR(255) NOT NULL
                    DEFAULT 'Book Size | Page Count',
                cover_safe_distance_json LONGTEXT NULL,
                spine_safe_distance_json LONGTEXT NULL,
                back_cover_safe_distance_json LONGTEXT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        CatalogRepository._ensure_columns(
            connection,
            "products",
            {
                "specifications_json": "TEXT NOT NULL DEFAULT '[]'",
                "specification_field": (
                    "VARCHAR(255) NOT NULL DEFAULT 'Book Size | Page Count'"
                ),
                "common_spec_values_json": "TEXT NOT NULL DEFAULT '[]'",
                # MySQL does not consistently allow literal defaults on
                # TEXT/LONGTEXT columns. Nulls are normalized below instead.
                "cover_safe_distance_json": "LONGTEXT NULL",
                "spine_safe_distance_json": "LONGTEXT NULL",
                "back_cover_safe_distance_json": "LONGTEXT NULL",
            },
        )
        connection.execute(
            """
            UPDATE products
            SET specifications_json = '[]'
            WHERE specifications_json IS NULL OR specifications_json = ''
            """
        )
        connection.execute(
            """
            UPDATE products
            SET specification_field = 'Book Size | Page Count'
            WHERE specification_field IS NULL OR TRIM(specification_field) = ''
            """
        )
        connection.execute(
            """
            UPDATE products
            SET common_spec_values_json = '[]'
            WHERE common_spec_values_json IS NULL OR common_spec_values_json = ''
            """
        )
        for column in (
            "cover_safe_distance_json",
            "spine_safe_distance_json",
            "back_cover_safe_distance_json",
        ):
            connection.execute(
                f"UPDATE products SET {column} = '{{}}' "
                f"WHERE {column} IS NULL OR {column} = ''"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_shops (
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (product_id, shop_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(product_id, name)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_shops_shop_id ON product_shops(shop_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_names_product_id ON product_names(product_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_names_name ON product_names(name)"
        )

        self_columns = {
            row["name"] for row in table_columns(connection, "size_templates")
        }
        if "product_id" not in self_columns:
            connection.execute(
                "ALTER TABLE size_templates ADD COLUMN product_id INTEGER REFERENCES products(id) ON DELETE SET NULL"
            )
        layout_columns = {
            row["name"]
            for row in table_columns(connection, "font_layout_library")
        }
        if "product_id" not in layout_columns:
            connection.execute(
                "ALTER TABLE font_layout_library ADD COLUMN product_id INTEGER REFERENCES products(id) ON DELETE SET NULL"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_size_templates_product_id ON size_templates(product_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_font_layout_library_product_id ON font_layout_library(product_id)"
        )
    @staticmethod
    def _remove_size_template_font_layout_schema(connection):
        connection.execute("DROP TABLE IF EXISTS size_template_font_layout_sizes")
        connection.execute("DROP TABLE IF EXISTS size_template_font_layouts")
        columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        if "font_layouts_json" in columns:
            connection.execute(
                "ALTER TABLE size_templates DROP COLUMN font_layouts_json"
            )

    @staticmethod
    def _migrate_product_relations(connection):
        """Backfill normalized products from legacy JSON columns exactly once."""
        now = utc_now()

        def product_id(name: str):
            name = compact_string(name)
            if not name:
                return None
            row = connection.execute(
                "SELECT id FROM products WHERE name = ?", (name,)
            ).fetchone()
            if row:
                return row["id"]
            cursor = connection.execute(
                "INSERT INTO products (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
            return cursor.lastrowid

        def link_name(pid: int, name: str):
            name = compact_string(name)
            if not name:
                return
            connection.execute(
                "INSERT IGNORE INTO product_names (product_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (pid, name, now, now),
            )

        # Shop product whitelists become product records and product-shop links.
        for shop in connection.execute("SELECT id, product_names_json FROM shops").fetchall():
            names = normalize_string_list(json_load(shop["product_names_json"], []))
            for name in names:
                pid = product_id(name)
                link_name(pid, name)
                connection.execute(
                    "INSERT IGNORE INTO product_shops (product_id, shop_id, created_at) VALUES (?, ?, ?)",
                    (pid, shop["id"], now),
                )

        # Existing templates carry the most useful category/name signal.
        columns = {row["name"] for row in table_columns(connection, "size_templates")}
        product_column = (
            "product_names_json" if "product_names_json" in columns
            else "product_names" if "product_names" in columns
            else "'[]'"
        )
        name_column = "template_name" if "template_name" in columns else None
        for row in connection.execute(
            "SELECT id, shop_id, product_id, " + product_column + " AS product_names_json, "
            + ("template_name" if name_column else "'' AS template_name")
            + " FROM size_templates"
        ).fetchall():
            pid = row["product_id"]
            names = normalize_string_list(json_load(row["product_names_json"], []))
            category = compact_string(row["template_name"]) or (names[0] if names else "")
            if pid is None and category:
                pid = product_id(category)
                connection.execute(
                    "UPDATE size_templates SET product_id = ? WHERE id = ?",
                    (pid, row["id"]),
                )
            if pid is None:
                continue
            connection.execute(
                "INSERT IGNORE INTO product_shops (product_id, shop_id, created_at) VALUES (?, ?, ?)",
                (pid, row["shop_id"], now),
            )
            for name in names:
                link_name(pid, name)

        # A library layout can be shared by all products of its legacy shop.
        for row in connection.execute(
            "SELECT fl.id, fl.shop_id, fl.product_id FROM font_layout_library fl"
        ).fetchall():
            if row["product_id"] is not None:
                continue
            candidate = connection.execute(
                "SELECT product_id FROM product_shops WHERE shop_id = ? ORDER BY product_id LIMIT 1",
                (row["shop_id"],),
            ).fetchone()
            if candidate:
                connection.execute(
                    "UPDATE font_layout_library SET product_id = ? WHERE id = ?",
                    (candidate["product_id"], row["id"]),
                )

    def _migrate_size_template_product_storage(self, connection):
        columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        legacy_products_exist = table_exists(connection, "size_template_products")
        for row in connection.execute(
            "SELECT id, product_names_json FROM size_templates"
        ).fetchall():
            stored_product_names = json_load(row["product_names_json"], [])
            product_names = normalize_string_list(stored_product_names)
            if not product_names and "product_name" in columns:
                legacy_name = connection.execute(
                    "SELECT product_name FROM size_templates WHERE id = ?",
                    (row["id"],),
                ).fetchone()["product_name"]
                legacy_name = compact_string(legacy_name)
                if legacy_name:
                    product_names.append(legacy_name)
            if not product_names and legacy_products_exist is not None:
                product_names.extend(
                    item["product_name"]
                    for item in connection.execute(
                        """
                        SELECT product_name FROM size_template_products
                        WHERE size_template_id = ? ORDER BY id
                        """,
                        (row["id"],),
                    ).fetchall()
                )
            connection.execute(
                "UPDATE size_templates SET product_names_json = ? WHERE id = ?",
                (json_dump(unique_preserve_order(product_names)), row["id"]),
            )
        if legacy_products_exist:
            connection.execute("DROP TABLE size_template_products")
        for column in ("name", "product_name"):
            if column in columns:
                connection.execute(f"ALTER TABLE size_templates DROP COLUMN {column}")

    @staticmethod
    def _create_size_template_option_schema(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS size_template_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                size_template_id INTEGER NOT NULL REFERENCES size_templates(id) ON DELETE CASCADE,
                size_option_id TEXT NOT NULL,
                label TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                size_unit TEXT NOT NULL,
                single_side_width REAL NOT NULL,
                single_side_height REAL NOT NULL,
                bleed REAL NOT NULL DEFAULT 0,
                spine_width REAL NOT NULL DEFAULT 0,
                spine_bleed REAL NOT NULL DEFAULT 0,
                spine_width_mode TEXT NOT NULL DEFAULT 'fixed',
                spine_width_formula_json TEXT NOT NULL DEFAULT '{}',
                layers_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(size_template_id, size_option_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_size_template_options_template_id "
            "ON size_template_options(size_template_id)"
        )
        CatalogRepository._ensure_columns(
            connection,
            "size_template_options",
            {"layers_json": "TEXT NOT NULL DEFAULT '{}'"},
        )
    @classmethod
    def _migrate_size_template_fields(cls, connection):
        from backend.templates.size_variants import normalize_size_spec

        columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        if "fields_json" not in columns:
            return

        rows = connection.execute(
            "SELECT id, fields_json, created_at, updated_at FROM size_templates"
        ).fetchall()
        required = (
            "size_unit",
            "single_side_width",
            "single_side_height",
            "bleed",
            "spine_width",
            "spine_bleed",
        )
        for row in rows:
            fields = json_load(row["fields_json"], {})
            fields = fields if isinstance(fields, dict) else {}
            raw_spec = fields.get("size_spec")
            if not isinstance(raw_spec, dict):
                raw_spec = {}
                if all(fields.get(key) not in (None, "") for key in required):
                    raw_spec = {
                        "selected": "default",
                        "options": [
                            {
                                "id": "default",
                                "label": (
                                    f"{fields['single_side_width']}*"
                                    f"{fields['single_side_height']}"
                                ),
                                **{key: fields.get(key) for key in required},
                            }
                        ],
                    }
            spec = normalize_size_spec(raw_spec)
            existing_count = connection.execute(
                "SELECT COUNT(*) AS count FROM size_template_options "
                "WHERE size_template_id = ?",
                (row["id"],),
            ).fetchone()["count"]
            if not existing_count:
                cls._insert_size_template_options(
                    connection,
                    int(row["id"]),
                    spec["options"],
                    compact_string(row.get("created_at")) or utc_now(),
                )
            page_count = fields.get("page_count")
            try:
                page_count = int(page_count) if page_count not in (None, "") else None
            except (TypeError, ValueError):
                page_count = None
            connection.execute(
                """
                UPDATE size_templates SET
                    selected_size_option_id = ?, display_unit = ?,
                    page_count = ?, page_count_options_json = ?
                WHERE id = ?
                """,
                (
                    spec.get("selected") or "",
                    spec.get("display_unit") or "in",
                    page_count,
                    json_dump(cls._normalize_page_count_options(
                        fields.get("page_count_arr")
                    )),
                    row["id"],
                ),
            )
        connection.execute("ALTER TABLE size_templates DROP COLUMN fields_json")

    @staticmethod
    def _migrate_shop_product_storage(connection, include_legacy_sources: bool):
        shop_products_exist = table_exists(connection, "shop_products")
        orders_exists = table_exists(connection, "orders")
        for shop_row in connection.execute(
            "SELECT id, product_names_json FROM shops"
        ).fetchall():
            product_names = normalize_string_list(
                json_load(shop_row["product_names_json"], [])
            )
            if shop_products_exist:
                product_names.extend(
                    row["product_name"]
                    for row in connection.execute(
                        """
                        SELECT product_name FROM shop_products
                        WHERE shop_id = ? ORDER BY id
                        """,
                        (shop_row["id"],),
                    ).fetchall()
                )
            if include_legacy_sources:
                product_names.extend(
                    row["product_name"]
                    for row in connection.execute(
                        """
                        SELECT product.value AS product_name
                        FROM size_templates st,
                             JSON_TABLE(
                                 st.product_names_json,
                                 '$[*]' COLUMNS(value VARCHAR(2048) PATH '$')
                             ) AS product
                        WHERE st.shop_id = ?
                          AND TRIM(product.value) != ''
                        """,
                        (shop_row["id"],),
                    ).fetchall()
                )
            if include_legacy_sources and orders_exists:
                product_names.extend(
                    row["product"]
                    for row in connection.execute(
                        """
                        SELECT product FROM orders
                        WHERE shop_id = ? AND TRIM(COALESCE(product, '')) != ''
                        """,
                        (shop_row["id"],),
                    ).fetchall()
                )
            product_names = unique_preserve_order(product_names)
            connection.execute(
                """
                UPDATE shops SET product_names_json = ?, product_count = ?
                WHERE id = ?
                """,
                (json_dump(product_names), len(product_names), shop_row["id"]),
            )
        if shop_products_exist:
            connection.execute("DROP TABLE shop_products")

    @staticmethod
    def _ensure_columns(connection, table: str, columns: dict[str, str]):
        existing = {
            row["name"]
            for row in table_columns(connection, table)
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
        products = self._payload_shop_products(payload)
        now = utc_now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shops (
                    shop, shop_name, product_names_json, wecom_robot_webhook_url,
                    product_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop,
                    shop_name,
                    json_dump(products),
                    compact_string(
                        payload.get("wecom_robot_webhook_url")
                        or payload.get("wecom_robot")
                        or payload.get("企业微信机器人")
                    ),
                    len(products),
                    now,
                    now,
                ),
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
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            connection.commit()
        return {
            "items": [self._shop_row_to_dict(row) for row in rows],
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
        return self._shop_row_to_dict(row)

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
                WHERE shop IN ({placeholders})
                   OR shop_name IN ({placeholders})
                ORDER BY id
                LIMIT 1
                """,
                [*candidates, *candidates],
            ).fetchone()
        return self._shop_row_to_dict(row) if row is not None else None

    def find_size_template_product(self, product_name: str, *shop_names: str):
        product_name = compact_string(product_name)
        candidates = []
        for name in shop_names:
            normalized = compact_string(name)
            if normalized and normalized.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(normalized)
        if not product_name or not candidates:
            return None

        placeholders = ", ".join("?" for _ in candidates)
        with self._lock, self.connect() as connection:
            product_column = self._size_template_product_column(connection)
            row = connection.execute(
                f"""
                SELECT ? AS product_name, st.id AS size_template_id,
                       s.id AS shop_id, s.shop, s.shop_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                WHERE (s.shop IN ({placeholders})
                    OR s.shop_name IN ({placeholders}))
                  AND {json_array_contains_sql(connection, f'st.{product_column}')}
                ORDER BY st.id
                LIMIT 1
                """,
                [product_name, *candidates, *candidates, product_name],
            ).fetchone()
        return dict(row) if row is not None else None

    def find_shop_product(self, product_name: str, *shop_names: str):
        product_name = compact_string(product_name)
        candidates = []
        for name in shop_names:
            normalized = compact_string(name)
            if normalized and normalized.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(normalized)
        if not product_name or not candidates:
            return None

        placeholders = ", ".join("?" for _ in candidates)
        with self._lock, self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT ? AS product_name, s.id AS shop_id,
                       s.shop, s.shop_name
                FROM shops s
                WHERE (s.shop IN ({placeholders})
                    OR s.shop_name IN ({placeholders}))
                  AND {json_array_contains_sql(connection, 's.product_names_json')}
                ORDER BY s.id
                LIMIT 1
                """,
                [product_name, *candidates, *candidates, product_name],
            ).fetchone()
        return dict(row) if row is not None else None

    def ensure_shop_product(self, product_name: str, *shop_names: str):
        """Return a shop product, adding it to a matched shop when missing.

        Mail intake uses this method for its shop whitelist.  Template
        associations remain owned by ``size_templates`` and are not changed
        here.
        """
        product_name = compact_string(product_name)
        candidates = []
        for name in shop_names:
            normalized = compact_string(name)
            if normalized and normalized.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(normalized)
        if not product_name or not candidates:
            return None

        shop_placeholders = ", ".join("?" for _ in candidates)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT *
                FROM shops
                WHERE shop IN ({shop_placeholders})
                   OR shop_name IN ({shop_placeholders})
                ORDER BY id
                LIMIT 1
                """,
                [*candidates, *candidates],
            ).fetchone()
            if row is None:
                return None

            product_names = normalize_string_list(
                json_load(row["product_names_json"], [])
            )
            product_key = product_name.casefold()
            created = False
            if product_key not in {name.casefold() for name in product_names}:
                product_names.append(product_name)
                created = True
                connection.execute(
                    """
                    UPDATE shops
                    SET product_names_json = ?, product_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(product_names),
                        len(product_names),
                        utc_now(),
                        row["id"],
                    ),
                )

            return {
                "product_name": product_name,
                "shop_id": row["id"],
                "shop": row["shop"],
                "shop_name": row["shop_name"],
                "created": created,
            }

    def update_shop(self, shop_id: int, payload: dict[str, Any]):
        updates = {}
        has_products = any(
            key in payload for key in ("products", "product_names", "商品列表")
        )
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
        if any(
            key in payload
            for key in ("wecom_robot_webhook_url", "wecom_robot", "企业微信机器人")
        ):
            updates["wecom_robot_webhook_url"] = compact_string(
                payload.get("wecom_robot_webhook_url")
                if "wecom_robot_webhook_url" in payload
                else payload.get("wecom_robot")
                if "wecom_robot" in payload
                else payload.get("企业微信机器人")
            )
        if not updates and not has_products:
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
            if has_products:
                product_names = self._payload_shop_products(payload)
                connection.execute(
                    """
                    UPDATE shops
                    SET product_names_json = ?, product_count = ?
                    WHERE id = ?
                    """,
                    (json_dump(product_names), len(product_names), shop_id),
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

    # ------------------------------------------------------------------
    # Product-first catalog API
    # ------------------------------------------------------------------
    def create_text_generation_rule(self, payload: dict[str, Any]):
        name = compact_string(payload.get("name"))
        if not name:
            raise ValueError("文字生成规则名称不能为空")
        now = utc_now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO template_text_generation_rules
                    (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, compact_string(payload.get("description")), now, now),
            )
            rule_id = cursor.lastrowid
        return self.get_text_generation_rule(rule_id)

    def list_text_generation_rules(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
    ):
        where = ""
        params: list[Any] = []
        keyword = compact_string(search)
        if keyword:
            where = "WHERE name LIKE ? OR description LIKE ?"
            params.extend((f"%{keyword}%", f"%{keyword}%"))
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM template_text_generation_rules {where}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT id, name, description, created_at, updated_at
                FROM template_text_generation_rules
                {where}
                ORDER BY name ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_text_generation_rule(self, rule_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, name, description, created_at, updated_at "
                "FROM template_text_generation_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        if row is None:
            raise LookupError("文字生成规则不存在")
        return dict(row)

    def update_text_generation_rule(self, rule_id: int, payload: dict[str, Any]):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT id, name, description FROM template_text_generation_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("文字生成规则不存在")
            name = (
                compact_string(payload.get("name"))
                if "name" in payload
                else compact_string(existing["name"])
            )
            if not name:
                raise ValueError("文字生成规则名称不能为空")
            description = (
                compact_string(payload.get("description"))
                if "description" in payload
                else compact_string(existing["description"])
            )
            connection.execute(
                "UPDATE template_text_generation_rules "
                "SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                (name, description, utc_now(), rule_id),
            )
        return self.get_text_generation_rule(rule_id)

    def create_product(self, payload: dict[str, Any]):
        name = compact_string(payload.get("name") or payload.get("product") or payload.get("产品名"))
        if not name:
            raise ValueError("产品名称不能为空")
        names = normalize_string_list(
            payload.get("product_names")
            if "product_names" in payload
            else payload.get("names")
            if "names" in payload
            else [name]
        )
        if not names:
            raise ValueError("product_names 不能为空")
        specifications = unique_preserve_order(
            normalize_string_list(payload.get("specifications"))
        )
        specification_field = compact_string(
            payload.get("specification_field") or "Book Size | Page Count"
        )
        if not specification_field:
            raise ValueError("specification_field 不能为空")
        common_spec_values = normalize_common_spec_values(
            payload.get("common_spec_values")
        )
        safe_distances = self._product_safe_distance_values(payload)
        shop_ids = self._payload_int_list(payload.get("shop_ids") or payload.get("shops"))
        now = utc_now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO products (
                    name, description, specifications_json, specification_field,
                    common_spec_values_json,
                    cover_safe_distance_json, spine_safe_distance_json,
                    back_cover_safe_distance_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    compact_string(payload.get("description")),
                    json_dump(specifications),
                    specification_field,
                    json_dump(common_spec_values),
                    json_dump(safe_distances["cover_safe_distance"]),
                    json_dump(safe_distances["spine_safe_distance"]),
                    json_dump(safe_distances["back_cover_safe_distance"]),
                    int(bool(payload.get("enabled", True))),
                    now,
                    now,
                ),
            )
            product_id = cursor.lastrowid
            self._replace_product_relations(connection, product_id, names, shop_ids, now)
        return self.get_product(product_id)

    def list_products(self, limit: int = 50, offset: int = 0, search: str | None = None, shop_id: int | None = None):
        where: list[str] = []
        params: list[Any] = []
        if search:
            where.append("p.name LIKE ?")
            params.append(f"%{search}%")
        if shop_id is not None:
            where.append("EXISTS (SELECT 1 FROM product_shops psf WHERE psf.product_id = p.id AND psf.shop_id = ?)")
            params.append(shop_id)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS count FROM products p {where_sql}", params).fetchone()["count"]
            rows = connection.execute(
                f"SELECT p.* FROM products p {where_sql} ORDER BY p.name, p.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = [self._product_row_to_dict(connection, row) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_product(self, product_id: int):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if row is None:
                raise LookupError("产品不存在")
            return self._product_row_to_dict(connection, row)

    def update_product(self, product_id: int, payload: dict[str, Any]):
        with self._lock, self.connect() as connection:
            existing = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if existing is None:
                raise LookupError("产品不存在")
            name = compact_string(payload.get("name") or payload.get("product") or existing["name"])
            if not name:
                raise ValueError("产品名称不能为空")
            names = None
            if "product_names" in payload or "names" in payload:
                names = normalize_string_list(payload.get("product_names") if "product_names" in payload else payload.get("names"))
                if not names:
                    raise ValueError("product_names 不能为空")
            shop_ids = None
            if "shop_ids" in payload or "shops" in payload:
                shop_ids = self._payload_int_list(payload.get("shop_ids") if "shop_ids" in payload else payload.get("shops"))
            specifications = None
            if "specifications" in payload:
                specifications = unique_preserve_order(
                    normalize_string_list(payload.get("specifications"))
                )
            specification_field = (
                compact_string(payload.get("specification_field"))
                if "specification_field" in payload
                else existing["specification_field"]
            )
            if not specification_field:
                raise ValueError("specification_field 不能为空")
            common_spec_values = None
            if "common_spec_values" in payload:
                common_spec_values = normalize_common_spec_values(
                    payload.get("common_spec_values")
                )
            safe_distances = self._product_safe_distance_values(payload, existing)
            updates = {
                "name": name,
                "description": compact_string(payload.get("description")) if "description" in payload else existing["description"],
                "specifications_json": (
                    json_dump(specifications)
                    if specifications is not None
                    else existing["specifications_json"]
                ),
                "specification_field": specification_field,
                "common_spec_values_json": (
                    json_dump(common_spec_values)
                    if common_spec_values is not None
                    else existing.get("common_spec_values_json", "[]")
                ),
                "cover_safe_distance_json": json_dump(
                    safe_distances["cover_safe_distance"]
                ),
                "spine_safe_distance_json": json_dump(
                    safe_distances["spine_safe_distance"]
                ),
                "back_cover_safe_distance_json": json_dump(
                    safe_distances["back_cover_safe_distance"]
                ),
                "enabled": int(bool(payload.get("enabled"))) if "enabled" in payload else existing["enabled"],
                "updated_at": utc_now(),
            }
            connection.execute(
                """
                UPDATE products SET
                    name = :name,
                    description = :description,
                    specifications_json = :specifications_json,
                    specification_field = :specification_field,
                    common_spec_values_json = :common_spec_values_json,
                    cover_safe_distance_json = :cover_safe_distance_json,
                    spine_safe_distance_json = :spine_safe_distance_json,
                    back_cover_safe_distance_json = :back_cover_safe_distance_json,
                    enabled = :enabled,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                {**updates, "id": product_id},
            )
            if names is not None or shop_ids is not None:
                old_names = self._product_names(connection, product_id)
                old_shops = self._product_shop_ids(connection, product_id)
                self._replace_product_relations(connection, product_id, names or old_names, shop_ids if shop_ids is not None else old_shops, updates["updated_at"])
        return self.get_product(product_id)

    def delete_product(self, product_id: int):
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
            if row is None:
                raise LookupError("产品不存在")
            connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return {"deleted": True, "id": product_id}

    @staticmethod
    def _payload_int_list(value: Any):
        if value is None:
            return []
        values = value if isinstance(value, (list, tuple, set)) else [value]
        result = []
        for item in values:
            try:
                parsed = int(item.get("id") if isinstance(item, dict) else item)
            except (TypeError, ValueError):
                continue
            if parsed > 0 and parsed not in result:
                result.append(parsed)
        return result

    @staticmethod
    def _product_names(connection, product_id: int):
        return [row["name"] for row in connection.execute("SELECT name FROM product_names WHERE product_id = ? ORDER BY sort_order, id", (product_id,)).fetchall()]

    @staticmethod
    def _link_product_to_matching_size_templates(connection, product_id: int, names: list[str], shop_ids: list[int]):
        """Attach only unassigned templates matching both shop and product name."""
        if not names or not shop_ids:
            return
        columns = {row["name"] for row in table_columns(connection, "size_templates")}
        if "product_id" not in columns:
            return
        product_column = "product_names_json" if "product_names_json" in columns else "product_names"
        shop_placeholders = ", ".join("?" for _ in shop_ids)
        connection.execute(
            f"""
            UPDATE size_templates
               SET product_id = ?
             WHERE product_id IS NULL
               AND shop_id IN ({shop_placeholders})
               AND {json_array_contains_any_sql(connection, f'size_templates.{product_column}', len(names))}
            """,
            [product_id, *shop_ids, *names],
        )

    @classmethod
    def _link_existing_products_to_size_templates(cls, connection):
        for row in connection.execute("SELECT id FROM products ORDER BY id").fetchall():
            product_id = row["id"]
            cls._link_product_to_matching_size_templates(
                connection,
                product_id,
                cls._product_names(connection, product_id),
                cls._product_shop_ids(connection, product_id),
            )

    @staticmethod
    def _product_shop_ids(connection, product_id: int):
        return [row["shop_id"] for row in connection.execute("SELECT shop_id FROM product_shops WHERE product_id = ? ORDER BY shop_id", (product_id,)).fetchall()]

    @staticmethod
    def _replace_product_relations(connection, product_id: int, names: list[str], shop_ids: list[int], now: str):
        for shop_id in shop_ids:
            if connection.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone() is None:
                raise ValueError(f"店铺不存在: {shop_id}")
        connection.execute("DELETE FROM product_names WHERE product_id = ?", (product_id,))
        connection.execute("DELETE FROM product_shops WHERE product_id = ?", (product_id,))
        for order, value in enumerate(unique_preserve_order(names)):
            connection.execute("INSERT INTO product_names (product_id, name, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (product_id, value, order, now, now))
        for shop_id in shop_ids:
            connection.execute("INSERT INTO product_shops (product_id, shop_id, created_at) VALUES (?, ?, ?)", (product_id, shop_id, now))
            row = connection.execute("SELECT product_names_json FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if row is not None:
                existing = normalize_string_list(json_load(row["product_names_json"], []))
                merged = unique_preserve_order([*existing, *names])
                if merged != existing:
                    connection.execute(
                        "UPDATE shops SET product_names_json = ?, product_count = ?, updated_at = ? WHERE id = ?",
                        (json_dump(merged), len(merged), now, shop_id),
                    )
        CatalogRepository._link_product_to_matching_size_templates(
            connection, product_id, unique_preserve_order(names), shop_ids
        )

    @staticmethod
    def _product_row_to_dict(connection, row):
        data = dict(row)
        data["specifications"] = unique_preserve_order(
            normalize_string_list(
                json_load(data.pop("specifications_json", "[]"), [])
            )
        )
        data["specification_field"] = compact_string(
            data.get("specification_field") or "Book Size | Page Count"
        )
        data["common_spec_values"] = normalize_common_spec_values(
            json_load(data.pop("common_spec_values_json", "[]"), [])
        )
        for key in (
            "cover_safe_distance",
            "spine_safe_distance",
            "back_cover_safe_distance",
        ):
            column = f"{key}_json"
            data[key] = CatalogRepository._normalize_safe_distance(
                json_load(data.pop(column, "{}"), {}), key
            )
        data["product_names"] = CatalogRepository._product_names(connection, row["id"])
        data["shops"] = [dict(item) for item in connection.execute(
            "SELECT s.id, s.shop, s.shop_name FROM shops s JOIN product_shops ps ON ps.shop_id = s.id WHERE ps.product_id = ? ORDER BY s.id",
            (row["id"],),
        ).fetchall()]
        data["shop_ids"] = [item["id"] for item in data["shops"]]
        data["size_template_ids"] = [item["id"] for item in connection.execute("SELECT id FROM size_templates WHERE product_id = ? ORDER BY id", (row["id"],)).fetchall()]
        return data

    @classmethod
    def _product_safe_distance_values(
        cls,
        payload: dict[str, Any],
        fallback: Any | None = None,
    ) -> dict[str, dict[str, float]]:
        """Normalize product-level safe distances used by every renderer."""
        fallback = dict(fallback or {})
        result = {}
        for key in (
            "cover_safe_distance",
            "spine_safe_distance",
            "back_cover_safe_distance",
        ):
            if key in payload:
                value = payload.get(key)
            else:
                value = json_load(fallback.get(f"{key}_json", "{}"), {})
            result[key] = cls._normalize_safe_distance(value, key)
        return result

    @staticmethod
    def _migrate_product_safe_distances(connection) -> None:
        """Backfill new product settings from legacy size-template settings once."""
        products = connection.execute(
            "SELECT id, cover_safe_distance_json, spine_safe_distance_json, "
            "back_cover_safe_distance_json FROM products"
        ).fetchall()
        for product in products:
            template = connection.execute(
                """
                SELECT cover_safe_distance_json, spine_safe_distance_json,
                       back_cover_safe_distance_json
                FROM size_templates
                WHERE product_id = ?
                  AND (
                    cover_safe_distance_json <> '{}'
                    OR spine_safe_distance_json <> '{}'
                    OR back_cover_safe_distance_json <> '{}'
                  )
                ORDER BY id
                LIMIT 1
                """,
                (product["id"],),
            ).fetchone()
            if template is None:
                continue
            updates = {}
            for key in (
                "cover_safe_distance",
                "spine_safe_distance",
                "back_cover_safe_distance",
            ):
                column = f"{key}_json"
                current = str(product[column] or "{}").strip()
                if current in {"", "{}", "null"}:
                    value = str(template[column] or "{}").strip()
                    if value not in {"", "{}", "null"}:
                        updates[column] = value
            if updates:
                assignments = ", ".join(f"{key} = ?" for key in updates)
                connection.execute(
                    f"UPDATE products SET {assignments}, updated_at = ? WHERE id = ?",
                    [*updates.values(), utc_now(), product["id"]],
                )

    @staticmethod
    def _mail_shop_candidates(*shop_names: str):
        candidates = []
        for name in shop_names:
            normalized = compact_string(name)
            if normalized and normalized.casefold() not in {
                item.casefold() for item in candidates
            }:
                candidates.append(normalized)
        return candidates

    def find_product_name_for_shop(self, product_name: str, *shop_names: str):
        product_name = compact_string(product_name)
        candidates = self._mail_shop_candidates(*shop_names)
        if not product_name or not candidates:
            return None
        candidate_placeholders = ", ".join("?" for _ in candidates)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT p.*, s.id AS matched_shop_id,
                       s.shop AS matched_shop, s.shop_name AS matched_shop_name,
                       pn.name AS matched_product_name,
                       (
                           SELECT st.id FROM size_templates st
                           WHERE st.product_id = p.id AND st.shop_id = s.id
                           ORDER BY st.id LIMIT 1
                       ) AS size_template_id
                FROM products p
                JOIN product_shops ps ON ps.product_id = p.id
                JOIN shops s ON s.id = ps.shop_id
                JOIN product_names pn ON pn.product_id = p.id
                WHERE p.enabled = 1
                  AND (s.shop IN ({candidate_placeholders})
                       OR s.shop_name IN ({candidate_placeholders}))
                  AND LOWER(TRIM(pn.name)) = LOWER(?)
                ORDER BY p.id
                LIMIT 1
                """,
                [*candidates, *candidates, product_name],
            ).fetchone()
        return self._mail_product_row(row) if row is not None else None

    def list_mail_product_candidates(self, *shop_names: str):
        candidates = self._mail_shop_candidates(*shop_names)
        if not candidates:
            return []
        candidate_placeholders = ", ".join("?" for _ in candidates)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, s.id AS matched_shop_id,
                       s.shop AS matched_shop, s.shop_name AS matched_shop_name,
                       NULL AS matched_product_name,
                       (
                           SELECT st.id FROM size_templates st
                           WHERE st.product_id = p.id AND st.shop_id = s.id
                           ORDER BY st.id LIMIT 1
                       ) AS size_template_id
                FROM products p
                JOIN product_shops ps ON ps.product_id = p.id
                JOIN shops s ON s.id = ps.shop_id
                WHERE p.enabled = 1
                  AND (s.shop IN ({candidate_placeholders})
                       OR s.shop_name IN ({candidate_placeholders}))
                ORDER BY p.id
                """,
                [*candidates, *candidates],
            ).fetchall()
        return [self._mail_product_row(row) for row in rows]

    def associate_mail_product_name(
        self,
        product_id: int,
        shop_id: int,
        product_name: str,
    ):
        product_name = compact_string(product_name)
        if not product_name:
            raise ValueError("商品名不能为空")
        now = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT p.*, s.id AS matched_shop_id,
                       s.shop AS matched_shop, s.shop_name AS matched_shop_name,
                       pn.name AS matched_product_name,
                       (
                           SELECT st.id FROM size_templates st
                           WHERE st.product_id = p.id AND st.shop_id = s.id
                           ORDER BY st.id LIMIT 1
                       ) AS size_template_id
                FROM products p
                JOIN product_shops ps ON ps.product_id = p.id
                JOIN shops s ON s.id = ps.shop_id
                JOIN product_names pn ON pn.product_id = p.id
                WHERE s.id = ? AND LOWER(TRIM(pn.name)) = LOWER(?)
                ORDER BY p.id LIMIT 1
                """,
                (shop_id, product_name),
            ).fetchone()
            if existing is not None:
                return self._mail_product_row(existing)

            product = connection.execute(
                """
                SELECT p.*, s.id AS matched_shop_id,
                       s.shop AS matched_shop, s.shop_name AS matched_shop_name,
                       NULL AS matched_product_name,
                       (
                           SELECT st.id FROM size_templates st
                           WHERE st.product_id = p.id AND st.shop_id = s.id
                           ORDER BY st.id LIMIT 1
                       ) AS size_template_id
                FROM products p
                JOIN product_shops ps ON ps.product_id = p.id
                JOIN shops s ON s.id = ps.shop_id
                WHERE p.id = ? AND p.enabled = 1 AND s.id = ?
                LIMIT 1
                """,
                (product_id, shop_id),
            ).fetchone()
            if product is None:
                raise ValueError("产品不存在、已停用或未关联当前店铺")
            sort_order = connection.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order
                FROM product_names WHERE product_id = ?
                """,
                (product_id,),
            ).fetchone()["next_order"]
            connection.execute(
                """
                INSERT INTO product_names (
                    product_id, name, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (product_id, product_name, sort_order, now, now),
            )
            shop_row = connection.execute(
                "SELECT product_names_json FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
            legacy_names = normalize_string_list(
                json_load(shop_row["product_names_json"], [])
            )
            if product_name.casefold() not in {
                value.casefold() for value in legacy_names
            }:
                legacy_names.append(product_name)
                connection.execute(
                    """
                    UPDATE shops
                    SET product_names_json = ?, product_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(legacy_names), len(legacy_names), now, shop_id),
                )
            data = self._mail_product_row(product)
            data["matched_product_name"] = product_name
            return data

    @staticmethod
    def _mail_product_row(row):
        data = dict(row)
        data["product_id"] = data.pop("id")
        data["shop_id"] = data.pop("matched_shop_id")
        data["shop"] = data.pop("matched_shop")
        data["shop_name"] = data.pop("matched_shop_name")
        data["product_name"] = data.pop("matched_product_name", None)
        data["specifications"] = unique_preserve_order(
            normalize_string_list(
                json_load(data.pop("specifications_json", "[]"), [])
            )
        )
        data["specification_field"] = compact_string(
            data.get("specification_field") or "Book Size | Page Count"
        )
        data["common_spec_values"] = normalize_common_spec_values(
            json_load(data.pop("common_spec_values_json", "[]"), [])
        )
        for key in (
            "cover_safe_distance",
            "spine_safe_distance",
            "back_cover_safe_distance",
        ):
            data[key] = CatalogRepository._normalize_safe_distance(
                json_load(data.pop(f"{key}_json", "{}"), {}), key
            )
        return data

    def find_product_template_specification(
        self,
        product_id: int,
        shop_id: int,
        specification_value: str,
    ):
        """Find a product template option contained in an order field value."""
        source = " ".join(str(specification_value or "").split()).casefold()
        if not source:
            return None
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT st.id AS size_template_id,
                       sto.size_option_id,
                       sto.label
                FROM size_templates st
                JOIN size_template_options sto
                  ON sto.size_template_id = st.id
                WHERE st.product_id = ? AND st.shop_id = ?
                ORDER BY st.id, sto.sort_order, sto.id
                """,
                (product_id, shop_id),
            ).fetchall()
        for row in rows:
            aliases = (row["size_option_id"], row["label"])
            for alias in aliases:
                normalized = " ".join(str(alias or "").split()).casefold()
                if normalized and normalized in source:
                    return {
                        "size_template_id": row["size_template_id"],
                        "matched_specification": alias,
                    }
        return None

    def create_size_template(self, payload: dict[str, Any]):
        product_id = self._optional_int(payload.get("product_id") or payload.get("product"))
        shop_id_value = payload.get("shop_id")
        shop_id = self._optional_int(shop_id_value)
        product_names = self._payload_product_names(payload)
        page_count = self._normalize_page_count(payload.get("page_count"))
        page_count_options = self._normalize_page_count_options(
            payload.get("page_count_options", payload.get("page_count_arr"))
        )
        size_spec = self._size_template_spec_from_payload(payload)
        display_unit = self._normalize_size_spec(
            {"display_unit": payload.get("display_unit") or "in"}
        ).get("display_unit", "in")
        column_values = self._size_template_column_values(payload)
        size_template_info = self._normalize_size_template_info(
            payload.get("size_template_info")
            if "size_template_info" in payload
            else payload.get("template_info", [])
        )
        now = utc_now()
        with self._lock, self.connect() as connection:
            if product_id is not None:
                product_row = connection.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
                if product_row is None:
                    raise ValueError("产品不存在")
                if not product_names:
                    product_names = self._product_names(connection, product_id)
                if not product_names:
                    raise ValueError("产品至少需要一个 product_names 商品名")
                product_shop_ids = self._product_shop_ids(connection, product_id)
                if shop_id is None:
                    shop_id = product_shop_ids[0] if product_shop_ids else None
                if shop_id is None or shop_id not in product_shop_ids:
                    raise ValueError("产品未关联该店铺")
            if shop_id is None:
                raise ValueError("店铺id不能为空")
            if not product_names:
                raise ValueError("商品名不能为空")
            self._require_shop(connection, shop_id)
            if product_id is None:
                self._require_shop_products(connection, shop_id, product_names)
            size_columns = {
                row["name"]
                for row in table_columns(connection, "size_templates")
            }
            product_column = self._size_template_product_column(connection)
            insert_columns = [
                "shop_id",
                product_column,
                "selected_size_option_id",
                "display_unit",
                "page_count",
                "page_count_options_json",
            ]
            insert_values: list[Any] = [
                shop_id,
                json_dump(product_names),
                size_spec.get("selected") or "",
                display_unit,
                page_count,
                json_dump(page_count_options),
            ]
            if product_id is not None and "product_id" in size_columns:
                insert_columns.insert(1, "product_id")
                insert_values.insert(1, product_id)
            template_name = compact_string(
                payload.get("template_name") or payload.get("name")
            )
            if "template_name" in size_columns:
                insert_columns.append("template_name")
                insert_values.append(template_name)
            if "name" in size_columns:
                insert_columns.append("name")
                insert_values.append(template_name)
            if "size_template_info_json" in size_columns:
                insert_columns.append("size_template_info_json")
                insert_values.append(json_dump(size_template_info))
            if "preview_image_path" in size_columns:
                insert_columns.append("preview_image_path")
                insert_values.append(
                    normalize_preview_url(
                        payload.get("preview_image_path") or payload.get("preview_image")
                    )
                )
            for column in (
                "background_color",
                "paper_thickness_mm",
                "min_spine_width",
                "max_spine_width",
                "spine_width_basis",
                "max_spine_bleed",
                "min_spine_bleed",
            ):
                if column in size_columns:
                    insert_columns.append(column)
                    insert_values.append(column_values.get(column, 0))
            for column, value_key in (
                ("cover_safe_distance_json", "cover_safe_distance"),
                ("spine_safe_distance_json", "spine_safe_distance"),
                ("back_cover_safe_distance_json", "back_cover_safe_distance"),
            ):
                if column in size_columns:
                    insert_columns.append(column)
                    insert_values.append(json_dump(column_values[value_key]))
            for timestamp_column in ("created_at", "updated_at"):
                if timestamp_column in size_columns:
                    insert_columns.append(timestamp_column)
                    insert_values.append(now)
            cursor = connection.execute(
                f"""
                INSERT INTO size_templates ({', '.join(insert_columns)})
                VALUES ({placeholders(insert_values)})
                """,
                insert_values,
            )
            template_id = cursor.lastrowid
            if size_spec["options"]:
                self._insert_size_template_options(
                    connection,
                    int(template_id),
                    size_spec["options"],
                    now,
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
        product_id: int | None = None,
    ):
        where = []
        params: list[Any] = []
        with self.connect() as connection:
            size_columns = {
                row["name"]
                for row in table_columns(connection, "size_templates")
            }
            product_column = (
                "product_names_json"
                if "product_names_json" in size_columns
                else "product_names"
            )
            if shop_id is not None:
                where.append("st.shop_id = ?")
                params.append(shop_id)
            if product_id is not None:
                where.append("st.product_id = ?")
                params.append(product_id)
            if product_name:
                if getattr(connection, "mysql", False):
                    where.append(
                        f"JSON_SEARCH(st.{product_column}, 'one', CONCAT('%', ?, '%')) IS NOT NULL"
                    )
                else:
                    where.append(
                        f"""
                        EXISTS (
                            SELECT 1 FROM JSON_TABLE(
                                st.{product_column},
                                '$[*]' COLUMNS(value VARCHAR(2048) PATH '$')
                            ) AS product
                            WHERE product.value LIKE ?
                        )
                        """
                    )
                params.append(f"%{product_name}%")
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
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
                SELECT st.*, s.shop, s.shop_name, p.name AS product_category_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                LEFT JOIN products p ON p.id = st.product_id
                {where_sql}
                ORDER BY CAST(st.updated_at AS DATETIME) DESC, st.id DESC
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
                SELECT st.*, s.shop, s.shop_name, p.name AS product_category_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                LEFT JOIN products p ON p.id = st.product_id
                WHERE st.id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise LookupError("尺寸模板不存在")
        return self._size_template_row_to_dict(row)

    def get_size_template_detail(self, template_id: int):
        """Return size-editing data without product or font-layout payloads."""
        with self.connect() as connection:
            columns = [
                row["name"]
                for row in table_columns(connection, "size_templates")
                if row["name"]
                not in {"product_names", "product_names_json"}
            ]
            select_columns = ", ".join(f"st.{column}" for column in columns)
            row = connection.execute(
                f"""
                SELECT {select_columns}, s.shop, s.shop_name
                FROM size_templates st
                JOIN shops s ON s.id = st.shop_id
                WHERE st.id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise LookupError("尺寸模板不存在")
        return self._size_template_row_to_dict(row, include_related=False)

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
            size_columns = {
                row["name"]
                for row in table_columns(connection, "size_templates")
            }
            product_column = (
                "product_names_json"
                if "product_names_json" in size_columns
                else "product_names"
            )
            shop_row = connection.execute(
                "SELECT id FROM shops WHERE shop = ?",
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
                f"""
                SELECT st.id
                FROM size_templates st
                WHERE st.shop_id = ?
                  AND {json_array_contains_sql(connection, f'st.{product_column}')}
                ORDER BY st.id
                LIMIT 1
                """,
                (shop_id, product_name),
            ).fetchone()
            if existing is not None:
                template_id = existing["id"]
                incoming_spec = (fields or {}).get("size_spec")
                if isinstance(incoming_spec, dict) and incoming_spec.get("options"):
                    existing_row = connection.execute(
                        "SELECT * FROM size_templates WHERE id = ?",
                        (template_id,),
                    ).fetchone()
                    existing_spec = self._size_template_spec_from_row(
                        connection,
                        existing_row,
                    )
                    merged_options = self._merge_size_option_summaries(
                        existing_spec.get("options") or [],
                        list(incoming_spec["options"]),
                    )
                    merged_spec = self._normalize_size_spec({
                        **incoming_spec,
                        **existing_spec,
                        "selected": existing_spec.get("selected")
                        or incoming_spec.get("selected"),
                        "options": merged_options,
                    })
                    connection.execute(
                        "UPDATE size_templates SET selected_size_option_id = ?, "
                        "display_unit = ?, updated_at = ? WHERE id = ?",
                        (
                            merged_spec.get("selected") or "",
                            merged_spec.get("display_unit") or "in",
                            now,
                            template_id,
                        ),
                    )
                    self._replace_size_template_options(
                        connection,
                        int(template_id),
                        merged_spec["options"],
                        now,
                    )
            else:
                size_spec = self._normalize_size_spec(
                    (fields or {}).get("size_spec")
                )
                insert_columns = [
                    "shop_id",
                    product_column,
                    "selected_size_option_id",
                    "display_unit",
                    "page_count",
                    "page_count_options_json",
                ]
                insert_values: list[Any] = [
                    shop_id,
                    json_dump([product_name]),
                    size_spec.get("selected") or "",
                    size_spec.get("display_unit") or "in",
                    self._normalize_page_count((fields or {}).get("page_count")),
                    json_dump(self._normalize_page_count_options(
                        (fields or {}).get("page_count_arr")
                    )),
                ]
                if "template_name" in size_columns:
                    insert_columns.append("template_name")
                    insert_values.append(compact_string(name) or product_name)
                if "name" in size_columns:
                    insert_columns.append("name")
                    insert_values.append(compact_string(name) or product_name)
                for timestamp_column in ("created_at", "updated_at"):
                    if timestamp_column in size_columns:
                        insert_columns.append(timestamp_column)
                        insert_values.append(now)
                cursor = connection.execute(
                    f"""
                    INSERT INTO size_templates ({', '.join(insert_columns)})
                    VALUES ({placeholders(insert_values)})
                    """,
                    insert_values,
                )
                template_id = cursor.lastrowid
                self._insert_size_template_options(
                    connection,
                    int(template_id),
                    size_spec["options"],
                    now,
                )
                shop_products = json_load(
                    connection.execute(
                        "SELECT product_names_json FROM shops WHERE id = ?",
                        (shop_id,),
                    ).fetchone()["product_names_json"],
                    [],
                )
                if product_name.casefold() not in {
                    item.casefold() for item in shop_products
                }:
                    shop_products.append(product_name)
                    connection.execute(
                        """
                        UPDATE shops
                        SET product_names_json = ?, product_count = ?
                        WHERE id = ?
                        """,
                        (json_dump(shop_products), len(shop_products), shop_id),
                    )
            self._refresh_shop_counts(connection, shop_id)
        return self.get_size_template(template_id)

    def find_render_template(
        self,
        product_name: str,
        shop_id: int | None = None,
        shop: str | None = None,
        template_id: int | None = None,
        product_id: int | None = None,
    ):
        if template_id is not None:
            try:
                template = self.get_size_template(template_id)
            except LookupError:
                return None
            # An order's template id is only an association hint.  Keep the
            # product/shop gate in place so a stale id cannot bypass the
            # product -> shop -> template matching contract.
            if shop_id is not None and int(template.get("shop_id") or -1) != int(shop_id):
                return None
            if shop:
                candidates = {
                    str(value).strip().casefold()
                    for value in (template.get("shop"), template.get("shop_name"))
                    if str(value or "").strip()
                }
                requested = {
                    str(value).strip().casefold()
                    for value in self._shop_name_candidates(shop)
                    if str(value or "").strip()
                }
                if candidates and not candidates.intersection(requested):
                    return None
            if product_name:
                template_product_id = self._optional_int(template.get("product_id"))
                normalized_product_name = compact_string(product_name)
                if template_product_id is not None:
                    with self.connect() as connection:
                        if table_exists(connection, "product_names"):
                            matched = connection.execute(
                                """
                                SELECT 1 FROM product_names
                                WHERE product_id = ?
                                  AND LOWER(TRIM(name)) = LOWER(TRIM(?))
                                LIMIT 1
                                """,
                                (template_product_id, normalized_product_name),
                            ).fetchone()
                            if matched is None:
                                return None
                        else:
                            names = {
                                compact_string(value).casefold()
                                for value in template.get("product_names") or []
                                if compact_string(value)
                            }
                            if names and normalized_product_name.casefold() not in names:
                                return None
                else:
                    names = {
                        compact_string(value).casefold()
                        for value in template.get("product_names") or []
                        if compact_string(value)
                    }
                    if names and normalized_product_name.casefold() not in names:
                        return None
            return self._flatten_render_template(template)

        product_name = compact_string(product_name)
        if not product_name:
            return None
        candidate_shop_ids = []
        with self.connect() as connection:
            if product_id is not None:
                if table_exists(connection, "product_names"):
                    matched_product = connection.execute(
                        """
                        SELECT 1 FROM product_names
                        WHERE product_id = ?
                          AND LOWER(TRIM(name)) = LOWER(TRIM(?))
                        LIMIT 1
                        """,
                        (product_id, product_name),
                    ).fetchone()
                    if matched_product is None:
                        return None
                sql = "SELECT id FROM size_templates WHERE product_id = ?"
                parameters = [product_id]
                if shop_id is not None:
                    sql += " AND shop_id = ?"
                    parameters.append(shop_id)
                sql += " ORDER BY id LIMIT 1"
                row = connection.execute(sql, parameters).fetchone()
                if row is not None:
                    return self._flatten_render_template(self.get_size_template(row["id"]))
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
                    WHERE shop IN ({sql_placeholders})
                       OR shop_name IN ({sql_placeholders})
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
                WHERE st.shop_id IN ({id_placeholders})
                  AND {json_array_contains_sql(connection, f"st.{self._size_template_product_column(connection)}")}
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
            product_id = self._optional_int(payload.get("product_id") or payload.get("product")) if ("product_id" in payload or "product" in payload) else existing["product_id"]
            shop_id = self._optional_int(payload.get("shop_id")) or existing["shop_id"]
            self._require_shop(connection, shop_id)
            product_column = self._size_template_product_column(connection)
            product_names = (
                self._payload_product_names(payload)
                if self._payload_has_product_names(payload)
                else json_load(existing[product_column], [])
            )
            if not product_names:
                if product_id is not None:
                    product_names = self._product_names(connection, product_id)
                if not product_names:
                    raise ValueError("商品名不能为空")
            if product_id is not None:
                product_shops = self._product_shop_ids(connection, product_id)
                if not product_shops or shop_id not in product_shops:
                    raise ValueError("产品未关联该店铺")
            else:
                self._require_shop_products(connection, shop_id, product_names)
            size_spec = self._size_template_spec_from_row(connection, existing)
            replace_options = "size_options" in payload
            if replace_options:
                from backend.templates.size_variants import normalize_size_options

                size_spec["options"] = normalize_size_options(
                    payload.get("size_options") or []
                )
            if "selected_size_option_id" in payload:
                size_spec["selected"] = payload.get("selected_size_option_id")
            elif replace_options and size_spec.get("selected") not in {
                str(option["id"]) for option in size_spec["options"]
            }:
                size_spec["selected"] = None
            if "display_unit" in payload:
                size_spec["display_unit"] = payload.get("display_unit")
            size_spec = self._normalize_size_spec(size_spec)
            column_values = self._size_template_column_values(payload, dict(existing))
            now = utc_now()
            existing_columns = set(existing.keys())
            updates = {
                "shop_id": shop_id,
                "product_id": product_id,
                product_column: json_dump(product_names),
                "selected_size_option_id": size_spec.get("selected") or "",
                "display_unit": size_spec.get("display_unit") or "in",
                "updated_at": now,
                "background_color": column_values["background_color"],
                "paper_thickness_mm": column_values["paper_thickness_mm"],
                "min_spine_width": column_values["min_spine_width"],
                "max_spine_width": column_values["max_spine_width"],
                "spine_width_basis": column_values["spine_width_basis"],
                "max_spine_bleed": column_values["max_spine_bleed"],
                "min_spine_bleed": column_values["min_spine_bleed"],
                "cover_safe_distance_json": json_dump(
                    column_values["cover_safe_distance"]
                ),
                "spine_safe_distance_json": json_dump(
                    column_values["spine_safe_distance"]
                ),
                "back_cover_safe_distance_json": json_dump(
                    column_values["back_cover_safe_distance"]
                ),
            }
            if "page_count" in payload:
                updates["page_count"] = self._normalize_page_count(
                    payload.get("page_count")
                )
            if "page_count_options" in payload or "page_count_arr" in payload:
                updates["page_count_options_json"] = json_dump(
                    self._normalize_page_count_options(
                        payload.get(
                            "page_count_options",
                            payload.get("page_count_arr"),
                        )
                    )
                )
            if "preview_image_path" in existing_columns and (
                "preview_image_path" in payload or "preview_image" in payload
            ):
                updates["preview_image_path"] = normalize_preview_url(
                    payload.get("preview_image_path") or payload.get("preview_image")
                )
            if "template_name" in existing_columns and "template_name" in payload:
                updates["template_name"] = compact_string(payload["template_name"])
            elif "template_name" in existing_columns and "name" in payload:
                updates["template_name"] = compact_string(payload["name"])
            if "name" in existing_columns and (
                "name" in payload or "template_name" in payload
            ):
                updates["name"] = compact_string(
                    payload.get("name") or payload.get("template_name")
                )
            if "size_template_info_json" in existing_columns and (
                "size_template_info" in payload or "template_info" in payload
            ):
                raw_info = (
                    payload.get("size_template_info")
                    if "size_template_info" in payload
                    else payload.get("template_info")
                )
                updates["size_template_info_json"] = json_dump(
                    self._normalize_size_template_info(raw_info)
                )
            set_sql = ", ".join(f"{column} = :{column}" for column in updates)
            connection.execute(
                f"UPDATE size_templates SET {set_sql} WHERE id = :id",
                {**updates, "id": template_id},
            )
            if replace_options:
                self._upsert_size_template_options_preserving_variants(
                    connection,
                    template_id,
                    size_spec["options"],
                    now,
                )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_size_template(template_id)

    def create_size_template_option(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        from backend.templates.size_variants import normalize_size_options

        option_payload = dict(payload or {})
        options_to_add = normalize_size_options([option_payload])
        if not options_to_add:
            raise ValueError("自定义尺寸方案不能为空")
        new_option = options_to_add[0]
        select_option = new_option.get("select", False)

        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise LookupError("尺寸模板不存在")
            spec = self._size_template_spec_from_row(connection, row)
            options = spec["options"]
            if any(str(item.get("id")) == str(new_option.get("id")) for item in options):
                raise ValueError(f"尺寸方案 id 已存在: {new_option.get('id')}")
            now = utc_now()
            self._insert_size_template_option(
                connection,
                template_id,
                new_option,
                len(options),
                now,
            )
            if select_option or not spec.get("selected"):
                selected = new_option["id"]
            else:
                selected = spec.get("selected") or ""
            connection.execute(
                "UPDATE size_templates SET selected_size_option_id = ?, "
                "updated_at = ? WHERE id = ?",
                (selected, now, template_id),
            )
            connection.commit()
        return self.get_size_template(template_id)

    def get_size_template_form(
        self,
        template_id: int,
        size_option: str | None = None,
        size_unit: str | None = None,
    ):
        from backend.templates.size_variants import build_size_form

        template = self.get_size_template(template_id)
        return build_size_form(
            template.get("fields") or {},
            size_option=size_option,
            size_unit=size_unit,
        )

    def update_size_template_selection(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        from backend.templates.size_variants import build_size_form

        size_option = compact_string(payload.get("size_option"))
        size_unit = compact_string(payload.get("size_unit")).lower()
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise LookupError("尺寸模板不存在")
            spec = self._size_template_spec_from_row(connection, row)
            spec["selected"] = size_option
            spec["display_unit"] = size_unit
            spec = self._normalize_size_spec(spec)
            fields = self._size_template_fields_from_row(row, spec)
            size_form = build_size_form(
                fields,
                size_option=size_option,
                size_unit=size_unit,
            )
            if size_form["status"] != "ready":
                raise ValueError(f"尺寸方案尚未填写完整: {size_option}")
            connection.execute(
                "UPDATE size_templates SET selected_size_option_id = ?, "
                "display_unit = ?, updated_at = ? WHERE id = ?",
                (size_option, size_unit, utc_now(), template_id),
            )
            connection.commit()
        return size_form

    def update_size_template_option(
        self,
        template_id: int,
        option_id: str,
        payload: dict[str, Any],
    ):
        from backend.templates.size_variants import merge_size_option

        option_id = compact_string(option_id)
        option_updates = dict(payload or {})

        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise LookupError("尺寸模板不存在")
            spec = self._size_template_spec_from_row(connection, row)
            options = spec["options"]
            match_index = next(
                (
                    index
                    for index, item in enumerate(options)
                    if str(item.get("id")) == option_id
                ),
                None,
            )
            if match_index is None:
                raise LookupError(f"尺寸方案不存在: {option_id}")

            options[match_index] = merge_size_option(
                options[match_index],
                {**option_updates, "id": option_id},
            )
            now = utc_now()
            self._update_size_template_option_row(
                connection,
                template_id,
                option_id,
                options[match_index],
                now,
            )
            selected = (
                option_id
                if options[match_index].get("select")
                else spec.get("selected") or ""
            )
            connection.execute(
                "UPDATE size_templates SET selected_size_option_id = ?, "
                "updated_at = ? WHERE id = ?",
                (selected, now, template_id),
            )
            connection.commit()
        return self.get_size_template(template_id)

    def delete_size_template_option(self, template_id: int, option_id: str):
        option_id = compact_string(option_id)
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM size_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise LookupError("尺寸模板不存在")
            spec = self._size_template_spec_from_row(connection, row)
            options = spec["options"]
            remaining = [
                item for item in options if str(item.get("id")) != option_id
            ]
            if len(remaining) == len(options):
                raise LookupError(f"尺寸方案不存在: {option_id}")
            selected = spec.get("selected")
            if str(selected) == option_id:
                selected = remaining[0]["id"] if remaining else ""
            now = utc_now()
            connection.execute(
                "DELETE FROM size_template_options WHERE size_template_id = ? "
                "AND size_option_id = ?",
                (template_id, option_id),
            )
            connection.execute(
                "UPDATE size_templates SET selected_size_option_id = ?, "
                "updated_at = ? WHERE id = ?",
                (selected or "", now, template_id),
            )
            connection.commit()
        return {
            "deleted": True,
            "option_id": option_id,
            "size_template": self.get_size_template(template_id),
        }

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

    def create_font_layout_library_template(self, payload: dict[str, Any]):
        product_id = self._optional_int(payload.get("product_id") or payload.get("product"))
        shop_id = self._optional_int(payload.get("shop_id"))
        name = compact_string(payload.get("name"))
        if not name:
            raise ValueError("字体布局模板名称不能为空")
        sort_key = compact_string(payload.get("sort_key"))
        layers = self._normalize_layers(payload.get("layers", {}))
        preview_image_path = normalize_preview_url(
            payload.get("preview_image_path") or payload.get("preview_image")
        )
        now = utc_now()
        with self._lock, self.connect() as connection:
            if product_id is not None:
                if connection.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone() is None:
                    raise ValueError("产品不存在")
                product_shops = self._product_shop_ids(connection, product_id)
                if shop_id is None:
                    shop_id = product_shops[0] if product_shops else None
                if shop_id is None or shop_id not in product_shops:
                    raise ValueError("产品未关联该店铺")
            if shop_id is None:
                raise ValueError("店铺id不能为空")
            self._require_shop(connection, shop_id)
            enriched_fonts = self._enrich_layers_font_urls(
                layers,
                connection.execute("SELECT * FROM fonts WHERE enabled = 1").fetchall(),
            )
            if enriched_fonts:
                print(
                    f"[模板保存] 已从字体库补全基础图层字体链接："
                    f"模板={name}，字体={','.join(sorted(enriched_fonts))}",
                    flush=True,
                )
            cursor = connection.execute(
                """
                INSERT INTO font_layout_library (
                    shop_id, product_id, name, sort_key, preview_image_path, layers_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    product_id,
                    name,
                    sort_key,
                    preview_image_path,
                    json_dump(layers),
                    now,
                    now,
                ),
            )
            template_id = cursor.lastrowid
            self._refresh_shop_counts(connection, shop_id)
        return self.get_font_layout_library_template(template_id)

    def _inner_page_template_row_to_dict(self, row) -> dict[str, Any]:
        data = dict(row)
        data.pop("layers_json", None)
        data["preview_image"] = compact_string(
            data.pop("preview_image_path", "")
        ) or None
        data["shop"] = data.get("shop", "")
        data["shop_name"] = data.get("shop_name", "")
        data["product"] = None
        if data.get("product_id") is not None:
            try:
                data["product"] = self.get_product(int(data["product_id"]))
            except LookupError:
                data["product"] = None
        with self.connect() as connection:
            data["size_options"] = self._inner_page_template_options(
                connection, int(data["id"])
            )
        return data

    @staticmethod
    def _inner_page_template_option_row_to_dict(row):
        item = dict(row)
        item["id"] = str(item.pop("size_option_id"))
        item["label"] = compact_string(item["label"])
        item["size_unit"] = compact_string(item["size_unit"])
        item["layers"] = CatalogRepository._normalize_layers(
            json_load(item.pop("layers_json"), {})
        )
        return item

    @classmethod
    def _inner_page_template_options(cls, connection, template_id: int):
        rows = connection.execute(
            """
            SELECT id, size_option_id, label, size_unit, sort_order, layers_json,
                   created_at, updated_at
            FROM inner_page_template_options
            WHERE inner_page_template_id = ?
            ORDER BY sort_order, id
            """,
            (template_id,),
        ).fetchall()
        return [cls._inner_page_template_option_row_to_dict(row) for row in rows]

    @staticmethod
    def _inner_page_template_option_row(connection, template_id: int, option_id: str):
        return connection.execute(
            """
            SELECT id, size_option_id, label, size_unit, sort_order, layers_json,
                   created_at, updated_at
            FROM inner_page_template_options
            WHERE inner_page_template_id = ? AND size_option_id = ?
            """,
            (template_id, option_id),
        ).fetchone()

    @staticmethod
    def _normalize_inner_page_options(raw_options: Any):
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError("size_options 至少需要一个内页规格")
        result = []
        seen = set()
        for index, raw in enumerate(raw_options):
            if not isinstance(raw, dict):
                raise ValueError(f"size_options[{index}] 必须是对象")
            option_id = compact_string(raw.get("id") or raw.get("size_option_id"))
            label = compact_string(raw.get("label") or option_id)
            size_unit = compact_string(raw.get("size_unit"))
            if not option_id:
                raise ValueError(f"size_options[{index}].id 不能为空")
            if option_id in seen:
                raise ValueError(f"内页规格不能重复: {option_id}")
            if size_unit not in {"in", "mm", "cm"}:
                raise ValueError(
                    f"size_options[{index}].size_unit 必须是 in、mm 或 cm"
                )
            if not isinstance(raw.get("layers"), dict):
                raise ValueError(f"size_options[{index}].layers 必须是对象")
            seen.add(option_id)
            result.append({
                "id": option_id,
                "label": label,
                "size_unit": size_unit,
                "layers": CatalogRepository._normalize_layers(raw["layers"]),
            })
        return result

    def create_inner_page_template(self, payload: dict[str, Any]):
        shop_id = self._optional_int(payload.get("shop_id"))
        product_id = self._optional_int(payload.get("product_id"))
        name = compact_string(payload.get("name"))
        if shop_id is None:
            raise ValueError("店铺id不能为空")
        if product_id is None:
            raise ValueError("product_id 不能为空")
        if not name:
            raise ValueError("内页模板名称不能为空")
        options = self._normalize_inner_page_options(payload.get("size_options"))
        preview = normalize_preview_url(
            payload.get("preview_image_path") or payload.get("preview_image")
        )
        now = utc_now()
        with self._lock, self.connect() as connection:
            self._require_shop(connection, shop_id)
            if product_id is not None:
                if connection.execute(
                    "SELECT id FROM products WHERE id = ?", (product_id,)
                ).fetchone() is None:
                    raise ValueError("产品不存在")
                if shop_id not in self._product_shop_ids(connection, product_id):
                    raise ValueError("产品未关联该店铺")
            cursor = connection.execute(
                """
                INSERT INTO inner_page_templates (
                    shop_id, product_id, name, description,
                    preview_image_path, layers_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    product_id,
                    name,
                    compact_string(payload.get("description")),
                    preview,
                    json_dump({}),
                    now,
                    now,
                ),
            )
            template_id = cursor.lastrowid
            for sort_order, option in enumerate(options):
                connection.execute(
                    """
                    INSERT INTO inner_page_template_options (
                        inner_page_template_id, size_option_id, label, size_unit,
                        sort_order, layers_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template_id,
                        option["id"],
                        option["label"],
                        option["size_unit"],
                        sort_order,
                        json_dump(option["layers"]),
                        now,
                        now,
                    ),
                )
        return self.get_inner_page_template(template_id)

    def list_inner_page_templates(
        self,
        limit: int = 50,
        offset: int = 0,
        shop_id: int | None = None,
        product_id: int | None = None,
        search: str | None = None,
    ):
        where = []
        params: list[Any] = []
        if shop_id is not None:
            where.append("ipt.shop_id = ?")
            params.append(shop_id)
        if product_id is not None:
            where.append("ipt.product_id = ?")
            params.append(product_id)
        if compact_string(search):
            keyword = f"%{compact_string(search)}%"
            where.append("(ipt.name LIKE ? OR ipt.description LIKE ?)")
            params.extend((keyword, keyword))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM inner_page_templates ipt {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT ipt.*, s.shop, s.shop_name, p.name AS product_name
                FROM inner_page_templates ipt
                JOIN shops s ON s.id = ipt.shop_id
                LEFT JOIN products p ON p.id = ipt.product_id
                {where_sql}
                ORDER BY ipt.updated_at DESC, ipt.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._inner_page_template_row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_inner_page_template(self, template_id: int):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT ipt.*, s.shop, s.shop_name, p.name AS product_name
                FROM inner_page_templates ipt
                JOIN shops s ON s.id = ipt.shop_id
                LEFT JOIN products p ON p.id = ipt.product_id
                WHERE ipt.id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise LookupError("内页模板不存在")
        return self._inner_page_template_row_to_dict(row)

    def create_inner_page_template_option(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        option = self._normalize_inner_page_options([payload])[0]
        now = utc_now()
        with self._lock, self.connect() as connection:
            if connection.execute(
                "SELECT id FROM inner_page_templates WHERE id = ?",
                (template_id,),
            ).fetchone() is None:
                raise LookupError("内页模板不存在")
            if self._inner_page_template_option_row(
                connection, template_id, option["id"]
            ) is not None:
                raise ValueError(f"内页规格已存在: {option['id']}")
            sort_row = connection.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM inner_page_template_options
                WHERE inner_page_template_id = ?
                """,
                (template_id,),
            ).fetchone()
            sort_order = int(sort_row["next_sort_order"])
            connection.execute(
                """
                INSERT INTO inner_page_template_options (
                    inner_page_template_id, size_option_id, label, size_unit,
                    sort_order, layers_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    option["id"],
                    option["label"],
                    option["size_unit"],
                    sort_order,
                    json_dump(option["layers"]),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE inner_page_templates SET updated_at = ? WHERE id = ?",
                (now, template_id),
            )
            row = self._inner_page_template_option_row(
                connection, template_id, option["id"]
            )
        return self._inner_page_template_option_row_to_dict(row)

    def update_inner_page_template_option(
        self,
        template_id: int,
        option_id: str,
        payload: dict[str, Any],
    ):
        option_id = compact_string(option_id)
        updates = dict(payload or {})
        if not updates:
            raise ValueError("至少传入 label、size_unit 或 layers 中的一个字段")
        for key, value in updates.items():
            if value is None:
                raise ValueError(f"{key} 不能为空")
        with self._lock, self.connect() as connection:
            if connection.execute(
                "SELECT id FROM inner_page_templates WHERE id = ?",
                (template_id,),
            ).fetchone() is None:
                raise LookupError("内页模板不存在")
            existing_row = self._inner_page_template_option_row(
                connection, template_id, option_id
            )
            if existing_row is None:
                raise LookupError(f"内页规格不存在: {option_id}")
            existing = self._inner_page_template_option_row_to_dict(existing_row)
            option = self._normalize_inner_page_options(
                [
                    {
                        "id": option_id,
                        "label": updates.get("label", existing["label"]),
                        "size_unit": updates.get(
                            "size_unit", existing["size_unit"]
                        ),
                        "layers": updates.get("layers", existing["layers"]),
                    }
                ]
            )[0]
            now = utc_now()
            connection.execute(
                """
                UPDATE inner_page_template_options
                SET label = ?, size_unit = ?, layers_json = ?, updated_at = ?
                WHERE inner_page_template_id = ? AND size_option_id = ?
                """,
                (
                    option["label"],
                    option["size_unit"],
                    json_dump(option["layers"]),
                    now,
                    template_id,
                    option_id,
                ),
            )
            connection.execute(
                "UPDATE inner_page_templates SET updated_at = ? WHERE id = ?",
                (now, template_id),
            )
            row = self._inner_page_template_option_row(
                connection, template_id, option_id
            )
        return self._inner_page_template_option_row_to_dict(row)

    def delete_inner_page_template_option(
        self,
        template_id: int,
        option_id: str,
    ):
        option_id = compact_string(option_id)
        with self._lock, self.connect() as connection:
            if connection.execute(
                "SELECT id FROM inner_page_templates WHERE id = ?",
                (template_id,),
            ).fetchone() is None:
                raise LookupError("内页模板不存在")
            if self._inner_page_template_option_row(
                connection, template_id, option_id
            ) is None:
                raise LookupError(f"内页规格不存在: {option_id}")
            connection.execute(
                """
                DELETE FROM inner_page_template_options
                WHERE inner_page_template_id = ? AND size_option_id = ?
                """,
                (template_id, option_id),
            )
            connection.execute(
                "UPDATE inner_page_templates SET updated_at = ? WHERE id = ?",
                (utc_now(), template_id),
            )
        return {
            "deleted": True,
            "template_id": template_id,
            "size_option_id": option_id,
        }

    def update_inner_page_template(self, template_id: int, payload: dict[str, Any]):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM inner_page_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if existing is None:
                raise LookupError("内页模板不存在")
            shop_id = self._optional_int(payload.get("shop_id")) or existing["shop_id"]
            product_id = (
                self._optional_int(payload.get("product_id"))
                if "product_id" in payload
                else existing["product_id"]
            )
            if product_id is None:
                raise ValueError("product_id 不能为空")
            self._require_shop(connection, shop_id)
            if product_id is not None:
                if connection.execute(
                    "SELECT id FROM products WHERE id = ?", (product_id,)
                ).fetchone() is None:
                    raise ValueError("产品不存在")
                if shop_id not in self._product_shop_ids(connection, product_id):
                    raise ValueError("产品未关联该店铺")
            updates = {"shop_id": shop_id, "product_id": product_id}
            for key in ("name", "description"):
                if key in payload:
                    value = compact_string(payload[key])
                    if key == "name" and not value:
                        raise ValueError("内页模板名称不能为空")
                    updates[key] = value
            if "preview_image" in payload or "preview_image_path" in payload:
                updates["preview_image_path"] = normalize_preview_url(
                    payload.get("preview_image_path") or payload.get("preview_image")
                )
            options = None
            if "size_options" in payload:
                options = self._normalize_inner_page_options(payload.get("size_options"))
            updates["updated_at"] = utc_now()
            set_sql = ", ".join(f"{key} = :{key}" for key in updates)
            connection.execute(
                f"UPDATE inner_page_templates SET {set_sql} WHERE id = :id",
                {**updates, "id": template_id},
            )
            if options is not None:
                connection.execute(
                    "DELETE FROM inner_page_template_options WHERE inner_page_template_id = ?",
                    (template_id,),
                )
                for sort_order, option in enumerate(options):
                    connection.execute(
                        """
                        INSERT INTO inner_page_template_options (
                            inner_page_template_id, size_option_id, label, size_unit,
                            sort_order, layers_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            template_id,
                            option["id"],
                            option["label"],
                            option["size_unit"],
                            sort_order,
                            json_dump(option["layers"]),
                            updates["updated_at"],
                            updates["updated_at"],
                        ),
                    )
        return self.get_inner_page_template(template_id)

    def delete_inner_page_template(self, template_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM inner_page_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if existing is None:
                raise LookupError("内页模板不存在")
            connection.execute(
                "DELETE FROM inner_page_templates WHERE id = ?", (template_id,)
            )
        return {"deleted": True, "id": template_id}

    def list_font_layout_library_templates(
        self,
        limit: int = 50,
        offset: int = 0,
        shop_id: int | None = None,
        search: str | None = None,
        product_id: int | None = None,
    ):
        where = []
        params: list[Any] = []
        if shop_id is not None:
            where.append("flt.shop_id = ?")
            params.append(shop_id)
        if product_id is not None:
            where.append("flt.product_id = ?")
            params.append(product_id)
        if compact_string(search):
            keyword = f"%{compact_string(search)}%"
            where.append("(flt.name LIKE ? OR flt.sort_key LIKE ?)")
            params.extend((keyword, keyword))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM font_layout_library flt {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT flt.*, s.shop, s.shop_name, p.name AS product_category_name
                FROM font_layout_library flt
                JOIN shops s ON s.id = flt.shop_id
                LEFT JOIN products p ON p.id = flt.product_id
                {where_sql}
                ORDER BY
                    CASE WHEN trim(flt.sort_key) = '' THEN 1 ELSE 0 END,
                    flt.sort_key ASC,
                    CAST(flt.updated_at AS DATETIME) DESC,
                    flt.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._font_layout_library_row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _font_layout_size_context(connection, layout_row, size_template_id: int):
        size_template = connection.execute(
            "SELECT id, shop_id, product_id FROM size_templates WHERE id = ?",
            (size_template_id,),
        ).fetchone()
        if size_template is None:
            raise LookupError("尺寸模板不存在")
        layout_product_id = layout_row.get("product_id")
        if layout_product_id is None:
            raise ValueError("字体布局模板尚未关联产品，不能同步尺寸规格")
        if int(layout_row["shop_id"]) != int(size_template["shop_id"]):
            raise ValueError("字体布局模板与尺寸模板不属于同一店铺")
        if (
            size_template.get("product_id") is None
            or int(layout_product_id) != int(size_template["product_id"])
        ):
            raise ValueError("字体布局模板与尺寸模板不属于同一产品")
        return connection.execute(
            "SELECT id, size_option_id, label, layers_json FROM size_template_options "
            "WHERE size_template_id = ? ORDER BY sort_order, id",
            (size_template_id,),
        ).fetchall()

    @staticmethod
    def _set_selected_font_layout(connection, size_template_id: int, layout_id: int):
        connection.execute(
            "UPDATE size_templates SET selected_font_layout_id = ?, updated_at = ? "
            "WHERE id = ?",
            (layout_id, utc_now(), size_template_id),
        )

    @staticmethod
    def _selected_font_layout_id(connection, size_template_id: int):
        row = connection.execute(
            "SELECT selected_font_layout_id FROM size_templates WHERE id = ?",
            (size_template_id,),
        ).fetchone()
        return CatalogRepository._optional_int(
            row["selected_font_layout_id"] if row else None
        )

    @staticmethod
    def _font_layout_size_status_payload(
        template_id: int,
        size_template_id: int,
        options,
        variants,
    ):
        variant_option_ids = {
            int(row["size_template_option_id"])
            for row in variants
        }
        size_options = []
        synced_ids = []
        missing_ids = []
        for option in options:
            option_id = compact_string(option["size_option_id"])
            direct_layers = json_load(option.get("layers_json"), {})
            has_direct_layers = isinstance(direct_layers, dict) and bool(direct_layers)
            has_variant = int(option["id"]) in variant_option_ids or has_direct_layers
            if has_variant:
                synced_ids.append(option_id)
            else:
                missing_ids.append(option_id)
            size_options.append(
                {
                    "size_option_id": option_id,
                    "label": compact_string(option["label"]),
                    "has_size_variant": has_variant,
                    "layers_source": (
                        "size_template_option"
                        if has_direct_layers
                        else "size_variant" if has_variant else "base"
                    ),
                    "using_base_layers": not has_variant,
                    "message": (
                        "当前规格已有独立图层数据"
                        if has_variant
                        else "当前规格暂无独立图层数据，将使用基础字体布局模板"
                    ),
                }
            )
        return {
            "font_layout_id": template_id,
            "size_template_id": size_template_id,
            "size_options": size_options,
            "synced_size_option_ids": synced_ids,
            "missing_size_option_ids": missing_ids,
            "all_size_options_synced": bool(size_options) and not missing_ids,
            "message": (
                "尺寸模板暂无规格，请先添加规格"
                if not size_options
                else (
                    "全部规格都已有独立字体布局数据"
                    if not missing_ids
                    else f"还有 {len(missing_ids)} 个规格将使用基础字体布局模板"
                )
            ),
        }

    def get_font_layout_library_template(
        self,
        template_id: int,
        size_template_id: int | None = None,
        size_option_id: str | None = None,
    ):
        requested_option_id = compact_string(size_option_id)
        if requested_option_id and size_template_id is None:
            raise ValueError("查询规格字体布局时必须同时传 size_template_id")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT flt.*, s.shop, s.shop_name, p.name AS product_category_name
                FROM font_layout_library flt
                JOIN shops s ON s.id = flt.shop_id
                LEFT JOIN products p ON p.id = flt.product_id
                WHERE flt.id = ?
                """,
                (template_id,),
            ).fetchone()
            if row is None:
                # A library template may be physically deleted after its
                # per-size JSON snapshots were applied. Keep those snapshots
                # readable from the size-template context.
                if size_template_id is None:
                    raise LookupError("字体布局模板不存在")
                selected_row = connection.execute(
                    "SELECT selected_font_layout_id FROM size_templates "
                    "WHERE id = ?",
                    (size_template_id,),
                ).fetchone()
                if (
                    selected_row is None
                    or self._optional_int(selected_row.get("selected_font_layout_id"))
                    != int(template_id)
                ):
                    raise LookupError("字体布局模板不存在")
                snapshot = connection.execute(
                    """
                    SELECT st.shop_id, st.product_id
                    FROM size_templates st
                    JOIN font_layout_size_variants flsv
                      ON flsv.size_template_id = st.id
                    WHERE st.id = ? AND flsv.font_layout_id = ?
                    LIMIT 1
                    """,
                    (size_template_id, template_id),
                ).fetchone()
                if snapshot is None:
                    raise LookupError("字体布局模板不存在")
                row = {
                    "id": int(template_id),
                    "shop_id": snapshot["shop_id"],
                    "product_id": snapshot.get("product_id"),
                    "name": "已删除字体布局",
                    "sort_key": "",
                    "preview_image_path": "",
                    "layers_json": "{}",
                    "created_at": "",
                    "updated_at": "",
                }
            result = self._font_layout_library_row_to_dict(row)
            if size_template_id is None:
                return result

            size_template_id = int(size_template_id)
            selected_layout_id = self._selected_font_layout_id(
                connection, size_template_id
            )
            options = self._font_layout_size_context(
                connection,
                row,
                size_template_id,
            )
            variants = connection.execute(
                "SELECT flsv.* FROM font_layout_size_variants flsv "
                "JOIN size_template_options sto "
                "ON sto.id = flsv.size_template_option_id "
                "WHERE flsv.font_layout_id = ? AND sto.size_template_id = ?",
                (template_id, size_template_id),
            ).fetchall()
            status = self._font_layout_size_status_payload(
                template_id,
                size_template_id,
                options,
                variants,
            )
            result.update(
                {
                    "size_template_id": size_template_id,
                    "size_layout_status": status["size_options"],
                    "synced_size_option_ids": status["synced_size_option_ids"],
                    "missing_size_option_ids": status["missing_size_option_ids"],
                    "all_size_options_synced": status["all_size_options_synced"],
                    "is_current_size_template_layout": selected_layout_id == template_id,
                }
            )
            if not requested_option_id:
                if selected_layout_id not in (None, template_id):
                    result["message"] = (
                        "当前尺寸模板正在使用其他字体布局；本模板规格副本仍可独立编辑"
                    )
                else:
                    result["message"] = status["message"]
                return result

            option = next(
                (
                    item
                    for item in options
                    if compact_string(item["size_option_id"])
                    == requested_option_id
                ),
                None,
            )
            if option is None:
                raise LookupError(f"尺寸方案不存在: {requested_option_id}")
            variant = next(
                (
                    item
                    for item in variants
                    if int(item["size_template_option_id"]) == int(option["id"])
                ),
                None,
            )
            result["size_option_id"] = requested_option_id
            direct_layers = self._normalize_layers(json_load(option.get("layers_json"), {}))
            has_direct_layers = bool(direct_layers)
            if selected_layout_id not in (None, template_id) and not has_direct_layers:
                result["layers_source"] = "base"
                result["using_base_layers"] = True
                result["has_size_variant"] = False
                result["size_variant_id"] = None
                result["message"] = (
                    "当前尺寸模板正在使用其他字体布局，已使用本模板基础图层"
                )
                return result
            result["has_size_variant"] = variant is not None or has_direct_layers
            result["using_base_layers"] = not result["has_size_variant"]
            if has_direct_layers:
                result["layers"] = direct_layers
                objects = direct_layers.get("objects")
                result["layer_count"] = len(objects) if isinstance(objects, list) else 0
                result["layers_source"] = "size_template_option"
                result["size_variant_id"] = int(variant["id"]) if variant is not None else None
                result["message"] = "已加载当前规格独立图层数据"
            elif variant is not None:
                result["layers"] = self._normalize_layers(
                    json_load(variant["layers_json"], {})
                )
                objects = result["layers"].get("objects")
                result["layer_count"] = len(objects) if isinstance(objects, list) else 0
                result["layers_source"] = "size_variant"
                result["size_variant_id"] = int(variant["id"])
                result["message"] = "已加载当前规格的字体布局数据"
            else:
                result["layers_source"] = "base"
                result["size_variant_id"] = None
                result["message"] = (
                    "当前规格暂无独立图层数据，已使用基础字体布局模板"
                )
            return result

    def get_font_layout_size_option_status(
        self,
        template_id: int,
        size_template_id: int,
    ):
        with self.connect() as connection:
            layout = connection.execute(
                "SELECT * FROM font_layout_library WHERE id = ?",
                (template_id,),
            ).fetchone()
            selected_layout_id = self._selected_font_layout_id(
                connection, size_template_id
            )
            if layout is None:
                if selected_layout_id != template_id:
                    raise LookupError("字体布局模板不存在")
                snapshot = connection.execute(
                    """
                    SELECT st.shop_id, st.product_id
                    FROM size_templates st
                    JOIN font_layout_size_variants flsv
                      ON flsv.size_template_id = st.id
                    WHERE st.id = ? AND flsv.font_layout_id = ?
                    LIMIT 1
                    """,
                    (size_template_id, template_id),
                ).fetchone()
                if snapshot is None:
                    raise LookupError("字体布局模板不存在")
                layout = {
                    "id": int(template_id),
                    "shop_id": snapshot["shop_id"],
                    "product_id": snapshot.get("product_id"),
                    "name": "已删除字体布局",
                    "sort_key": "",
                    "preview_image_path": "",
                    "layers_json": "{}",
                    "created_at": "",
                    "updated_at": "",
                }
            options = self._font_layout_size_context(
                connection,
                layout,
                size_template_id,
            )
            variants = connection.execute(
                "SELECT flsv.id, flsv.size_template_option_id "
                "FROM font_layout_size_variants flsv "
                "JOIN size_template_options sto "
                "ON sto.id = flsv.size_template_option_id "
                "WHERE flsv.font_layout_id = ? AND sto.size_template_id = ?",
                (template_id, size_template_id),
            ).fetchall()
        result = self._font_layout_size_status_payload(
            template_id,
            size_template_id,
            options,
            variants,
        )
        result["is_current_size_template_layout"] = selected_layout_id == template_id
        if selected_layout_id not in (None, template_id):
            result["message"] = (
                "当前尺寸模板正在使用其他字体布局；切换到本模板后将使用已同步规格"
            )
        return result

    def sync_font_layout_size_options(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        size_template_id = self._optional_int(payload.get("size_template_id"))
        if size_template_id is None:
            raise ValueError("size_template_id 不能为空")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("items 至少需要一个规格图层")

        normalized_items = []
        seen_option_ids = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] 必须是对象")
            option_id = compact_string(item.get("size_option_id"))
            if not option_id:
                raise ValueError(f"items[{index}].size_option_id 不能为空")
            if option_id in seen_option_ids:
                raise ValueError(f"规格不能重复同步: {option_id}")
            if not isinstance(item.get("layers"), dict):
                raise ValueError(f"items[{index}].layers 必须是完整图层文档对象")
            seen_option_ids.add(option_id)
            normalized_items.append(
                {
                    "size_option_id": option_id,
                    "layers": self._normalize_layers(item.get("layers")),
                }
            )

        now = utc_now()
        with self._lock, self.connect() as connection:
            layout = connection.execute(
                "SELECT * FROM font_layout_library WHERE id = ?",
                (template_id,),
            ).fetchone()
            if layout is None:
                raise LookupError("字体布局模板不存在")
            options = self._font_layout_size_context(
                connection,
                layout,
                size_template_id,
            )
            option_by_id = {
                compact_string(option["size_option_id"]): option
                for option in options
            }
            missing = [
                item["size_option_id"]
                for item in normalized_items
                if item["size_option_id"] not in option_by_id
            ]
            if missing:
                raise LookupError(f"尺寸方案不存在: {', '.join(missing)}")
            font_records = connection.execute(
                "SELECT * FROM fonts WHERE enabled = 1"
            ).fetchall()
            for item in normalized_items:
                enriched_fonts = self._enrich_layers_font_urls(
                    item["layers"],
                    font_records,
                )
                if enriched_fonts:
                    print(
                        f"[模板保存] 已从字体库补全规格图层字体链接："
                        f"字体布局ID={template_id}，规格={item['size_option_id']}，"
                        f"字体={','.join(sorted(enriched_fonts))}",
                        flush=True,
                    )
            self._set_selected_font_layout(
                connection,
                size_template_id,
                template_id,
            )
            for item in normalized_items:
                option = option_by_id[item["size_option_id"]]
                # Synchronization is a copy operation: keep the canonical
                # editable layer document on the size option itself. The
                # variant row remains populated for existing clients and
                # historical layout-status queries.
                connection.execute(
                    """
                    UPDATE size_template_options
                    SET layers_json = ?, updated_at = ?
                    WHERE id = ? AND size_template_id = ?
                    """,
                    (
                        json_dump(item["layers"]),
                        now,
                        option["id"],
                        size_template_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO font_layout_size_variants (
                        font_layout_id, size_template_id, size_template_option_id, layers_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        size_template_id = VALUES(size_template_id),
                        layers_json = VALUES(layers_json),
                        updated_at = VALUES(updated_at)
                    """,
                    (
                        template_id,
                        size_template_id,
                        option["id"],
                        json_dump(item["layers"]),
                        now,
                        now,
                    ),
                )
        result = self.get_font_layout_size_option_status(
            template_id,
            size_template_id,
        )
        result["just_synced_size_option_ids"] = [
            item["size_option_id"] for item in normalized_items
        ]
        result["synced_count"] = len(normalized_items)
        synced_labels = ", ".join(
            item["size_option_id"] for item in normalized_items
        )
        result["message"] = (
            f"已同步 {len(normalized_items)} 个规格（{synced_labels}）"
            + "，当前尺寸模板已切换到该字体布局"
        )
        return result

    def delete_font_layout_size_option(
        self,
        template_id: int,
        size_template_id: int,
        size_option_id: str,
    ):
        size_option_id = compact_string(size_option_id)
        with self._lock, self.connect() as connection:
            layout = connection.execute(
                "SELECT * FROM font_layout_library WHERE id = ?",
                (template_id,),
            ).fetchone()
            if layout is None:
                raise LookupError("字体布局模板不存在")
            options = self._font_layout_size_context(
                connection,
                layout,
                size_template_id,
            )
            option = next(
                (
                    item
                    for item in options
                    if compact_string(item["size_option_id"]) == size_option_id
                ),
                None,
            )
            if option is None:
                raise LookupError(f"尺寸方案不存在: {size_option_id}")
            existing = connection.execute(
                "SELECT id FROM font_layout_size_variants "
                "WHERE font_layout_id = ? AND size_template_option_id = ?",
                (template_id, option["id"]),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "DELETE FROM font_layout_size_variants "
                    "WHERE font_layout_id = ? AND size_template_option_id = ?",
                    (template_id, option["id"]),
                )
        result = self.get_font_layout_library_template(
            template_id,
            size_template_id,
            size_option_id,
        )
        result["deleted"] = existing is not None
        result["message"] = (
            "字体布局历史快照已删除；规格自己的独立图层仍保留"
            if existing is not None
            else "当前规格没有该字体布局历史快照，规格独立图层保持不变"
        )
        return result

    def update_font_layout_library_template(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM font_layout_library WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体布局模板不存在")
            product_id = self._optional_int(payload.get("product_id") or payload.get("product")) if ("product_id" in payload or "product" in payload) else existing["product_id"]
            shop_id = self._optional_int(payload.get("shop_id")) or existing["shop_id"]
            if product_id is not None:
                product_shops = self._product_shop_ids(connection, product_id)
                if not product_shops or shop_id not in product_shops:
                    raise ValueError("产品未关联该店铺")
            self._require_shop(connection, shop_id)
            variant_contexts = connection.execute(
                """
                SELECT DISTINCT st.shop_id, st.product_id
                FROM font_layout_size_variants flsv
                JOIN size_template_options sto
                    ON sto.id = flsv.size_template_option_id
                JOIN size_templates st
                    ON st.id = sto.size_template_id
                WHERE flsv.font_layout_id = ?
                """,
                (template_id,),
            ).fetchall()
            if variant_contexts and (
                product_id is None
                or any(
                    int(context["shop_id"]) != int(shop_id)
                    or context.get("product_id") is None
                    or int(context["product_id"]) != int(product_id)
                    for context in variant_contexts
                )
            ):
                raise ValueError(
                    "字体布局已有规格图层，不能改到不匹配的产品或店铺；"
                    "请先删除规格图层"
                )
            name = (
                compact_string(payload.get("name"))
                if "name" in payload
                else compact_string(existing["name"])
            )
            if not name:
                raise ValueError("字体布局模板名称不能为空")
            sort_key = (
                compact_string(payload.get("sort_key"))
                if "sort_key" in payload
                else compact_string(existing["sort_key"])
            )
            layers = (
                self._normalize_layers(payload.get("layers"))
                if "layers" in payload
                else json_load(existing["layers_json"], {})
            )
            enriched_fonts = self._enrich_layers_font_urls(
                layers,
                connection.execute("SELECT * FROM fonts WHERE enabled = 1").fetchall(),
            )
            if enriched_fonts:
                print(
                    f"[模板保存] 已从字体库补全基础图层字体链接："
                    f"字体布局ID={template_id}，字体={','.join(sorted(enriched_fonts))}",
                    flush=True,
                )
            preview_image_path = (
                normalize_preview_url(
                    payload.get("preview_image_path")
                    or payload.get("preview_image")
                )
                if "preview_image_path" in payload or "preview_image" in payload
                else compact_string(existing["preview_image_path"])
            )
            connection.execute(
                """
                UPDATE font_layout_library
                SET shop_id = ?, product_id = ?, name = ?, sort_key = ?, preview_image_path = ?,
                    layers_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    shop_id,
                    product_id,
                    name,
                    sort_key,
                    preview_image_path,
                    json_dump(layers),
                    utc_now(),
                    template_id,
                ),
            )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
            self._refresh_shop_counts(connection, shop_id)
        return self.get_font_layout_library_template(template_id)

    def save_font_layout_library_template_as(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        source = self.get_font_layout_library_template(template_id)
        return self.create_font_layout_library_template(
            {
                "shop_id": payload.get("shop_id") or source["shop_id"],
                "product_id": payload.get("product_id") or source.get("product_id"),
                "name": payload.get("name") or f"{source['name']} - 副本",
                "sort_key": payload.get("sort_key", source.get("sort_key", "")),
                "preview_image_path": source.get("preview_image_path", ""),
                "layers": deepcopy(source.get("layers") or {}),
            }
        )

    def delete_font_layout_library_template(self, template_id: int):
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT shop_id, preview_image_path "
                "FROM font_layout_library WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体布局模板不存在")
            connection.execute(
                "DELETE FROM font_layout_library WHERE id = ?",
                (template_id,),
            )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
        return {
            "deleted": True,
            "id": template_id,
            "preview_image_path": compact_string(existing["preview_image_path"]),
        }
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
            cursor = connection.execute(
                """
                INSERT INTO font_templates (
                    shop_id, size_template_id, name, description,
                    safe_distance, elements_json, options_json,
                    size_option_layouts_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop_id,
                    size_template_id,
                    values["name"],
                    values["description"],
                    values["safe_distance"],
                    json_dump(values["elements"]),
                    json_dump(values["options"]),
                    json_dump({}),
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
                SELECT ft.*
                FROM font_templates ft
                {where_sql}
                ORDER BY CAST(ft.updated_at AS DATETIME) DESC, ft.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._font_template_summary(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_font_template(
        self,
        template_id: int,
        size_option: str | None = None,
        include_size_option_layouts: bool = False,
    ):
        initial_size_option: str | None = None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT ft.*
                FROM font_templates ft
                WHERE ft.id = ?
                """,
                (template_id,),
            ).fetchone()
            requested_size_option = compact_string(size_option)
            if row is not None and requested_size_option:
                size_row = connection.execute(
                    "SELECT selected_size_option_id FROM size_templates WHERE id = ?",
                    (int(row["size_template_id"]),),
                ).fetchone()
                if size_row is not None:
                    initial_size_option = compact_string(
                        size_row["selected_size_option_id"]
                    ) or None
                option_ids = self._size_template_option_ids(
                    connection,
                    int(row["size_template_id"]),
                )
                if requested_size_option not in option_ids:
                    raise LookupError(
                        f"尺寸方案不存在: {requested_size_option}"
                    )
        if row is None:
            raise LookupError("字体模板不存在")
        result = self._font_template_row_to_dict(
            row,
            size_option,
            include_size_option_layouts,
        )
        if requested_size_option and result.get("sync_required"):
            result["requested_size_option"] = requested_size_option
            result["size_option"] = initial_size_option or requested_size_option
        return result

    def update_font_template(
        self,
        template_id: int,
        payload: dict[str, Any],
        size_option: str | None = None,
    ):
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
                "description": existing["description"],
                "safe_distance": existing["safe_distance"],
                "elements": json_load(existing["elements_json"], []),
                "options": json_load(existing["options_json"], {}),
            }
            values = self._font_template_values(payload, existing_payload)
            size_option_layouts = json_load(
                existing["size_option_layouts_json"],
                {},
            )
            if not isinstance(size_option_layouts, dict):
                size_option_layouts = {}
            requested_size_option = compact_string(size_option)
            if requested_size_option:
                option_ids = self._size_template_option_ids(
                    connection,
                    size_template_id,
                )
                if requested_size_option not in option_ids:
                    raise LookupError(
                        f"尺寸方案不存在: {requested_size_option}"
                    )
                current_layout = size_option_layouts.get(requested_size_option)
                if not isinstance(current_layout, dict):
                    raise LookupError(
                        f"字体布局尚未同步到尺寸方案: {requested_size_option}"
                    )
                if "elements" in payload:
                    current_layout = dict(current_layout)
                    current_layout["elements"] = deepcopy(values["elements"])
                    size_option_layouts[requested_size_option] = current_layout
                    values["elements"] = existing_payload["elements"]
            now = utc_now()
            connection.execute(
                """
                UPDATE font_templates SET
                    shop_id = ?,
                    size_template_id = ?,
                    name = ?,
                    description = ?,
                    safe_distance = ?,
                    elements_json = ?,
                    options_json = ?,
                    size_option_layouts_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    shop_id,
                    size_template_id,
                    values["name"],
                    values["description"],
                    values["safe_distance"],
                    json_dump(values["elements"]),
                    json_dump(values["options"]),
                    json_dump(size_option_layouts),
                    now,
                    template_id,
                ),
            )
            self._refresh_shop_counts(connection, int(existing["shop_id"]))
            self._refresh_shop_counts(connection, shop_id)
            connection.commit()
        return self.get_font_template(template_id, size_option)

    def sync_font_template_size_options(
        self,
        template_id: int,
        payload: dict[str, Any],
    ):
        source_option_id = compact_string(payload.get("size_option"))
        elements = payload.get("elements")
        canvas = payload.get("canvas")
        if not source_option_id:
            raise ValueError("size_option 不能为空")
        if not isinstance(elements, list):
            raise ValueError("elements 必须是数组")

        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM font_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("字体模板不存在")
            stored_options = json_load(existing["options_json"], {})
            source_canvas = self._resolve_font_layout_canvas(
                canvas,
                stored_options,
                elements,
            )
            options = self._size_template_options(
                connection,
                int(existing["size_template_id"]),
            )
            option_by_id = {
                str(option.get("id")): option
                for option in options
                if isinstance(option, dict) and option.get("id") not in (None, "")
            }
            source_option = option_by_id.get(source_option_id)
            if source_option is None:
                raise LookupError(f"尺寸方案不存在: {source_option_id}")
            source_size = self._font_layout_physical_size(source_option)
            layouts: dict[str, dict[str, Any]] = {}
            skipped_size_options: list[dict[str, str]] = []
            for option_id, option in option_by_id.items():
                try:
                    target_size = self._font_layout_physical_size(option)
                except ValueError as exc:
                    skipped_size_options.append(
                        {
                            "size_option": option_id,
                            "reason": str(exc),
                        }
                    )
                    continue
                scale_x = target_size["width"] / source_size["width"]
                scale_y = target_size["height"] / source_size["height"]
                layouts[option_id] = {
                    "elements": self._scale_font_layout_elements(
                        elements,
                        scale_x,
                        scale_y,
                    ),
                    "canvas": {
                        "width": round(source_canvas["width"] * scale_x, 4),
                        "height": round(source_canvas["height"] * scale_y, 4),
                    },
                }
            connection.execute(
                """
                UPDATE font_templates SET
                    elements_json = ?,
                    size_option_layouts_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dump(elements),
                    json_dump(layouts),
                    utc_now(),
                    template_id,
                ),
            )
            connection.commit()
        result = self.get_font_template(template_id, source_option_id)
        result["synced_size_option_ids"] = list(layouts)
        result["skipped_size_options"] = skipped_size_options
        return result

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
                    font_name, font_family, font_preferred, font_en, font_all_name,
                    post_script_name,
                    file_path, source, enabled,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["font_name"],
                    values["font_family"],
                    values["font_preferred"],
                    values["font_en"],
                    values["font_all_name"],
                    values["post_script_name"],
                    values["file_path"],
                    values["source"],
                    values["enabled"],
                    json_dump(values["metadata"]),
                    now,
                    now,
                ),
            )
            font_id = cursor.lastrowid
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
            where.append(
                "(LOWER(font_name) LIKE LOWER(?) "
                "OR LOWER(font_preferred) LIKE LOWER(?) "
                "OR LOWER(font_en) LIKE LOWER(?) "
                "OR LOWER(font_all_name) LIKE LOWER(?))"
            )
            params.extend([f"%{search}%"] * 4)
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
                ORDER BY enabled DESC, font_name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [
                self._font_row_to_dict(row)
                for row in rows
            ]
        return {
            "items": items,
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
                    font_preferred = ?,
                    font_en = ?,
                    font_all_name = ?,
                    post_script_name = ?,
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
                    values["font_preferred"],
                    values["font_en"],
                    values["font_all_name"],
                    values["post_script_name"],
                    values["file_path"],
                    values["source"],
                    values["enabled"],
                    json_dump(values["metadata"]),
                    now,
                    font_id,
                ),
            )
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
    def _optional_int(value: Any):
        if value in (None, ""):
            return None
        if isinstance(value, dict):
            value = value.get("id")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ID必须是数字") from exc

    @staticmethod
    def _payload_has_product_names(payload: dict[str, Any]):
        return any(
            key in payload
            for key in ("product_names", "applicable_products", "商品名列表")
        )

    @staticmethod
    def _size_template_product_column(connection):
        columns = {
            row["name"]
            for row in table_columns(connection, "size_templates")
        }
        return "product_names_json" if "product_names_json" in columns else "product_names"

    def _payload_product_names(self, payload: dict[str, Any]):
        return unique_preserve_order(
            normalize_string_list(
                payload.get("product_names")
                if "product_names" in payload
                else payload.get("applicable_products")
                if "applicable_products" in payload
                else payload.get("商品名列表")
            )
        )

    @staticmethod
    def _payload_shop_products(payload: dict[str, Any]):
        return unique_preserve_order(
            normalize_string_list(
                payload.get("products")
                if "products" in payload
                else payload.get("product_names")
                if "product_names" in payload
                else payload.get("商品列表")
            )
        )

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

    @staticmethod
    def _size_options_are_complete(options: Any) -> bool:
        if not isinstance(options, list):
            return False
        if not options:
            return True
        required = {
            "id",
            "label",
            "size_unit",
            "single_side_width",
            "single_side_height",
            "bleed",
            "spine_width",
            "spine_bleed",
        }
        return all(
            isinstance(option, dict) and required.issubset(option)
            for option in options
        )

    @staticmethod
    def _merge_size_option_summaries(
        existing_options: Any,
        incoming_options: list[Any],
    ):
        from backend.templates.size_variants import (
            merge_size_option,
            normalize_size_options,
        )

        existing_by_id = {
            str(item.get("id")): item
            for item in normalize_size_options(existing_options or [])
        }
        merged_options = []
        incoming_ids: set[str] = set()
        for raw_option in incoming_options:
            if not isinstance(raw_option, dict):
                merged_options.append(raw_option)
                continue
            incoming = dict(raw_option)
            option_id = compact_string(
                incoming.get("id") or incoming.get("value")
            )
            if option_id:
                incoming_ids.add(option_id)
            existing = dict(existing_by_id.get(option_id) or {})
            if not existing:
                incoming.pop("disabled", None)
                incoming.pop("status", None)
                merged_options.append(incoming)
                continue

            merged_options.append(merge_size_option(existing, incoming))
        for option_id, existing in existing_by_id.items():
            if option_id not in incoming_ids:
                merged_options.append(existing)
        return merged_options

    @staticmethod
    def _normalize_safe_distance(value: Any, field_name: str) -> dict[str, float]:
        if value in (None, ""):
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} 必须是包含 top、right、bottom、left 的对象")
        result = {}
        for side in ("top", "right", "bottom", "left"):
            raw = value.get(side, 0)
            if isinstance(raw, bool):
                raise ValueError(f"{field_name}.{side} 必须是数字")
            try:
                numeric = float(raw or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name}.{side} 必须是数字") from exc
            if numeric < 0:
                raise ValueError(f"{field_name}.{side} 必须大于或等于 0")
            result[side] = numeric
        return result

    @classmethod
    def _size_template_column_values(
        cls,
        payload: dict[str, Any],
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = fallback or {}
        if "background_color" in payload:
            background_color = payload.get("background_color")
        elif "背景色" in payload:
            background_color = payload.get("背景色")
        else:
            background_color = fallback.get("background_color", "")

        result = {"background_color": compact_string(background_color)}
        aliases = {
            "paper_thickness_mm": "纸张厚度",
            "min_spine_width": "最小背脊宽",
            "max_spine_width": "最大背脊宽",
        }
        for key, alias in aliases.items():
            value = payload.get(key, payload.get(alias, fallback.get(key, 0)))
            if isinstance(value, bool):
                raise ValueError(f"{key} 必须是数字")
            try:
                numeric = float(value or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if numeric < 0:
                raise ValueError(f"{key} 必须大于或等于 0")
            result[key] = numeric
        legacy_aliases = {
            "max_spine_bleed": "最大背脊出血",
            "min_spine_bleed": "最小背脊出血",
        }
        for key, alias in legacy_aliases.items():
            value = payload.get(key, payload.get(alias, fallback.get(key, 0)))
            if isinstance(value, bool):
                raise ValueError(f"{key} 必须是数字")
            try:
                numeric = float(value or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if numeric < 0:
                raise ValueError(f"{key} 必须大于或等于 0")
            result[key] = numeric
        basis = payload.get(
            "spine_width_basis",
            payload.get("背脊依据", fallback.get("spine_width_basis", 0)),
        )
        if isinstance(basis, bool):
            basis = int(basis)
        try:
            basis = int(basis)
        except (TypeError, ValueError) as exc:
            raise ValueError("spine_width_basis 必须是 0 或 1") from exc
        if basis not in (0, 1):
            raise ValueError("spine_width_basis 必须是 0 或 1")
        result["spine_width_basis"] = basis
        if (
            result["max_spine_width"] > 0
            and result["max_spine_width"] < result["min_spine_width"]
        ):
            raise ValueError("max_spine_width 不能小于 min_spine_width")

        safe_distance_fields = {
            "cover_safe_distance": ("封面安全距离", "cover_safe_distance_json"),
            "spine_safe_distance": ("背脊安全距离", "spine_safe_distance_json"),
            "back_cover_safe_distance": ("封底安全距离", "back_cover_safe_distance_json"),
        }
        for key, (alias, column) in safe_distance_fields.items():
            if key in payload:
                value = payload.get(key)
            elif alias in payload:
                value = payload.get(alias)
            else:
                value = json_load(fallback.get(column, "{}"), {})
            result[key] = cls._normalize_safe_distance(value, key)
        return result

    @staticmethod
    def _normalize_size_spec(value: Any) -> dict[str, Any]:
        from backend.templates.size_variants import normalize_size_spec

        return normalize_size_spec(value)

    @classmethod
    def _size_template_spec_from_payload(cls, payload: dict[str, Any]):
        return cls._normalize_size_spec(
            {
                "selected": payload.get("selected_size_option_id"),
                "display_unit": payload.get("display_unit") or "in",
                "options": payload.get("size_options") or [],
            }
        )

    @staticmethod
    def _normalize_page_count(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError("page_count 必须是大于或等于 0 的整数")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("page_count 必须是大于或等于 0 的整数") from exc
        if result < 0:
            raise ValueError("page_count 必须是大于或等于 0 的整数")
        return result

    @classmethod
    def _normalize_page_count_options(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            try:
                page_count = cls._normalize_page_count(item)
            except ValueError:
                continue
            if page_count is not None and page_count not in result:
                result.append(page_count)
        return result

    @classmethod
    def _size_template_fields_from_row(
        cls,
        row,
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        from backend.templates.size_variants import (
            apply_selected_size_variant,
            resolve_spine_width,
        )

        fields = {
            "size_spec": deepcopy(spec),
            "page_count": row.get("page_count"),
            "page_count_options": cls._normalize_page_count_options(
                json_load(row.get("page_count_options_json"), [])
            ),
            "min_spine_width": float(row.get("min_spine_width") or 0),
            "max_spine_width": float(row.get("max_spine_width") or 0),
            "spine_width_basis": int(row.get("spine_width_basis") or 0),
            "paper_thickness_mm": float(row.get("paper_thickness_mm") or 0),
        }
        fields["page_count_arr"] = list(fields["page_count_options"])
        template = {"fields": fields}
        apply_selected_size_variant(template)
        fields = template["fields"]
        if fields.get("spine_width_mode") == "by_page_count":
            fields["spine_width"] = resolve_spine_width(fields)
        return fields

    @staticmethod
    def _normalize_size_template_info(value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ValueError("size_template_info 必须是对象数组")
        result = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"size_template_info[{index}] 必须是对象")
            result.append(deepcopy(item))
        return result

    @staticmethod
    def _normalize_layers(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return deepcopy(value)
        raise ValueError("layers 必须是完整图层文档对象")

    @staticmethod
    def _select_font_source(
        records: list[Any],
        family: str,
        *,
        weight: str = "normal",
        style: str = "normal",
    ) -> str:
        family_key = compact_string(family).casefold()
        if not family_key:
            return ""
        wants_italic = compact_string(style).casefold() in {"italic", "oblique"}
        wants_bold = compact_string(weight).casefold() in {
            "bold", "bolder", "600", "700", "800", "900",
        }

        def score(raw_record: Any) -> tuple[int, int]:
            record = dict(raw_record)
            names = [
                compact_string(record.get(field)).casefold()
                for field in (
                    "font_family",
                    "font_preferred",
                    "font_en",
                    "font_all_name",
                )
            ]
            if family_key not in names:
                return (-1, 0)
            descriptor = " ".join(
                compact_string(record.get(field)).casefold()
                for field in ("font_name", "post_script_name", "file_path")
            )
            is_italic = "italic" in descriptor or "oblique" in descriptor
            is_bold = "bold" in descriptor or "semibold" in descriptor
            style_score = 20 if is_italic == wants_italic else -20
            weight_score = 10 if is_bold == wants_bold else -10
            exact_family_score = 20 if names[0] == family_key else 0
            try:
                record_id = int(record.get("id") or 0)
            except (TypeError, ValueError):
                record_id = 0
            return (100 + exact_family_score + style_score + weight_score, -record_id)

        candidates = [
            dict(record)
            for record in records
            if bool(dict(record).get("enabled", True))
            and compact_string(dict(record).get("file_path"))
            and score(record)[0] >= 0
        ]
        selected = max(candidates, key=score, default=None)
        return compact_string((selected or {}).get("file_path"))

    @classmethod
    def _enrich_layers_font_urls(
        cls,
        layers: dict[str, Any],
        font_records: list[Any],
    ) -> set[str]:
        enriched: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            family = compact_string(
                value.get("fontFamily") or value.get("font_family")
            )
            font_url = compact_string(
                value.get("fontUrl") or value.get("font_url")
            )
            if family and not font_url:
                source = cls._select_font_source(
                    font_records,
                    family,
                    weight=compact_string(value.get("fontWeight")) or "normal",
                    style=compact_string(value.get("fontStyle")) or "normal",
                )
                if source:
                    value["fontUrl"] = source
                    enriched.add(family)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)

        visit(layers)
        return enriched

    @staticmethod
    def _size_template_option_row_to_dict(row) -> dict[str, Any]:
        data = {
            "id": str(row["size_option_id"]),
            "label": compact_string(row["label"]),
            "select": False,
            "size_unit": compact_string(row["size_unit"]) or "in",
            "single_side_width": float(row["single_side_width"] or 0),
            "single_side_height": float(row["single_side_height"] or 0),
            "bleed": float(row["bleed"] or 0),
            "spine_width": float(row["spine_width"] or 0),
            "spine_bleed": float(row["spine_bleed"] or 0),
        }
        layers = json_load(row.get("layers_json"), {})
        if isinstance(layers, dict) and layers:
            data["layers"] = CatalogRepository._normalize_layers(layers)
        mode = compact_string(row.get("spine_width_mode")) or "fixed"
        if mode != "fixed":
            data["spine_width_mode"] = mode
            formula = json_load(row.get("spine_width_formula_json"), {})
            if isinstance(formula, dict) and formula:
                data["spine_width_formula"] = formula
        return data

    @classmethod
    def _size_template_options(
        cls,
        connection,
        template_id: int,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM size_template_options WHERE size_template_id = ? "
            "ORDER BY sort_order, id",
            (template_id,),
        ).fetchall()
        return [cls._size_template_option_row_to_dict(row) for row in rows]

    @classmethod
    def _size_template_spec_from_row(cls, connection, row) -> dict[str, Any]:
        return cls._normalize_size_spec(
            {
                "selected": row.get("selected_size_option_id"),
                "display_unit": row.get("display_unit") or "in",
                "options": cls._size_template_options(connection, int(row["id"])),
            }
        )

    @classmethod
    def _insert_size_template_option(
        cls,
        connection,
        template_id: int,
        option: dict[str, Any],
        sort_order: int,
        now: str,
    ):
        connection.execute(
            """
            INSERT INTO size_template_options (
                size_template_id, size_option_id, label, sort_order,
                size_unit, single_side_width, single_side_height,
                bleed, spine_width, spine_bleed, spine_width_mode,
                spine_width_formula_json, layers_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                str(option["id"]),
                compact_string(option.get("label")) or str(option["id"]),
                sort_order,
                compact_string(option.get("size_unit")) or "in",
                float(option.get("single_side_width") or 0),
                float(option.get("single_side_height") or 0),
                float(option.get("bleed") or 0),
                float(option.get("spine_width") or 0),
                float(option.get("spine_bleed") or 0),
                compact_string(option.get("spine_width_mode")) or "fixed",
                json_dump(option.get("spine_width_formula") or {}),
                json_dump(option.get("layers") or {}),
                now,
                now,
            ),
        )

    @classmethod
    def _insert_size_template_options(
        cls,
        connection,
        template_id: int,
        options: list[dict[str, Any]],
        now: str,
    ):
        for sort_order, option in enumerate(options):
            cls._insert_size_template_option(
                connection,
                template_id,
                option,
                sort_order,
                now,
            )

    @classmethod
    def _replace_size_template_options(
        cls,
        connection,
        template_id: int,
        options: list[dict[str, Any]],
        now: str,
    ):
        connection.execute(
            "DELETE FROM size_template_options WHERE size_template_id = ?",
            (template_id,),
        )
        cls._insert_size_template_options(
            connection,
            template_id,
            options,
            now,
        )

    @classmethod
    def _upsert_size_template_options_preserving_variants(
        cls,
        connection,
        template_id: int,
        options: list[dict[str, Any]],
        now: str,
    ):
        """Update options by business ID without recreating unchanged rows.

        Replacing ``size_template_options`` wholesale would cascade-delete
        ``font_layout_size_variants``. Keep matching option rows in place so
        saving a size-template editor cannot erase saved font layers.
        """
        existing_rows = connection.execute(
            "SELECT size_option_id, layers_json FROM size_template_options "
            "WHERE size_template_id = ?",
            (template_id,),
        ).fetchall()
        existing_ids = {str(row["size_option_id"]) for row in existing_rows}
        existing_layers = {
            str(row["size_option_id"]): json_load(row.get("layers_json"), {})
            for row in existing_rows
        }
        incoming_ids = {str(option["id"]) for option in options}

        for sort_order, option in enumerate(options):
            option_id = str(option["id"])
            if option_id in existing_ids:
                if "layers" not in option:
                    option = {**option, "layers": existing_layers.get(option_id, {})}
                cls._update_size_template_option_row(
                    connection,
                    template_id,
                    option_id,
                    option,
                    now,
                )
                connection.execute(
                    "UPDATE size_template_options SET sort_order = ? "
                    "WHERE size_template_id = ? AND size_option_id = ?",
                    (sort_order, template_id, option_id),
                )
            else:
                cls._insert_size_template_option(
                    connection,
                    template_id,
                    option,
                    sort_order,
                    now,
                )

        removed_ids = existing_ids - incoming_ids
        for option_id in removed_ids:
            connection.execute(
                "DELETE FROM size_template_options "
                "WHERE size_template_id = ? AND size_option_id = ?",
                (template_id, option_id),
            )

    @classmethod
    def _update_size_template_option_row(
        cls,
        connection,
        template_id: int,
        option_id: str,
        option: dict[str, Any],
        now: str,
    ):
        connection.execute(
            """
            UPDATE size_template_options SET
                label = ?, size_unit = ?, single_side_width = ?,
                single_side_height = ?, bleed = ?, spine_width = ?,
                spine_bleed = ?, spine_width_mode = ?,
                spine_width_formula_json = ?, layers_json = ?, updated_at = ?
            WHERE size_template_id = ? AND size_option_id = ?
            """,
            (
                compact_string(option.get("label")) or option_id,
                compact_string(option.get("size_unit")) or "in",
                float(option.get("single_side_width") or 0),
                float(option.get("single_side_height") or 0),
                float(option.get("bleed") or 0),
                float(option.get("spine_width") or 0),
                float(option.get("spine_bleed") or 0),
                compact_string(option.get("spine_width_mode")) or "fixed",
                json_dump(option.get("spine_width_formula") or {}),
                json_dump(option.get("layers") or {}),
                now,
                template_id,
                option_id,
            ),
        )

    @classmethod
    def _size_template_option_ids(cls, connection, template_id: int) -> set[str]:
        return {
            str(item.get("id"))
            for item in cls._size_template_options(connection, template_id)
            if item.get("id") not in (None, "")
        }

    @staticmethod
    def _font_layout_canvas(value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            raise ValueError("canvas 必须是对象")
        try:
            width = float(value.get("width") or value.get("width_px"))
            height = float(value.get("height") or value.get("height_px"))
        except (TypeError, ValueError) as exc:
            raise ValueError("canvas.width 和 canvas.height 必须是数字") from exc
        if width <= 0 or height <= 0:
            raise ValueError("canvas.width 和 canvas.height 必须大于 0")
        return {"width": width, "height": height}

    @classmethod
    def _resolve_font_layout_canvas(
        cls,
        explicit_canvas: Any,
        options: Any,
        elements: list[dict[str, Any]],
    ) -> dict[str, float]:
        if isinstance(explicit_canvas, dict):
            return cls._font_layout_canvas(explicit_canvas)

        options = options if isinstance(options, dict) else {}
        candidates = [options.get("reference_canvas")]
        scaling = options.get("scaling")
        if isinstance(scaling, dict):
            candidates.append(scaling.get("reference_canvas"))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                return cls._font_layout_canvas(candidate)
            except ValueError:
                continue

        width_candidates: list[float] = []
        height_candidates: list[float] = []
        max_right = 0.0
        max_bottom = 0.0
        for element in elements:
            if not isinstance(element, dict):
                continue
            frame = element.get("frame")
            if not isinstance(frame, dict):
                continue
            try:
                x = float(frame.get("x") or 0)
                y = float(frame.get("y") or 0)
                width = float(frame.get("width") or 0)
                height = float(frame.get("height") or 0)
            except (TypeError, ValueError):
                continue
            max_right = max(max_right, x + max(0, width))
            max_bottom = max(max_bottom, y + max(0, height))
            relative = frame.get("relative")
            if not isinstance(relative, dict):
                continue
            for absolute, ratio, bucket in (
                (x, relative.get("x"), width_candidates),
                (width, relative.get("width"), width_candidates),
                (y, relative.get("y"), height_candidates),
                (height, relative.get("height"), height_candidates),
            ):
                try:
                    ratio_value = float(ratio)
                except (TypeError, ValueError):
                    continue
                if absolute > 0 and ratio_value > 0:
                    bucket.append(absolute / ratio_value)

        def middle(values: list[float], fallback: float) -> float:
            usable = sorted(value for value in values if value > 0)
            if usable:
                return usable[len(usable) // 2]
            return fallback

        width = middle(width_candidates, max_right)
        height = middle(height_candidates, max_bottom)
        if width <= 0 or height <= 0:
            raise ValueError(
                "无法从字体模板推导设计坐标范围，请确保 options.reference_canvas "
                "或 elements[].frame.relative 完整"
            )
        return {"width": width, "height": height}

    @staticmethod
    def _font_layout_physical_size(option: dict[str, Any]) -> dict[str, float]:
        from backend.templates.size_variants import size_values_for_unit

        values = size_values_for_unit(
            option if isinstance(option, dict) else {},
            "in",
        )
        width = (
            values["single_side_width"] * 2
            + values["spine_width"]
            + values["bleed"] * 2
        )
        height = values["single_side_height"] + values["bleed"] * 2
        if width <= 0 or height <= 0:
            raise ValueError(
                f"尺寸方案尚未填写完整: {option.get('id') or ''}"
            )
        return {"width": width, "height": height}

    @staticmethod
    def _scale_font_layout_elements(
        elements: dict[str, Any],
        scale_x: float,
        scale_y: float,
    ) -> dict[str, Any]:
        scaled = deepcopy(elements)
        font_scale = (scale_x * scale_y) ** 0.5
        objects = scaled.get("objects")
        if not isinstance(objects, list):
            return scaled
        for element in objects:
            if not isinstance(element, dict):
                continue
            for key, factor in (("left", scale_x), ("top", scale_y)):
                value = element.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    element[key] = round(float(value) * factor, 4)
            for key, factor in (("scaleX", scale_x), ("scaleY", scale_y)):
                value = element.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    element[key] = round(float(value) * factor, 4)
            frame = element.get("frame")
            if isinstance(frame, dict):
                for key, factor in (
                    ("x", scale_x),
                    ("y", scale_y),
                    ("width", scale_x),
                    ("height", scale_y),
                ):
                    value = frame.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        frame[key] = round(float(value) * factor, 4)
            font = element.get("font")
            if isinstance(font, dict):
                for key in ("size_px", "leading_px"):
                    value = font.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        font[key] = round(float(value) * font_scale, 4)
                runs = font.get("runs")
                if isinstance(runs, list):
                    for run in runs:
                        if not isinstance(run, dict):
                            continue
                        for key in ("size_px", "leading_px"):
                            value = run.get(key)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                run[key] = round(float(value) * font_scale, 4)
        return scaled

    @staticmethod
    def _validate_responsive_font_options(
        options: dict[str, Any],
        elements: list[dict[str, Any]],
        size_option_ids: set[str],
    ) -> dict[str, Any]:
        result = deepcopy(options)
        if "responsive_layout" not in result:
            return result

        responsive_layout = result.get("responsive_layout")
        if not isinstance(responsive_layout, dict):
            raise ValueError("options.responsive_layout 必须是对象")
        version = result.get("responsive_version", 1)
        if isinstance(version, bool) or version != 1:
            raise ValueError("options.responsive_version 当前只能是 1")
        result["responsive_version"] = 1

        reference = compact_string(result.get("reference_size_option"))
        if not reference:
            raise ValueError("options.reference_size_option 不能为空")
        if reference not in size_option_ids:
            raise ValueError(
                "options.reference_size_option 未匹配尺寸模板的 size_options: "
                f"{reference}"
            )
        result["reference_size_option"] = reference

        element_by_id = {
            str(element.get("id")): element
            for element in elements
            if isinstance(element, dict) and element.get("id") not in (None, "")
        }
        allowed_regions = {"back", "cover", "spine"}
        allowed_anchor_x = {"left", "center", "right"}
        allowed_anchor_y = {"top", "middle", "bottom"}
        allowed_font_scales = {"area", "width", "height", "fixed"}
        ratio_keys = {
            "position_ratio_x",
            "position_ratio_y",
            "width_ratio",
            "height_ratio",
            "max_width_ratio",
            "max_height_ratio",
        }

        for element_id, raw_rule in responsive_layout.items():
            element_id = str(element_id)
            if element_id not in element_by_id:
                raise ValueError(f"responsive_layout 图层不存在: {element_id}")
            if not isinstance(raw_rule, dict):
                raise ValueError(f"responsive_layout.{element_id} 必须是对象")
            rule = raw_rule
            if rule.get("region") not in allowed_regions:
                raise ValueError(
                    f"responsive_layout.{element_id}.region 只能是 back、cover 或 spine"
                )
            if rule.get("anchor_x") not in allowed_anchor_x:
                raise ValueError(
                    f"responsive_layout.{element_id}.anchor_x 只能是 left、center 或 right"
                )
            if rule.get("anchor_y") not in allowed_anchor_y:
                raise ValueError(
                    f"responsive_layout.{element_id}.anchor_y 只能是 top、middle 或 bottom"
                )
            for required_ratio in ("position_ratio_x", "position_ratio_y"):
                if required_ratio not in rule:
                    raise ValueError(
                        f"responsive_layout.{element_id}.{required_ratio} 不能为空"
                    )
            for key in ratio_keys:
                if key not in rule:
                    continue
                value = rule[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"responsive_layout.{element_id}.{key} 必须是数字")
                if value < 0 or value > 1:
                    raise ValueError(
                        f"responsive_layout.{element_id}.{key} 必须在 0 到 1 之间"
                    )

            element = element_by_id[element_id]
            element_type = str(element.get("type") or "").lower()
            if element_type not in {"text", "image"}:
                raise ValueError(f"响应式图层 {element_id} 的 type 只能是 text 或 image")
            frame = element.get("frame")
            if not isinstance(frame, dict):
                raise ValueError(f"响应式图层 {element_id}.frame 必须是对象")
            for key in ("x", "y", "width", "height"):
                value = frame.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"响应式图层 {element_id}.frame.{key} 必须是数字")
                if key in {"width", "height"} and value <= 0:
                    raise ValueError(f"响应式图层 {element_id}.frame.{key} 必须大于 0")

            if element_type == "text":
                font = element.get("font")
                size_px = font.get("size_px") if isinstance(font, dict) else None
                if isinstance(size_px, bool) or not isinstance(size_px, (int, float)) or size_px <= 0:
                    raise ValueError(f"响应式文字图层 {element_id}.font.size_px 必须大于 0")
                font_scale = rule.get("font_scale", "fixed")
                if font_scale not in allowed_font_scales:
                    raise ValueError(
                        f"responsive_layout.{element_id}.font_scale 只能是 area、width、height 或 fixed"
                    )
                for key in ("min_font_size", "max_font_size"):
                    if key not in rule:
                        continue
                    value = rule[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                        raise ValueError(f"responsive_layout.{element_id}.{key} 必须大于 0")
                minimum = rule.get("min_font_size")
                maximum = rule.get("max_font_size")
                if minimum is not None and maximum is not None and minimum > maximum:
                    raise ValueError(
                        f"responsive_layout.{element_id}.min_font_size 不能大于 max_font_size"
                    )
            else:
                if "aspect_lock" in rule and not isinstance(rule["aspect_lock"], bool):
                    raise ValueError(
                        f"responsive_layout.{element_id}.aspect_lock 必须是布尔值"
                    )
                if "fit" in rule and rule["fit"] not in {"cover", "contain", "fill"}:
                    raise ValueError(
                        f"responsive_layout.{element_id}.fit 只能是 cover、contain 或 fill"
                    )
        return result

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
        if "description" in payload:
            description_value = payload.get("description")
        elif "描述" in payload:
            description_value = payload.get("描述")
        else:
            description_value = fallback.get("description")
        description = compact_string(description_value)
        elements = payload.get("elements")
        if elements is None:
            # Internal import callers may still provide the former layout key.
            legacy_layout = payload.get("layout")
            if isinstance(legacy_layout, dict) and isinstance(
                legacy_layout.get("elements"),
                list,
            ):
                elements = legacy_layout["elements"]
            elif isinstance(legacy_layout, list):
                elements = legacy_layout
        if elements is None:
            elements = fallback.get("elements", [])
        if not isinstance(elements, list):
            raise ValueError("elements 必须是数组")
        options = payload.get("options")
        if options is None:
            options = fallback.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("options 必须是对象")
        options = deepcopy(options)
        for deprecated_key in (
            "responsive_version",
            "reference_size_option",
            "responsive_layout",
        ):
            options.pop(deprecated_key, None)
        safe_distance_value = payload.get(
            "safe_distance",
            payload.get("安全距离", fallback.get("safe_distance", 0)),
        )
        if isinstance(safe_distance_value, bool):
            raise ValueError("safe_distance 必须是数字")
        try:
            safe_distance = float(safe_distance_value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("safe_distance 必须是数字") from exc
        if safe_distance < 0:
            raise ValueError("safe_distance 必须大于或等于 0")
        return {
            "name": name,
            "description": description,
            "safe_distance": safe_distance,
            "elements": elements,
            "options": options,
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
            "font_preferred": compact_string(
                payload.get("font_preferred") or fallback.get("font_preferred")
            ),
            "font_en": compact_string(
                payload.get("font_en") or fallback.get("font_en")
            ),
            "font_all_name": compact_string(
                payload.get("font_all_name") or fallback.get("font_all_name")
            ),
            "post_script_name": compact_string(
                payload.get("post_script_name") or fallback.get("post_script_name")
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

    @staticmethod
    def _require_shop_products(connection, shop_id: int, product_names: list[str]):
        if not product_names:
            return
        row = connection.execute(
            "SELECT product_names_json FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone()
        existing = {
            name.casefold()
            for name in normalize_string_list(
                json_load(row["product_names_json"], []) if row else []
            )
        }
        missing = [name for name in product_names if name.casefold() not in existing]
        if missing:
            raise ValueError(f"商品未关联到该店铺：{', '.join(missing)}")

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
        if not table_exists(connection, "orders"):
            return
        columns = {
            row["name"]
            for row in table_columns(connection, "orders")
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
        if not table_exists(connection, "orders"):
            return 0
        columns = {
            row["name"]
            for row in table_columns(connection, "orders")
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
        if connection.execute(
            "SELECT 1 FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone() is None:
            return
        order_count = self._orders_count(connection, shop_id)
        status_counts = {status: 0 for status in range(6)}
        if table_exists(connection, "orders"):
            order_columns = {row["name"] for row in table_columns(connection, "orders")}
            if "shop_id" in order_columns and "status" in order_columns:
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
        size_template_count = connection.execute(
            "SELECT COUNT(*) AS count FROM size_templates WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]
        font_template_count = connection.execute(
            "SELECT COUNT(*) AS count FROM font_layout_library WHERE shop_id = ?",
            (shop_id,),
        ).fetchone()["count"]
        shop_row = connection.execute(
            "SELECT product_names_json FROM shops WHERE id = ?",
            (shop_id,),
        ).fetchone()
        product_count = len(
            normalize_string_list(json_load(shop_row["product_names_json"], []))
        )
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
                font_template_count = ?,
                updated_at = CASE WHEN updated_at = '' THEN ? ELSE updated_at END
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
                utc_now(),
                shop_id,
            ),
        )

    def _size_template_row_to_dict(self, row, *, include_related: bool = True):
        from backend.templates.size_variants import build_size_form

        data = dict(row)
        with self.connect() as connection:
            size_spec = self._size_template_spec_from_row(connection, data)
        product_names_json = data.pop(
            "product_names_json",
            data.get("product_names", "[]"),
        )
        product_names = (
            normalize_string_list(json_load(product_names_json, []))
            if include_related
            else []
        )
        if include_related:
            data["product_names"] = product_names
        data["preview_image"] = compact_string(
            data.pop("preview_image_path", data.get("preview_image", ""))
        )
        if include_related:
            data["applicable_products"] = list(product_names)
            data["product_category_name"] = compact_string(
                data.get("product_category_name")
            )
            data["product"] = None
            if data.get("product_id") is not None:
                try:
                    data["product"] = self.get_product(int(data["product_id"]))
                    data["product_category_name"] = data["product"]["name"]
                except LookupError:
                    data["product"] = None
        data["fields"] = self._size_template_fields_from_row(data, size_spec)
        data["size_form"] = build_size_form(data["fields"])
        data["name"] = compact_string(data.get("template_name"))
        data["template_name"] = data["name"]
        data["size_options"] = deepcopy(size_spec.get("options") or [])
        data["selected_size_option_id"] = size_spec.get("selected")
        data["selected_font_layout_id"] = self._optional_int(
            data.get("selected_font_layout_id")
        )
        try:
            data["paper_thickness_mm"] = float(data.get("paper_thickness_mm") or 0)
        except (TypeError, ValueError):
            data["paper_thickness_mm"] = 0.0
        data["display_unit"] = size_spec.get("display_unit") or "in"
        try:
            data["spine_width_basis"] = int(data.get("spine_width_basis") or 0)
        except (TypeError, ValueError):
            data["spine_width_basis"] = 0
        data["page_count"] = self._normalize_page_count(data.get("page_count"))
        data["page_count_options"] = deepcopy(
            self._normalize_page_count_options(
                json_load(data.pop("page_count_options_json", "[]"), [])
            )
        )
        for key, column in (
            ("cover_safe_distance", "cover_safe_distance_json"),
            ("spine_safe_distance", "spine_safe_distance_json"),
            ("back_cover_safe_distance", "back_cover_safe_distance_json"),
        ):
            data[key] = self._normalize_safe_distance(
                json_load(data.pop(column, "{}"), {}),
                key,
            )
        data["size_template_info"] = self._normalize_size_template_info(
            json_load(data.pop("size_template_info_json", "[]"), [])
        )
        return data

    @staticmethod
    def _font_layout_library_row_to_dict(row):
        data = dict(row)
        data["layers"] = CatalogRepository._normalize_layers(
            json_load(data.pop("layers_json", "{}"), {})
        )
        data["preview_image_path"] = compact_string(
            data.get("preview_image_path")
        )
        data["preview_image"] = data["preview_image_path"]
        objects = data["layers"].get("objects")
        data["layer_count"] = len(objects) if isinstance(objects, list) else 0
        return data

    def _shop_row_to_dict(self, row):
        data = dict(row)
        data["products"] = json_load(data["product_names_json"], [])
        # Keep the URL field explicit in the public shop payload.  The value
        # is intentionally returned as-is so the frontend can edit it.
        data["wecom_robot_webhook_url"] = compact_string(
            data.get("wecom_robot_webhook_url")
        )
        return data

    def get_shop_wecom_robot_webhook(self, shop_id: int | None):
        """Return the configured WeCom webhook for a shop, or an empty value."""
        if shop_id is None:
            return ""
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT wecom_robot_webhook_url FROM shops WHERE id = ?",
                (shop_id,),
            ).fetchone()
        return compact_string(row["wecom_robot_webhook_url"]) if row else ""

    def has_configured_shop_wecom_robot(self):
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM shops
                WHERE TRIM(wecom_robot_webhook_url) <> ''
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    @staticmethod
    def _size_template_display_name(
        template_id: int,
        fields: dict[str, Any],
        product_names: list[str],
    ):
        for key in ("template_name", "name"):
            value = compact_string(fields.get(key))
            if value:
                return value
        if product_names:
            return compact_string(product_names[0])
        return f"尺寸模板 {template_id}"

    @staticmethod
    def _merge_render_size_layouts(
        direct_layouts: list[dict[str, Any]],
        variant_layouts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Use the size option's own copy before legacy variant snapshots."""
        merged: list[dict[str, Any]] = []
        direct_ids = set()
        for item in direct_layouts:
            option_id = compact_string(item.get("size_option_id"))
            if not option_id:
                continue
            direct_ids.add(option_id)
            merged.append(deepcopy(item))
        for item in variant_layouts:
            if compact_string(item.get("size_option_id")) not in direct_ids:
                merged.append(deepcopy(item))
        return merged

    def _flatten_render_template(self, template: dict[str, Any]):
        direct_size_layouts = []
        for option in template.get("size_options") or []:
            if not isinstance(option, dict) or not isinstance(option.get("layers"), dict):
                continue
            layers = self._normalize_layers(option["layers"])
            if not layers:
                continue
            direct_size_layouts.append(
                {
                    "size_option_id": str(option.get("id") or ""),
                    "layers": layers,
                    "canvas": deepcopy(layers.get("canvas") or {}),
                    "layers_source": "size_template_option",
                }
            )
        layouts = self.list_font_layout_library_templates(
            limit=500,
            shop_id=int(template["shop_id"]),
            product_id=self._optional_int(template.get("product_id")),
        )["items"]
        selected_layout_id = self._optional_int(
            template.get("selected_font_layout_id")
        )
        # A size template has one active font layout at most.  Until the
        # frontend explicitly syncs a layout, do not expose every matching
        # library template as if they were active simultaneously.
        if selected_layout_id is None:
            layouts = []
            if direct_size_layouts:
                base_layers = deepcopy(direct_size_layouts[0]["layers"])
                layouts = [{
                    "id": None,
                    "name": "尺寸模板规格图层",
                    "preview_image": "",
                    "layers": base_layers,
                    "size_layouts": direct_size_layouts,
                }]
        else:
            layouts = [
                layout for layout in layouts
                if int(layout.get("id")) == selected_layout_id
            ]
        if selected_layout_id is not None and not layouts:
            # The selected library row may have been physically removed after
            # its per-size snapshots were saved. Reconstruct a minimal active
            # layout from the preserved variant rows for rendering.
            with self.connect() as connection:
                has_variant = connection.execute(
                    """
                    SELECT 1 FROM font_layout_size_variants
                    WHERE font_layout_id = ? AND size_template_id = ?
                    LIMIT 1
                    """,
                    (selected_layout_id, int(template["id"])),
                ).fetchone()
            if has_variant is not None:
                layouts = [{
                    "id": selected_layout_id,
                    "name": "已删除字体布局",
                    "preview_image": "",
                    "layers": {"objects": []},
                }]
            elif direct_size_layouts:
                # A stale/deleted selected layout must not hide independent
                # layers already copied onto the size options.
                layouts = [{
                    "id": None,
                    "name": "尺寸模板规格图层",
                    "preview_image": "",
                    "layers": deepcopy(direct_size_layouts[0]["layers"]),
                    "size_layouts": direct_size_layouts,
                }]
        variants_by_layout: dict[int, list[dict[str, Any]]] = {}
        layout_ids = [
            int(layout["id"])
            for layout in layouts
            if self._optional_int(layout.get("id")) is not None
        ]
        if layout_ids:
            with self.connect() as connection:
                id_placeholders = placeholders(layout_ids)
                variant_rows = connection.execute(
                    f"""
                    SELECT
                        flsv.font_layout_id,
                        sto.size_option_id,
                        flsv.layers_json
                    FROM font_layout_size_variants flsv
                    JOIN size_template_options sto
                        ON sto.id = flsv.size_template_option_id
                    WHERE sto.size_template_id = ?
                      AND flsv.font_layout_id IN ({id_placeholders})
                    ORDER BY sto.sort_order, sto.id
                    """,
                    [int(template["id"]), *layout_ids],
                ).fetchall()
            for row in variant_rows:
                layers = self._normalize_layers(
                    json_load(row["layers_json"], {})
                )
                variant = {
                    "size_option_id": compact_string(row["size_option_id"]),
                    "layers": layers,
                    "layers_source": "size_variant",
                }
                # New layer documents may carry their reference canvas inside
                # the document. Preserve it instead of making renderers guess.
                if isinstance(layers.get("canvas"), dict):
                    variant["canvas"] = deepcopy(layers["canvas"])
                variants_by_layout.setdefault(
                    int(row["font_layout_id"]), []
                ).append(variant)
        font_layout_templates = [
            {
                "id": layout.get("id"),
                "name": layout.get("name", ""),
                "preview_image": layout.get("preview_image", ""),
                # Keep the Fabric document's object list as the canonical
                # renderer input. `elements` is retained only as a response
                # compatibility alias for older clients.
                "objects": deepcopy(
                    (layout.get("layers") or {}).get("objects") or []
                ),
                "elements": deepcopy(
                    (layout.get("layers") or {}).get("objects") or []
                ),
                "canvas": deepcopy(
                    (layout.get("layers") or {}).get("canvas") or {}
                ),
                "options": {
                    "reference_canvas": deepcopy(
                        (layout.get("layers") or {}).get("canvas") or {}
                    )
                },
                "size_layouts": deepcopy(
                    self._merge_render_size_layouts(
                        direct_size_layouts,
                        variants_by_layout.get(
                            self._optional_int(layout.get("id")), []
                        ),
                    )
                ),
            }
            for layout in layouts
        ]
        return {
            **template["fields"],
            "id": template["id"],
            "shop_id": template["shop_id"],
            "shop": template.get("shop", ""),
            "shop_name": template.get("shop_name", ""),
            "product_id": template.get("product_id"),
            "product": deepcopy(template.get("product")),
            "product_category_name": template.get("product_category_name", ""),
            "product_names": template["product_names"],
            "background_color": template.get("background_color", ""),
            "size_template_info": deepcopy(template.get("size_template_info") or []),
            "fields": dict(template["fields"]),
            "selected_font_layout_id": selected_layout_id,
            "font_layout_templates": font_layout_templates,
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
                SELECT ft.*
                FROM font_templates ft
                WHERE size_template_id = ?
                ORDER BY id
                """,
                (template_id,),
            ).fetchall()
        return [self._font_template_summary(row) for row in rows]

    @staticmethod
    def _font_template_row_to_dict(
        row,
        size_option: str | None = None,
        include_size_option_layouts: bool = False,
    ):
        data = dict(row)
        data["elements"] = json_load(data.pop("elements_json"), [])
        data["options"] = json_load(data.pop("options_json"), {})
        if not isinstance(data["options"], dict):
            data["options"] = {}
        for deprecated_key in (
            "responsive_version",
            "reference_size_option",
            "responsive_layout",
        ):
            data["options"].pop(deprecated_key, None)
        layouts = json_load(data.pop("size_option_layouts_json", "{}"), {})
        if not isinstance(layouts, dict):
            layouts = {}
        data["synced_size_option_ids"] = list(layouts)
        requested_size_option = compact_string(size_option)
        if requested_size_option:
            layout = layouts.get(requested_size_option)
            if not isinstance(layout, dict) and layouts:
                raise LookupError(
                    f"字体布局尚未同步到尺寸方案: {requested_size_option}"
                )
            data["size_option"] = requested_size_option
            if isinstance(layout, dict):
                data["elements"] = deepcopy(layout.get("elements") or [])
                data["canvas"] = deepcopy(layout.get("canvas") or {})
                data["is_synced"] = True
                data["sync_required"] = False
            else:
                reference_canvas = data["options"].get("reference_canvas")
                data["canvas"] = (
                    deepcopy(reference_canvas)
                    if isinstance(reference_canvas, dict)
                    else {}
                )
                data["is_synced"] = False
                data["sync_required"] = True
        if include_size_option_layouts:
            data["_size_option_layouts"] = layouts
        return data

    @staticmethod
    def _font_template_summary(row):
        data = dict(row)
        data.pop("elements_json", None)
        data.pop("options_json", None)
        layouts = json_load(data.pop("size_option_layouts_json", "{}"), {})
        data["synced_size_option_ids"] = (
            list(layouts) if isinstance(layouts, dict) else []
        )
        return data

    @staticmethod
    def _font_row_to_dict(row):
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data
