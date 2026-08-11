from fastapi import APIRouter

import qq_idleCopy as core
from backend.config import settings
from backend.context import listener, order_service
from backend.responses import api_success


router = APIRouter(tags=["system"])


@router.get("/config")
async def get_config():
    return api_success(
        {
            "imap": {
                "host": core.IMAP_HOST,
                "port": core.IMAP_PORT,
                "user": core.QQ_USER,
                "configured": bool(core.QQ_USER and core.QQ_AUTH_CODE),
            },
            "deepseek": {
                "url": core.DEEPSEEK_API_URL,
                "model": core.DEEPSEEK_MODEL,
                "configured": bool(core.DEEPSEEK_API_KEY),
                "enabled": False,
            },
            "google_sheets": {
                "url": core.APPS_SCRIPT_URL,
                "configured": bool(core.APPS_SCRIPT_URL),
            },
            "wecom_robot": {
                "configured": bool(settings.wecom_robot_webhook_url),
            },
            "personalization_rules": {"enabled": False},
            "state": {"last_uid": core.load_last_uid()},
            "auto_start_mail_listener": settings.auto_start_mail_listener,
            "orders_db_path": str(settings.orders_db_path),
            "catalog_db_path": str(settings.orders_db_path),
            "templates_db_path": str(settings.templates_db_path),
            "templates_db_in_use": False,
            "listener": listener.status(),
            "google_sheets_retry": order_service.google_sheets_retry_status(),
        },
        message="配置查询成功",
    )
