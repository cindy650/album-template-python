# 订单模板领域上下文

## 核心术语

- **订单**：包含订单号、店铺、商品名和 `product_information` 的业务记录。订单入库后保存 `shop_id` 与 `size_template_id`，后续输出都以这两个关联字段为入口。
- **店铺商品关联**：店铺的商品白名单与尺寸模板的商品名列表共同决定某个订单能否匹配模板。
- **尺寸模板**：一个店铺商品对应的物理尺寸集合，包含一个或多个 `size_spec.options[]`。每个 option 是一个独立规格，至少描述 `size_unit`、单面宽、单面高、出血、背脊宽和背脊出血。
- **尺寸规格**：尺寸模板中的一个 option。它是本次订单渲染的最终物理尺寸，不是字体布局。
- **字体布局实例**：尺寸模板当前挂载的布局，具有 `id`、`name` 和每个尺寸规格对应的 `size_layouts[]`。同一布局在不同尺寸下可以拥有不同的图层数据。
- **图层模板**：字体布局实例中某个尺寸的 `layers`/`elements` 与 `canvas`。图层数据是预览图、生产单、SVG、PSD 的共同视觉来源。
- **渲染上下文**：一次订单输出确定后的不可变结果，至少包含订单、尺寸模板、选中的尺寸规格、选中的字体布局、该尺寸的图层、画布和 DeepSeek 生成的文字值。

## 订单到模板的解析顺序

1. 订单解析出店铺、商品名和商品信息，并写入订单库。
2. 入库关联阶段按店铺标识（`shop`、`shop_name` 及店铺路径片段）和商品名查找商品关联；命中后保存 `shop_id` 与 `size_template_id`。这里选择的是尺寸模板，不选择具体尺寸 option。
3. 输出阶段必须读取订单库中的订单，并用 `size_template_id` 加载完整尺寸模板；如果接口允许前端传 `template_json`，它只能覆盖本次视觉数据，不能改变订单的店铺、商品和模板归属。
4. 从订单 `product_information` 中提取尺寸/页数/大小相关字段，与 `size_spec.options[]` 的 `id`、`label`、`value` 和尺寸表达式匹配。优先匹配明确的规格文本或尺寸数值对；未命中时使用模板已选 `size_spec.selected`，不能让模型猜尺寸。
5. 选定 option 后，将它的物理字段覆盖到本次渲染上下文，并计算目标画布：宽度为 `单面宽*2 + 背脊宽 + 出血*2 + 背脊出血*2`，高度为 `单面高 + 出血*2`，再按 `size_unit` 和 DPI 转成像素。
6. 在已选尺寸的布局实例中按订单 `Font Style & Lettering color` 与布局 `name` 匹配。布局名称支持订单值带颜色后缀的情况，优先最长匹配；没有匹配时不能把其他布局当成当前布局。
7. 从字体布局实例的 `size_layouts[]` 选择与已选 option 对应的布局图层；没有该尺寸专属图层时才使用布局基础图层，并记录来源是 `base` 还是 `size_variant`。
8. 对选中图层执行自然语言规则：凡图层存在 `rule.description`，必须调用 DeepSeek 根据订单商品信息生成该图层的文字值；尺寸 option 和尺寸子布局由本地规则决定，不能交给 DeepSeek 选择。缺少必需文字值时本次生成失败。
9. 以上结果组装成唯一的渲染上下文后，预览图、生产单、SVG、PSD 必须复用同一个上下文和同一份图层数据，不应各自重新匹配规格或字体布局。

## 当前实现与新图层格式的边界

- 当前 `CatalogRepository.find_render_template()` 已负责按订单商品、店铺和 `size_template_id` 加载尺寸模板，并展开当前激活的字体布局。
- 当前 `TemplateImageGenerator._resolve_order_template()` 已负责尺寸 option 匹配、字体布局匹配、尺寸子布局选择和 DeepSeek 文字值解析。
- 新图层格式的适配重点是把 `size_layouts[].layers/canvas` 作为唯一图层来源，并让导出器和生产单消费同一个已解析的渲染上下文；不能继续在各模块中读取旧的 `elements`、旧响应式字段或自行计算另一套规格。
- `template_json` 的前端覆盖也必须在尺寸 option 确定后参与本次渲染，不能把前端上一尺寸生成的坐标写回数据库作为新的基准坐标。

## 公共解析模块

`OrderTemplateResolver` 是订单模板解析的唯一接口。它接收数据库模板和订单上下文，返回本次渲染使用的完整模板：已选尺寸 option、尺寸子布局图层、字体布局、DeepSeek 文字值及解析元数据。预览图通过 `TemplateImageGenerator` 调用该模块；生产单、SVG、PSD 通过同一个生成器的内存渲染结果间接复用它。导出器的 PSD 缓存尺寸校验也只能调用解析模块的只读尺寸匹配方法，不能重新实现匹配规则。

图层数值换算的版本化契约见 `backend/templates/LAYER_RENDERING_RULES.md`，实现定义见
`backend/templates/rendering_rules.py`。该契约只存在于代码和文档中，不写入订单；订单
只保存命中的 `matched_template_json` 与解析后的 `resolved_layers_json`。
