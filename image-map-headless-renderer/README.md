# Image Map Headless Renderer

这是从 `CatalogTemplate-web/src/image-map-editor` 摘出的最小绘图模块。它不包含
React、Ant Design、编辑交互、属性面板、接口请求和状态管理，只负责：

1. 接收 image-map-editor 导出的完整 Fabric JSON；
2. 在无头 Chromium 中用 Fabric `7.4.0` 还原图层；
3. 按 `id === "workarea"` 裁切画布；
4. JPG/PNG 位图导出不绘制打印辅助线；
5. 导出 JPG、PNG、可编辑 SVG 和转曲 SVG。

运行时不依赖 React 或现有页面；`browser/editor-svg-export.js` 是从编辑器当前 SVG 导出源码生成的独立 bundle，本目录可整体复制到其他服务。

## Python

浏览器里的绘图代码仍然是同一个原生 JS，Python 只负责启动无头浏览器和写文件：

```powershell
cd image-map-headless-renderer
pip install -r python/requirements.txt
python python/render.py D:\data\layout.json --dpi 300
```

未指定输出参数时，会在 JSON 所在目录生成同名的 `.jpg`、`.png`、`.svg` 和 `.text-to-svg.svg`。

```powershell
python python/render.py D:\data\layout.json `
  --jpg D:\output\preview.jpg `
  --png D:\output\preview.png `
  --svg D:\output\preview.svg `
  --text-to-svg D:\output\preview-converted.svg `
  --output-json D:\output\layout-fitted.json `
  --dpi 300
```

## 浏览器原生 API

`browser/runner.html` 加载后会提供：

```js
const result = await globalThis.ImageMapHeadlessRenderer.render({
  json: layoutObject,
  formats: ['jpg', 'png', 'svg', 'text-to-svg'],
  dpi: 300,
  quality: 0.95,
});

result.images.jpg; // data:image/jpeg;base64,...
result.images.png; // data:image/png;base64,...
result.images.svg; // XML string
result.images.textToSvg; // XML string，文字已转为路径
result.json; // 克隆后的完整 JSON，包含安全区检查后的最终 fontSize
```

Python Playwright、Selenium 或其他无头浏览器都可以直接调用这一个 JS API。

## 输入契约

支持三种结构：

```js
[{ id: 'workarea' }, ...]
{ objects: [{ id: 'workarea' }, ...] }
{ layers: { objects: [{ id: 'workarea' }, ...] } }
```

- 必须包含 `id === "workarea"`。
- 内容对象保持编辑器导出的原始 Fabric 场景坐标，不使用 `workarea-local`。
- 对象中的 `fontUrl` 会先通过浏览器 `FontFace` 加载。
- Python 启动器会把本地或远程图片转为 data URL；字体保留原始 `fontUrl`，并在内部增加 `fontDataUrl` 供无头加载和转曲使用。
- 支持 Fabric 标准对象及编辑器的 SVG、Arrow、Cube、wordSpacing。
- `Textbox` 在渲染时使用自然文字宽度，只有 JSON 文本中的显式换行符才会换行；不会因文本框宽度自动换行。
- Chart、Element、Iframe、Video 是编辑器 DOM 覆盖层，本来就不是 Fabric 静态像素，导出时忽略并返回 warning。
- JPG/PNG 位图导出不会绘制 `workarea.printGuides` 中的任何打印辅助线。SVG 导出仍保留全部打印线。
- 单排画布且 `spineWidth > 0` 时，产图前会在本次 Fabric JSON 的 `workarea` 后插入背脊顶部、底部两个黑色 `Rect` 图层；宽度等于实际背脊宽度，高度固定为 `5mm`。固定图层 ID 会在每次生成时先删除再重建，避免重复，并随最终图层 JSON 保存回订单。JPG、PNG、SVG 和转曲 SVG 统一使用这份 JSON。
- `svg` 使用编辑器当前完整的可编辑 SVG 导出逻辑；`text-to-svg` 使用编辑器当前的 `text-to-svg` 路径转换逻辑。两者都包含工作区裁剪、背景、图层名称、打印线、CorelDRAW XML 头和格式化缩进。
- `workarea.canvasRows === 2` 时使用双排画布：总高度为两倍单排高度加 `canvasRowGap` 分隔缝，未提供该字段时默认无分隔缝（`0px`）；双排是封面/封底两面结构，不包含背脊或背脊出血，宽度为两面单面宽加左右出血；竖向辅助线贯穿整个工作区，每排分别生成横向辅助线。仍只渲染一套内容图层，不会复制图层。
- 双排 JSON 会按 `unit`、`sideWidth`、`sideHeight` 和出血字段重新核准工作区几何及辅助线，并将 `spineWidth/spineBleed` 归零。旧 JSON 未提供 `canvasRows` 时继续按单排原样渲染。
- 内容图层带有 `horizontalCentered: true` 或 `verticalCentered: true` 时，渲染器会执行两次对应的居中：第一次在创建 Fabric 对象前应用标识，作为安全区检测和字号缩小的初始位置；第二次在字号调整完成后再次按标识归位，确保最终文字几何居中。两个标识独立生效；没有对应标识的轴两次都不会移动。居中只修改本次渲染使用的克隆 JSON，调用方传入的原始 JSON 不会被修改，四种导出格式使用同一份最终归位结果。
- 文字图层导出前会检查其真实边界是否超出所属产品的安全范围，处理顺序固定为“标识居中 → 安全区检测 → 必要时缩小字号 → 再次按标识居中”：有 `horizontalCentered`/`verticalCentered` 标识的轴先居中，没有标识的轴保持原位置；随后根据对象所在区域的安全边界测量文字真实外框。若越界，每次固定减小 `0.5px` 并重新测量，直到实际边界合规后才生成文件；字号缩到 `1` 后仍越界则中止导出。安全距离由后端从产品接口/产品记录读取后注入 `workarea.safeDistances`，包含 `cover_safe_distance`、`spine_safe_distance`、`back_cover_safe_distance` 四边数值，产品接口保存的数值单位固定为毫米，再转换为 CSS 像素。渲染器本身不从订单 JSON 猜测产品安全距离；没有注入产品配置时才使用兼容默认值（单排封面/封底 `0.4in`、背脊 `0.05in`，双排封面/封底 `6mm`）。最终字号和两次归位后的结果会写入返回的 `result.json`，调整结果会在 `warnings` 中报告图层名称、所属区域、是否移动以及调整前后的字号。Python 启动器可通过 `--output-json` 写出该结果，不会覆盖原始输入文件。
- 当 `workarea.separateBleed === true` 时，宽度和竖向辅助线使用 `horizontalBleed`，高度和横向辅助线使用 `verticalBleed`；否则继续使用原来的统一 `bleed`。

## 尺寸

逻辑与当前编辑器 `saveCanvasImage` 保持一致：编辑坐标按 96 CSS px/in，工作区
裁切尺寸先向上取整，默认导出倍率为 `300 / 96`。输入 JSON 不会被修改。
