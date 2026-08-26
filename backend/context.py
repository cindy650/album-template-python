from backend.config import settings
import qq_idleCopy as core
from backend.catalog import CatalogRepository
from backend.mail import MailListenerManager
from backend.notifications import WeComRobotNotifier
from backend.orders import OrderRepository, OrderService
from backend.orders.print_image import (
    DeepSeekProductInformationTranslator,
    OrderPrintImageGenerator,
    WeComOrderInfoImageGenerator,
)
from backend.tasks import TaskRegistry
from backend.storage import FileStorageService
from backend.templates import (
    DeepSeekTemplateRuleResolver,
    TemplateExportService,
    TemplateImageGenerator,
    TemplateImportService,
    ImageMapHeadlessRenderer,
)
catalog_repository = CatalogRepository(settings.mysql_url)
order_repository = OrderRepository(settings.mysql_url)
file_storage_service = FileStorageService(
    project_root=settings.project_root,
    access_key_id=settings.oss_access_key_id,
    access_key_secret=settings.oss_access_key_secret,
    bucket_name=settings.oss_bucket,
    endpoint=settings.oss_endpoint,
    region=settings.oss_region,
    object_prefix=settings.oss_object_prefix,
    endpoint_is_cname=settings.oss_endpoint_is_cname,
)
image_map_renderer = ImageMapHeadlessRenderer(
    project_root=settings.project_root,
    enabled=settings.image_map_renderer_enabled,
    timeout_seconds=settings.image_map_renderer_timeout_seconds,
    no_sandbox=settings.image_map_renderer_no_sandbox,
    browser_executable=settings.image_map_renderer_browser,
    dpi=settings.template_image_dpi,
    recycle_after=settings.image_map_renderer_recycle_after,
    render_retry_attempts=settings.image_map_renderer_retry_attempts,
    resource_retry_attempts=settings.image_map_resource_retry_attempts,
    resource_retry_delay_seconds=settings.image_map_resource_retry_delay_seconds,
    resource_cache_max_bytes=settings.image_map_resource_cache_max_bytes,
    max_pending=settings.image_map_renderer_max_pending,
    max_output_pixels=settings.image_map_renderer_max_output_pixels,
)
template_image_generator = TemplateImageGenerator(
    catalog_repository,
    output_dir=settings.template_jpg_dir,
    dpi=settings.template_image_dpi,
    template_id=int(settings.default_template_id)
    if settings.default_template_id
    else None,
    storage_service=file_storage_service,
    order_repository=order_repository,
    deepseek_resolver=DeepSeekTemplateRuleResolver(
        api_url=core.DEEPSEEK_API_URL,
        api_key=core.DEEPSEEK_API_KEY,
        model=core.DEEPSEEK_MODEL,
        timeout_seconds=core.DEEPSEEK_TIMEOUT,
    ),
    image_map_renderer=image_map_renderer,
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
    output_dir=settings.order_print_image_dir,
    storage_service=file_storage_service,
)
wecom_order_info_image_generator = WeComOrderInfoImageGenerator(
    order_repository,
    template_image_generator,
    product_information_analyzer=product_information_translator,
    output_dir=settings.order_files_dir,
    storage_service=file_storage_service,
)
template_export_service = TemplateExportService(
    order_repository,
    catalog_repository,
    template_image_generator,
    output_dir=settings.template_export_dir,
    storage_service=file_storage_service,
    order_print_image_generator=order_print_image_generator,
    wecom_order_info_image_generator=wecom_order_info_image_generator,
)
template_import_service = TemplateImportService(
    source_dir=settings.template_import_dir,
    catalog_repository=catalog_repository,
    deepseek_api_url=core.DEEPSEEK_API_URL,
    deepseek_api_key=core.DEEPSEEK_API_KEY,
    deepseek_model=core.DEEPSEEK_MODEL,
    deepseek_timeout=core.DEEPSEEK_TIMEOUT,
    max_upload_bytes=settings.template_import_max_bytes,
    tesseract_command=settings.tesseract_command,
    ocr_languages=settings.template_ocr_languages,
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
    wecom_notifier=wecom_notifier,
    order_print_image_generator=wecom_order_info_image_generator,
    production_artifact_service=template_export_service,
)


def find_mail_shop(original_shop: str, resolved_shop: str):
    return catalog_repository.find_shop_by_names(
        original_shop,
        resolved_shop,
    )


def find_mail_product(shop: str, shop_name: str, product_name: str):
    return catalog_repository.ensure_shop_product(
        product_name,
        shop,
        shop_name,
    )


listener = MailListenerManager(
    order_service.handle_mail_order,
    shop_lookup=find_mail_shop,
    product_lookup=find_mail_product,
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
        product_lookup=find_mail_product,
    )
