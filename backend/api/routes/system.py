from fastapi import APIRouter

import qq_idleCopy as core
from backend.config import settings
from backend.context import listener
from backend.responses import api_success


router = APIRouter(tags=["system"])


@router.get("/config")
async def get_config():
    rules = core.validate_personalization_config(
        core.read_personalization_config()
    )
    shops = {
        shop_name: list(shop_rules["products"])
        for shop_name, shop_rules in rules["shops"].items()
    }
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
            },
            "google_sheets": {
                "url": core.APPS_SCRIPT_URL,
                "configured": bool(core.APPS_SCRIPT_URL),
            },
            "rules": shops,
            "state": {"last_uid": core.load_last_uid()},
            "auto_start_mail_listener": settings.auto_start_mail_listener,
            "orders_db_path": str(settings.orders_db_path),
            "templates_db_path": str(settings.templates_db_path),
            "listener": listener.status(),
        },
        message="配置查询成功",
    )
