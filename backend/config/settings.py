from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path):
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(PROJECT_ROOT / ".env")


class Settings:
    project_root = PROJECT_ROOT
    api_token = os.getenv("BACKEND_API_TOKEN", "").strip()
    auto_start_mail_listener = (
        os.getenv("AUTO_START_MAIL_LISTENER", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            ",".join(
                [
                    "http://localhost:3000",
                    "http://127.0.0.1:3000",
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "http://localhost:5174",
                    "http://127.0.0.1:5174",
                    "http://localhost:5175",
                    "http://127.0.0.1:5175",
                ]
            ),
        ).split(",")
        if origin.strip()
    ]
    database_url = os.getenv("DATABASE_URL", "").strip()
    mysql_host = os.getenv("MYSQL_HOST", "127.0.0.1").strip()
    mysql_port = int(os.getenv("MYSQL_PORT", "3306"))
    mysql_user = os.getenv("MYSQL_USER", "root").strip()
    mysql_password = os.getenv("MYSQL_PASSWORD", "")
    mysql_database = os.getenv("MYSQL_DATABASE", "dcjt").strip()
    mysql_charset = os.getenv("MYSQL_CHARSET", "utf8mb4").strip()
    mysql_connect_timeout_seconds = max(
        1,
        int(os.getenv("MYSQL_CONNECT_TIMEOUT_SECONDS", "5")),
    )
    mysql_connect_retry_attempts = max(
        1,
        int(os.getenv("MYSQL_CONNECT_RETRY_ATTEMPTS", "5")),
    )
    mysql_connect_retry_delay_seconds = max(
        0,
        float(os.getenv("MYSQL_CONNECT_RETRY_DELAY_SECONDS", "0.5")),
    )
    mysql_pool_size = max(1, int(os.getenv("MYSQL_POOL_SIZE", "3")))
    mysql_pool_max_overflow = max(
        0,
        int(os.getenv("MYSQL_POOL_MAX_OVERFLOW", "2")),
    )
    mysql_pool_timeout_seconds = max(
        0.1,
        float(os.getenv("MYSQL_POOL_TIMEOUT_SECONDS", "10")),
    )
    mysql_pool_recycle_seconds = max(
        1,
        int(os.getenv("MYSQL_POOL_RECYCLE_SECONDS", "600")),
    )
    mysql_pool_pre_ping = (
        os.getenv("MYSQL_POOL_PRE_PING", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    mysql_url = (
        database_url
        or f"mysql://{mysql_user}:{mysql_password}@{mysql_host}:{mysql_port}/"
        f"{mysql_database}?charset={mysql_charset}"
    )
    order_files_dir = Path(
        os.getenv(
            "ORDER_FILES_DIR",
            str(PROJECT_ROOT / "generated_orders"),
        )
    )
    template_jpg_dir = order_files_dir
    template_image_dpi = int(os.getenv("TEMPLATE_IMAGE_DPI", "300"))
    image_map_renderer_enabled = (
        os.getenv("IMAGE_MAP_RENDERER_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    image_map_renderer_timeout_seconds = float(
        os.getenv("IMAGE_MAP_RENDERER_TIMEOUT_SECONDS", "120")
    )
    image_map_renderer_no_sandbox = (
        os.getenv("IMAGE_MAP_RENDERER_NO_SANDBOX", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    image_map_renderer_browser = os.getenv(
        "IMAGE_MAP_RENDERER_BROWSER",
        "",
    ).strip()
    image_map_renderer_recycle_after = max(
        1,
        int(os.getenv("IMAGE_MAP_RENDERER_RECYCLE_AFTER", "100")),
    )
    image_map_renderer_retry_attempts = max(
        1,
        int(os.getenv("IMAGE_MAP_RENDERER_RETRY_ATTEMPTS", "2")),
    )
    image_map_resource_retry_attempts = max(
        1,
        int(os.getenv("IMAGE_MAP_RESOURCE_RETRY_ATTEMPTS", "3")),
    )
    image_map_resource_retry_delay_seconds = max(
        0,
        float(os.getenv("IMAGE_MAP_RESOURCE_RETRY_DELAY_SECONDS", "1")),
    )
    image_map_resource_cache_max_bytes = max(
        0,
        int(os.getenv("IMAGE_MAP_RESOURCE_CACHE_MAX_BYTES", str(64 * 1024 * 1024))),
    )
    image_map_renderer_max_pending = max(
        1,
        int(os.getenv("IMAGE_MAP_RENDERER_MAX_PENDING", "4")),
    )
    image_map_renderer_max_output_pixels = max(
        1,
        int(os.getenv("IMAGE_MAP_RENDERER_MAX_OUTPUT_PIXELS", "50000000")),
    )
    order_print_image_dpi = int(os.getenv("ORDER_PRINT_IMAGE_DPI", "300"))
    order_print_image_dir = order_files_dir
    template_export_dir = order_files_dir
    template_import_dir = Path(
        os.getenv(
            "TEMPLATE_IMPORT_DIR",
            str(PROJECT_ROOT / "generated_template_imports"),
        )
    )
    font_layout_preview_dir = Path(
        os.getenv(
            "FONT_LAYOUT_PREVIEW_DIR",
            str(PROJECT_ROOT / "generated_font_layout_previews"),
        )
    )
    template_import_max_bytes = int(
        os.getenv("TEMPLATE_IMPORT_MAX_BYTES", str(50 * 1024 * 1024))
    )
    tesseract_command = os.getenv("TESSERACT_COMMAND", "tesseract").strip()
    template_ocr_languages = os.getenv(
        "TEMPLATE_OCR_LANGUAGES",
        "eng+chi_sim",
    ).strip()
    wecom_order_info_image_dir = Path(
        os.getenv(
            "WECOM_ORDER_INFO_IMAGE_DIR",
            str(order_files_dir),
        )
    )
    default_template_id = os.getenv("DEFAULT_TEMPLATE_ID", "").strip()
    template_image_retry_attempts = int(
        os.getenv("TEMPLATE_IMAGE_RETRY_ATTEMPTS", "3")
    )
    template_image_retry_delay_seconds = float(
        os.getenv("TEMPLATE_IMAGE_RETRY_DELAY_SECONDS", "5")
    )
    wecom_robot_timeout_seconds = float(
        os.getenv("WECOM_ROBOT_TIMEOUT_SECONDS", "10")
    )
    oss_access_key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    oss_access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    oss_bucket = os.getenv("OSS_BUCKET", "fakestar-oss").strip()
    oss_region = os.getenv("OSS_REGION", "oss-us-west-1").strip()
    oss_endpoint = os.getenv(
        "OSS_ENDPOINT",
        "fakestar-oss.oss-us-west-1.aliyuncs.com",
    ).strip()
    oss_object_prefix = os.getenv("OSS_OBJECT_PREFIX", "").strip()
    oss_endpoint_is_cname = (
        os.getenv("OSS_ENDPOINT_IS_CNAME", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )


settings = Settings()
