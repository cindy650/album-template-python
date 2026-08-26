from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

class ParseOrderRequest(BaseModel):
    subject: str = Field(description="邮件标题")
    body: str = Field(description="已解码的纯文本邮件正文")
    email_date: str = Field(default="", description="邮件原始 Date 请求头")
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="邮件来源、发件人等附加元数据",
    )
    uid: int | None = Field(default=None, description="邮箱中的邮件 UID")


class OrderData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    order_number: str = Field(default="", alias="订单号", description="订单号")
    shop: str = Field(default="", alias="店铺", description="店铺唯一标识")
    shop_name: str = Field(default="", alias="店铺名", description="店铺名称")
    product: str = Field(default="", alias="产品", description="订单商品名称")
    product_information: dict[str, str] = Field(
        default_factory=dict,
        alias="商品信息",
        description="订单商品的规格、定制文字等信息",
    )
    payment_method: str = Field(default="", alias="付款方式", description="付款方式")
    shipping_address: str = Field(default="", alias="邮寄地址", description="收货地址")
    transaction_id: str = Field(default="", alias="交易编号", description="交易编号")
    quantity: int | str = Field(default="", alias="数量", description="商品数量")
    price: str = Field(default="", alias="价格", description="订单价格")


class PublishResult(BaseModel):
    order: OrderData = Field(description="已处理的结构化订单")
    local_order: dict[str, Any] | None = Field(
        default=None,
        description="MySQL 订单库写入结果",
    )


class JobAccepted(BaseModel):
    task_id: str = Field(description="后台任务 ID")
    status: str = Field(description="后台任务状态")
    name: str = Field(description="后台任务名称")


class ListenerStatus(BaseModel):
    status: str = Field(description="邮箱监听状态")
    running: bool = Field(description="邮箱监听是否正在运行")
    stop_requested: bool = Field(description="是否已请求停止监听")
    started_at: str | None = Field(default=None, description="最近启动时间")
    stopped_at: str | None = Field(default=None, description="最近停止时间")
    last_error: str | None = Field(default=None, description="最近一次错误信息")
    last_uid: int = Field(description="最后处理的邮件 UID")


class OrderStatusUpdate(BaseModel):
    status: Literal[0, 1, 2, 3, 4, 5] = Field(
        description=(
            "订单状态值：0 新订单、1 示意图已发送/客户确认中、2 待生产、"
            "3 生产中、4 待发货、5 订单已完成"
        ),
    )


class OrderPrintImageRequest(BaseModel):
    order_id: int = Field(gt=0, description="本地订单 ID")
    order_number: str = Field(min_length=1, description="订单号")


class OrderStatusAdvanceRequest(OrderPrintImageRequest):
    """将订单推进到下一个生产流程状态。"""


class OrderTemplateJsonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_number: str = Field(min_length=1, description="订单号")
    template_json: dict[str, Any] = Field(
        description="前端编辑后的完整订单模板 JSON，按原值保存",
    )


class OrderTemplateExportRequest(OrderPrintImageRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    file_format: Literal["psd", "eps", "svg", "jpg", "png"] | None = Field(
        default=None,
        alias="format",
        deprecated=True,
        description="旧版兼容参数，当前固定生成四个订单文件，不再生成源文件",
    )
    template_json: dict[str, Any] | None = Field(
        default=None,
        description=(
            "前端可选传入的模板 JSON。本次导出使用其中的视觉字段覆盖数据库模板；"
            "模板 ID、店铺、商品等数据库关联字段始终保留订单所关联模板中的值。"
            "未传时使用订单关联的数据库模板。"
        ),
        examples=[
            {
                "cover_text": "Wedding Guest Book",
                "cover_names": "Alice & Bob",
                "fields": {"cover_subtitle": "Our Wedding Day"},
            }
        ],
    )


class ImageMapRenderRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document: dict[str, Any] | list[dict[str, Any]] = Field(
        alias="json",
        description=(
            "image-map-editor 导出的完整 Fabric JSON；支持对象数组、objects 或 layers.objects 结构"
        ),
    )
    file_format: Literal["jpg", "png"] = Field(
        default="png",
        alias="format",
        description="导出图片格式，可选 jpg 或 png",
    )
    dpi: float = Field(
        default=300,
        ge=1,
        le=1200,
        description="输出分辨率，单位为 DPI，默认 300",
    )
    quality: float = Field(
        default=0.95,
        ge=0,
        le=1,
        description="JPG 压缩质量，取值范围 0 到 1；PNG 会忽略该参数",
    )
    background_color: str | None = Field(
        default=None,
        description="可选画布背景色；未传时使用 workarea 的背景色",
    )


class ShopCreate(BaseModel):
    shop: str = Field(description="店铺唯一标识")
    shop_name: str = Field(default="", description="店铺名")
    products: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("products", "product_names"),
        description="需要关联到店铺的商品名数组",
    )


class ShopUpdate(BaseModel):
    shop: str | None = Field(default=None, description="店铺唯一标识")
    shop_name: str | None = Field(default=None, description="店铺名")
    products: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("products", "product_names"),
        description="店铺关联的完整商品名数组；传入时替换原有关联",
    )


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="产品分类名称，例如婚礼签到册")
    description: str = Field(default="", description="产品说明")
    product_names: list[str] = Field(
        min_length=1,
        description="用于匹配订单的商品名数组；可同时填写多个商品名",
    )
    shop_ids: list[int] = Field(default_factory=list, description="关联店铺 ID，可关联多个店铺")
    enabled: bool = Field(default=True, description="是否启用")

    @field_validator("product_names", mode="before")
    @classmethod
    def validate_product_names(cls, value):
        if not isinstance(value, list):
            raise ValueError("product_names 必须是数组")
        return value


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, description="产品分类名称")
    description: str | None = Field(default=None, description="产品说明")
    product_names: list[str] | None = Field(
        default=None,
        description="用于匹配订单的商品名数组；传入时替换全部商品名",
    )
    shop_ids: list[int] | None = Field(default=None, description="关联店铺 ID 数组")
    enabled: bool | None = Field(default=None, description="是否启用")

    @field_validator("product_names", mode="before")
    @classmethod
    def validate_product_names(cls, value):
        if value is not None and not isinstance(value, list):
            raise ValueError("product_names 必须是数组")
        return value


class SizeTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int = Field(description="所属店铺 ID")
    template_name: str | None = Field(
        default=None,
        description="尺寸模板名称，例如 婚礼签到册",
    )
    background_color: str = Field(default="", description="背景色")
    max_spine_bleed: float = Field(
        default=0,
        ge=0,
        description="最大背脊出血，必须大于或等于 0",
    )
    min_spine_bleed: float = Field(
        default=0,
        ge=0,
        description="最小背脊出血，必须大于或等于 0",
    )
    product_names: list[str] = Field(
        min_length=1,
        description="一个或多个可匹配订单商品名的商品名",
    )
    size_option: str | None = Field(
        default=None,
        description="当前选中的尺寸方案 ID；不传时默认第一个方案",
    )
    size_unit: Literal["in", "mm", "cm"] | None = Field(
        default=None,
        description="表单当前显示单位；新增默认 in",
    )
    page_count: int | None = Field(
        default=None,
        ge=0,
        description="模板默认页数；不属于单个尺寸方案",
    )
    page_count_arr: list[int] = Field(
        default_factory=list,
        description="模板可选页数列表，例如 [20, 40, 50, 80, 100]",
    )
    size_options: list[dict[str, Any]] = Field(
        default_factory=list,
        description="扁平尺寸方案数组；数值按 size_unit 原样保存",
    )
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="前端传回的动态尺寸字段",
    )


class SizeTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int | None = Field(default=None, description="所属店铺 ID")
    template_name: str | None = Field(
        default=None,
        description="尺寸模板名称，例如 婚礼签到册",
    )
    background_color: str | None = Field(default=None, description="背景色")
    max_spine_bleed: float | None = Field(
        default=None,
        ge=0,
        description="最大背脊出血，必须大于或等于 0",
    )
    min_spine_bleed: float | None = Field(
        default=None,
        ge=0,
        description="最小背脊出血，必须大于或等于 0",
    )
    product_names: list[str] | None = Field(
        default=None,
        description="一个或多个可匹配订单商品名的商品名",
    )
    size_option: str | None = Field(
        default=None,
        description="当前选中的尺寸方案 ID",
    )
    size_unit: Literal["in", "mm", "cm"] | None = Field(
        default=None,
        description="表单当前显示单位",
    )
    page_count: int | None = Field(
        default=None,
        ge=0,
        description="模板默认页数；不属于单个尺寸方案",
    )
    page_count_arr: list[int] | None = Field(
        default=None,
        description="模板可选页数列表，例如 [20, 40, 50, 80, 100]",
    )
    size_options: list[dict[str, Any]] | None = Field(
        default=None,
        description="扁平尺寸方案数组；数值按 size_unit 原样保存",
    )
    fields: dict[str, Any] | None = Field(
        default=None,
        description="前端传回的动态尺寸字段",
    )


class SizeTemplateSelectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size_option: str = Field(min_length=1, description="当前选中的尺寸方案 ID")
    size_unit: Literal["in", "mm", "cm"] = Field(description="表单显示单位")


class SizeTemplateOptionCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="尺寸方案唯一标识；不传时由名称生成")
    value: str | None = Field(default=None, description="尺寸方案唯一标识，等同 id")
    label: str = Field(min_length=1, description="前端显示的尺寸名称，例如 9*6")
    size_unit: Literal["in", "mm", "cm"] = Field(
        default="in",
        description="当前尺寸值使用的单位",
    )
    single_side_width: float | None = Field(default=None, gt=0, description="单面宽")
    single_side_height: float | None = Field(default=None, gt=0, description="单面高")
    bleed: float | None = Field(default=None, ge=0, description="出血")
    spine_width: float | None = Field(default=None, ge=0, description="背脊宽")
    spine_bleed: float | None = Field(default=None, ge=0, description="背脊出血")
    spine_width_mode: Literal["fixed", "by_page_count"] = Field(
        default="fixed",
        description="背脊宽固定或按页数计算",
    )
    spine_width_formula: dict[str, Any] | None = Field(
        default=None,
        description="按页数计算背脊宽时使用的公式参数",
    )
    select: bool = Field(default=False, description="新增后是否立即选中")


class SizeTemplateOptionUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str | None = Field(default=None, min_length=1, description="尺寸名称")
    size_unit: Literal["in", "mm", "cm"] | None = Field(
        default=None,
        description="尺寸单位",
    )
    single_side_width: float | None = Field(default=None, gt=0, description="单面宽")
    single_side_height: float | None = Field(default=None, gt=0, description="单面高")
    bleed: float | None = Field(default=None, ge=0, description="出血")
    spine_width: float | None = Field(default=None, ge=0, description="背脊宽")
    spine_bleed: float | None = Field(default=None, ge=0, description="背脊出血")
    spine_width_mode: Literal["fixed", "by_page_count"] | None = Field(
        default=None,
        description="背脊宽计算方式",
    )
    spine_width_formula: dict[str, Any] | None = Field(
        default=None,
        description="按页数计算背脊宽时使用的公式参数",
    )
    select: bool = Field(default=False, description="修改后是否切换为当前方案")


class SafeDistanceValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top: float = Field(default=0, ge=0, description="上安全距离")
    right: float = Field(default=0, ge=0, description="右安全距离")
    bottom: float = Field(default=0, ge=0, description="下安全距离")
    left: float = Field(default=0, ge=0, description="左安全距离")


class SizeTemplateOptionV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="尺寸方案 ID，例如 9x6")
    label: str = Field(min_length=1, description="尺寸方案名称，例如 9*6")
    select: bool = Field(default=False, description="是否为当前选中的尺寸方案")
    size_unit: Literal["in", "mm", "cm"] = Field(description="该方案数值的单位")
    single_side_width: float = Field(gt=0, description="单面宽")
    single_side_height: float = Field(gt=0, description="单面高")
    bleed: float = Field(ge=0, description="出血")
    spine_width: float = Field(ge=0, description="背脊宽")
    spine_bleed: float = Field(ge=0, description="背脊出血")


class SizeTemplateCreateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: int | None = Field(default=None, gt=0, description="兼容旧数据的所属店铺 ID")
    product_id: int | None = Field(default=None, gt=0, description="产品分类 ID；新链路优先使用")
    name: str = Field(min_length=1, description="模板名称")
    preview_image: str | None = Field(default=None, description="预览图 OSS 访问链接；也可通过预览图上传接口生成")
    selected_size_option_id: str | None = Field(
        default=None,
        description="当前默认尺寸方案 ID；传 size_options 时可选",
    )
    size_options: list[SizeTemplateOptionV2] | None = Field(
        default=None,
        description="可选的初始尺寸规格；不传则创建后规格为空",
    )
    background_color: str = Field(default="", description="模板背景色")
    paper_thickness_mm: float = Field(
        default=0,
        ge=0,
        description="纸张厚度，单位固定为毫米，不随 display_unit 换算",
    )
    min_spine_width: float = Field(
        default=0,
        ge=0,
        description="最小背脊宽，单位跟随 display_unit",
    )
    max_spine_width: float = Field(
        default=0,
        ge=0,
        description="最大背脊宽，单位跟随 display_unit",
    )
    spine_width_basis: Literal[0, 1] = Field(
        default=0,
        description="背脊依据：0 固定，1 按页数",
    )
    cover_safe_distance: SafeDistanceValues = Field(
        default_factory=lambda: SafeDistanceValues(),
        description="封面安全距离，包含 top、right、bottom、left",
    )
    spine_safe_distance: SafeDistanceValues = Field(
        default_factory=lambda: SafeDistanceValues(),
        description="背脊安全距离，包含 top、right、bottom、left",
    )
    back_cover_safe_distance: SafeDistanceValues = Field(
        default_factory=lambda: SafeDistanceValues(),
        description="封底安全距离，包含 top、right、bottom、left",
    )
    display_unit: Literal["in", "mm", "cm"] = Field(
        default="in",
        description="尺寸编辑时的显示单位",
    )
    page_count: int | None = Field(default=None, ge=0, description="默认页数")
    page_count_options: list[int] = Field(
        default_factory=list,
        description="可选页数数组",
    )
    size_template_info: list[dict[str, Any]] = Field(
        default_factory=list,
        description="该模板拥有的尺寸模板信息对象数组",
    )


class SizeTemplateUpdateV2(BaseModel):
    # Ignore stale read-only/legacy fields when an older editor sends a full
    # detail object back. Only the editable contract below reaches storage.
    model_config = ConfigDict(extra="ignore")

    shop_id: int | None = Field(default=None, gt=0, description="所属店铺 ID")
    product_id: int | None = Field(default=None, gt=0, description="产品分类 ID")
    name: str | None = Field(default=None, min_length=1, description="模板名称")
    preview_image: str | None = Field(default=None, description="预览图 OSS 访问链接；传空字符串可清除")
    background_color: str | None = Field(default=None, description="模板背景色")
    paper_thickness_mm: float | None = Field(
        default=None,
        ge=0,
        description="纸张厚度，单位固定为毫米，不随 display_unit 换算",
    )
    min_spine_width: float | None = Field(
        default=None,
        ge=0,
        description="最小背脊宽，单位跟随 display_unit",
    )
    max_spine_width: float | None = Field(
        default=None,
        ge=0,
        description="最大背脊宽，单位跟随 display_unit",
    )
    spine_width_basis: Literal[0, 1] | None = Field(
        default=None,
        description="背脊依据：0 固定，1 按页数",
    )
    cover_safe_distance: SafeDistanceValues | None = Field(
        default=None,
        description="封面安全距离，包含 top、right、bottom、left",
    )
    spine_safe_distance: SafeDistanceValues | None = Field(
        default=None,
        description="背脊安全距离，包含 top、right、bottom、left",
    )
    back_cover_safe_distance: SafeDistanceValues | None = Field(
        default=None,
        description="封底安全距离，包含 top、right、bottom、left",
    )
    selected_size_option_id: str | None = Field(
        default=None,
        description="当前默认尺寸方案 ID",
    )
    display_unit: Literal["in", "mm", "cm"] | None = Field(
        default=None,
        description="尺寸编辑时的显示单位",
    )
    page_count: int | None = Field(default=None, ge=0, description="默认页数")
    page_count_options: list[int] | None = Field(
        default=None,
        description="可选页数数组",
    )
    size_options: list[SizeTemplateOptionV2] | None = Field(
        default=None,
        description="完整扁平尺寸方案数组；数值按 size_unit 原样保存",
    )
    size_template_info: list[dict[str, Any]] | None = Field(
        default=None,
        description="完整尺寸模板信息对象数组",
    )


class FontLayoutLibraryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: int | None = Field(default=None, gt=0, description="兼容旧链路的所属店铺 ID")
    product_id: int | None = Field(default=None, gt=0, description="所属产品分类 ID")
    name: str = Field(min_length=1, description="字体布局模板名称")
    sort_key: str = Field(
        default="",
        description="用于列表排序和搜索的自定义字符串",
    )
    layers: dict[str, Any] = Field(
        default_factory=dict,
        description="完整图层文档",
    )


class FontLayoutLibraryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: int | None = Field(default=None, gt=0, description="所属店铺 ID")
    product_id: int | None = Field(default=None, gt=0, description="所属产品分类 ID")
    name: str | None = Field(default=None, min_length=1, description="模板名称")
    sort_key: str | None = Field(
        default=None,
        description="用于列表排序和搜索的自定义字符串；空字符串表示清除",
    )
    layers: dict[str, Any] | None = Field(
        default=None,
        description="完整图层文档",
    )


class FontLayoutLibrarySaveAs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="新模板名称")
    shop_id: int | None = Field(default=None, gt=0, description="新模板所属店铺 ID")
    sort_key: str | None = Field(
        default=None,
        description="新模板排序搜索键；不传时继承源模板",
    )


class FontLayoutSizeVariantItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size_option_id: str = Field(
        min_length=1,
        description="尺寸模板中的规格 ID，例如 9x6",
    )
    layers: dict[str, Any] = Field(
        description="该规格对应的完整图层文档；后端按原值保存",
    )


class FontLayoutSizeSync(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size_template_id: int = Field(gt=0, description="当前使用的尺寸模板 ID")
    items: list[FontLayoutSizeVariantItem] = Field(
        min_length=1,
        description="需要同步的一个或多个规格图层",
    )


class FontLayoutTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int = Field(description="所属店铺 ID")
    size_template_id: int = Field(description="所属尺寸模板 ID")
    name: str = Field(default="", description="字体模板名称")
    description: str = Field(default="", description="字体模板描述，由前端定义")
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


class FontLayoutTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    shop_id: int | None = Field(default=None, description="所属店铺 ID")
    size_template_id: int | None = Field(default=None, description="所属尺寸模板 ID")
    name: str | None = Field(default=None, description="字体模板名称")
    description: str | None = Field(default=None, description="字体模板描述，由前端定义")
    font_id: int | None = Field(default=None, description="默认字体库 ID")
    font_name: str | None = Field(default=None, description="默认字体名称")
    layout: dict[str, Any] | list[dict[str, Any]] | None = Field(
        default=None,
        description="文字位置、字体、坐标等排版信息",
    )
    options: dict[str, Any] | None = Field(
        default=None,
        description="下拉项、选项、特殊排版规则",
    )
    fields: dict[str, Any] | None = Field(
        default=None,
        description="前端传回的动态字体模板字段",
    )


class FontTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: int = Field(gt=0, description="所属店铺 ID")
    size_template_id: int = Field(gt=0, description="所属尺寸模板 ID")
    name: str = Field(default="", description="字体布局模板名称")
    description: str = Field(default="", description="字体布局模板描述")
    safe_distance: float = Field(
        default=0,
        ge=0,
        description="安全距离，数值类型，必须大于或等于 0",
    )
    elements: list[dict[str, Any]] = Field(
        default_factory=list,
        description="完整字体布局元素数组",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="字体布局选项",
    )


class FontTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: int | None = Field(default=None, gt=0, description="所属店铺 ID")
    size_template_id: int | None = Field(
        default=None,
        gt=0,
        description="所属尺寸模板 ID",
    )
    name: str | None = Field(default=None, description="字体布局模板名称")
    description: str | None = Field(default=None, description="字体布局模板描述")
    safe_distance: float | None = Field(
        default=None,
        ge=0,
        description="安全距离，数值类型，必须大于或等于 0",
    )
    elements: list[dict[str, Any]] | None = Field(
        default=None,
        description="完整字体布局元素数组",
    )
    options: dict[str, Any] | None = Field(
        default=None,
        description="字体布局选项",
    )


class FontTemplateSizeSync(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size_option: str = Field(
        min_length=1,
        description="当前正在编辑的尺寸方案 ID，作为同步源",
    )
    elements: list[dict[str, Any]] = Field(
        description="当前尺寸方案画布中的完整字体布局元素",
    )
    canvas: dict[str, Any] | None = Field(
        default=None,
        description="可选的设计坐标范围；通常由后端从模板数据自动推导",
    )


class FontCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    font_name: str = Field(description="字体名称")
    font_family: str = Field(default="", description="字体族")
    font_preferred: str = Field(default="", description="CSV 中的首选字体名")
    font_en: str = Field(default="", description="CSV 中的英文字体名")
    font_all_name: str = Field(default="", description="CSV 中的完整字体名称")
    post_script_name: str = Field(default="", description="CSV 中的 PostScript 字体名称")
    file_path: str = Field(default="", description="字体文件路径")
    source: str = Field(default="", description="来源")
    enabled: bool = Field(default=True, description="字体是否启用")
    metadata: dict[str, Any] = Field(default_factory=dict, description="字体扩展信息")


class FontUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    font_name: str | None = Field(default=None, description="字体名称")
    font_family: str | None = Field(default=None, description="字体族")
    font_preferred: str | None = Field(default=None, description="CSV 中的首选字体名")
    font_en: str | None = Field(default=None, description="CSV 中的英文字体名")
    font_all_name: str | None = Field(default=None, description="CSV 中的完整字体名称")
    post_script_name: str | None = Field(default=None, description="CSV 中的 PostScript 字体名称")
    file_path: str | None = Field(default=None, description="字体文件路径")
    source: str | None = Field(default=None, description="字体来源")
    enabled: bool | None = Field(default=None, description="字体是否启用")
    metadata: dict[str, Any] | None = Field(default=None, description="字体扩展信息")


class TemplateFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_json: dict[str, Any] = Field(
        description="模板识别草稿或前端二次编辑后的完整模板 JSON",
    )
    strict_fonts: bool = Field(
        default=True,
        description="是否要求每个文字元素都已经绑定字体 ID、字体文件或字体名称",
    )
    size_template_id: int | None = Field(
        default=None,
        gt=0,
        description="可选的尺寸模板 ID；提供后会创建字体布局模板库记录并挂载到该尺寸模板",
    )


class TemplateSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_json: dict[str, Any] = Field(
        description="前端完成尺寸、颜色、文字和图片编辑后的模板 JSON",
    )
    shop_id: int = Field(gt=0, description="模板所属店铺 ID")
    product_names: list[str] = Field(
        min_length=1,
        description="需要关联此尺寸模板的商品名列表",
    )
    template_name: str = Field(default="", description="尺寸模板名称")
    strict_fonts: bool = Field(
        default=True,
        description="是否要求每个文字元素都绑定字体文件、字体 ID 或 PostScript 字体名",
    )
    size_template_id: int | None = Field(
        default=None,
        gt=0,
        description="可选的现有尺寸模板 ID；不传则新建尺寸模板",
    )
