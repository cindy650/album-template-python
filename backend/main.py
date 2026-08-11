from contextlib import asynccontextmanager
import socket

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
import requests

import qq_idleCopy as core
from backend import __version__
from backend.api import api_router
from backend.config import settings
from backend.context import listener, order_service, tasks
from backend.responses import api_code_for_status, api_error, api_success


@asynccontextmanager
async def lifespan(app):
    core.configure_utf8_console()
    socket.setdefaulttimeout(30)
    order_service.start()
    if settings.auto_start_mail_listener:
        listener.start()
    yield
    listener.shutdown()
    order_service.shutdown()
    tasks.shutdown()


app = FastAPI(
    title="Etsy QQ Mail Order Backend",
    version=__version__,
    description=(
        "监听 QQ 邮箱中的 Etsy 订单，解析订单与独立商品字段，"
        "写入 Google Sheets、本地 SQLite，并通过 SSE 推送实时事件。"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(core.ConfigurationError)
async def configuration_error_handler(request, exc):
    return api_error(
        message=str(exc),
        code="CONFIGURATION_ERROR",
        status_code=422,
        error={"type": type(exc).__name__, "detail": str(exc)},
    )


@app.exception_handler(requests.RequestException)
async def upstream_error_handler(request, exc):
    return api_error(
        message="上游服务请求失败",
        code="UPSTREAM_ERROR",
        status_code=502,
        error={"type": type(exc).__name__, "detail": str(exc)},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return api_error(
        message="请求参数错误",
        code="VALIDATION_ERROR",
        status_code=422,
        error={"type": type(exc).__name__, "detail": exc.errors()},
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    detail = exc.detail
    message = detail if isinstance(detail, str) else "请求失败"
    return api_error(
        message=message,
        code=api_code_for_status(exc.status_code),
        status_code=exc.status_code,
        error={"type": type(exc).__name__, "detail": detail},
    )


@app.exception_handler(Exception)
async def internal_error_handler(request, exc):
    return api_error(
        message="服务器内部错误",
        code="INTERNAL_ERROR",
        status_code=500,
        error={"type": type(exc).__name__, "detail": str(exc)},
    )


@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": app.title,
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"])
async def health():
    try:
        core.validate_config()
        configuration = "valid"
        configuration_error = None
    except core.ConfigurationError as exc:
        configuration = "invalid"
        configuration_error = str(exc)
    return api_success(
        {
            "status": "ok",
            "version": __version__,
            "configuration": configuration,
            "configuration_error": configuration_error,
            "listener": listener.status(),
            "google_sheets_retry": order_service.google_sheets_retry_status(),
        },
        message="服务健康检查成功",
    )


app.include_router(api_router)
