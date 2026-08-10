from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    product: str = Field(default="", alias="产品")
    specifications: str = Field(default="", alias="规格/尺寸")
    personalization: dict[str, str] = Field(
        default_factory=dict,
        alias="定制信息",
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
