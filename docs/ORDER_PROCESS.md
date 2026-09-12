# 订单邮件处理与模板匹配流程

本文记录当前邮件订单从接收、商品分类、订单入库到模板匹配和产图的实际流程。
代码变更后应同步更新本文，避免实现规则只存在于代码或聊天记录中。

## 1. 总体流程

```text
IMAP 新邮件
  -> 主题过滤
  -> 读取邮件头和正文
  -> 解析店铺、订单组、商品项
  -> 店铺匹配
  -> 每个商品项匹配 product_names / 产品分类 / 规格
  -> 写入 orders（无论分类是否成功）
  -> 异步发送店铺订单摘要
  -> 有 size_template_id：模板解析、DeepSeek 图层文字、产图和图片通知
  -> 无 size_template_id：保留新订单，等待前端人工关联
```

入口主要位于 `qq_idleCopy.py:process_new_messages`，服务处理位于
`backend/orders/service.py`，商品分类位于
`backend/catalog/mail_product_resolver.py`。

## 2. 邮件解析与店铺匹配

1. 监听器只处理主题符合 Etsy 订单格式的邮件。
2. 先读取 `RFC822.HEADER`，再读取完整 `RFC822` 邮件。
3. 从邮件头保存 `Date` 对应的 `email_sent_at`。
4. 解析店铺原始名称、店铺显示名、订单号、订单总计、收货地址、客户姓名、国家等订单组字段。
5. 一个邮件包含多个 `Transaction ID` 时，按交易块拆成多个商品项；每个商品项独立执行后续商品匹配。
6. 商品信息拆分以邮件中的商品块边界为准，不使用固定字段白名单。每个商品项都会保留自己的商品标题、商品信息、数量、价格和交易编号；标题左侧的 Etsy 商品缩略图会以 `product_image` 字段写入该商品项的 `商品信息` JSON。Etsy 将图片和标题放在两个链接中时，解析器按相同 `Transaction ID` 绑定；`spacer-trans.gif` 等占位图会被忽略；如果真实图片位于标题锚点之后，会回填到刚结束的商品项。多商品邮件按各自交易编号及商品块顺序对应图片，不能共用第一件商品的图片。
7. 店铺按 `shops.shop`、`shops.shop_name` 匹配；还会尝试斜杠分隔店铺名称的组成部分。
8. 店铺匹配失败时跳过该邮件，不写入订单；店铺匹配成功后才进入商品处理。

## 3. 商品分类流程

对邮件中的每一个商品项，调用 `MailProductResolver.resolve()`：

### 3.1 精确命中已有商品名

先按当前店铺和商品标题，在规范化表中查询：

```text
shops -> product_shops -> products -> product_names
```

匹配条件为产品启用且 `product_names.name` 与邮件商品标题不区分大小写、去除首尾空格后完全相等。

如果命中，直接得到 `product_id` 和店铺信息，不调用 DeepSeek。

### 3.2 未命中时添加店铺商品名

如果没有命中已有 `product_names`：

1. 将邮件商品标题写入当前店铺的 `shops.product_names_json`（去重）。
2. 查询当前店铺启用的产品分类候选。
3. 如果没有候选产品，直接返回“待人工关联”，但订单仍必须入库。

### 3.3 DeepSeek 产品语义分类

存在候选产品时，DeepSeek 只负责：

1. 将 listing title 翻译成中文。
2. 判断它与候选产品的产品类型是否相同。
3. 只能返回候选列表内的 `product_id`，不确定时返回 `null`。

DeepSeek 不负责判断尺寸、页数或其他规格。调用失败、返回空结果或未匹配到候选产品时，订单仍入库，等待人工关联。

### 3.4 产品规格判断

匹配到产品后读取产品的 `specification_field`，默认值为：

```text
Book Size | Page Count
```

然后在订单的商品信息对象中按字段名（规范化后不区分大小写）查找字段值。

- 产品配置了 `specifications`：按配置数组逐项判断，订单字段值包含某个规格即视为命中；较长的规格值优先判断。
- 产品没有配置 `specifications`：读取该产品在当前店铺下的尺寸模板选项，使用 `size_option_id` 或选项 `label` 做包含判断。
- 没有订单字段值或没有命中规格：订单仍入库，但不自动关联产品模板。

规格命中后，邮件商品标题才会写入规范化 `product_names` 表，建立商品标题到产品分类的关联。

## 4. 订单入库

商品分类结果不会阻断订单入库。`OrderRepository.upsert()` 会为每个商品项写入一条 `orders` 记录；同一订单号的多个商品项通过 `order_group_id` 归组。

入库保存的关键字段包括：

- `shop_id`、`shop`、`shop_name`
- `product`、`product_information`
- `quantity`、`price`、`transaction_id`
- `order_group_id`、`email_sent_at`
- `size_template_id`
- `status=0`（新订单）

订单成功入库后，订单摘要通知与模板处理相互独立。没有产品分类或没有模板，不影响订单摘要通知。

## 5. `size_template_id` 的当前实际匹配方法

### 5.1 入库时使用的入口

订单入库在 `OrderRepository.upsert()` 中调用 `_resolve_product_link()`，使用：

```text
店铺 + 商品标题 + 商品信息
```

查询商品和模板关联。

### 5.2 优先使用规范化关联表

当以下表都存在时，优先执行规范化查询：

```text
products
product_shops
product_names
```

查询要求：

- 店铺与 `shops.shop` 或 `shops.shop_name` 相等。
- 商品标题与 `product_names.name` 完全匹配（忽略大小写和首尾空格）。
- `products.enabled = 1`。
- 模板通过 `size_templates.product_id = products.id` 且 `size_templates.shop_id = shops.id` 关联。

### 5.3 兼容旧 JSON 关联

如果规范化表不完整，则回退读取：

- `shops.product_names_json`
- `size_templates.product_names_json`（旧字段名为 `product_names` 时兼容读取）

店铺和商品标题仍需匹配，模板按店铺和商品名包含关系筛选。

### 5.4 模板样式编号选择

查询出候选模板后，`_select_template_for_style()` 根据商品信息中的字体样式字段选择模板：

1. 查找字段名规范化后等于 `fontstyleletteringcolor`，或同时包含 `font` 和 `lettering` 的字段。
2. 从字段值中提取样式编号：优先提取 `#数字`，否则提取独立数字。
3. 将候选模板名末尾的 `-#数字`、`_#数字`、`/#数字` 等后缀与样式编号比较。
4. 后缀编号一致时，返回该模板的 `size_template_id`。
5. 没有样式编号或没有找到同编号模板时，返回商品关联结果，但将 `size_template_id` 置为 `NULL`，等待人工处理。

示例：

```text
订单商品信息：Font Style & Lettering color = "#3 and Silver"
模板名称：婚礼签到册-#3
结果：size_template_id = 该模板 ID
```

### 5.5 当前实现的注意事项

商品分类阶段在 `MailProductResolver` 中会先根据产品 `specifications` 或模板规格选项得到一个规格匹配结果；但邮件监听随后调用 `upsert()` 时，当前接口只传入商品标题和商品信息，`upsert()` 会再次执行上述 `_resolve_product_link()`。

因此，当前最终写入 `orders.size_template_id` 的依据是入库阶段的商品名和样式编号匹配结果，不是分类阶段临时返回的规格模板 ID。若要让规格匹配结果成为唯一依据，需要后续把分类结果中的模板 ID 显式传给 `upsert()`，并在入库时校验店铺、产品和规格归属。

## 6. 模板解析与产图

当订单写入后存在 `size_template_id` 时：

1. 读取该店铺、产品对应的 `size_templates`。
2. `OrderTemplateResolver` 根据订单商品信息匹配尺寸选项和页数。
3. 按页数和模板配置计算背脊宽度等渲染参数。
4. 选择字体布局；没有字体布局时跳过产图。
5. 模板包含自然语言图层时调用 DeepSeek 生成图层文字。
6. DeepSeek 未返回全部必需文字时立即停止后续步骤，并发送人工处理异常通知。
7. 成功后保存 `matched_template_json` 和 `resolved_layers_json` 快照。
8. 生成预览图、企业微信辅助图和生产文件，并按店铺机器人配置发送。
9. 预览图发送成功后，订单状态从 `0` 更新为 `1`。

没有 `size_template_id` 的订单不会自动产图，保留为新订单，前端关联商品/模板后可再次执行发送示意图流程。

## 7. 新订单手动关联产品和规格模板

当订单状态为 `0` 且自动分类没有得到模板时，前端使用订单返回的 `shop_id` 作为当前店铺，
依次查询产品和尺寸模板，最后调用：

```http
PUT /api/v1/orders/{order_id}/template-association
```

请求体：

```json
{
  "order_number": "4162641089",
  "product_id": 4,
  "size_template_id": 12,
  "size_option_id": "10x8"
}
```

该接口在一个事务中校验订单状态、订单店铺、产品店铺关联、尺寸模板产品关联和规格归属，
然后按以下优先级读取图层：

```text
size_template_options.layers_json
-> font_layout_size_variants.layers_json
-> font_layout_library.layers_json
```

成功后更新 `orders.size_template_id`、`matched_template_json` 和 `resolved_layers_json`，
状态仍为 `0`，响应只返回一次完整模板 JSON，字段名为 `template_json`。

前端随后调用 `/api/v1/orders/preview-images/send`。发送示意图流程会识别手动关联快照，
保留前端选择的模板和规格，不再被自动模板匹配覆盖；图片发送成功后才推进到状态 `1`。

## 8. 相关代码索引

- 邮件监听与多商品拆分：`qq_idleCopy.py`
- 店铺/商品分类：`backend/catalog/mail_product_resolver.py`
- 商品、模板和规格查询：`backend/catalog/repository.py`
- 订单入库和 `size_template_id` 关联：`backend/orders/repository.py`
- 订单状态、摘要和产图编排：`backend/orders/service.py`
- 尺寸选项、页数和字体布局解析：`backend/templates/order_resolver.py`
