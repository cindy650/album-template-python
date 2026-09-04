from contextlib import contextmanager
from threading import Lock
import unittest

from backend.catalog.repository import CatalogRepository


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, tuple(params)))
        if normalized.startswith("SELECT shop_id, preview_image_path"):
            return _Result(
                {
                    "shop_id": 1,
                    "preview_image_path": "https://example.com/layout.png",
                }
            )
        return _Result()


class FontLayoutLibraryDeleteTests(unittest.TestCase):
    def test_delete_hides_library_record_without_removing_applied_layout(self):
        repository = CatalogRepository.__new__(CatalogRepository)
        repository._lock = Lock()
        connection = _Connection()

        @contextmanager
        def connect():
            yield connection

        repository.connect = connect
        repository._refresh_shop_counts = lambda *_args: None

        result = repository.delete_font_layout_library_template(38)

        statements = [sql for sql, _params in connection.statements]
        self.assertTrue(result["deleted"])
        self.assertTrue(
            any(sql.startswith("DELETE FROM font_layout_library") for sql in statements)
        )
        self.assertFalse(
            any(sql.startswith("DELETE FROM font_layout_size_variants") for sql in statements)
        )
        self.assertFalse(
            any(
                "UPDATE size_templates SET selected_font_layout_id = NULL" in sql
                for sql in statements
            )
        )


if __name__ == "__main__":
    unittest.main()
