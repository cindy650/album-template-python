from fastapi import APIRouter

import qq_idleCopy as core
from backend.config import settings
from backend.context import listener
from backend.responses import api_success


router = APIRouter(tags=["系统"])


@router.get(
    "/config",
    summary="查询系统配置状态",
    description="查询邮箱、企业微信、OSS、数据库、文件目录和后台任务的配置状态；敏感密钥不会返回。",
)
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
            "wecom_robot": {
                "configured": bool(settings.wecom_robot_webhook_url),
            },
            "oss": {
                "configured": bool(
                    settings.oss_access_key_id
                    and settings.oss_access_key_secret
                    and settings.oss_bucket
                    and settings.oss_endpoint
                ),
                "bucket": settings.oss_bucket,
                "region": settings.oss_region,
                "endpoint": settings.oss_endpoint,
                "object_prefix": settings.oss_object_prefix,
            },
            "image_map_renderer": {
                "enabled": settings.image_map_renderer_enabled,
                "script_exists": (
                    settings.project_root
                    / "image-map-headless-renderer"
                    / "python"
                    / "render.py"
                ).is_file(),
                "timeout_seconds": settings.image_map_renderer_timeout_seconds,
                "no_sandbox": settings.image_map_renderer_no_sandbox,
                "browser": settings.image_map_renderer_browser or "auto",
                "recycle_after": settings.image_map_renderer_recycle_after,
                "render_retry_attempts": settings.image_map_renderer_retry_attempts,
                "resource_retry_attempts": settings.image_map_resource_retry_attempts,
                "resource_cache_max_bytes": settings.image_map_resource_cache_max_bytes,
                "max_pending": settings.image_map_renderer_max_pending,
                "max_output_pixels": settings.image_map_renderer_max_output_pixels,
            },
            "personalization_rules": {"enabled": False},
            "state": {"last_uid": core.load_last_uid()},
            "auto_start_mail_listener": settings.auto_start_mail_listener,
            "database_url": settings.database_url or "configured_by_mysql_settings",
            "database_driver": "mysql",
            "template_storage": {
                "database": settings.mysql_database,
                "order_files_dir": str(settings.order_files_dir),
                "preview_dir": str(settings.template_jpg_dir),
                "print_image_dir": str(settings.order_print_image_dir),
                "export_dir": str(settings.template_export_dir),
                "tables": [
                    "shops",
                    "products",
                    "product_shops",
                    "product_names",
                    "size_templates",
                    "fonts",
                    "font_layout_library",
                ],
            },
            "listener": listener.status(),
        },
        message="配置查询成功",
    )
