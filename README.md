# Etsy QQ Mail Order Backend

This project turns the original qq_idleCopy.py script into a backend service
with direct Etsy field extraction, QQ IMAP polling, MySQL persistence, JPEG
template previews, and production-file generation.
After a mailbox order image is generated, the backend also sends two image
messages to the configured WeCom group robot: the A4 order information image
first, then the original order preview image.

## Run

Install dependencies:

    pip install -r requirements.txt

Start the backend:

    python run_backend.py

Mailbox polling is controlled by `AUTO_START_MAIL_LISTENER`. Use `false` for
local development and `true` on the single deployed server responsible for
email monitoring.

Default URLs:

- API root: http://127.0.0.1:8000
- Swagger docs: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Authentication

Set BACKEND_API_TOKEN to protect all /api/v1/* endpoints. Frontend requests must then include X-API-Key: your-token.

If BACKEND_API_TOKEN is empty, local API endpoints are open.

## 主要接口

- GET /health - 查询服务健康状态、配置有效性和邮箱监听状态。
- GET /api/v1/shops/ - 分页查询店铺；返回商品数、订单总数、模板数及六种订单状态数量。
- POST /api/v1/shops - 新增店铺；可传 `products` 商品名数组自动关联商品。
- PATCH /api/v1/shops/{shop_id} - 编辑店铺；传 `products` 时替换该店铺的完整商品关联。
- DELETE /api/v1/shops/{shop_id} - 删除店铺。
- GET /api/v1/orders - 查询订单列表。支持分页及订单号、交易编号、店铺、
  店铺 ID、数值状态筛选；使用 `pages` 指定页码（从 `1` 开始），使用
  `limit` 指定每页数量。响应包含 `pages`、`limit`、`total`、`total_pages`，
  每条订单返回 `status`、`status_text`、`status_button_text`。
- GET /api/v1/orders/statuses - 从独立状态表查询全部订单状态和按钮文案。
- POST /api/v1/orders/status/advance - 使用订单 ID 和订单号校验订单，
  并将订单推进到下一状态。
- PUT /api/v1/orders/{order_id}/template-json - 使用订单号校验订单，
  将前端编辑后的 `template_json` 保存回订单；后续预览和生产导出直接复用。
- PUT /api/v1/orders/{order_id}/template-association - 新订单由前端选择产品、尺寸模板和规格，
  后端校验当前店铺归属并读取规格图层，一次性关联 `size_template_id` 和保存订单模板 JSON。
- POST /api/v1/orders/preview-images/send - 生成订单预览图和企业微信订单辅助图，
  两张图发送成功后将订单状态从 0 更新为 1；未关联模板时返回提示且状态不变。
- GET /api/v1/config - 查询已脱敏的运行配置和各项服务配置状态。
- GET /api/v1/orders/monthly-statistics - 查询按店铺和月份累计的 CAD Subtotal、运费、订单数和商品件数。
- GET /api/v1/orders/daily-statistics - 查询按店铺和日期累计的 CAD Subtotal、运费、订单数和商品件数。
- GET /api/v1/events - 建立 SSE 长连接，订阅订单、任务和监听器事件。
- POST /api/v1/events/send - 传入 `msg`，立即向当前所有 SSE 订阅者广播消息。
- POST /api/v1/image-map/render - 接收 image-map-editor 的完整 Fabric JSON，
  按 `workarea` 裁切并直接下载 1-1200 DPI 的 JPG 或 PNG。
- GET /api/v1/personalization-rules - 查询旧版订单定制规则状态的兼容接口。
- POST /api/v1/orders/parse - 将邮件标题和正文解析为结构化订单，不写入数据库。
- POST /api/v1/orders/publish - 写入前端提交的结构化订单。
- POST /api/v1/orders/parse-and-publish - 同步解析邮件订单并写入。
- POST /api/v1/orders/print-image - 仅生成 A4 订单图片的旧版兼容接口，前端生产流程无需调用。
- POST /api/v1/orders/template-export - 从 OSS 读取订单文件夹，临时打包 ZIP 并直接下载。
  请求体只传 `order_id`、`order_number`。接口不会产图、上传文件或修改订单状态；
  ZIP 返回完成或客户端中断后会释放内存缓冲区。

订单邮件处理、商品分类、规格判断、`size_template_id` 的实际匹配规则以及模板产图流程，
统一记录在 [docs/ORDER_PROCESS.md](docs/ORDER_PROCESS.md)。代码修改后请同步更新该文档。

### SSE 消息推送

`POST /api/v1/events/send` 请求体支持普通消息和结构化字段：

```json
{
  "type": "order.saved",
  "msg": "新订单已入库",
  "data": {
    "source": "frontend",
    "order": {
      "id": 632,
      "order_number": "4162641089",
      "shop": "LuxeJoy",
      "shop_name": "3号店",
      "product": "Personalized Wedding Guest Book",
      "shop_id": 1,
      "size_template_id": 12,
      "status": 0,
      "status_text": "新订单",
      "status_button_text": "发送示意图"
    }
  },
  "order_id": 78,
  "status": 3,
  "status_text": "生产中"
}
```

成功响应中的 `data` 是已广播的事件对象。所有当前在线的
`GET /api/v1/events` 订阅者都会收到对应类型的事件；上例会收到 `event: order.saved`：

```text
event: message
data: {"type":"order.saved","msg":"新订单已入库", "data":{"source":"frontend", "order":{"id":632,"status":0,"status_text":"新订单"}, "order_id":632,"status":0,"status_text":"新订单"}}
```

`type` 可以省略，默认是 `message`；模拟真实订单入库时传 `type: "order.saved"`。
`msg` 也可以省略，此时至少传入 `data`、`status`、`status_text` 或 `order_id` 之一。
`msg` 不能为空或只包含空格，接口遵循全局 `X-API-Key` 鉴权配置。

### 订单状态

订单查询项直接返回 `email_sent_at`，其值仅来自原始邮件头的 `Date`，格式为
`YYYY-MM-DD HH:MM:SS`。它表示邮件发送时间；`created_at` 仍表示订单写入数据库的时间。
订单组同时返回 `customer_name`、`country`、`currency_code`、`subtotal_amount`、
`shipping_amount` 和 `order_product_quantity`。月累计统计通过
`GET /api/v1/orders/monthly-statistics?shop_id=1&stat_month=2026-09` 查询；统计只在订单组首次入库时写入，按店铺和月份保存 CAD 的 Subtotal、运费、订单数和商品件数，后续不会回算。
日累计可通过 `GET /api/v1/orders/daily-statistics?shop_id=1&stat_date=2026-09-02` 查询。

配置了 `wecom_robot_webhook_url` 的店铺在新邮件订单入库成功后，会在后台并行向其对应企业微信
机器人发送纯文本订单摘要，同时继续产品分类、模板匹配和预览图生成。摘要发送失败不会阻断订单
后续处理；重复入库的订单不会重复发送。未配置机器人的店铺只跳过通知，不影响订单入库和图片生成。

| `status` | `status_text` | `status_button_text` |
| --- | --- | --- |
| `0` | 新订单 | 发送示意图 |
| `1` | 示意图已发送/客户确认中 | 客户已确认 |
| `2` | 待生产 | 确认生产 |
| `3` | 生产中 | 完成生产 |
| `4` | 待发货 | 已发货 |
| `5` | 订单已完成 | 空字符串 |

`POST /api/v1/orders/status/advance` 请求体：

```json
{
  "order_id": 6,
  "order_number": "4141461118"
}
```

后端按 `order_statuses` 表中的状态数值顺序推进。订单 ID 与订单号不匹配时拒绝请求，
状态 `0` 必须先通过 `/api/v1/orders/preview-images/send` 成功发送两张图片后进入
状态 `1`，不能直接调用推进接口跳过发送。订单已完成后不能继续推进。
`orders` 表只保存状态数值，状态名称和按钮文案从
`order_statuses` 独立表读取。

### 保存订单模板 JSON

`PUT /api/v1/orders/{order_id}/template-json` 请求体：

```json
{
  "order_number": "4141461118",
  "template_json": {
    "version": "7.4.0",
    "objects": []
  }
}
```

`template_json` 必须是 JSON 对象，后端按原值保存。订单列表中的
`template_json` 返回当前保存值；订单 ID 与订单号不匹配时返回 `404`。

### 生产文件导出请求

`POST /api/v1/orders/template-export` 及其兼容地址
`POST /api/v1/orders/export-template` 从 OSS 读取该订单已经生成的文件夹，后端仅在
本次请求中将全部文件打包为 ZIP 并直接返回。此接口不会重新生成文件、不会把 ZIP
上传到 OSS，也不会修改订单状态。请求体如下：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `order_id` | integer | 是 | 本地订单 ID，必须大于 0。 |
| `order_number` | string | 是 | 订单号，必须与 `order_id` 对应的订单一致。 |

Example:

```json
{
  "order_id": 9,
  "order_number": "4141461118"
}
```

成功时响应类型为 `application/zip`，前端应将响应按 Blob 处理并直接触发下载。
ZIP 使用内存流发送，响应完成或客户端中断后后端会立即关闭并释放缓冲区。
`POST /api/v1/orders/template-export/download` 保留为相同功能的兼容地址。

### PSD/图片模板识别

`POST /api/v1/template-imports/analyze` 使用 `multipart/form-data` 接收：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | PSD、PNG、JPG、WEBP、BMP 或 TIFF 文件。 |
| `options_json` | string | 否 | JSON 字符串，可包含区域、字体分配、文字规则、OCR 语言和是否启用 DeepSeek。 |

PSD 优先读取原始图层的文字、坐标框、字体、字号、字距和行距，非文字图层统一作为
`type: "image"` 返回。`font_layout_templates[].elements` 可直接作为字体模板的
`elements` 提交，文字和图片元素无需前端再次转换。普通图片在配置
Tesseract 后读取 OCR 文字块；未配置 OCR 时仍返回画布和参考图片元素，由前端补充文字块。
DeepSeek 只生成字段角色和绑定建议，不修改 PSD/OCR 得到的坐标。

元素位置返回原始像素和区域内比例，不返回 `frame.formula` 或 `font.size_formula`。
前端应把元素关联到 `canvas`、`front_cover`、`back_cover` 或 `spine` 等区域；切换尺寸时使用区域比例重算，
避免书脊宽度变化导致封面元素漂移。

前端编辑完成后调用 `POST /api/v1/template-imports/finalize`：

```json
{
  "strict_fonts": true,
  "size_template_id": 3,
  "template_json": {
    "schema_version": "1.0",
    "document": {
      "reference": {"width": 1000, "height": 500, "dpi": 300},
      "regions": {
        "front_cover": {"x": 550, "y": 20, "width": 400, "height": 460}
      }
    },
    "elements": []
  }
}
```

`strict_fonts` 开启时，每个文字块必须独立绑定字体 ID、字体文件或字体名称。
提供 `size_template_id` 后，字体布局通过 `font_templates.size_template_id`
独立关联到对应尺寸模板；尺寸模板本身不保存完整字体布局。不提供时只返回校验和公式计算后的模板 JSON。

前端完成尺寸、规格和布局编辑后调用 `POST /api/v1/template-imports/save` 保存。
`analyze`/`finalize` 阶段允许尺寸字段为空；`save` 阶段必须填写
`size_unit`、`single_side_width`、`single_side_height`、`bleed`、`spine_width`
和 `spine_bleed`。如果 `size_template.spec.options` 有选项，还必须填写
`spec.selected`。保存会为每个布局分组创建独立的字体布局模板库记录，并挂载到尺寸模板。

每个 `size_template.spec.options[]` 都是一个扁平尺寸方案，固定尺寸结构为
`{id, label, select, size_unit, single_side_width, single_side_height, bleed, spine_width, spine_bleed}`。
尺寸数值按 `size_unit` 原样保存，不生成或持久化 `in/mm/cm` 换算副本。
动态背脊额外使用
`spine_width_mode: "by_page_count"` 与
`spine_width_formula: {"base": 0.4, "reference_page_count": 80, "per_page": 0.01}`。
切换 `spec.selected` 后，服务端会重新套用该方案的全部尺寸字段。

### 尺寸模板与字体布局

`/api/v1/products` 是产品分类及关联维护接口。产品分类（例如婚礼签到册、牧师册）通过
`product_shops` 关联多个店铺，通过 `product_names` 保存用于区分具体订单商品的商品名。
产品保留用于邮件订单自动匹配的 `specifications` 字符串数组；同时支持可选的
`common_spec_values`（常用规格值）对象数组，数据库按 JSON 原样保存前端提供的对象。
产品通过 `spine_width_mode` 明确选择背脊模式：`range`（固定范围）、`formula`（产品公式）
或 `page_count_table`（按页数分段）。`range` 模式继续使用尺寸模板的
`min_spine_width`、`max_spine_width` 和页数选项做线性计算；产品接口不需要保存这两个范围值。
`formula` 模式使用 `spine_width_formula`，公式计算为：
`页数 * page_count_coefficient * page_count_thickness + base_width + additional_width`。
`page_count_table` 模式使用 `spine_width_page_rules`，例如：

```json
{
  "spine_width_mode": "page_count_table",
  "spine_width_page_rules": {
    "unit": "cm",
    "match_strategy": "ceil",
    "items": [
      {"page_count": 10, "spine_width": 1.5, "spine_bleed": 0},
      {"page_count": 20, "spine_width": 1.8, "spine_bleed": 0}
    ]
  }
}
```

`ceil` 会选择不小于当前页数的下一个节点，超出最大节点时使用最大节点。
公式或分段表的单位由 `unit` 指定，结果会临时换算为当前尺寸规格单位。
旧产品未保存 `spine_width_mode` 时，服务端会根据已有公式或分段表推断模式，没有配置时按
`range` 处理。切换模式时只提交对应配置，服务端会清空不生效的另一种配置。
`common_spec_values` 中的字段由前端决定，后端不按产品类型强制校验规格结构；例如特殊出血可以使用
四边对象，普通规格也可以继续使用单个数字。后端会原样保存并返回，是否使用特殊出血由前端自行切换后提交。
字段不传时默认为空数组；PATCH 传入空数组可以清空。也可以使用中文请求键名 `常用规格值`。
例如：

```json
{
  "common_spec_values": [
    {
      "id": "9*6",
      "label": "9*6",
      "unit": "in",
      "pageCount": 50,
      "pageCountOptions": [50, 100],
      "sideWidth": 9,
      "sideHeight": 6,
      "bleed": 0.79,
      "spineWidthMode": "fixed",
      "spineWidth": 0.55,
      "minSpineWidth": 0.55,
      "maxSpineWidth": 0.7,
      "spineBleed": 0.55,
      "paperThickness": 0
    }
  ]
}
```

特殊四边出血示例：

```json
{
  "id": "default",
  "label": "默认规格",
  "unit": "mm",
  "pageCount": null,
  "pageCountOptions": [],
  "sideWidth": 102.02,
  "sideHeight": 140.04,
  "bleed": {
    "top": 0,
    "right": 45.97,
    "bottom": 0,
    "left": 58.0
  },
  "spineWidthMode": "none",
  "spineWidth": null,
  "minSpineWidth": null,
  "maxSpineWidth": null,
  "spineBleed": null,
  "paperThickness": 0
}
```

产品公式示例（照片留言册）：

```json
{
  "spine_width_mode": "formula",
  "spine_width_formula": {
    "unit": "cm",
    "page_count_coefficient": 0.2,
    "page_count_thickness": 0.3,
    "base_width": 1,
    "additional_width": 0.9,
    "spine_bleed": 0
  }
}
```
创建或编辑产品时传入 `spine_width_mode: "formula"` 和该字段即可；编辑时传
`spine_width_mode: "range"` 可恢复尺寸模板范围计算。
`/api/v1/size-templates` 是尺寸模板列表和整体编辑接口。主接口使用 `product_id` 关联产品分类，
返回扁平的 `size_options[]` 和 `size_template_info[]`，不再内嵌产品对象、商品名数组或字体布局。
尺寸模板响应包含可选的 `preview_image` OSS 地址。前端通过
`PUT /api/v1/size-templates/{id}/preview` 上传预览图，文件保存到 OSS 的
`font_layout_image/` 目录，数据库仅保存上传成功后返回的访问链接。

尺寸模板顶层还包含 `background_color`、`min_spine_width`、`max_spine_width`。
`spine_width_basis` 现在使用 `range`、`formula` 或 `page_count_table` 字符串枚举值，
不再返回或接受旧的 `0/1`；产品接口单独返回产品级 `spine_width_mode`。
`use_safe_distance` 控制产图时是否启用文字安全距离监测；为 `false` 时跳过安全区判断、字号自动缩小和越界阻止。
`cover_safe_distance`、
`spine_safe_distance`、`back_cover_safe_distance` 分别保存封面、背脊、封底安全距离，
每项结构均为 `{top, right, bottom, left}`，数值单位跟随 `display_unit`。

字体布局模板是可复用的来源，尺寸模板规格拥有自己的独立图层副本。规格图层保存在
`size_template_options.layers_json`；同步字体布局时会复制到这里，同时保留
`font_layout_size_variants` 历史快照兼容旧客户端。后续直接编辑规格图层不会修改字体布局模板，
删除字体布局模板也不会影响已复制的规格图层。没有规格副本时才回退到字体布局基础图层。

`/api/v1/inner-page-templates` 管理独立的内页模板库。内页模板必须关联一个店铺和一个产品分类，
并在 `size_options[]` 子记录中保存每个规格的完整 `layers` 图层文档。编辑时可以更换 `product_id`，
保存名称、描述和 `preview_image` 标识图 OSS 链接。
内页模板不关联尺寸模板或字体布局。标识图先通过公共图片上传接口
`POST /api/v1/uploads/images` 取得 OSS 链接，再由新增或编辑接口的
`preview_image` 字段保存到模板记录。

#### 内页模板接口（前端调用说明）

接口前缀：`/api/v1`。所有接口遵循统一响应结构：

```json
{
  "success": true,
  "code": "OK",
  "message": "操作成功",
  "data": {},
  "error": null
}
```

内页模板字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `shop_id` | integer | 创建时是 | 所属店铺 ID；编辑时不传则沿用原店铺。 |
| `product_id` | integer | 是 | 关联产品分类 ID，必须属于该店铺；编辑时可更换。 |
| `name` | string | 创建时是 | 模板名称。 |
| `description` | string | 否 | 模板说明。 |
| `preview_image` | string | 否 | 标识图 OSS 地址；推荐先使用上传接口。 |
| `size_options` | array | 创建时是 | 内页规格数组；每项必须包含 `id`、`label`、`size_unit`、`layers`。 |

规格字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 规格业务 ID，例如 `9*6`、`10x12`；同一模板内不能重复。 |
| `label` | string | 前端显示名称。 |
| `size_unit` | string | 必填，支持 `in`、`mm`、`cm`；后端按提交值原样保存，不换算。 |
| `layers` | object | 当前规格完整图层 JSON，后端原样保存。 |

1. 查询内页模板列表

```http
GET /api/v1/inner-page-templates?shop_id=1&product_id=4&search=婚礼&limit=50&offset=0
```

筛选参数都可省略：`shop_id`、`product_id`、`search`。返回的 `data` 是分页对象：

```json
{
  "items": [
    {
      "id": 1,
      "shop_id": 1,
      "shop": "LuxeJoy",
      "shop_name": "3号店",
      "product_id": 4,
      "product": {"id": 4, "name": "婚礼签到册"},
      "name": "婚礼签到册内页",
      "description": "通用内页",
      "preview_image": "https://oss.example/inner-page.png",
      "size_options": [
        {"id": "9*6", "label": "9*6", "size_unit": "in", "layers": {"objects": []}}
      ],
      "created_at": "2026-09-01T07:00:00+00:00",
      "updated_at": "2026-09-01T07:00:00+00:00"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

2. 新增内页模板

```http
POST /api/v1/inner-page-templates
Content-Type: application/json
```

```json
{
  "shop_id": 1,
  "product_id": 4,
  "name": "婚礼签到册内页",
  "description": "通用内页",
  "size_options": [
    {
      "id": "9*6",
      "label": "9*6",
      "size_unit": "in",
      "layers": {"objects": [], "canvas": {"width": 1200, "height": 800}}
    },
    {
      "id": "10x12",
      "label": "10x12",
      "size_unit": "in",
      "layers": {"objects": []}
    }
  ]
}
```

创建成功后，使用返回的 `data.id` 上传标识图或继续编辑。

3. 查询详情

```http
GET /api/v1/inner-page-templates/{id}
```

详情返回完整 `size_options[]` 和每个规格的 `layers`。前端切换规格时，在数组中按 `id` 查找对应图层即可。

4. 编辑内页模板

```http
PATCH /api/v1/inner-page-templates/{id}
Content-Type: application/json
```

`product_id` 必须传入，可以修改关联产品；`shop_id` 可选，传入时可以同时修改店铺。只修改名称时：

```json
{
  "shop_id": 1,
  "product_id": 4,
  "name": "婚礼签到册内页-新版"
}
```

修改规格图层时传入完整的 `size_options[]`：

```json
{
  "shop_id": 1,
  "product_id": 4,
  "size_options": [
    {
      "id": "9*6",
      "label": "9*6",
      "size_unit": "in",
      "layers": {"objects": [{"type": "textbox", "text": "签到"}]}
    }
  ]
}
```

传 `size_options` 时会用提交的数组替换当前规格列表；不传 `size_options` 时保留已有规格和图层。

5. 单独新增一个规格

```http
POST /api/v1/inner-page-templates/{template_id}/size-options
Content-Type: application/json
```

```json
{
  "id": "12*12",
  "label": "12*12",
  "size_unit": "in",
  "layers": {"objects": []}
}
```

该接口只新增请求中的一个规格，不影响其他规格。`id` 在同一内页模板中不能重复。
成功响应的 `data` 是新增后的单个规格。

6. 保存当前规格

```http
PATCH /api/v1/inner-page-templates/{template_id}/size-options/{size_option_id}
Content-Type: application/json
```

`label`、`size_unit`、`layers` 可以只传需要修改的字段，未传字段保持原值。例如只保存图层：

```json
{
  "layers": {
    "objects": [{"type": "textbox", "text": "签到"}]
  }
}
```

`size_option_id` 是规格的业务 ID，不允许通过 PATCH 修改。路径参数应使用
`encodeURIComponent(sizeOptionId)` 编码。

7. 删除当前规格

```http
DELETE /api/v1/inner-page-templates/{template_id}/size-options/{size_option_id}
```

该接口只删除指定规格，不影响主模板和其他规格。

8. 上传标识图到 OSS

```http
POST /api/v1/uploads/images
Content-Type: multipart/form-data
```

表单字段名必须是 `file`，支持 `PNG`、`JPG`、`JPEG`、`WEBP`，文件最大 10MB。
图片统一上传到 OSS 的 `font_layout_image/` 目录。
该接口不需要模板 ID，也不会修改数据库；前端取得链接后，在新增或编辑模板时通过
`preview_image` 提交并保存。
上传成功后只返回 OSS 链接：

```json
{
  "success": true,
  "code": "OK",
  "message": "内页模板标识图上传成功",
  "data": {
    "url": "https://fakestar-oss.oss-us-west-1.aliyuncs.com/inner_page_template_image/inner-page-template-12-xxx.png"
  },
  "error": null
}
```

前端只需要读取 `response.data.url`。

9. 删除内页模板

```http
DELETE /api/v1/inner-page-templates/{id}
```

删除主模板时，其 `inner_page_template_options` 规格记录会通过外键自动删除。

推荐调用顺序：新增模板 → 上传标识图（可选）→ 查询详情并按规格编辑 → 使用规格级 POST/PATCH 保存。修改 `product_id` 时，新产品必须已经关联目标店铺（传入的 `shop_id` 或原店铺），否则返回 `产品未关联该店铺`。

常见错误：模板或规格不存在返回 `404`；`product_id` 缺失返回 `422`；产品不存在或产品未关联店铺返回 `400`；规格 ID 重复、缺少单位、缺少图层或规格为空返回 `400`。数据库处于 RDS `LOCK_WRITE` 只读锁定时返回 `503`，解除锁定后重试即可。

`/api/v1/font-layout-templates` 管理可复用的字体布局模板库，支持新增、查询、修改、
删除、另存为，以及通过 `PUT /font-layout-templates/{id}/preview` 手动上传可选预览图。
模板库记录包含产品分类、兼容的店铺、名称、`sort_key`、预览图和原始 `layers[]`，不再绑定某个尺寸模板。
`sort_key` 是可选字符串，列表将非空值优先升序排列；`search` 同时匹配名称和该字段。
删除字体布局模板会物理删除模板库主记录，但已应用到尺寸模板的规格图层 JSON 和尺寸模板当前布局引用都会保留；此时通过尺寸模板上下文仍可读取已保存的规格图层，未保存规格没有基础图层可回退。

保存某个字体布局模板的规格图层使用：

```http
POST /api/v1/font-layout-templates/{font_layout_id}/sync-size-options
```

请求体必须包含尺寸模板 ID 和需要保存的规格；接口只处理 `items` 中传入的规格，
不会把其他规格的图层重新同步：

```json
{
  "size_template_id": 78,
  "items": [
    {
      "size_option_id": "9*6",
      "layers": {"objects": []}
    }
  ]
}
```

成功消息格式为：
`已同步 1 个规格（9*6），当前尺寸模板已切换到该字体布局`。
新增规格时必须先通过 `PATCH /api/v1/size-templates/{id}` 保存规格，
再调用本同步接口保存该规格图层；已有规格只修改图层时可直接调用本接口。

- GET /api/v1/tasks/mail-listener - 查询邮箱监听状态。
- POST /api/v1/tasks/mail-listener/start - 启动 QQ 邮箱持续监听。
- POST /api/v1/tasks/mail-listener/stop - 停止邮箱持续监听。
- POST /api/v1/tasks/mail-poll - 提交一次异步邮箱拉取任务。
- POST /api/v1/tasks/orders/parse-and-publish - 异步解析并写入订单。
- GET /api/v1/tasks/jobs - 查询后台任务列表。
- GET /api/v1/tasks/jobs/{task_id} - 查询单个后台任务详情。

### 邮件新订单处理流程

持续监听由 `AUTO_START_MAIL_LISTENER` 控制。服务器通常设置为 `true`，本地开发设置为
`false`；监听器使用 `qq_imap_state.json` 保存最近处理的邮件 UID。

收到邮箱 `EXISTS` 事件后，服务会搜索 UID 大于上次记录值的邮件，并按以下顺序处理：

1. 检查邮件主题是否符合 Etsy 成交通知格式。不符合的邮件直接跳过并推进 UID。
2. 读取邮件正文，解析订单号、店铺、商品名、商品信息、数量、价格和收货地址；邮件底部的 `Order total`、折扣、运费和税费明细作为订单组总计保存。同一封邮件按 `Transaction ID` 拆分为多个独立商品项；共享的订单号、付款、收货和总计信息归入 `order_groups`，每个商品项单独写入 `orders`。商品标题左侧的缩略图 URL 会以 `product_image` 写入对应商品项的 `商品信息` JSON；图片链接与标题链接分离时按相同 `Transaction ID` 绑定，多商品不会串图，Etsy 的透明占位图会过滤掉。
3. 匹配 `shops` 店铺。店铺不存在时跳过订单并推进 UID。
4. 在当前店铺的商品名中查找商品：
   - 先查产品分类的标准化 `product_names`；
   - 未命中时，将原始商品标题追加到当前店铺的 `shops.product_names_json`，防止下次重复识别；
   - 如果当前店铺存在启用的候选产品，再调用 DeepSeek 做商品语义分类；
   - 分类成功后，读取产品的 `specification_field`。产品配置了 `specifications` 时，
     判断订单字段值是否包含其中的规格；产品未配置规格（空数组）时，改为在该产品
     关联尺寸模板的规格列表中判断是否包含订单字段值；
   - 语义分类和规格都成功时，才把原始商品标题写入对应产品的 `product_names`。
5. 商品未命中、店铺没有候选产品、DeepSeek 分类失败或规格不匹配，都不会阻止订单入库。
   这些情况只表示订单暂时没有自动关联产品或模板，后续由前端人工处理。
6. 每个商品项分别写入 `orders` 表，初始状态为 `0`（新订单），并通过 `order_group_id` 归属订单组；每个商品项分别发布 `order.saved` SSE 事件。
   自动商品关联失败时，`shop_id` 可以正常保存，`size_template_id` 为空。
7. 如果订单没有尺寸模板，服务停止后续图片生成，订单保持状态 `0`，前端可先关联产品和模板，
   再调用发送示意图接口。
8. 如果订单已关联尺寸模板，服务继续生成预览图和企业微信辅助图，并发送企业微信。发送成功后，
   订单从状态 `0` 自动进入状态 `1`；图片生成或发送失败时，订单不会因失败被推进到下一状态。

商品分类失败的订单项仍会推进邮件 UID，因为订单已经完成邮件处理并进入订单库；数据库写入、解析等
真正的处理异常会保留 UID，等待监听器重试。IMAP 连接出现 `socket error: EOF` 时，监听器会断开并
自动重连，不会修改已经保存的 UID。

## Configuration

The code still has local defaults for compatibility, but every important value can be overridden with environment variables. See .env.example for the full list.
The backend also loads a local `.env` file from the project root on startup;
keep real secrets there and leave `.env.example` as a safe template.

### Image Map Python headless renderer

The Fabric renderer used by order previews and direct JPG/PNG exports lives
under `image-map-headless-renderer/` and is called directly from Python. Install
the root Python requirements once:

```powershell
pip install -r requirements.txt
```

Then enable it in the Python service `.env` and restart the backend:

```dotenv
IMAGE_MAP_RENDERER_ENABLED=true
IMAGE_MAP_RENDERER_TIMEOUT_SECONDS=120
IMAGE_MAP_RENDERER_BROWSER=
IMAGE_MAP_RENDERER_RECYCLE_AFTER=100
IMAGE_MAP_RENDERER_RETRY_ATTEMPTS=2
IMAGE_MAP_RESOURCE_RETRY_ATTEMPTS=3
IMAGE_MAP_RESOURCE_RETRY_DELAY_SECONDS=1
IMAGE_MAP_RESOURCE_CACHE_MAX_BYTES=67108864
IMAGE_MAP_RENDERER_MAX_PENDING=4
IMAGE_MAP_RENDERER_MAX_OUTPUT_PIXELS=50000000
IMAGE_MAP_RENDERER_NO_SANDBOX=false
```

The service owns one dedicated rendering thread and reuses one Chromium page.
It recycles Chromium after `IMAGE_MAP_RENDERER_RECYCLE_AFTER` jobs, retries
browser failures after rebuilding the process, and retries remote font/image
downloads with exponential backoff. The resource LRU cache is bounded by
`IMAGE_MAP_RESOURCE_CACHE_MAX_BYTES`. `POST /api/v1/image-map/render` exposes
the same renderer to the frontend and returns the selected image directly.
The pending-job limit prevents an unbounded in-memory queue, and the output
pixel limit rejects oversized DPI/canvas combinations before Chromium starts.
Each Uvicorn worker owns one Chromium instance, so keep one worker unless the
server memory budget explicitly allows more.

Fabric order previews export a native SVG from the same restored canvas as the
JPG. Text and shape nodes remain vector data, and loaded fonts are embedded as
data URLs so the SVG does not depend on fonts installed on the viewing machine.

#### Product safe-distance monitoring

安全距离是产品级配置，不以订单模板 JSON 为准。订单产图先通过订单关联的
`size_template_id` 找到 `product_id`，再从产品接口对应的产品记录读取
`cover_safe_distance`、`spine_safe_distance` 和 `back_cover_safe_distance` 三组
四边值；后端将这份产品配置作为本次渲染的只读 `safe_distances` 参数传入无头渲染器。
当 `use_safe_distance` 为 `false` 时，渲染器跳过安全区检测、自动缩小字号和越界阻止；为 `true`（默认）时执行完整监测。
渲染器不根据前端 JSON 猜测产品，也不把安全距离写回订单模板快照。

文字安全区监测顺序固定如下：

1. 读取对象的 `horizontalCentered` 和 `verticalCentered` 标识。
2. 有标识的轴先在对象当前所属的封面、背脊、封底或双排画布区域内居中；没有标识的轴保持原位置。
3. 使用产品对应区域的四边安全距离计算安全矩形，并测量文字的真实旋转后边界。
4. 文字越界时每次缩小 `0.5px` 后重新测量，直到边界合规；字号降到 `1px` 仍越界则拒绝导出。
5. 字号调整完成后，再次按原有居中标识执行居中，确保缩小后的最终文字仍处在对应区域中心；没有标识的轴不执行这一步。

每次监测需要保留产品 ID、三组安全距离、模板 ID、尺寸方案、对象 ID/名称、对象所属区域、
居中标识、是否发生位置调整、原始字号、最终字号、是否越界和最终导出结果。渲染器返回的
`resolved_json`/`warnings` 是本次实际使用的监测结果；产品配置缺失时必须记录告警，不能把
兼容默认值误认为产品配置。

The launcher uses `IMAGE_MAP_RENDERER_BROWSER` first, then installed Edge,
Chrome, or Chromium. A Linux server without a system browser must install the
Playwright Chromium binary and its OS libraries once during deployment:

```bash
python -m playwright install --with-deps chromium
```

If Chrome/Chromium is already managed by the server image, set
`IMAGE_MAP_RENDERER_BROWSER` to its executable path instead; no Playwright
browser download is then required.

Default CORS origins include common local frontend ports 3000, 5173, 5174, and 5175 on both localhost and 127.0.0.1. If your frontend uses another origin, add it to CORS_ORIGINS and restart the backend.

Important files:

- qq_idleCopy.py - core mail parsing and integrations.
- backend/main.py - FastAPI app and HTTP endpoints.
- backend/task_manager.py - listener and async task management.

Order preview images, A4 print images, SVG production files, WeCom auxiliary
images, and grouped product-information text are saved under `generated_orders/` by default. Set
`ORDER_FILES_DIR` to change the root directory. Each order gets a directory
named `店铺名-订单号-YYYYMMDD`; generated files use the same stem. A completed order directory
contains:

- `店铺名-订单号-YYYYMMDD-预览图.jpg`
- `店铺名-订单号-YYYYMMDD.svg`
- `店铺名-订单号-YYYYMMDD-转曲.svg`
- `店铺名-订单号-YYYYMMDD-企业微信.jpg`
- `店铺名-订单号-YYYYMMDD-生产单.jpg`
- `店铺名-订单号-YYYYMMDD-要求.txt`

After all required files are complete, each file is uploaded under the same OSS
order prefix (OSS has no physical folders). The database records each artifact
and its OSS URL. The text file lists all numbered source product-information
lines first, followed by all numbered Chinese translations. ZIP is created only by the download endpoint and is not
persisted or uploaded during production.

When a new order preview is generated, the renderer first uses the order's
product information to select the matching option in the associated size
template (for example `9x6` or `10x12`). It then matches the value of the
`Font Style & Lettering color` product-information field against the names of
font layouts attached to that size template. When `DEEPSEEK_API_KEY` is
configured, the same product information and each selected layout element's
`rule.description` are sent to DeepSeek. DeepSeek returns only structured
values (selected option/layout and text per element); the local renderer still
performs all unit conversion, responsive positioning, real-font measurement,
rotation, boundary correction, and image drawing. If DeepSeek is unavailable,
the local field/rule evaluator remains the fallback. Matching layout elements
are rendered with their stored coordinates, fonts, colors, rotations, and text
generation rules.
WeCom order information images remain separate and use
`WECOM_ORDER_INFO_IMAGE_DIR`. Configure each shop's robot through
`wecom_robot_webhook_url` on `POST /api/v1/shops` or
`PATCH /api/v1/shops/{shop_id}`. New-order summaries, order preview images,
WeCom order information images, and automation-exception messages all use the
robot configured for the order's shop. A shop without a robot still persists
and processes orders, but skips automatic notifications; the manual preview
send endpoint returns a configuration error. There is no global robot or
environment-variable fallback.
Set `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_BUCKET`, `OSS_REGION`,
and `OSS_ENDPOINT` to upload the completed order artifacts to Aliyun OSS after
their local copies are written. Access keys should be provided through
environment variables, not committed into source files.

Printable order sheets use `ORDER_PRINT_IMAGE_DPI` (300 by default). Product
information is translated to Chinese with the configured DeepSeek API before
the sheet is rendered.

PSD export uses PhotoshopAPI and EPS export uses PyX; neither requires
Photoshop on the server.

## Notes

- The continuous listener and one-time poll share qq_imap_state.json.
- Before product parsing, mailbox orders must match
  an existing row in the MySQL `shops` table. Missing shops are skipped with a
  `系统没有此店铺` console message and advance UID.
- Etsy option fields are extracted directly by their email labels. Wrapped
  lines under `Names/date/location for the cover` are joined into one value.
  Product titles are read from the text immediately before the first option
  field; the obsolete `规格/尺寸` field is not emitted or stored.
- Product option labels are not limited to a fixed list. The text between the
  product title and `Shop:` is parsed into the single `商品信息` object. MySQL
  stores it directly in `orders.product_information`; the order API restores
  the object for dynamic frontend columns. No `product_information_json`
  column or child information table is used.
- When a mail order's product name is not in the matched shop's normalized
  `product_names`, the original title is first appended to that shop's
  `shops.product_names_json` list. The order is then inserted even when no
  product category, specification, or size template can be resolved. In that
  case `shop_id` is saved, `size_template_id` remains empty, and the order
  stays at status `0` until the frontend completes the association. When a
  product category is matched, its `product_shops`, `product_names`,
  `specification_field`, `specifications`, and `size_templates.product_id`
  relations are used to resolve the production template.
- MySQL stores order values under English field names. Each business field has
  a matching `<field_name>_text` column containing its Chinese display name.
  Legacy `personalization_json` and `personalization_text` columns are removed
  automatically when the order repository initializes.
- The former `personalization_rules.json` shop/product rules file has been
  removed. Mail product fields are parsed from the product block directly.
- QQ IMAP polling runs in a worker thread so HTTP requests do not block the event loop.
# MySQL 数据库配置

项目的订单、商品目录和模板仓储共用 MySQL 数据库。在 `.env` 设置：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=你的密码
MYSQL_DATABASE=dcjt
MYSQL_CHARSET=utf8mb4
```

也可以直接设置 `DATABASE_URL=mysql://用户:密码@主机:3306/dcjt?charset=utf8mb4`。
### 店铺订单状态计数

`GET /api/v1/shops/` 和 `GET /api/v1/shops/{shop_id}` 返回店铺基础统计字段，
并按 `orders.status` 返回当前店铺订单数量：

| 字段 | 状态值 | 含义 |
| --- | ---: | --- |
| `new_order_count` | 0 | 新订单 |
| `confirmation_count` | 1 | 示意图已发送/客户确认中 |
| `pending_production_count` | 2 | 待生产 |
| `in_production_count` | 3 | 生产中 |
| `pending_shipment_count` | 4 | 待发货 |
| `completed_order_count` | 5 | 订单已完成 |

没有处于某状态的订单时，对应字段为 `0`。订单新增、修改状态或推进状态后会同步刷新店铺计数；服务启动时也会根据历史订单回填。

店铺新增示例：

```json
{
  "shop": "LuxeJoy",
  "shop_name": "3号店",
  "products": ["婚礼签到册", "牧师册"]
}
```

店铺列表中每个 `items[]` 元素会额外包含：

```json
{
  "product_count": 2,
  "order_count": 8,
  "new_order_count": 2,
  "confirmation_count": 1,
  "pending_production_count": 1,
  "in_production_count": 2,
  "pending_shipment_count": 1,
  "completed_order_count": 1
}
```
