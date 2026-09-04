import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.events.bus import EventBus
from backend.schemas import SSEMessageRequest


def load_events_route_module():
    path = Path(__file__).parents[1] / "backend" / "api" / "routes" / "events.py"
    spec = importlib.util.spec_from_file_location("events_route_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SSEMessageTests(unittest.TestCase):
    def test_send_route_is_registered(self):
        route_module = load_events_route_module()
        routes = {
            (route.path, frozenset(route.methods or []))
            for route in route_module.router.routes
        }
        self.assertIn(("/events/send", frozenset({"POST"})), routes)

    def test_send_endpoint_broadcasts_msg_to_current_subscribers(self):
        route_module = load_events_route_module()
        bus = EventBus()
        subscriber_id, queue = bus.subscribe()
        try:
            with patch.object(route_module, "event_bus", bus):
                response = asyncio.run(
                    route_module.send_sse_message(
                        SSEMessageRequest(msg="  hello SSE  ")
                    )
                )

            event = queue.get_nowait()
            body = json.loads(response.body)
            self.assertEqual(event["type"], "message")
            self.assertEqual(event["msg"], "hello SSE")
            self.assertEqual(event["data"], {})
            self.assertEqual(body["message"], "SSE 消息发送成功")
            self.assertEqual(body["data"], event)
        finally:
            bus.unsubscribe(subscriber_id)

    def test_blank_msg_is_rejected(self):
        with self.assertRaises(ValidationError):
            SSEMessageRequest(msg="   ")

    def test_sse_format_contains_message_event_and_msg(self):
        bus = EventBus()
        event = bus.publish("message", msg="测试消息")

        formatted = bus.format_sse(event)

        self.assertIn("event: message\n", formatted)
        self.assertIn('"msg": "测试消息"', formatted)


if __name__ == "__main__":
    unittest.main()
