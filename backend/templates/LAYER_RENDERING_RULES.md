# 图层 JSON 渲染换算规则

本文档是预览图、生产单、SVG 和 PSD 共用的图层几何契约。数据库中的
`font_layout_size_variants.layers_json` 是基准数据，渲染过程不得把目标画布坐标
写回模板 JSON。

## 1. 目标画布

尺寸规格统一先换算为英寸：

```text
canvas_width_in = single_side_width * 2
                 + spine_width
                 + bleed * 2
                 + spine_bleed * 2
canvas_height_in = single_side_height + bleed * 2
pixel_width  = round(canvas_width_in  * dpi)
pixel_height = round(canvas_height_in * dpi)
```

这里的 `spine_bleed` 是两侧面板相邻的背脊出血，不是普通外出血。

## 2. 成品区域

编辑器和后端都把背脊出血并入两侧面板：

```text
panel_width = single_side_width + spine_bleed
back   = (bleed,  bleed, panel_width, single_side_height)
spine  = (bleed + panel_width, bleed, spine_width, single_side_height)
cover  = (spine.x + spine_width, bleed, panel_width, single_side_height)
```

区域坐标在目标像素画布中按 `dpi` 和尺寸单位换算。文字区域由图层的中心点
判断；明确提供 `region` 时优先使用该字段。

## 3. Fabric 对象坐标

`font_layout_size_variants.layers_json` 中的 Fabric `left/top` 保持编辑器原始
scene 坐标，不在模板数据中提前减去工作区原点，也不需要保存
`canvas.coordinate_space`。完整
导出 JSON 中的 `objects` 应包含 `id=workarea` 的工作区对象；渲染器从该对象
计算裁切原点。场景坐标在渲染边界处转换为局部坐标：

```text
local_left = object.left - workarea_left
local_top  = object.top  - workarea_top
```

缺少 `workarea` 时直接失败；不读取、不写入 `coordinate_space`、
`workarea_left/workarea_top` 或 `scene_width/scene_height` 等历史字段。

`left/top` 的锚点语义继续由 Fabric 对象自身的 `originX/originY` 决定。
`Textbox`、`Rect`、`Image` 的缩放、旋转、翻转和文字度量均由 Chromium 中的 Fabric.js
读取原始 JSON 执行，后端不重新实现一套文字框算法。

再围绕 `(cx, cy)` 应用 `flipX/flipY` 和 Fabric 的顺时针 `angle`。旋转只改变
绘制图层的视觉包围盒，不反过来改变字号或原始缩放。

## 4. 参考坐标到目标坐标

参考工作区和目标画布都只由同一个命中规格计算：

```text
reference_width  = canvas_width_in  * 96
reference_height = canvas_height_in * 96
target_width     = round(canvas_width_in  * dpi)
target_height    = round(canvas_height_in * dpi)
scale            = target_width / reference_width
target_x         = local_x * scale
target_y         = local_y * scale
```

宽高比例必须一致，否则渲染器直接报错。项目中不存在 `1000x800`、
`6369x2274` 等生产画布默认值。

辅助线也使用同一规格和 96 px/单位基准计算；竖线依次位于外边缘、普通出血、
左右单面、两侧背脊出血和背脊边界，样式与前端 `WorkareaHandler` 一致为
`#1677ff`、虚线 `[6, 4]`。

## 5. 文字数值

- 基准字号来自 JSON 的 `fontSize`，对象自身 `scaleX/scaleY` 由 Fabric 应用一次，
  工作区到输出画布的统一缩放由 viewport transform 应用一次。
- `charSpacing` 遵循 Fabric 的 1/1000 em：
  `charSpacing * rendered_font_size / 1000`。
- 当前 Fabric 布局中的数值 `wordSpacing` 与 `charSpacing` 一样使用 1/1000 em；
  目标像素值为 `wordSpacing * rendered_font_size / 1000`。带 `px` 后缀的值才按
  绝对 CSS 像素处理。
- 使用 JSON 的 `fontFamily` 和 `fontUrl`。浏览器必须先完成 `FontFace.load()` 和
  `document.fonts.check()`，再创建 Fabric 文字对象；PSD 必须先读取字体文件并解析
  PostScript 名称，再创建任何可编辑文字图层。指定字体加载失败时本次生成直接失败，
  不允许静默回退到 Arial 等替代字体。
- PhotoshopAPI 的 `font_size` 使用文档像素字号：
  `fontSize * scaleY * (dpi / 96)`；不能预先转换成 pt，否则 Photoshop 会再次按
  `72 / dpi` 换算，造成字号缩小。
- Photoshop 不渲染附着在空格字符上的 tracking。PSD 使用等效 tracking 保持与
  Fabric 相同的整行宽度：
  `charSpacing + wordSpacing * 空格数 / (字符数 - 1)`。
- 文本先在未旋转框内按等比缩放和字距排版，再旋转、翻转并以中心点合成。
- Image Map 无头渲染器在字体加载和文字替换完成后测量每个对象的视觉框；该
  临时几何只用于校正 Photoshop 段落文字的字体 ascender 偏移，不写入订单 JSON。

## 6. 数据边界

DeepSeek 只负责自然语言规则得到最终文字内容；尺寸规格、区域、坐标、字号、
字距、旋转和最终图片合成都由本地代码完成。解析后的
`matched_template_json` 与 `resolved_layers_json` 保存订单快照，后续导出复用该
快照，不重新猜测坐标。

## 7. 规则版本

Fabric 生产预览实现位于 `image-map-headless-renderer/python/render.py`，Python 负责传入命中规格、
坐标元数据、最终文字和字体文件。

订单只保存两个紧凑 JSON：`matched_template_json` 只包含模板 ID、店铺/商品 ID、
选中的尺寸和物理尺寸字段；`resolved_layers_json` 直接保存选中的字体布局图层
JSON。字体名称、字体 URL、字号、坐标、旋转和字距都跟随图层 JSON 保存，渲染器
按照本文件的统一规则读取，不额外生成运行时字体来源或坐标轨迹字段。
