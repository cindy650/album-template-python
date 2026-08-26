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
- GET /api/v1/orders - 查询订单列表。支持分页及订单号、交易编号、店铺、
  店铺 ID、数值状态筛选；使用 `pages` 指定页码（从 `1` 开始），使用
  `limit` 指定每页数量。响应包含 `pages`、`limit`、`total`、`total_pages`，
  每条订单返回 `status`、`status_text`、`status_button_text`。
- GET /api/v1/orders/statuses - 从独立状态表查询全部订单状态和按钮文案。
- POST /api/v1/orders/status/advance - 使用订单 ID 和订单号校验订单，
  并将订单推进到下一状态。
- PUT /api/v1/orders/{order_id}/template-json - 使用订单号校验订单，
  将前端编辑后的 `template_json` 保存回订单；后续预览和生产导出直接复用。
- POST /api/v1/orders/preview-images/send - 生成订单预览图和企业微信订单辅助图，
  两张图发送成功后将订单状态从 0 更新为 1；未关联模板时返回提示且状态不变。
- GET /api/v1/config - 查询已脱敏的运行配置和各项服务配置状态。
- POST /api/v1/image-map/render - 接收 image-map-editor 的完整 Fabric JSON，
  按 `workarea` 裁切并直接下载 1-1200 DPI 的 JPG 或 PNG。
- GET /api/v1/personalization-rules - 查询旧版订单定制规则状态的兼容接口。
- POST /api/v1/orders/parse - 将邮件标题和正文解析为结构化订单，不写入数据库。
- POST /api/v1/orders/publish - 写入前端提交的结构化订单。
- POST /api/v1/orders/parse-and-publish - 同步解析邮件订单并写入。
- POST /api/v1/orders/print-image - 仅生成 A4 订单图片的旧版兼容接口，前端生产流程无需调用。
- POST /api/v1/orders/template-export - 确认生产并检查订单的四个固定生产文件。
  请求体传 `order_id`、`order_number`；可选的 `template_json` 只覆盖本次导出的视觉字段，
  店铺和商品等关联信息仍使用数据库模板。旧版 `format` 参数仍可传入但会被忽略。
  固定产物为订单预览图、SVG、企业微信辅助图和 A4 生产单，不再生成源文件。
  四个文件全部本地生成成功后，后端才统一上传 OSS 并记录每个文件的 OSS 地址。
  上传成功后订单变更为 `status: 3`、`status_text: "生产中"`。

### 订单状态

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
`POST /api/v1/orders/export-template` 接收以下 JSON 请求体：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `order_id` | integer | 是 | 本地订单 ID，必须大于 0。 |
| `order_number` | string | 是 | 订单号，必须与 `order_id` 对应的订单一致。 |
| `format` | string | 否 | 旧版兼容参数，可省略；当前不会根据该参数生成源文件。 |
| `template_json` | object | 否 | 前端当前模板 JSON；不传时使用数据库模板。视觉字段只覆盖本次导出，不能修改 `id`、`shop_id`、`shop`、`shop_name`、`product_names` 等关联字段。 |

Example:

```json
{
  "order_id": 9,
  "order_number": "4141461118",
  "template_json": {
    "cover_text": "Wedding Guest Book",
    "cover_names": "Alice & Bob",
    "cover_wedding_date": "2026.08.12",
    "fields": {
      "cover_subtitle": "Our Wedding Day"
    }
  }
}
```

成功响应返回四个文件的文件名、本地记录和各自 OSS 上传结果。下载接口只在请求时将
这四个现有文件临时打包为 ZIP，不会保存或上传 ZIP。

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
`/api/v1/size-templates` 是尺寸模板列表和整体编辑接口。主接口使用 `product_id` 关联产品分类，
返回扁平的 `size_options[]` 和 `size_template_info[]`，不再内嵌产品对象、商品名数组或字体布局。
尺寸模板响应包含可选的 `preview_image` OSS 地址。前端通过
`PUT /api/v1/size-templates/{id}/preview` 上传预览图，文件保存到 OSS 的
`font_layout_image/` 目录，数据库仅保存上传成功后返回的访问链接。

尺寸模板顶层还包含 `background_color`、`min_spine_width`、`max_spine_width` 和
`spine_width_basis`（`0` 固定、`1` 按页数）。`cover_safe_distance`、
`spine_safe_distance`、`back_cover_safe_distance` 分别保存封面、背脊、封底安全距离，
每项结构均为 `{top, right, bottom, left}`，数值单位跟随 `display_unit`。

`font_layouts[]` 中的每项是挂载到当前尺寸模板的布局实例，结构为
`{id, name, font_layout_template_id, size_layouts:[{size_option_id, layers, canvas}]}`。
每个尺寸方案都有自己的图层本体。
`GET /size-templates/{id}/font-layouts` 单独返回 `{size_template_id, items, total}`，
前端在添加、删除或修改布局后可直接调用该接口刷新已应用列表。
PUT /size-templates/{id}/font-layouts/{layoutId}/sizes/{sizeOptionId}` 只保存当前尺寸；
`POST .../sync` 可把一个尺寸的布局按物理尺寸缩放或原样复制到其他尺寸。

`/api/v1/font-layout-templates` 管理可复用的字体布局模板库，支持新增、查询、修改、
删除、另存为，以及通过 `PUT /font-layout-templates/{id}/preview` 手动上传可选预览图。
模板库记录包含产品分类、兼容的店铺、名称、`sort_key`、预览图和原始 `layers[]`，不再绑定某个尺寸模板。
`sort_key` 是可选字符串，列表将非空值优先升序排列；`search` 同时匹配名称和该字段。

- GET /api/v1/tasks/mail-listener - 查询邮箱监听状态。
- POST /api/v1/tasks/mail-listener/start - 启动 QQ 邮箱持续监听。
- POST /api/v1/tasks/mail-listener/stop - 停止邮箱持续监听。
- POST /api/v1/tasks/mail-poll - 提交一次异步邮箱拉取任务。
- POST /api/v1/tasks/orders/parse-and-publish - 异步解析并写入订单。
- GET /api/v1/tasks/jobs - 查询后台任务列表。
- GET /api/v1/tasks/jobs/{task_id} - 查询单个后台任务详情。

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

Order preview images, A4 print images, SVG production files, and WeCom
auxiliary images are saved under `generated_orders/` by default. Set
`ORDER_FILES_DIR` to change the root directory. Each order gets a directory
named `店铺名-订单号-YYYYMMDD`; generated files use the same stem. A completed order directory
contains:

- `店铺名-订单号-YYYYMMDD-预览图.jpg`
- `店铺名-订单号-YYYYMMDD.svg`
- `店铺名-订单号-YYYYMMDD-企业微信.jpg`
- `店铺名-订单号-YYYYMMDD-生产单.jpg`

After all required files are complete, each file is uploaded under the same OSS
order prefix (OSS has no physical folders). The database records each artifact
and its OSS URL. ZIP is created only by the download endpoint and is not
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
`WECOM_ORDER_INFO_IMAGE_DIR`.
Set `WECOM_ROBOT_WEBHOOK_URL` to override or disable the WeCom group robot
webhook. WeCom notifications send two image messages: first the A4 order
information image, then the original order preview image.
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
- Before an order is inserted or updated, its product name must exist in the
  matched shop's legacy whitelist or the normalized `product_names` table.
  A matching product classification and its `size_templates.product_id` are
  then used to resolve the production template; the legacy JSON columns remain
  readable for existing orders.
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
