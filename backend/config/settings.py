from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings:
    api_token = os.getenv("BACKEND_API_TOKEN", "").strip()
    auto_start_mail_listener = (
        os.getenv("AUTO_START_MAIL_LISTENER", "true").strip().lower()
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
    orders_db_path = Path(
        os.getenv("ORDERS_DB_PATH", str(PROJECT_ROOT / "orders.db"))
    )
    templates_db_path = Path(
        os.getenv("TEMPLATES_DB_PATH", str(PROJECT_ROOT / "templates.db"))
    )
    template_jpg_dir = Path(
        os.getenv(
            "TEMPLATE_JPG_DIR",
            str(PROJECT_ROOT / "generated_template_jpgs"),
        )
    )
    template_image_dpi = int(os.getenv("TEMPLATE_IMAGE_DPI", "300"))
    order_print_image_dpi = int(os.getenv("ORDER_PRINT_IMAGE_DPI", "300"))
    default_template_id = os.getenv("DEFAULT_TEMPLATE_ID", "").strip()
    template_image_retry_attempts = int(
        os.getenv("TEMPLATE_IMAGE_RETRY_ATTEMPTS", "3")
    )
    template_image_retry_delay_seconds = float(
        os.getenv("TEMPLATE_IMAGE_RETRY_DELAY_SECONDS", "5")
    )
    google_sheets_retry_initial_seconds = float(
        os.getenv("GOOGLE_SHEETS_RETRY_INITIAL_SECONDS", "30")
    )
    google_sheets_retry_max_seconds = float(
        os.getenv("GOOGLE_SHEETS_RETRY_MAX_SECONDS", "900")
    )
    wecom_robot_webhook_url = os.getenv(
        "WECOM_ROBOT_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
        "0ec1dd8c-8714-4855-8a94-83b001a6fded",
    ).strip()
    wecom_robot_timeout_seconds = float(
        os.getenv("WECOM_ROBOT_TIMEOUT_SECONDS", "10")
    )


settings = Settings()
