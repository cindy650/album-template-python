from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from backend.context import template_import_service
from backend.responses import api_success
from backend.schemas import TemplateFinalizeRequest, TemplateSaveRequest


router = APIRouter(prefix="/template-imports", tags=["模板识别"])


@router.post(
    "/analyze",
    summary="识别 PSD 或图片模板",
    description=(
        "接收 PSD 或普通图片；PSD 文字图层保留文字、字体和字号，其他图层统一作为图片元素，"
        "并返回可由前端二次编辑的模板草稿，不生成图层位置或字号公式字段。"
        "font_layout_templates.elements 直接返回统一的文字/图片元素结构和完整坐标；"
        "图片在配置 Tesseract 后会附带 OCR 文字块；"
        "DeepSeek 只负责字段语义建议，不修改识别坐标。"
    ),
)
async def analyze_template_file(
    file: UploadFile = File(
        description="需要识别的 PSD、PNG、JPG、WEBP、BMP 或 TIFF 模板文件",
    ),
    options_json: str = Form(
        default="{}",
        description=(
            "JSON 字符串，可包含 regions、rules、font_assignments、use_ai、"
            "strict_fonts 和 ocr_languages"
        ),
    ),
):
    try:
        options = json.loads(options_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="options_json 不是有效 JSON",
        ) from exc
    if not isinstance(options, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="options_json 必须是 JSON 对象",
        )
    content = await file.read(template_import_service.max_upload_bytes + 1)
    try:
        result = await run_in_threadpool(
            template_import_service.analyze,
            file.filename or "source",
            content,
            file.content_type or "application/octet-stream",
            options,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return api_success(result, message="模板文件识别成功")


@router.post(
    "/finalize",
    summary="确认并保存模板布局",
    description=(
        "接收前端编辑后的模板草稿，更新元素相对于画布或指定区域的位置比例，"
        "校验文字字体和生成规则，并可选保存到字体布局模板库后挂载到现有尺寸模板。"
    ),
)
async def finalize_template(payload: TemplateFinalizeRequest):
    try:
        result = await run_in_threadpool(
            template_import_service.finalize,
            payload.template_json,
            payload.strict_fonts,
            payload.size_template_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return api_success(result, message="模板布局确认成功")


@router.post(
    "/save",
    summary="保存尺寸模板并创建字体布局模板",
    description=(
        "保存前端编辑后的尺寸模板草稿；尺寸规格、出血和书脊参数由前端确认后写入，"
        "同时根据图层分组创建字体布局模板库记录，并挂载到尺寸模板的当前尺寸方案。"
    ),
)
async def save_template(payload: TemplateSaveRequest):
    try:
        result = await run_in_threadpool(
            template_import_service.save,
            payload.template_json,
            payload.shop_id,
            payload.product_names,
            payload.template_name,
            payload.strict_fonts,
            payload.size_template_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return api_success(result, message="尺寸模板和字体布局模板保存成功", status_code=201)
