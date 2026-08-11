import qq_idleCopy as core

from backend.catalog import CatalogRepository
from backend.catalog.defaults import (
    DEFAULT_SIZE_TEMPLATE_FIELDS,
    DEFAULT_SIZE_TEMPLATE_PRODUCT,
)
from backend.config import settings
from backend.mail import MailListenerManager
from backend.notifications import WeComRobotNotifier
from backend.orders import OrderRepository, OrderService
from backend.orders.print_image import (
    DeepSeekProductInformationTranslator,
    OrderPrintImageGenerator,
)
from backend.tasks import TaskRegistry
from backend.templates import TemplateImageGenerator


catalog_repository = CatalogRepository(settings.orders_db_path)
catalog_repository.ensure_size_template(
    shop="LuxeJoy",
    shop_name="3号店",
    product_name=DEFAULT_SIZE_TEMPLATE_PRODUCT,
    name=DEFAULT_SIZE_TEMPLATE_PRODUCT,
    fields=DEFAULT_SIZE_TEMPLATE_FIELDS,
)
order_repository = OrderRepository(settings.orders_db_path)
template_image_generator = TemplateImageGenerator(
    catalog_repository,
    output_dir=settings.template_jpg_dir,
    dpi=settings.template_image_dpi,
    template_id=int(settings.default_template_id)
    if settings.default_template_id
    else None,
)
product_information_translator = DeepSeekProductInformationTranslator(
    api_url=core.DEEPSEEK_API_URL,
    api_key=core.DEEPSEEK_API_KEY,
    model=core.DEEPSEEK_MODEL,
    timeout_seconds=core.DEEPSEEK_TIMEOUT,
)
order_print_image_generator = OrderPrintImageGenerator(
    order_repository,
    template_image_generator,
    product_information_translator,
    dpi=settings.order_print_image_dpi,
)
wecom_notifier = (
    WeComRobotNotifier(
        settings.wecom_robot_webhook_url,
        timeout_seconds=settings.wecom_robot_timeout_seconds,
    )
    if settings.wecom_robot_webhook_url
    else None
)
order_service = OrderService(
    order_repository,
    template_image_generator,
    template_image_retry_attempts=settings.template_image_retry_attempts,
    template_image_retry_delay_seconds=settings.template_image_retry_delay_seconds,
    google_sheets_retry_initial_seconds=(
        settings.google_sheets_retry_initial_seconds
    ),
    google_sheets_retry_max_seconds=settings.google_sheets_retry_max_seconds,
    wecom_notifier=wecom_notifier,
)


def find_mail_shop(original_shop: str, resolved_shop: str):
    return catalog_repository.find_shop_by_names(
        original_shop,
        resolved_shop,
    )


listener = MailListenerManager(
    order_service.handle_mail_order,
    shop_lookup=find_mail_shop,
)
tasks = TaskRegistry()


def parse_order_payload(payload):
    return core.parse_order_fields(
        subject=payload.subject,
        body=payload.body,
        email_date=payload.email_date,
        metadata=payload.metadata,
        uid=payload.uid,
        shop_lookup=find_mail_shop,
    )


def parse_and_publish(payload):
    order = parse_order_payload(payload)
    return order_service.publish_order(
        order,
        source="api",
        uid=payload.uid,
        metadata=payload.metadata,
    )


def poll_mailbox_once():
    return core.poll_mailbox_once(
        order_service.handle_mail_order,
        shop_lookup=find_mail_shop,
    )
