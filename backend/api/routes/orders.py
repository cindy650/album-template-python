from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Query, Response, status as http_status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.context import (
    order_repository,
    order_print_image_generator,
    order_service,
    template_export_service,
    parse_and_publish,
    parse_order_payload,
)
from backend.responses import api_success
from backend.templates.exporter import stream_and_close
from backend.schemas import (
    OrderData,
    OrderPrintImageRequest,
    OrderStatusAdvanceRequest,
    OrderStatusUpdate,
    OrderTemplateJsonUpdate,
    ParseOrderRequest,
)


router = APIRouter(prefix="/orders", tags=["订单"])


@router.get(
    "/statuses",
    summary="查询订单状态配置",
    description="从独立订单状态表读取状态数值、状态名称和前端操作按钮文案。",
)
async def list_order_statuses():
    result = await run_in_threadpool(order_repository.list_statuses)
    return api_success(result, message="订单状态配置查询成功")


@router.get(
    "",
    summary="查询订单列表",
    description=(
        "从第 1 页开始分页查询订单，可按订单号、交易编号、店铺、店铺 ID 和状态值筛选。"
        "同一 Etsy 订单号可能返回多条商品项；请使用每条记录的 id 操作模板、状态和导出。"
        "响应包含当前页 pages、每页数量 limit、总记录数 total 和总页数 total_pages。"
        "每条订单都会返回 status、status_text 和 status_button_text。"
    ),
    operation_id="查询订单列表",
)
async def list_orders(
    limit: int = Query(default=50, ge=1, le=500, description="每页订单数量"),
    pages: int = Query(default=1, ge=1, description="页码，从第 1 页开始"),
    order_number: str | None = Query(default=None, description="按订单号精确筛选"),
    transaction_id: str | None = Query(default=None, description="按交易编号精确筛选"),
    shop: str | None = Query(default=None, description="按店铺名称模糊筛选"),
    shop_id: int | None = Query(default=None, description="按店铺 ID 筛选"),
    status: int | None = Query(
        default=None,
        ge=0,
        le=5,
        description=(
            "按状态值筛选：0 新订单、1 示意图已发送/客户确认中、2 待生产、"
            "3 生产中、4 待发货、5 订单已完成"
        ),
    ),
):
    try:
        result = await run_in_threadpool(
            order_repository.list,
            limit,
            pages,
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


@router.get(
    "/monthly-statistics",
    summary="查询店铺月累计订单统计",
    description="返回入库时累计的 CAD 金额、运费、订单数和商品件数；不会重新计算历史订单。",
)
async def list_monthly_statistics(
    shop_id: int | None = Query(default=None, gt=0),
    stat_month: str | None = Query(default=None, description="月份，YYYY-MM"),
):
    try:
        result = await run_in_threadpool(
            order_repository.monthly_statistics, shop_id, stat_month
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return api_success(result, message="月统计查询成功")


@router.get("/daily-statistics", summary="查询店铺日累计订单统计")
async def list_daily_statistics(
    shop_id: int | None = Query(default=None, gt=0),
    stat_date: str | None = Query(default=None, description="日期，YYYY-MM-DD"),
):
    try:
        result = await run_in_threadpool(order_repository.daily_statistics, shop_id, stat_date)
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return api_success(result, message="日统计查询成功")


@router.patch(
    "/{order_id}/status",
    deprecated=True,
    summary="直接设置订单状态（兼容接口）",
    description="旧版兼容接口：根据订单 ID 直接设置指定状态值。新流程请使用订单状态推进接口。",
)
async def update_order_status(
    payload: OrderStatusUpdate,
    order_id: int = Path(gt=0, description="本地订单 ID"),
):
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


@router.post(
    "/status/advance",
    summary="推进订单状态",
    description=(
        "根据订单 ID 和订单号校验订单，并按状态表配置推进到下一状态。"
        "当前状态为 1（客户已确认）时，会先重新生成并覆盖 SVG 和 A4 生产单的"
        "本地文件及 OSS 文件，全部成功后才推进到状态 2。"
        "订单已完成后不能继续推进。"
    ),
    operation_id="推进订单状态",
)
async def advance_order_status(payload: OrderStatusAdvanceRequest):
    try:
        result = await run_in_threadpool(
            order_service.advance_status,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单状态推进成功")


@router.put(
    "/{order_id}/template-json",
    summary="保存订单模板 JSON",
    description=(
        "根据订单 ID 和订单号校验订单，将前端编辑后的完整模板 JSON 保存回订单。"
        "后续订单预览和生产导出会读取保存后的 JSON。"
    ),
)
async def save_order_template_json(
    payload: OrderTemplateJsonUpdate,
    order_id: int = Path(gt=0, description="本地订单 ID"),
):
    try:
        result = await run_in_threadpool(
            order_repository.save_template_json,
            order_id,
            payload.order_number,
            payload.template_json,
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
    return api_success(result, message="订单模板 JSON 保存成功")


@router.post(
    "/preview-images/send",
    summary="生成并发送订单示意图",
    description=(
        "根据订单当前关联的商品模板生成订单预览图和企业微信订单辅助图，"
        "发送到企业微信；两张图发送成功后将订单状态更新为 1。"
        "商品未关联模板或企业微信发送失败时状态保持不变。"
    ),
)
async def send_order_preview_images(payload: OrderPrintImageRequest):
    try:
        result = await run_in_threadpool(
            order_service.send_preview_images,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单示意图已发送")


@router.post(
    "/print-image",
    deprecated=True,
    summary="生成 A4 订单打印图（兼容接口）",
    description="旧版兼容接口：根据订单 ID 和订单号生成 A4 订单打印图片。生产流程请使用订单生产文件导出接口。",
)
async def generate_order_print_image(payload: OrderPrintImageRequest):
    try:
        result = await run_in_threadpool(
            order_print_image_generator.generate,
            payload.order_id,
            payload.order_number,
            upload_to_oss=False,
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


@router.post(
    "/regenerate-local",
    summary="重新生成订单全部文件（仅本地）",
    description=(
        "根据订单当前保存的模板和图层 JSON，重新生成预览图、企业微信辅助图、"
        "SVG、转曲 SVG 和 A4 生产单。仅写入 generated_orders，不发送企业微信，"
        "不上传 OSS，也不修改订单状态。"
    ),
)
async def regenerate_order_artifacts_local(payload: OrderPrintImageRequest):
    try:
        result = await run_in_threadpool(
            template_export_service.generate_order_artifacts,
            payload.order_id,
            payload.order_number,
            update_status=False,
            upload_to_oss=False,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单全部文件已重新生成并保存到本地")


@router.post(
    "/template-export",
    summary="下载订单 OSS 文件夹",
    description=(
        "读取订单已经生成并上传到 OSS 文件夹中的全部文件，"
        "由后端临时打包为 ZIP 并直接返回下载；不重新生成文件、不上传 ZIP，"
        "也不修改订单状态。"
    ),
)
@router.post(
    "/export-template",
    deprecated=True,
    summary="下载订单 OSS 文件夹（兼容地址）",
    description="订单生产文件导出的旧版兼容地址，功能与 /api/v1/orders/template-export 相同。",
)
async def export_order_template(payload: OrderPrintImageRequest):
    try:
        archive, filename = await run_in_threadpool(
            template_export_service.package_for_download,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        print(
            f"[DOWNLOAD] 下载失败：订单ID={payload.order_id}，"
            f"订单号={payload.order_number}，错误={type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (RuntimeError, ValueError) as exc:
        print(
            f"[DOWNLOAD] 下载失败：订单ID={payload.order_id}，"
            f"订单号={payload.order_number}，错误={type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    fallback_filename = f"order-{payload.order_id}.zip"
    encoded_filename = quote(filename)
    return StreamingResponse(
        stream_and_close(archive),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{encoded_filename}"
            )
        },
    )


@router.post(
    "/template-export/download",
    summary="下载订单生产文件",
    description=(
        "兼容下载地址，功能与 /api/v1/orders/template-export 相同。"
    ),
)
async def download_order_template(payload: OrderPrintImageRequest):
    try:
        archive, filename = await run_in_threadpool(
            template_export_service.package_for_download,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    fallback_filename = f"order-{payload.order_id}.zip"
    return StreamingResponse(
        stream_and_close(archive),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.get(
    "/{order_id}/artifacts",
    summary="查询订单生产文件记录",
    description="查询订单目录中已生成文件的本地路径、OSS 对象键、OSS 地址和上传状态。",
)
async def list_order_artifacts(
    order_id: int = Path(gt=0, description="本地订单 ID"),
    order_number: str | None = Query(default=None, description="可选订单号校验"),
):
    try:
        result = await run_in_threadpool(
            order_repository.list_artifacts,
            order_id,
            order_number,
        )
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return api_success(result, message="订单生产文件记录查询成功")


@router.post(
    "/parse",
    summary="解析订单邮件",
    description="解析前端提交的邮件标题和正文，返回结构化订单字段，但不写入数据库。",
)
async def parse_order(payload: ParseOrderRequest):
    result = await run_in_threadpool(parse_order_payload, payload)
    return api_success(
        OrderData.model_validate(result).model_dump(by_alias=True),
        message="订单解析成功",
    )


@router.post(
    "/publish",
    summary="写入结构化订单",
    description="将前端提交的结构化订单写入 MySQL 数据库。",
)
async def publish_order(order: OrderData):
    try:
        result = await run_in_threadpool(
            order_service.publish_order,
            order.model_dump(by_alias=True),
            "api",
            None,
            {},
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单写入成功")


@router.post(
    "/parse-and-publish",
    summary="解析并写入订单",
    description="解析邮件订单后，将结果写入 MySQL 数据库。",
)
async def parse_and_publish_order(payload: ParseOrderRequest):
    result = await run_in_threadpool(parse_and_publish, payload)
    return api_success(result, message="订单解析并写入成功")
