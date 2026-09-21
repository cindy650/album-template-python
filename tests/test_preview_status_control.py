from contextlib import closing, nullcontext
import sqlite3
from threading import Lock
import unittest
from unittest.mock import Mock, patch

from backend.orders.repository import OrderRepository
from backend.orders.service import OrderService
from backend.orders.statuses import ORDER_STATUS_SEED


class PreviewStatusControlTests(unittest.TestCase):
    def setUp(self):
        self.order = {"id": 1, "order_number": "test-order", "status": 0, "size_template_id": 1}
        self.repository = Mock()
        self.repository.get_by_id_and_order_number.return_value = dict(self.order)
        self.repository.mark_wecom_preview_sent.return_value = {
            **self.order,
            "wecom_preview_sent": True,
        }
        self.service = OrderService(self.repository, template_image_generator=Mock())
        self.notifier = Mock()
        self.notifier.notify_order_image.return_value = {"ok": True, "status": "sent"}
        self.service._notifier_for_order = Mock(return_value=self.notifier)
        self.service._generate_order_info_image = Mock(return_value={"path": "info.jpg"})

    @patch("backend.orders.service.event_bus.publish")
    def test_manual_send_keeps_status_and_event_unchanged(self, publish):
        result = self.service.send_preview_images(1, "test-order")
        self.repository.update_status.assert_not_called()
        self.assertEqual(result["order"]["status"], 0)
        self.assertTrue(result["order"]["wecom_preview_sent"])
        self.repository.mark_wecom_preview_sent.assert_called_once_with(1, "test-order")
        self.assertEqual(publish.call_args.args[1]["order"]["status"], 0)

    @patch("backend.orders.service.event_bus.publish")
    def test_automatic_send_keeps_status(self, _publish):
        result = self.service._notify_wecom({}, self.order, {}, None)
        self.assertTrue(result["ok"])
        self.repository.update_status.assert_not_called()
        self.assertEqual(self.order["status"], 0)
        self.assertTrue(self.order["wecom_preview_sent"])
        self.repository.mark_wecom_preview_sent.assert_called_once_with(1, "test-order")

    @patch("backend.orders.service.event_bus.publish")
    def test_failed_manual_send_keeps_status(self, _publish):
        self.notifier.notify_order_image.return_value = {"ok": False, "status": "failed"}
        with self.assertRaises(RuntimeError):
            self.service.send_preview_images(1, "test-order")
        self.repository.update_status.assert_not_called()
        self.repository.mark_wecom_preview_sent.assert_not_called()

    def test_repository_allows_zero_to_one_and_guards_invalid_transitions(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                "CREATE TABLE orders (id INTEGER, order_number TEXT, status INTEGER, shop_id INTEGER, wecom_preview_sent INTEGER DEFAULT 0, updated_at TEXT);"
                "CREATE TABLE order_statuses (status INTEGER, status_text TEXT, status_button_text TEXT);"
                "INSERT INTO orders VALUES (1, 'test-order', 0, 1, 0, '');"
            )
            connection.executemany("INSERT INTO order_statuses VALUES (?, ?, ?)", ORDER_STATUS_SEED)
            repository = OrderRepository.__new__(OrderRepository)
            repository._lock = Lock()
            repository.connect = lambda: nullcontext(connection)
            repository._refresh_shop_counts = Mock()
            repository.get = lambda order_id: dict(connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone())
            with self.assertRaises(LookupError):
                repository.advance_status(1, "wrong-order", expected_status=0)
            result = repository.advance_status(1, "test-order", expected_status=0)
            self.assertEqual(result["status"], 1)
            with self.assertRaises(ValueError):
                repository.advance_status(1, "test-order", expected_status=0)
            connection.execute("UPDATE orders SET status = 5")
            with self.assertRaises(ValueError):
                repository.advance_status(1, "test-order", expected_status=5)

    def test_repository_marks_wecom_preview_as_sent(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE orders (id INTEGER, order_number TEXT, "
                "wecom_preview_sent INTEGER DEFAULT 0, updated_at TEXT)"
            )
            connection.execute("INSERT INTO orders VALUES (1, 'test-order', 0, '')")
            repository = OrderRepository.__new__(OrderRepository)
            repository._lock = Lock()
            repository.connect = lambda: nullcontext(connection)
            repository.get = lambda order_id: dict(connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone())
            result = repository.mark_wecom_preview_sent(1, "test-order")
            self.assertEqual(result["wecom_preview_sent"], 1)
