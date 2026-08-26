from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.events import event_bus


router = APIRouter(tags=["事件"])


@router.get(
    "/events",
    summary="订阅实时事件",
    description="通过服务器发送事件（SSE）长连接订阅订单、邮箱监听和后台任务的实时消息。",
)
async def stream_events():
    return StreamingResponse(
        event_bus.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
