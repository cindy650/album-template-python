from fastapi import APIRouter, HTTPException, Path, Query, Response, status as http_status
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
from backend.schemas import (
    OrderData,
    OrderPrintImageRequest,
    OrderStatusAdvanceRequest,
    OrderStatusUpdate,
    OrderTemplateExportRequest,
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
        "订单已完成后不能继续推进。"
    ),
    operation_id="推进订单状态",
)
async def advance_order_status(payload: OrderStatusAdvanceRequest):
    try:
        result = await run_in_threadpool(
            order_repository.advance_status,
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
    "/template-export",
    summary="确认生产并导出订单文件",
    description=(
        "根据订单和模板固定生成预览图、SVG、企业微信辅助图和 A4 生产单。"
        "四个文件全部保存成功后统一上传 OSS；不生成源文件或上传 ZIP。"
        "前端未传 template_json 时使用订单关联的数据库模板。"
    ),
)
@router.post(
    "/export-template",
    deprecated=True,
    summary="确认生产并导出订单文件（兼容地址）",
    description="订单生产文件导出的旧版兼容地址，功能与 /api/v1/orders/template-export 相同。",
)
async def export_order_template(payload: OrderTemplateExportRequest):
    """确认生产并返回订单文件及 OSS 地址，不在生产阶段打包。"""
    try:
        result = await run_in_threadpool(
            template_export_service.export,
            payload.order_id,
            payload.order_number,
            payload.file_format,
            payload.template_json,
        )
    except LookupError as exc:
        print(
            f"[EXPORT] 导出失败：订单ID={payload.order_id}，订单号={payload.order_number}，"
            f"格式={payload.file_format}，错误={type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (RuntimeError, ValueError) as exc:
        print(
            f"[EXPORT] 导出失败：订单ID={payload.order_id}，订单号={payload.order_number}，"
            f"格式={payload.file_format}，错误={type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return api_success(result, message="订单已确认生产，订单文件已上传 OSS")


@router.post(
    "/template-export/download",
    summary="下载订单生产文件",
    description=(
        "读取订单目录中已经生成的预览图、企业微信辅助图、A4 生产单和 SVG，"
        "仅在本次下载请求中临时打包为 ZIP 返回；不会把 ZIP 保存或上传到 OSS。"
    ),
)
async def download_order_template(payload: OrderPrintImageRequest):
    try:
        content, filename = await run_in_threadpool(
            template_export_service.package_for_download,
            payload.order_id,
            payload.order_number,
        )
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
