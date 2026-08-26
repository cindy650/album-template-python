from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from backend.database import connection_scope


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TemplateRepository:
    """MySQL storage for printable template fields and their UI labels."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cover_text TEXT NOT NULL DEFAULT '封面',
                    cover_names TEXT NOT NULL DEFAULT '',
                    cover_names_text TEXT NOT NULL DEFAULT '新人姓名',
                    cover_wedding_date TEXT NOT NULL DEFAULT '',
                    cover_wedding_date_text TEXT NOT NULL DEFAULT '婚礼日期',
                    cover_subtitle TEXT NOT NULL DEFAULT '',
                    cover_subtitle_text TEXT NOT NULL DEFAULT '副标题',
                    spine_text TEXT NOT NULL DEFAULT '书脊',
                    spine_top TEXT NOT NULL DEFAULT '',
                    spine_top_text TEXT NOT NULL DEFAULT '顶部文字',
                    spine_middle TEXT NOT NULL DEFAULT '',
                    spine_middle_text TEXT NOT NULL DEFAULT '中间文字',
                    spine_bottom TEXT NOT NULL DEFAULT '',
                    spine_bottom_text TEXT NOT NULL DEFAULT '底部文字',
                    back_cover_text TEXT NOT NULL DEFAULT '封底',
                    back_cover_content TEXT NOT NULL DEFAULT '',
                    back_cover_content_text TEXT NOT NULL DEFAULT '封底文字',
                    cover_size_text TEXT NOT NULL DEFAULT '封面尺寸',
                    size_unit TEXT NOT NULL DEFAULT 'in'
                        CHECK (size_unit IN ('in', 'mm', 'cm')),
                    size_unit_text TEXT NOT NULL DEFAULT '单位',
                    single_side_width REAL NOT NULL DEFAULT 12.01
                        CHECK (single_side_width >= 0),
                    single_side_width_text TEXT NOT NULL DEFAULT '单面宽',
                    single_side_height REAL NOT NULL DEFAULT 8.58
                        CHECK (single_side_height >= 0),
                    single_side_height_text TEXT NOT NULL DEFAULT '单面高',
                    bleed REAL NOT NULL DEFAULT 0.24
                        CHECK (bleed >= 0),
                    bleed_text TEXT NOT NULL DEFAULT '出血',
                    page_count INTEGER NOT NULL DEFAULT 80
                        CHECK (page_count >= 0),
                    page_count_text TEXT NOT NULL DEFAULT '页数',
                    spine_width REAL NOT NULL DEFAULT 0.65
                        CHECK (spine_width >= 0),
                    spine_width_text TEXT NOT NULL DEFAULT '背脊宽',
                    spine_bleed REAL NOT NULL DEFAULT 0.3
                        CHECK (spine_bleed >= 0),
                    spine_bleed_text TEXT NOT NULL DEFAULT '背脊出血',
                    is_fixed INTEGER NOT NULL DEFAULT 0
                        CHECK (is_fixed IN (0, 1)),
                    is_fixed_text TEXT NOT NULL DEFAULT '是否固定',
                    by_page_count INTEGER NOT NULL DEFAULT 0
                        CHECK (by_page_count IN (0, 1)),
                    by_page_count_text TEXT NOT NULL DEFAULT '按页数',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def insert(self, template: dict[str, Any] | None = None):
        values = {
            "cover_names": "",
            "cover_wedding_date": "",
            "cover_subtitle": "",
            "spine_top": "",
            "spine_middle": "",
            "spine_bottom": "",
            "back_cover_content": "",
            "size_unit": "in",
            "single_side_width": 12.01,
            "single_side_height": 8.58,
            "bleed": 0.24,
            "page_count": 80,
            "spine_width": 0.65,
            "spine_bleed": 0.3,
            "is_fixed": 0,
            "by_page_count": 0,
            **(template or {}),
        }
        now = utc_now()
        columns = tuple(values)
        placeholders = ", ".join(f":{column}" for column in columns)
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                f"INSERT INTO templates ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({placeholders}, :created_at, :updated_at)",
                {**values, "created_at": now, "updated_at": now},
            )
            template_id = cursor.lastrowid
            connection.commit()
        return self.get(template_id)

    def get(self, template_id: int):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_default(self, template_id: int | None = None):
        with self.connect() as connection:
            if template_id is not None:
                row = connection.execute(
                    "SELECT * FROM templates WHERE id = ?",
                    (template_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM templates ORDER BY id LIMIT 1"
                ).fetchone()
        return dict(row) if row is not None else None

    def list(self, limit: int = 50, offset: int = 0):
        with self.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM templates"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM templates ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
