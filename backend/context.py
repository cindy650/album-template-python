import qq_idleCopy as core

from backend.config import settings
from backend.mail import MailListenerManager
from backend.orders import OrderRepository, OrderService
from backend.tasks import TaskRegistry
from backend.templates import TemplateImageGenerator, TemplateRepository


order_repository = OrderRepository(settings.orders_db_path)
template_repository = TemplateRepository(settings.templates_db_path)
template_image_generator = TemplateImageGenerator(
    template_repository,
    output_dir=settings.template_images_dir,
    dpi=settings.template_image_dpi,
    template_id=int(settings.default_template_id)
    if settings.default_template_id
    else None,
)
order_service = OrderService(
    order_repository,
    template_image_generator,
    template_image_retry_attempts=settings.template_image_retry_attempts,
    template_image_retry_delay_seconds=settings.template_image_retry_delay_seconds,
)
listener = MailListenerManager(order_service.handle_mail_order)
tasks = TaskRegistry()


def parse_order_payload(payload):
    return core.parse_order_fields(
        subject=payload.subject,
        body=payload.body,
        email_date=payload.email_date,
        metadata=payload.metadata,
        uid=payload.uid,
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
    return core.poll_mailbox_once(order_service.handle_mail_order)
