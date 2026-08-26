import secrets

from fastapi import Header, HTTPException, status

from backend.config import settings


def verify_api_key(
    x_api_key: str | None = Header(
        default=None,
        description="接口访问密钥；未配置 API_TOKEN 时可不传",
    ),
):
    if settings.api_token and (
        not x_api_key
        or not secrets.compare_digest(x_api_key, settings.api_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key 无效",
        )
