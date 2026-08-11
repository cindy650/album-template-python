from __future__ import annotations

from typing import Any, Callable
import sqlite3

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.context import catalog_repository
from backend.responses import api_success
from backend.schemas import (
    FontCreate,
    FontTemplateCreate,
    FontTemplateUpdate,
    FontUpdate,
    ShopCreate,
    ShopUpdate,
    SizeTemplateCreate,
    SizeTemplateUpdate,
)


router = APIRouter(tags=["catalog"])


def payload_dict(payload: BaseModel):
    return payload.model_dump(exclude_unset=True)


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
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"数据唯一性或关联约束冲突：{exc}",
        ) from exc


@router.get("/shops", include_in_schema=False)
@router.get("/shops/")
async def list_shops(
    shop_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
):
    if shop_id is not None:
        result = await repo_call(catalog_repository.get_shop, shop_id)
        return api_success(result, message="店铺查询成功")
    result = await repo_call(catalog_repository.list_shops, limit, offset, search)
    return api_success(result, message="店铺查询成功")


@router.post("/shops", status_code=status.HTTP_201_CREATED)
async def create_shop(payload: ShopCreate):
    result = await repo_call(catalog_repository.create_shop, payload_dict(payload))
    return api_success(result, message="店铺创建成功", status_code=201)


@router.get("/shops/{shop_id}")
async def get_shop(shop_id: int):
    result = await repo_call(catalog_repository.get_shop, shop_id)
    return api_success(result, message="店铺查询成功")


@router.patch("/shops/{shop_id}")
async def update_shop(shop_id: int, payload: ShopUpdate):
    result = await repo_call(
        catalog_repository.update_shop,
        shop_id,
        payload_dict(payload),
    )
    return api_success(result, message="店铺更新成功")


@router.delete("/shops/{shop_id}")
async def delete_shop(shop_id: int):
    result = await repo_call(catalog_repository.delete_shop, shop_id)
    return api_success(result, message="店铺删除成功")


@router.get("/size-templates")
async def list_size_templates(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    shop_id: int | None = None,
    product_name: str | None = None,
):
    result = await repo_call(
        catalog_repository.list_size_templates,
        limit,
        offset,
        shop_id,
        product_name,
    )
    return api_success(result, message="尺寸模板查询成功")


@router.post("/size-templates", status_code=status.HTTP_201_CREATED)
async def create_size_template(payload: SizeTemplateCreate):
    result = await repo_call(
        catalog_repository.create_size_template,
        payload_dict(payload),
    )
    return api_success(result, message="尺寸模板创建成功", status_code=201)


@router.get("/size-templates/{template_id}")
async def get_size_template(template_id: int):
    result = await repo_call(catalog_repository.get_size_template, template_id)
    return api_success(result, message="尺寸模板查询成功")


@router.patch("/size-templates/{template_id}")
async def update_size_template(
    template_id: int,
    payload: SizeTemplateUpdate,
):
    result = await repo_call(
        catalog_repository.update_size_template,
        template_id,
        payload_dict(payload),
    )
    return api_success(result, message="尺寸模板更新成功")


@router.delete("/size-templates/{template_id}")
async def delete_size_template(template_id: int):
    result = await repo_call(catalog_repository.delete_size_template, template_id)
    return api_success(result, message="尺寸模板删除成功")


@router.get("/font-templates")
async def list_font_templates(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    shop_id: int | None = None,
    size_template_id: int | None = None,
):
    result = await repo_call(
        catalog_repository.list_font_templates,
        limit,
        offset,
        shop_id,
        size_template_id,
    )
    return api_success(result, message="字体模板查询成功")


@router.post("/font-templates", status_code=status.HTTP_201_CREATED)
async def create_font_template(payload: FontTemplateCreate):
    result = await repo_call(
        catalog_repository.create_font_template,
        payload_dict(payload),
    )
    return api_success(result, message="字体模板创建成功", status_code=201)


@router.get("/font-templates/{template_id}")
async def get_font_template(template_id: int):
    result = await repo_call(catalog_repository.get_font_template, template_id)
    return api_success(result, message="字体模板查询成功")


@router.patch("/font-templates/{template_id}")
async def update_font_template(
    template_id: int,
    payload: FontTemplateUpdate,
):
    result = await repo_call(
        catalog_repository.update_font_template,
        template_id,
        payload_dict(payload),
    )
    return api_success(result, message="字体模板更新成功")


@router.delete("/font-templates/{template_id}")
async def delete_font_template(template_id: int):
    result = await repo_call(catalog_repository.delete_font_template, template_id)
    return api_success(result, message="字体模板删除成功")


@router.get("/fonts")
async def list_fonts(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    enabled: bool | None = None,
    search: str | None = None,
):
    result = await repo_call(
        catalog_repository.list_fonts,
        limit,
        offset,
        enabled,
        search,
    )
    return api_success(result, message="字体库查询成功")


@router.post("/fonts", status_code=status.HTTP_201_CREATED)
async def create_font(payload: FontCreate):
    result = await repo_call(catalog_repository.create_font, payload_dict(payload))
    return api_success(result, message="字体创建成功", status_code=201)


@router.get("/fonts/{font_id}")
async def get_font(font_id: int):
    result = await repo_call(catalog_repository.get_font, font_id)
    return api_success(result, message="字体查询成功")


@router.patch("/fonts/{font_id}")
async def update_font(font_id: int, payload: FontUpdate):
    result = await repo_call(
        catalog_repository.update_font,
        font_id,
        payload_dict(payload),
    )
    return api_success(result, message="字体更新成功")


@router.delete("/fonts/{font_id}")
async def delete_font(font_id: int):
    result = await repo_call(catalog_repository.delete_font, font_id)
    return api_success(result, message="字体删除成功")
