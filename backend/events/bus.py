from __future__ import annotations

from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Lock
from typing import Any
from uuid import uuid4
import asyncio
import json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class EventBus:
    def __init__(self):
        self._lock = Lock()
        self._subscribers: dict[str, Queue] = {}

    def publish(self, event_type: str, data: dict[str, Any] | None = None):
        event = {
            "id": uuid4().hex,
            "type": event_type,
            "created_at": utc_now(),
            "data": data or {},
        }
        with self._lock:
            queues = list(self._subscribers.values())
        for queue in queues:
            queue.put(event)
        return event

    def subscribe(self):
        subscriber_id = uuid4().hex
        queue: Queue = Queue()
        with self._lock:
            self._subscribers[subscriber_id] = queue
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: str):
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    async def stream(self):
        subscriber_id, queue = self.subscribe()
        try:
            yield self.format_sse(
                self.publish(
                    "connection.opened",
                    {"message": "SSE connected"},
                )
            )
            while True:
                try:
                    event = await asyncio.to_thread(queue.get, True, 15)
                except Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield self.format_sse(event)
        finally:
            self.unsubscribe(subscriber_id)

    @staticmethod
    def format_sse(event: dict[str, Any]):
        return (
            f"id: {event['id']}\n"
            f"event: {event['type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        )


event_bus = EventBus()
