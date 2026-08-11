from fastapi import APIRouter, HTTPException, Query, status as http_status
from starlette.concurrency import run_in_threadpool

from backend.context import (
    order_repository,
    order_print_image_generator,
    order_service,
    parse_and_publish,
    parse_order_payload,
)
from backend.responses import api_success
from backend.schemas import (
    OrderData,
    OrderPrintImageRequest,
    OrderStatusUpdate,
    ParseOrderRequest,
)


router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def list_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order_number: str | None = None,
    transaction_id: str | None = None,
    shop: str | None = None,
    shop_id: int | None = None,
    status: str | None = None,
):
    try:
        result = await run_in_threadpool(
            order_repository.list,
            limit,
            offset,
            order_number,
            transaction_id,
            shop,
            shop_id,
            status,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单查询成功")


@router.patch("/{order_id}/status")
async def update_order_status(order_id: int, payload: OrderStatusUpdate):
    try:
        result = await run_in_threadpool(
            order_repository.update_status,
            order_id,
            payload.status,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单状态更新成功")


@router.post("/print-image")
async def generate_order_print_image(payload: OrderPrintImageRequest):
    try:
        result = await run_in_threadpool(
            order_print_image_generator.generate,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="A4 订单打印图片生成成功")


@router.post("/parse")
async def parse_order(payload: ParseOrderRequest):
    result = await run_in_threadpool(parse_order_payload, payload)
    return api_success(
        OrderData.model_validate(result).model_dump(by_alias=True),
        message="订单解析成功",
    )


@router.post("/publish")
async def publish_order(order: OrderData):
    result = await run_in_threadpool(
        order_service.publish_order,
        order.model_dump(by_alias=True),
        "api",
        None,
        {},
    )
    return api_success(result, message="订单写入成功")


@router.post("/parse-and-publish")
async def parse_and_publish_order(payload: ParseOrderRequest):
    result = await run_in_threadpool(parse_and_publish, payload)
    return api_success(result, message="订单解析并写入成功")
