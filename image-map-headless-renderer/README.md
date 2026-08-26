# Image Map Headless Renderer

这是从 `CatalogTemplate-web/src/image-map-editor` 摘出的最小 Python 无头绘图模块。
它不包含 React、Ant Design、编辑交互、接口请求和状态管理，只负责：

1. 接收 image-map-editor 导出的完整 Fabric JSON；
2. 由 Python Playwright 在无头 Chromium 中用 Fabric `7.4.0` 还原图层；
3. 按 `id === "workarea"` 裁切画布；
4. 在同一次 Fabric 渲染中导出 JPG/PNG 和原生 SVG。

当前后端通过 `backend/templates/image_map_renderer.py` 维护专用渲染线程并复用
Chromium。浏览器会定期回收，崩溃时自动重建；远程字体和图片带重试及有界缓存。

## 安装

项目根目录的 `requirements.txt` 已包含 Python Playwright：

```powershell
pip install -r requirements.txt
```

运行时优先使用显式指定或系统已安装的 Edge、Chrome、Chromium。Linux 服务器没有
浏览器时，部署阶段安装一次 Chromium 及其系统依赖：

```bash
python -m playwright install --with-deps chromium
```

服务器镜像已经安装 Chrome/Chromium 时，将 `IMAGE_MAP_RENDERER_BROWSER` 指向其
可执行文件即可，不需要再下载 Playwright 浏览器。

## 命令行

```powershell
python python/render.py D:\data\layout.json `
  --jpg D:\output\preview.jpg `
  --png D:\output\preview.png `
  --svg D:\output\preview.svg `
  --dpi 300
```

不写输出参数时，会在 JSON 目录同时生成 JPG、PNG 和 SVG。只传 `--jpg` 时会
自动在同目录生成同名 SVG。其他参数包括 `--quality`、`--background`、`--browser`、
`--base-dir` 和 `--no-sandbox`。

## Python API

```python
from pathlib import Path
from backend.templates import ImageMapHeadlessRenderer

renderer = ImageMapHeadlessRenderer(Path.cwd(), recycle_after=100)
try:
    result = renderer.render(
        layout=layout_object,
        output_path=Path("preview.png"),
        output_format="png",
    )
finally:
    renderer.close()
```

`ImageMapRendererSession` 及 `render_json` 位于 `python/render.py`。Session 必须只在
创建它的线程使用；后端适配器已经处理线程归属、串行队列、关闭和失败重试。

## HTTP 接口

```http
POST /api/v1/image-map/render
Content-Type: application/json

{
  "json": { "objects": [{ "id": "workarea" }] },
  "format": "png",
  "dpi": 300,
  "quality": 0.95
}
```

响应体是 `image/png` 或 `image/jpeg`，临时文件会在响应读取后删除。

## 输入契约

支持三种结构：

```js
[{ id: 'workarea' }, ...]
{ objects: [{ id: 'workarea' }, ...] }
{ layers: { objects: [{ id: 'workarea' }, ...] } }
```

- 必须包含 `id === "workarea"`。
- 内容对象保持编辑器导出的原始 Fabric 场景坐标，不使用 `workarea-local`。
- `fontUrl` 和图片 `src` 会由 Python 下载并转为 data URL，避免 OSS CORS。
- JPG 和 SVG 来自同一个浏览器画布；SVG 保留文字/形状节点并内嵌已加载字体。
- 远程资源失败会指数退避重试；相同 URL 会进入有总字节上限的 LRU 缓存。
- 支持 Fabric 标准对象及编辑器的 SVG、Arrow、Cube、wordSpacing。
- Chart、Element、Iframe、Video 是 DOM 覆盖层，静态导出时忽略并返回 warning。

编辑坐标按 96 CSS px/in，工作区裁切尺寸先向上取整，默认倍率为 `300 / 96`。
输入 JSON 不会被修改。
