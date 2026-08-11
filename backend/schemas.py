from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.orders.statuses import ORDER_STATUSES


class ParseOrderRequest(BaseModel):
    subject: str = Field(description="Email subject")
    body: str = Field(description="Decoded plain-text email body")
    email_date: str = Field(default="", description="Original email Date header")
    metadata: dict[str, str] = Field(default_factory=dict)
    uid: int | None = None


class OrderData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    order_number: str = Field(default="", alias="订单号")
    shop: str = Field(default="", alias="店铺")
    shop_name: str = Field(default="", alias="店铺名")
    product: str = Field(default="", alias="产品")
    product_information: dict[str, str] = Field(
        default_factory=dict,
        alias="商品信息",
    )
    payment_method: str = Field(default="", alias="付款方式")
    shipping_address: str = Field(default="", alias="邮寄地址")
    transaction_id: str = Field(default="", alias="交易编号")
    quantity: int | str = Field(default="", alias="数量")
    price: str = Field(default="", alias="价格")


class PublishResult(BaseModel):
    order: OrderData
    google_sheets: dict[str, Any]
    local_order: dict[str, Any] | None = None


class JobAccepted(BaseModel):
    task_id: str
    status: str
    name: str


class ListenerStatus(BaseModel):
    status: str
    running: bool
    stop_requested: bool
    started_at: str | None = None
    stopped_at: str | None = None
    last_error: str | None = None
    last_uid: int


class OrderStatusUpdate(BaseModel):
    status: str = Field(description=f"订单状态，可选：{', '.join(ORDER_STATUSES)}")


class OrderPrintImageRequest(BaseModel):
    order_id: int = Field(gt=0, description="本地订单 ID")
    order_number: str = Field(min_length=1, description="订单号")


class ShopCreate(BaseModel):
    shop: str = Field(description="店铺唯一标识")
    shop_name: str = Field(default="", description="店铺名")


class ShopUpdate(BaseModel):
    shop: str | None = None
    shop_name: str | None = None


class SizeTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int = Field(description="所属店铺 ID")
    name: str = Field(default="", description="尺寸模板名称")
    product_name: str | None = Field(default=None, description="单个商品名")
    product_names: list[str] = Field(
        default_factory=list,
        description="一个或多个可匹配订单商品名的商品名",
    )
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="前端传回的动态尺寸字段",
    )
    font_template_ids: list[int] = Field(
        default_factory=list,
        description="需要关联到该尺寸模板的字体模板 ID",
    )


class SizeTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int | None = None
    name: str | None = None
    product_name: str | None = None
    product_names: list[str] | None = None
    fields: dict[str, Any] | None = None
    font_template_ids: list[int] | None = None


class FontTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int = Field(description="所属店铺 ID")
    size_template_id: int = Field(description="所属尺寸模板 ID")
    name: str = Field(default="", description="字体模板名称")
    font_id: int | None = Field(default=None, description="默认字体库 ID")
    font_name: str | None = Field(default=None, description="默认字体名称")
    layout: dict[str, Any] | list[dict[str, Any]] = Field(
        default_factory=dict,
        description="文字位置、字体、坐标等排版信息",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="下拉项、选项、特殊排版规则",
    )
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="前端传回的动态字体模板字段",
    )


class FontTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int | None = None
    size_template_id: int | None = None
    name: str | None = None
    font_id: int | None = None
    font_name: str | None = None
    layout: dict[str, Any] | list[dict[str, Any]] | None = None
    options: dict[str, Any] | None = None
    fields: dict[str, Any] | None = None


class FontCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    font_name: str = Field(description="字体名称")
    font_family: str = Field(default="", description="字体族")
    file_path: str = Field(default="", description="字体文件路径")
    source: str = Field(default="", description="来源")
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class FontUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    font_name: str | None = None
    font_family: str | None = None
    file_path: str | None = None
    source: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None
