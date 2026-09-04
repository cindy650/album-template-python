from __future__ import annotations

from pathlib import Path as FilePath
import tempfile
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Path, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool

from backend.api.routes.catalog import (
    payload_dict,
    public_size_template_payload,
    repo_call,
)
from backend.context import catalog_repository, file_storage_service
from backend.responses import api_success
from backend.schemas import (
    FontLayoutLibraryCreate,
    FontLayoutLibrarySaveAs,
    FontLayoutLibraryUpdate,
    FontLayoutSizeSync,
    InnerPageTemplateCreate,
    InnerPageTemplateOption,
    InnerPageTemplateOptionUpdate,
    InnerPageTemplateUpdate,
    TextGenerationRuleCreate,
    TextGenerationRuleUpdate,
)


router = APIRouter(tags=["尺寸模板与字体布局"])

PREVIEW_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_PREVIEW_BYTES = 10 * 1024 * 1024


@router.get(
    "/text-generation-rules",
    summary="查询文字生成规则",
    description="分页查询文字生成规则，供前端下拉选择；可按名称或描述搜索。",
)
async def list_text_generation_rules(
    limit: int = Query(default=100, ge=1, le=500, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="跳过数量"),
    search: str | None = Query(default=None, description="按名称或描述搜索"),
):
    result = await repo_call(
        catalog_repository.list_text_generation_rules,
        limit,
        offset,
        search,
    )
    return api_success(result, message="文字生成规则查询成功")


@router.post(
    "/text-generation-rules",
    status_code=status.HTTP_201_CREATED,
    summary="新增文字生成规则",
)
async def create_text_generation_rule(payload: TextGenerationRuleCreate):
    result = await repo_call(
        catalog_repository.create_text_generation_rule,
        payload_dict(payload),
    )
    return api_success(result, message="文字生成规则创建成功", status_code=201)


@router.get(
    "/text-generation-rules/{rule_id}",
    summary="查询文字生成规则详情",
)
async def get_text_generation_rule(rule_id: int = Path(gt=0, description="文字生成规则 ID")):
    result = await repo_call(catalog_repository.get_text_generation_rule, rule_id)
    return api_success(result, message="文字生成规则查询成功")


@router.patch(
    "/text-generation-rules/{rule_id}",
    summary="编辑文字生成规则",
    description="只更新传入的字段；name 和 description 均可编辑。",
)
async def update_text_generation_rule(
    payload: TextGenerationRuleUpdate,
    rule_id: int = Path(gt=0, description="文字生成规则 ID"),
):
    result = await repo_call(
        catalog_repository.update_text_generation_rule,
        rule_id,
        payload_dict(payload),
    )
    return api_success(result, message="文字生成规则更新成功")


def public_font_layout_template(value):
    if isinstance(value, list):
        return [public_font_layout_template(item) for item in value]
    if not isinstance(value, dict):
        return value
    data = dict(value)
    if isinstance(data.get("items"), list):
        data["items"] = [public_font_layout_template(item) for item in data["items"]]
        return data
    path = str(data.pop("preview_image_path", "") or "").strip()
    data["preview_image"] = path or None
    return data


def public_inner_page_template(value):
    if isinstance(value, list):
        return [public_inner_page_template(item) for item in value]
    if not isinstance(value, dict):
        return value
    data = dict(value)
    if isinstance(data.get("items"), list):
        data["items"] = [public_inner_page_template(item) for item in data["items"]]
        return data
    path = str(
        data.pop("preview_image_path", data.get("preview_image", "")) or ""
    ).strip()
    data["preview_image"] = path or None
    return data


@router.put(
    "/size-templates/{template_id}/preview",
    summary="上传尺寸模板预览图",
    description=(
        "上传 PNG、JPG 或 WEBP 尺寸模板预览图到 OSS 的 font_layout_image 目录；"
        "数据库只保存 OSS 访问链接。"
    ),
)
async def upload_size_template_preview(
    file: UploadFile = File(description="PNG、JPG 或 WEBP 预览图"),
    template_id: int = Path(gt=0, description="尺寸模板 ID"),
):
    await repo_call(catalog_repository.get_size_template, template_id)
    suffix = FilePath(file.filename or "").suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预览图只支持 PNG、JPG、JPEG 或 WEBP",
        )
    content = await file.read(MAX_PREVIEW_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预览图不能为空",
        )
    if len(content) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="预览图不能超过 10MB",
        )

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="size-template-preview-",
            suffix=suffix,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = FilePath(temporary_file.name)
        object_key = (
            f"font_layout_image/size-template-{template_id}-"
            f"{uuid4().hex}{suffix}"
        )
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
                "尺寸模板预览图上传 OSS 失败："
                f"{uploaded.get('error') or uploaded.get('status') or '未知错误'}"
            ),
        )
    result = await repo_call(
        catalog_repository.update_size_template,
        template_id,
        {"preview_image": uploaded["url"]},
    )
    return api_success(
        public_size_template_payload(result),
        message="尺寸模板预览图上传成功",
    )


@router.get(
    "/inner-page-templates",
    summary="查询内页模板列表",
    description="按店铺、产品、名称或描述查询可复用的内页图层模板。",
)
async def list_inner_page_templates(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    shop_id: int | None = Query(default=None),
    product_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
):
    result = await repo_call(
        catalog_repository.list_inner_page_templates,
        limit,
        offset,
        shop_id,
        product_id,
        search,
    )
    return api_success(public_inner_page_template(result), message="内页模板查询成功")


@router.post(
    "/inner-page-templates",
    status_code=status.HTTP_201_CREATED,
    summary="新增内页模板",
    description="新增内页图层模板；shop_id、product_id 和 size_options 必填，图层按规格保存。",
)
async def create_inner_page_template(payload: InnerPageTemplateCreate):
    result = await repo_call(
        catalog_repository.create_inner_page_template,
        payload_dict(payload),
    )
    return api_success(
        public_inner_page_template(result),
        message="内页模板创建成功",
        status_code=201,
    )


@router.get(
    "/inner-page-templates/{template_id}",
    summary="查询内页模板详情",
)
async def get_inner_page_template(template_id: int = Path(gt=0)):
    result = await repo_call(catalog_repository.get_inner_page_template, template_id)
    return api_success(public_inner_page_template(result), message="内页模板查询成功")


@router.patch(
    "/inner-page-templates/{template_id}",
    summary="更新内页模板",
    description="编辑时 shop_id、product_id 必填且可更换关联产品；不传 size_options 时保留原规格图层。",
)
async def update_inner_page_template(
    payload: InnerPageTemplateUpdate,
    template_id: int = Path(gt=0),
):
    result = await repo_call(
        catalog_repository.update_inner_page_template,
        template_id,
        payload_dict(payload),
    )
    return api_success(public_inner_page_template(result), message="内页模板更新成功")


@router.delete(
    "/inner-page-templates/{template_id}",
    summary="删除内页模板",
)
async def delete_inner_page_template(template_id: int = Path(gt=0)):
    result = await repo_call(catalog_repository.delete_inner_page_template, template_id)
    return api_success(result, message="内页模板删除成功")


@router.post(
    "/inner-page-templates/{template_id}/size-options",
    status_code=status.HTTP_201_CREATED,
    summary="新增内页模板规格",
    description="只新增一个规格，不读取或覆盖该内页模板的其他规格。",
)
async def create_inner_page_template_option(
    payload: InnerPageTemplateOption,
    template_id: int = Path(gt=0, description="内页模板 ID"),
):
    result = await repo_call(
        catalog_repository.create_inner_page_template_option,
        template_id,
        payload_dict(payload),
    )
    return api_success(result, message="内页模板规格创建成功", status_code=201)


@router.patch(
    "/inner-page-templates/{template_id}/size-options/{size_option_id}",
    summary="保存单个内页模板规格",
    description=(
        "只更新路径指定的规格；label、size_unit、layers 均可单独传入，"
        "未传字段保持原值。size_option_id 不可通过该接口修改。"
    ),
)
async def update_inner_page_template_option(
    payload: InnerPageTemplateOptionUpdate,
    template_id: int = Path(gt=0, description="内页模板 ID"),
    size_option_id: str = Path(min_length=1, description="规格业务 ID，前端需进行 URL 编码"),
):
    result = await repo_call(
        catalog_repository.update_inner_page_template_option,
        template_id,
        size_option_id,
        payload_dict(payload),
    )
    return api_success(result, message="内页模板规格保存成功")


@router.delete(
    "/inner-page-templates/{template_id}/size-options/{size_option_id}",
    summary="删除单个内页模板规格",
    description="只删除路径指定的规格，不删除主模板或其他规格。",
)
async def delete_inner_page_template_option(
    template_id: int = Path(gt=0, description="内页模板 ID"),
    size_option_id: str = Path(min_length=1, description="规格业务 ID，前端需进行 URL 编码"),
):
    result = await repo_call(
        catalog_repository.delete_inner_page_template_option,
        template_id,
        size_option_id,
    )
    return api_success(result, message="内页模板规格删除成功")


@router.get(
    "/font-layout-templates",
    summary="查询字体布局模板列表",
    description="按产品、店铺、名称或 sort_key 查询模板库；非空 sort_key 优先升序排列。",
)
async def list_font_layout_templates(
    limit: int = Query(default=50, ge=1, le=500, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="跳过数量"),
    shop_id: int | None = Query(default=None, description="所属店铺 ID"),
    search: str | None = Query(default=None, description="按名称或 sort_key 搜索"),
    product_id: int | None = Query(default=None, description="所属产品分类 ID"),
):
    result = await repo_call(
        catalog_repository.list_font_layout_library_templates,
        limit,
        offset,
        shop_id,
        search,
        product_id,
    )
    return api_success(
        public_font_layout_template(result),
        message="字体布局模板查询成功",
    )


@router.post(
    "/font-layout-templates",
    status_code=status.HTTP_201_CREATED,
    summary="新增字体布局模板",
    description="创建独立字体布局模板；sort_key 用于排序和搜索，图层以完整文档对象保存。",
)
async def create_font_layout_template(payload: FontLayoutLibraryCreate):
    result = await repo_call(
        catalog_repository.create_font_layout_library_template,
        payload_dict(payload),
    )
    return api_success(
        public_font_layout_template(result),
        message="字体布局模板创建成功",
        status_code=201,
    )


@router.get(
    "/font-layout-templates/{template_id}",
    summary="查询字体布局模板详情",
    description=(
        "读取字体布局模板详情；一个尺寸模板同一时间只使用一个字体布局。"
        "传 size_template_id 可查看当前使用状态，再传 size_option_id 则优先返回规格图层，"
        "没有规格图层时返回基础图层并给出回退提示。"
    ),
)
async def get_font_layout_template(
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
    size_template_id: int | None = Query(
        default=None,
        gt=0,
        description="可选的尺寸模板 ID；传入后返回各规格同步状态",
    ),
    size_option_id: str | None = Query(
        default=None,
        min_length=1,
        description="可选的当前规格 ID；需和 size_template_id 一起传入",
    ),
):
    result = await repo_call(
        catalog_repository.get_font_layout_library_template,
        template_id,
        size_template_id,
        size_option_id,
    )
    message = result.get("message") or "字体布局模板查询成功"
    return api_success(
        public_font_layout_template(result),
        message=message,
    )


@router.get(
    "/font-layout-templates/{template_id}/size-options",
    summary="查询字体布局规格同步状态",
    description=(
        "列出尺寸模板中的全部规格，并提示当前字体布局是否为尺寸模板正在使用的唯一布局；"
        "未同步规格会回退基础图层。"
    ),
)
async def get_font_layout_size_option_status(
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
    size_template_id: int = Query(gt=0, description="尺寸模板 ID"),
):
    result = await repo_call(
        catalog_repository.get_font_layout_size_option_status,
        template_id,
        size_template_id,
    )
    return api_success(result, message=result["message"])


@router.post(
    "/font-layout-templates/{template_id}/sync-size-options",
    summary="同步字体布局到尺寸规格",
    description=(
        "把当前字体布局的图层复制到一个或多个尺寸模板规格的独立图层；"
        "同步后规格图层可直接编辑，不再依赖字体布局模板；"
        "接口只处理请求体 items 中传入的规格，不会重新同步其他规格；"
        "同时保留历史关联数据以兼容现有客户端。"
    ),
)
async def sync_font_layout_size_options(
    payload: FontLayoutSizeSync,
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
):
    result = await repo_call(
        catalog_repository.sync_font_layout_size_options,
        template_id,
        payload_dict(payload),
    )
    return api_success(result, message=result["message"])


@router.delete(
    "/font-layout-templates/{template_id}/size-options/{size_option_id}",
    summary="删除字体布局规格版本",
    description="删除字体布局历史快照；如果规格已有自己的图层副本，副本不会被删除。",
)
async def delete_font_layout_size_option(
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
    size_option_id: str = Path(min_length=1, description="尺寸规格 ID"),
    size_template_id: int = Query(gt=0, description="尺寸模板 ID"),
):
    result = await repo_call(
        catalog_repository.delete_font_layout_size_option,
        template_id,
        size_template_id,
        size_option_id,
    )
    return api_success(
        public_font_layout_template(result),
        message=result["message"],
    )


@router.patch(
    "/font-layout-templates/{template_id}",
    summary="修改字体布局模板",
    description="修改字体布局模板的店铺、名称、sort_key 或完整图层信息。",
)
async def update_font_layout_template(
    payload: FontLayoutLibraryUpdate,
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
):
    result = await repo_call(
        catalog_repository.update_font_layout_library_template,
        template_id,
        payload_dict(payload),
    )
    return api_success(
        public_font_layout_template(result),
        message="字体布局模板修改成功",
    )


@router.post(
    "/font-layout-templates/{template_id}/save-as",
    status_code=status.HTTP_201_CREATED,
    summary="字体布局模板另存为",
    description="复制源模板的图层和预览图引用，创建一个指定名称的新模板。",
)
async def save_font_layout_template_as(
    payload: FontLayoutLibrarySaveAs,
    template_id: int = Path(gt=0, description="源字体布局模板 ID"),
):
    result = await repo_call(
        catalog_repository.save_font_layout_library_template_as,
        template_id,
        payload_dict(payload),
    )
    return api_success(
        public_font_layout_template(result),
        message="字体布局模板另存为成功",
        status_code=201,
    )


@router.delete(
    "/font-layout-templates/{template_id}",
    summary="删除字体布局模板",
    description=(
        "物理删除字体布局模板主记录；已应用到尺寸模板的规格图层 JSON 保留，"
        "尺寸模板当前布局引用不清空。"
    ),
)
async def delete_font_layout_template(
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
):
    result = await repo_call(
        catalog_repository.delete_font_layout_library_template,
        template_id,
    )
    result.pop("preview_image_path", None)
    return api_success(result, message="字体布局模板删除成功")


@router.put(
    "/font-layout-templates/{template_id}/preview",
    summary="上传或替换字体布局模板预览图",
    description=(
        "上传 PNG、JPG 或 WEBP 到 OSS 的 font_layout_image 目录；"
        "数据库只保存 OSS 访问链接。"
    ),
)
async def upload_font_layout_template_preview(
    file: UploadFile = File(description="PNG、JPG 或 WEBP 预览图"),
    template_id: int = Path(gt=0, description="字体布局模板 ID"),
):
    await repo_call(catalog_repository.get_font_layout_library_template, template_id)
    suffix = FilePath(file.filename or "").suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预览图只支持 PNG、JPG、JPEG 或 WEBP",
        )
    content = await file.read(MAX_PREVIEW_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预览图不能为空",
        )
    if len(content) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="预览图不能超过 10MB",
        )
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="font-layout-preview-",
            suffix=suffix,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = FilePath(temporary_file.name)
        object_key = (
            f"font_layout_image/font-layout-template-{template_id}-"
            f"{uuid4().hex}{suffix}"
        )
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
                "字体布局模板预览图上传 OSS 失败："
                f"{uploaded.get('error') or uploaded.get('status') or '未知错误'}"
            ),
        )
    result = await repo_call(
        catalog_repository.update_font_layout_library_template,
        template_id,
        {"preview_image": uploaded["url"]},
    )
    return api_success(
        public_font_layout_template(result),
        message="字体布局模板预览图上传成功",
    )
