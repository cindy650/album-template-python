from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
}


def api_success(
    data: Any = None,
    message: str = "success",
    code: str = "OK",
    status_code: int = 200,
):
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            {
                "success": True,
                "code": code,
                "message": message,
                "data": data,
                "error": None,
            }
        ),
    )


def api_error(
    message: str,
    code: str = "ERROR",
    status_code: int = 500,
    error: dict[str, Any] | None = None,
):
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            {
                "success": False,
                "code": code,
                "message": message,
                "data": None,
                "error": error,
            }
        ),
    )


def api_code_for_status(status_code: int) -> str:
    return HTTP_ERROR_CODES.get(status_code, "ERROR")
