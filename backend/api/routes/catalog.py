from __future__ import annotations

from copy import deepcopy
from pathlib import Path as FilePath
from typing import Any, Callable
import tempfile

try:
    import pymysql
    MYSQL_INTEGRITY_ERROR = pymysql.IntegrityError
    MYSQL_OPERATIONAL_ERROR = pymysql.OperationalError
except ImportError:  # pragma: no cover - dependency error is reported at startup
    MYSQL_INTEGRITY_ERROR = ()
    MYSQL_OPERATIONAL_ERROR = ()

from fastapi import APIRouter, File, Form, HTTPException, Path, Query, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.context import catalog_repository, file_storage_service
from backend.responses import api_success
from backend.schemas import (
    FontCreate,
    FontUpdate,
    ProductCreate,
    ProductUpdate,
    ShopCreate,
    ShopUpdate,
    SizeTemplateCreateV2,
    SizeTemplateUpdateV2,
)


router = APIRouter(tags=["店铺与模板"])

FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".woff", ".woff2", ".fon"}
MAX_FONT_BYTES = 50 * 1024 * 1024


def payload_dict(payload: BaseModel):
    return payload.model_dump(exclude_unset=True)


def public_size_template_payload(value: Any):
    """Return the redesigned size-template contract without legacy storage fields."""
    if isinstance(value, list):
        return [
            public_size_template_payload(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    data = deepcopy(value)
    if "items" in data and isinstance(data["items"], list):
        data["items"] = [
            public_size_template_payload(item)
            for item in data["items"]
        ]
        return data
    if "size_template" in data and isinstance(data["size_template"], dict):
        data["size_template"] = public_size_template_payload(data["size_template"])
        return data
    if "id" not in data or "shop_id" not in data:
        return data
    keys = (
        "id",
        "shop_id",
        "shop",
        "shop_name",
        "product_id",
        "name",
        "template_name",
        "preview_image",
        "background_color",
        "paper_thickness_mm",
        "min_spine_width",
        "max_spine_width",
        "spine_width_basis",
        "product_spine_width_formula",
        "product_spine_width_page_rules",
        "cover_safe_distance",
        "spine_safe_distance",
        "back_cover_safe_distance",
        "selected_size_option_id",
        "selected_font_layout_id",
        "display_unit",
        "page_count",
        "page_count_options",
        "size_options",
        "size_template_info",
        "created_at",
        "updated_at",
    )
    return {
        key: deepcopy(data.get(key))
        for key in keys
    }


async def repo_call(func: Callable[..., Any], *args):
    try:
        return await run_in_threadpool(func, *args)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except MYSQL_INTEGRITY_ERROR as exc:
        if "products.name" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="产品分类名称已存在，请使用 PATCH /api/v1/products/{product_id} 更新该分类及其 product_names 数组",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"数据唯一性或关联约束冲突：{exc}",
        ) from exc
    except MYSQL_OPERATIONAL_ERROR as exc:
        detail = str(exc)
        if "1290" in detail or "LOCK_WRITE" in detail:
            detail = (
                "数据库当前处于只读锁定状态，暂时无法保存数据；"
                "请在 RDS 控制台解除 LOCK_WRITE 后重试"
            )
        else:
            detail = f"数据库暂时不可写，请稍后重试：{detail}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        ) from exc


@router.get("/shops", include_in_schema=False)
@router.get(
    "/shops/",
    summary="查询店铺列表",
    description=(
        "分页查询店铺，可按店铺 ID 精确查询或按店铺标识、店铺名搜索。"
        "返回 product_count、order_count、size_template_count、font_template_count，"
        "以及按订单状态统计的 new_order_count(0 新订单)、confirmation_count(1 确认中)、"
        "pending_production_count(2 待生产)、in_production_count(3 生产中)、"
        "pending_shipment_count(4 待发货)、completed_order_count(5 已完成)。"
    ),
)
async def list_shops(
    shop_id: int | None = Query(default=None, description="按店铺 ID 精确查询"),
    limit: int = Query(default=50, ge=1, le=500, description="每页返回的店铺数量"),
    offset: int = Query(default=0, ge=0, description="跳过的店铺数量"),
    search: str | None = Query(
        default=None,
        description="按店铺唯一标识或店铺名模糊搜索",
    ),
):
    if shop_id is not None:
        result = await repo_call(catalog_repository.get_shop, shop_id)
        return api_success(result, message="店铺查询成功")
    result = await repo_call(catalog_repository.list_shops, limit, offset, search)
    return api_success(result, message="店铺查询成功")


@router.post(
    "/shops",
    status_code=status.HTTP_201_CREATED,
    summary="创建店铺",
    description=(
        "创建一个店铺，可通过 products 商品名数组同时建立商品关联，"
        "并通过 wecom_robot_webhook_url 配置该店铺的企业微信机器人。"
    ),
)
async def create_shop(payload: ShopCreate):
    result = await repo_call(catalog_repository.create_shop, payload_dict(payload))
    return api_success(result, message="店铺创建成功", status_code=201)


@router.get(
    "/shops/{shop_id}",
    summary="查询店铺详情",
    description=(
        "根据店铺 ID 查询单个店铺的详细信息，包含各订单状态数量字段："
        "new_order_count、confirmation_count、pending_production_count、"
        "in_production_count、pending_shipment_count、completed_order_count。"
    ),
)
async def get_shop(shop_id: int = Path(description="店铺 ID")):
    result = await repo_call(catalog_repository.get_shop, shop_id)
    return api_success(result, message="店铺查询成功")


@router.post(
    "/fonts/upload",
    status_code=status.HTTP_201_CREATED,
    summary="上传字体",
    description=(
        "上传字体文件到 OSS 的 font_repository 目录并创建字体库记录；"
        "file_path 保存 OSS 地址，source 为空，metadata 为空对象。"
    ),
)
async def upload_font(
    file: UploadFile = File(description="TTF、OTF、TTC、WOFF、WOFF2 或 FON 字体文件"),
    font_name: str | None = Form(default=None, description="字体名称；默认取文件名"),
    font_family: str | None = Form(default=None, description="字体族；默认与字体名称相同"),
    post_script_name: str | None = Form(default=None, description="PostScript 字体名称"),
    enabled: bool = Form(default=True, description="字体是否启用"),
):
    filename = FilePath(file.filename or "").name.strip()
    suffix = FilePath(filename).suffix.lower()
    if not filename or suffix not in FONT_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="字体文件只支持 TTF、OTF、TTC、WOFF、WOFF2 或 FON",
        )
    resolved_name = str(font_name or filename).strip()
    resolved_family = str(font_family or resolved_name).strip()
    if not resolved_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="字体名称不能为空",
        )
    content = await file.read(MAX_FONT_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="字体文件不能为空",
        )
    if len(content) > MAX_FONT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="字体文件不能超过 50MB",
        )

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="font-upload-",
            suffix=suffix,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = FilePath(temporary_file.name)
        object_key = f"font_repository/{filename}"
        uploaded = await run_in_threadpool(
            file_storage_service.upload_file,
            temporary_path,
            object_key,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if not uploaded.get("ok") or not uploaded.get("url"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "字体上传 OSS 失败："
                f"{uploaded.get('error') or uploaded.get('status') or '未知错误'}"
            ),
        )
    result = await repo_call(
        catalog_repository.create_font,
        {
            "font_name": resolved_name,
            "font_family": resolved_family,
            "post_script_name": str(post_script_name or "").strip(),
            "file_path": uploaded["url"],
            "source": "",
            "enabled": enabled,
            "metadata": {},
        },
    )
    result["object_key"] = object_key
    return api_success(result, message="字体上传成功", status_code=201)


@router.patch(
    "/shops/{shop_id}",
    summary="更新店铺",
    description=(
        "根据店铺 ID 更新店铺信息；传入 products 时会替换店铺的全部商品关联；"
        "传入 wecom_robot_webhook_url 可设置或清除该店铺的企业微信机器人。"
    ),
)
async def update_shop(
    payload: ShopUpdate,
    shop_id: int = Path(description="店铺 ID"),
):
    result = await repo_call(
        catalog_repository.update_shop,
        shop_id,
        payload_dict(payload),
    )
    return api_success(result, message="店铺更新成功")


@router.delete(
    "/shops/{shop_id}",
    summary="删除店铺",
    description="根据店铺 ID 删除店铺；存在关联数据时将返回约束冲突。",
)
async def delete_shop(shop_id: int = Path(description="店铺 ID")):
    result = await repo_call(catalog_repository.delete_shop, shop_id)
    return api_success(result, message="店铺删除成功")


@router.get(
    "/products",
    summary="查询产品分类",
    description="按产品分类查询关联店铺、product_names 商品名、常用规格值、背脊模式和尺寸模板。",
)
async def list_products(
    limit: int = Query(default=50, ge=1, le=500, description="每页返回的产品分类数量"),
    offset: int = Query(default=0, ge=0, description="跳过的产品分类数量"),
    search: str | None = Query(default=None, description="按产品分类名称搜索"),
    shop_id: int | None = Query(default=None, gt=0, description="按关联店铺 ID 筛选"),
):
    result = await repo_call(catalog_repository.list_products, limit, offset, search, shop_id)
    return api_success(result, message="产品分类查询成功")


@router.post("/products", status_code=status.HTTP_201_CREATED, summary="创建产品分类", description="创建产品分类并关联 product_names 商品名、用于邮件自动匹配的 specifications、常用规格值及多个店铺；背脊模式通过 spine_width_mode 选择 range、formula 或 page_count_table；可同时提交三组产品安全距离。")
async def create_product(payload: ProductCreate):
    result = await repo_call(catalog_repository.create_product, payload_dict(payload))
    return api_success(result, message="产品分类创建成功", status_code=201)


@router.get("/products/{product_id}", summary="查询产品分类详情", description="查询产品分类、关联店铺、商品名、常用规格值、背脊模式和尺寸模板。")
async def get_product(product_id: int = Path(description="产品分类 ID")):
    result = await repo_call(catalog_repository.get_product, product_id)
    return api_success(result, message="产品分类查询成功")


@router.patch("/products/{product_id}", summary="更新产品分类及其关联", description="更新产品分类名称、商品名、specifications、常用规格值、背脊模式、背脊公式或按页数分段规则、产品安全距离和店铺关联；常用规格值传空数组可清空，安全距离对象不传则保持原值。")
async def update_product(payload: ProductUpdate, product_id: int = Path(description="产品分类 ID")):
    result = await repo_call(catalog_repository.update_product, product_id, payload_dict(payload))
    return api_success(result, message="产品分类更新成功")


@router.delete("/products/{product_id}", summary="删除产品分类", description="删除产品分类及其店铺、商品名关联。")
async def delete_product(product_id: int = Path(description="产品分类 ID")):
    result = await repo_call(catalog_repository.delete_product, product_id)
    return api_success(result, message="产品分类删除成功")


@router.get(
    "/size-templates",
    summary="查询尺寸模板列表",
    description="按产品分类查询尺寸模板，也兼容按店铺和订单商品名筛选。",
)
async def list_size_templates(
    limit: int = Query(default=50, ge=1, le=500, description="每页返回的尺寸模板数量"),
    offset: int = Query(default=0, ge=0, description="跳过的尺寸模板数量"),
    shop_id: int | None = Query(default=None, description="按所属店铺 ID 筛选"),
    product_name: str | None = Query(
        default=None,
        description="按关联的订单商品名筛选",
    ),
    product_id: int | None = Query(default=None, description="按产品分类 ID 筛选"),
):
    result = await repo_call(
        catalog_repository.list_size_templates,
        limit,
        offset,
        shop_id,
        product_name,
        product_id,
    )
    result = public_size_template_payload(result)
    return api_success(result, message="尺寸模板查询成功")


@router.post(
    "/size-templates",
    status_code=status.HTTP_201_CREATED,
    summary="创建尺寸模板",
    description=(
        "创建尺寸模板主记录；传入 size_options 时保存明确提交的初始规格，不传则规格为空；"
        "规格中的 layers 可作为规格自己的独立图层直接保存。安全距离包含 top、right、bottom、left 四个值。"
    ),
)
async def create_size_template(payload: SizeTemplateCreateV2):
    result = await repo_call(
        catalog_repository.create_size_template,
        payload_dict(payload),
    )
    result = public_size_template_payload(result)
    return api_success(result, message="尺寸模板创建成功", status_code=201)


@router.get(
    "/size-templates/{template_id}",
    summary="查询尺寸模板详情",
    description="根据尺寸模板 ID 查询尺寸数据、字体布局模板 ID 和摘要。",
)
async def get_size_template(template_id: int = Path(description="尺寸模板 ID")):
    result = await repo_call(
        catalog_repository.get_size_template_detail,
        template_id,
    )
    result = public_size_template_payload(result)
    return api_success(result, message="尺寸模板查询成功")


@router.patch(
    "/size-templates/{template_id}",
    summary="更新尺寸模板",
    description=(
        "根据尺寸模板 ID 更新模板及关联信息；背景色、背脊宽范围、背脊依据和三组安全距离使用顶层字段。"
        "size_options[].layers 可直接编辑规格图层；不传 layers 时保留已有规格图层。"
    ),
)
async def update_size_template(
    payload: SizeTemplateUpdateV2,
    template_id: int = Path(description="尺寸模板 ID"),
):
    result = await repo_call(
        catalog_repository.update_size_template,
        template_id,
        payload_dict(payload),
    )
    result = public_size_template_payload(result)
    return api_success(result, message="尺寸模板更新成功")


@router.delete(
    "/size-templates/{template_id}",
    summary="删除尺寸模板",
    description="根据尺寸模板 ID 删除模板及其关联记录。",
)
async def delete_size_template(template_id: int = Path(description="尺寸模板 ID")):
    result = await repo_call(catalog_repository.delete_size_template, template_id)
    return api_success(result, message="尺寸模板删除成功")


@router.get(
    "/fonts",
    summary="查询字体库列表",
    description="分页查询字体库，可按启用状态及 font_name、font_preferred、font_en、font_all_name 不区分大小写模糊搜索。",
)
async def list_fonts(
    limit: int = Query(default=100, ge=1, le=1000, description="每页返回的字体数量"),
    offset: int = Query(default=0, ge=0, description="跳过的字体数量"),
    enabled: bool | None = Query(default=None, description="按字体是否启用筛选"),
    search: str | None = Query(
        default=None,
        description="按 font_name、font_preferred、font_en 或 font_all_name 不区分大小写模糊搜索",
    ),
):
    result = await repo_call(
        catalog_repository.list_fonts,
        limit,
        offset,
        enabled,
        search,
    )
    total = int(result["total"])
    return api_success(
        result,
        message="字体库查询成功",
        pagination={
            "total": total,
            "limit": limit,
            "offset": offset,
            "page": offset // limit + 1,
            "total_pages": (total + limit - 1) // limit,
        },
    )


@router.post(
    "/fonts",
    status_code=status.HTTP_201_CREATED,
    summary="创建字体",
    description="向字体库新增字体文件及其来源、启用状态和扩展信息。",
)
async def create_font(payload: FontCreate):
    result = await repo_call(catalog_repository.create_font, payload_dict(payload))
    return api_success(result, message="字体创建成功", status_code=201)


@router.get(
    "/fonts/{font_id}",
    summary="查询字体详情",
    description="根据字体 ID 查询字体库中的单个字体。",
)
async def get_font(font_id: int = Path(description="字体 ID")):
    result = await repo_call(catalog_repository.get_font, font_id)
    return api_success(result, message="字体查询成功")


@router.patch(
    "/fonts/{font_id}",
    summary="更新字体",
    description=(
        "根据字体 ID 编辑字体名称、字体族、OSS 文件地址或启用状态；"
        "只修改请求体中提供的字段。"
    ),
)
async def update_font(
    payload: FontUpdate,
    font_id: int = Path(description="字体 ID"),
):
    result = await repo_call(
        catalog_repository.update_font,
        font_id,
        payload_dict(payload),
    )
    return api_success(result, message="字体更新成功")


@router.delete(
    "/fonts/{font_id}",
    summary="删除字体",
    description="根据字体 ID 删除字体；仍被字体模板引用时将返回约束冲突。",
)
async def delete_font(font_id: int = Path(description="字体 ID")):
    result = await repo_call(catalog_repository.delete_font, font_id)
    return api_success(result, message="字体删除成功")
