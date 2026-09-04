from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from backend.context import file_storage_service
from backend.responses import api_success


router = APIRouter(prefix="/uploads", tags=["公共上传"])

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_SUFFIXES = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


@router.post(
    "/images",
    summary="上传公共图片到 OSS",
    description=(
        "上传 PNG、JPEG 或 WEBP 图片并返回 OSS 链接；"
        "接口不关联或修改任何业务数据，业务保存接口自行存储返回的 URL。"
    ),
)
async def upload_image(
    file: UploadFile = File(description="PNG、JPEG 或 WEBP 图片，最大 10MB"),
):
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="图片不能为空",
        )
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="图片不能超过 10MB",
        )

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            suffix = IMAGE_SUFFIXES.get(str(image.format or "").upper())
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        suffix = None
    if suffix is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持有效的 PNG、JPG、JPEG 或 WEBP 图片",
        )

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="public-image-",
            suffix=suffix,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = Path(temporary_file.name)
        object_key = f"font_layout_image/{uuid4().hex}{suffix}"
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
                "图片上传 OSS 失败："
                f"{uploaded.get('error') or uploaded.get('status') or '未知错误'}"
            ),
        )
    return api_success(
        {"url": uploaded["url"]},
        message="图片上传成功",
    )
