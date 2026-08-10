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
    template_images_dir = Path(
        os.getenv(
            "TEMPLATE_IMAGES_DIR",
            str(PROJECT_ROOT / "generated_template_images"),
        )
    )
    template_image_dpi = int(os.getenv("TEMPLATE_IMAGE_DPI", "300"))
    default_template_id = os.getenv("DEFAULT_TEMPLATE_ID", "").strip()
    template_image_retry_attempts = int(
        os.getenv("TEMPLATE_IMAGE_RETRY_ATTEMPTS", "3")
    )
    template_image_retry_delay_seconds = float(
        os.getenv("TEMPLATE_IMAGE_RETRY_DELAY_SECONDS", "5")
    )


settings = Settings()
