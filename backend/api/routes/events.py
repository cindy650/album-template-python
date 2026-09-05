from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.events import event_bus
from backend.responses import api_success
from backend.schemas import SSEMessageRequest


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


@router.post(
    "/events/send",
    summary="发送 SSE 消息",
    description=(
        "接收 type/event_type、msg、data、status、status_text、order_id 等字段，"
        "并立即向当前所有 SSE 订阅者广播对应类型的事件。"
    ),
)
async def send_sse_message(payload: SSEMessageRequest):
    event_data = dict(payload.data)
    for key in ("status", "status_text", "order_id"):
        value = getattr(payload, key)
        if value is not None:
            event_data[key] = value
    event = event_bus.publish(
        payload.event_type,
        data=event_data,
        msg=payload.msg,
    )
    return api_success(event, message="SSE 消息发送成功")
