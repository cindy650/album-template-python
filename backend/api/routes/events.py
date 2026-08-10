from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.events import event_bus


router = APIRouter(tags=["events"])


@router.get("/events")
async def stream_events():
    return StreamingResponse(
        event_bus.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
