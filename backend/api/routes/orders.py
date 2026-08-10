from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from backend.context import (
    order_repository,
    order_service,
    parse_and_publish,
    parse_order_payload,
)
from backend.responses import api_success
from backend.schemas import OrderData, ParseOrderRequest


router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def list_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order_number: str | None = None,
    transaction_id: str | None = None,
    shop: str | None = None,
):
    result = await run_in_threadpool(
        order_repository.list,
        limit,
        offset,
        order_number,
        transaction_id,
        shop,
    )
    return api_success(result, message="订单查询成功")


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
