from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from backend.context import image_map_renderer
from backend.schemas import ImageMapRenderRequest


router = APIRouter(prefix="/image-map", tags=["图像映射渲染"])


@router.post(
    "/render",
    summary="导出图像映射 JPG 或 PNG",
    description=(
        "接收 image-map-editor 导出的完整 Fabric JSON，在无头 Chromium 中按 workarea 裁切，"
        "并直接返回指定 DPI 的 JPG 或 PNG 文件。"
    ),
    responses={
        200: {
            "description": "渲染后的 JPG 或 PNG 文件",
            "content": {
                "image/jpeg": {},
                "image/png": {},
            },
        }
    },
)
async def render_image_map(payload: ImageMapRenderRequest):
    _reject_local_resource_paths(payload.document)
    try:
        content, result = await run_in_threadpool(
            image_map_renderer.render_bytes,
            payload.document,
            output_format=payload.file_format,
            dpi=payload.dpi,
            quality=payload.quality,
            background_color=payload.background_color,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    media_type = "image/jpeg" if payload.file_format == "jpg" else "image/png"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="image-map-render.{payload.file_format}"'
            ),
            "X-Image-Width": str(result.get("width") or ""),
            "X-Image-Height": str(result.get("height") or ""),
            "X-Image-DPI": str(result.get("dpi") or payload.dpi),
            "X-Fabric-Version": str(result.get("fabric_version") or ""),
        },
    )


def _reject_local_resource_paths(document: Any) -> None:
    if isinstance(document, list):
        for item in document:
            _reject_local_resource_paths(item)
        return
    if not isinstance(document, dict):
        return
    for key in ("src", "fontUrl", "font_url"):
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip().lower()
        if normalized.startswith(("https://", "http://", "data:")):
            continue
        if key == "src" and value.lstrip().startswith("<"):
            continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{key} 只允许 HTTP(S)、data URL 或内联 SVG，不能读取服务器本地路径",
        )
    for value in document.values():
        if isinstance(value, (dict, list)):
            _reject_local_resource_paths(value)
