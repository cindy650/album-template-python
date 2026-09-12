from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
import sys
import time
from typing import Any
from urllib.request import urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "browser" / "runner.html").as_uri()


class ResourceLoader:
    def __init__(
        self,
        *,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1,
        cache_max_bytes: int = 64 * 1024 * 1024,
    ):
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_delay_seconds = max(0, float(retry_delay_seconds))
        self.cache_max_bytes = max(0, int(cache_max_bytes))
        self._cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._cache_bytes = 0

    def load(self, url: str, *, resource_kind: str = "资源", label: str = "") -> tuple[bytes, str]:
        cached = self._cache.pop(url, None)
        if cached is not None:
            self._cache[url] = cached
            return cached
        error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                with urlopen(url, timeout=30) as response:
                    content = response.read()
                    mime = response.headers.get_content_type()
                self._remember(url, content, mime)
                print(
                    f"[IMAGE MAP][{'FONT' if resource_kind == '字体' else 'RESOURCE'}] 加载成功"
                    f"{f'：{label}' if label else ''}（第 {attempt} 次）",
                    flush=True,
                )
                return content, mime
            except Exception as exc:
                error = exc
                print(
                    f"[IMAGE MAP][{'FONT' if resource_kind == '字体' else 'RESOURCE'}] 加载失败"
                    f"{f'：{label}' if label else ''}（第 {attempt}/{self.retry_attempts} 次）："
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                if attempt < self.retry_attempts and self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds * (2 ** (attempt - 1)))
        raise RuntimeError(
            f"远程图片或字体下载失败，已重试 {self.retry_attempts} 次：{url}；{error}"
        ) from error

    def clear(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0

    def _remember(self, url: str, content: bytes, mime: str) -> None:
        size = len(content)
        if not self.cache_max_bytes or size > self.cache_max_bytes:
            return
        while self._cache and self._cache_bytes + size > self.cache_max_bytes:
            _, (old_content, _) = self._cache.popitem(last=False)
            self._cache_bytes -= len(old_content)
        self._cache[url] = (content, mime)
        self._cache_bytes += size


class ImageMapRendererSession:
    """Own and reuse one Chromium instance from a single Python thread."""

    def __init__(
        self,
        *,
        browser_executable: str = "",
        no_sandbox: bool = False,
        recycle_after: int = 100,
        render_retry_attempts: int = 2,
        resource_retry_attempts: int = 3,
        resource_retry_delay_seconds: float = 1,
        resource_cache_max_bytes: int = 64 * 1024 * 1024,
    ):
        self.browser_executable = str(browser_executable or "").strip()
        self.no_sandbox = bool(no_sandbox)
        self.recycle_after = max(1, int(recycle_after))
        self.render_retry_attempts = max(1, int(render_retry_attempts))
        self.render_count = 0
        self.resource_loader = ResourceLoader(
            retry_attempts=resource_retry_attempts,
            retry_delay_seconds=resource_retry_delay_seconds,
            cache_max_bytes=resource_cache_max_bytes,
        )
        self._playwright = None
        self._browser = None
        self._page = None

    def render_document(
        self,
        document: dict[str, Any] | list[dict[str, Any]],
        *,
        jpg_path: Path | None = None,
        png_path: Path | None = None,
        svg_path: Path | None = None,
        text_to_svg_path: Path | None = None,
        include_svg: bool = False,
        include_text_to_svg: bool = False,
        dpi: float = 300,
        quality: float = 0.95,
        background_color: str | None = None,
        safe_distances: dict[str, Any] | None = None,
        use_safe_distance: bool | None = None,
        base_directory: Path | None = None,
    ) -> dict[str, Any]:
        if not jpg_path and not png_path and not svg_path and not text_to_svg_path and not include_svg and not include_text_to_svg:
            raise ValueError("至少需要 jpg_path、png_path 或 svg_path")
        document = deepcopy(document)
        # Preserve authored font URLs for SVG before embedding replaces them
        # with data URLs used by Chromium for deterministic rendering.
        font_sources = collect_font_sources(
            document,
            (base_directory or Path.cwd()).resolve(),
        )
        embed_resources(
            document,
            (base_directory or Path.cwd()).resolve(),
            resource_loader=self.resource_loader,
        )
        formats = [
            name
            for name, path_value in (
                ("jpg", jpg_path),
                ("png", png_path),
                ("svg", svg_path or include_svg),
                ("text-to-svg", text_to_svg_path or include_text_to_svg),
            )
            if path_value
        ]
        payload = {
            "json": document,
            "dpi": dpi,
            "quality": quality,
            "backgroundColor": background_color,
            "safeDistances": safe_distances,
            "useSafeDistance": use_safe_distance,
            "formats": formats,
            "fontSources": font_sources,
        }
        if self.render_count >= self.recycle_after:
            self._close_browser()
        error = None
        for attempt in range(1, self.render_retry_attempts + 1):
            try:
                self._ensure_started()
                result = self._page.evaluate(
                    "payload => globalThis.ImageMapHeadlessRenderer.render(payload)",
                    payload,
                )
                break
            except PlaywrightError as exc:
                if not is_retryable_browser_error(exc):
                    # JavaScript validation failures (for example text that
                    # remains outside the product safe area at 1px) are
                    # deterministic business errors, not Chromium failures.
                    raise
                error = exc
                self._close_browser()
                if attempt >= self.render_retry_attempts:
                    raise RuntimeError(
                        f"Chromium 渲染失败，已重试 {self.render_retry_attempts} 次：{exc}"
                    ) from exc
        self.render_count += 1
        images = result.get("images") or {}
        for font_status in result.get("fontStatus") or []:
            if not isinstance(font_status, dict):
                continue
            family = str(font_status.get("family") or "<未命名字体>")
            state = str(font_status.get("status") or "unknown")
            if state == "loaded":
                print(f"[IMAGE MAP][FONT] Chromium 加载成功：{family}", flush=True)
            else:
                error_detail = str(font_status.get("error") or "未知错误")
                print(
                    f"[IMAGE MAP][FONT] Chromium 加载失败：{family}：{error_detail}",
                    flush=True,
                )
        for format_name, output_path in (("jpg", jpg_path), ("png", png_path)):
            if output_path:
                write_data_url(Path(output_path), images[format_name])
        svg_content = result.get("svg") or images.get("svg") or ""
        text_to_svg_content = result.get("textToSvg") or images.get("textToSvg") or ""
        if svg_path:
            svg_path = Path(svg_path)
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            svg_path.write_text(svg_content, encoding="utf-8")
        if text_to_svg_path:
            text_to_svg_path = Path(text_to_svg_path)
            text_to_svg_path.parent.mkdir(parents=True, exist_ok=True)
            text_to_svg_path.write_text(text_to_svg_content, encoding="utf-8")
        # The frontend renderer renamed resolvedJson to json and moved SVG
        # payloads under images. Keep the server adapter's stable aliases so
        # the order renderer and artifact exporter can consume either version.
        result["resolvedJson"] = result.get("resolvedJson", result.get("json"))
        result["svg"] = svg_content
        result["textToSvg"] = text_to_svg_content
        return {key: value for key, value in result.items() if key != "images"}

    def close(self) -> None:
        self._close_browser()
        self.resource_loader.clear()

    def _close_browser(self) -> None:
        page, browser, playwright = self._page, self._browser, self._playwright
        self._page = self._browser = self._playwright = None
        self.render_count = 0
        if page is not None:
            try:
                page.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            playwright.stop()

    def _ensure_started(self) -> None:
        if self._page is not None and not self._page.is_closed():
            return
        self._playwright = sync_playwright().start()
        try:
            self._browser = launch_browser(
                self._playwright,
                browser_executable=self.browser_executable,
                no_sandbox=self.no_sandbox,
            )
            self._page = self._browser.new_page(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=1,
            )
            self._page.goto(RUNNER)
            self._page.wait_for_function(
                "Boolean(globalThis.ImageMapHeadlessRenderer)"
            )
        except Exception:
            self._close_browser()
            raise


def is_retryable_browser_error(error: Exception) -> bool:
    """Retry only transport/process failures, not deterministic JS errors."""
    message = str(error).casefold()
    retry_markers = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "browser disconnected",
        "connection closed",
        "connection reset",
        "protocol error",
        "page crashed",
        "target closed",
    )
    return any(marker in message for marker in retry_markers)


def render_json(
    input_path: Path,
    jpg_path: Path | None,
    png_path: Path | None,
    text_to_svg_path: Path | None = None,
    output_json_path: Path | None = None,
    dpi: float = 300,
    quality: float = 0.95,
    background_color: str | None = None,
    browser_executable: str = "",
    no_sandbox: bool = False,
    base_directory: Path | None = None,
    svg_path: Path | None = None,
) -> dict[str, Any]:
    input_path = input_path.resolve()
    document = json.loads(input_path.read_text(encoding="utf-8"))
    session = ImageMapRendererSession(
        browser_executable=browser_executable,
        no_sandbox=no_sandbox,
        recycle_after=1,
    )
    try:
        result = session.render_document(
            document,
            jpg_path=jpg_path,
            png_path=png_path,
            svg_path=svg_path,
            text_to_svg_path=text_to_svg_path,
            dpi=dpi,
            quality=quality,
            background_color=background_color,
            base_directory=base_directory or input_path.parent,
            include_svg=bool(
                output_json_path
                and not any((jpg_path, png_path, svg_path, text_to_svg_path))
            ),
        )
        apply_rendered_font_sizes(
            document,
            result.get("resolvedJson") or result.get("json"),
        )
        if output_json_path:
            output_json_path = Path(output_json_path)
            output_json_path.parent.mkdir(parents=True, exist_ok=True)
            output_json_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return result
    finally:
        session.close()


def apply_rendered_font_sizes(document: Any, rendered_document: Any) -> None:
    """Copy final font sizes without persisting embedded resource data URLs."""
    apply_object_font_sizes(
        extract_objects(document),
        extract_objects(rendered_document),
    )


def extract_objects(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        return []
    if isinstance(document.get("objects"), list):
        return document["objects"]
    layers = document.get("layers")
    if isinstance(layers, dict) and isinstance(layers.get("objects"), list):
        return layers["objects"]
    return []


def apply_object_font_sizes(
    original_objects: list[Any],
    rendered_objects: list[Any],
) -> None:
    rendered_by_id = {
        item.get("id"): item
        for item in rendered_objects
        if isinstance(item, dict) and item.get("id") is not None
    }
    for index, original in enumerate(original_objects):
        if not isinstance(original, dict):
            continue
        rendered = rendered_by_id.get(original.get("id"))
        if rendered is None and index < len(rendered_objects):
            candidate = rendered_objects[index]
            rendered = candidate if isinstance(candidate, dict) else None
        if not rendered:
            continue
        if "fontSize" in rendered:
            original["fontSize"] = rendered["fontSize"]
        apply_object_font_sizes(
            original.get("objects")
            if isinstance(original.get("objects"), list)
            else [],
            rendered.get("objects")
            if isinstance(rendered.get("objects"), list)
            else [],
        )


def launch_browser(playwright, *, browser_executable: str = "", no_sandbox: bool = False):
    candidates = []
    if browser_executable:
        candidates.append(Path(browser_executable))
    candidates.extend(
        [
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        ]
    )
    options = {
        "headless": True,
        "args": ["--no-sandbox"] if no_sandbox else [],
    }
    for candidate in candidates:
        if candidate.is_file():
            return playwright.chromium.launch(
                executable_path=str(candidate.resolve()),
                **options,
            )
    return playwright.chromium.launch(**options)


def embed_resources(
    document: Any,
    base_directory: Path,
    *,
    resource_loader: ResourceLoader | None = None,
) -> None:
    resource_loader = resource_loader or ResourceLoader()
    if isinstance(document, list):
        objects = document
    elif isinstance(document, dict):
        layers = document.get("layers") if isinstance(document.get("layers"), dict) else {}
        objects = document.get("objects") or layers.get("objects") or []
    else:
        objects = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        for key in ("src",):
            value = item.get(key)
            if not isinstance(value, str) or not value or value.startswith(("data:", "blob:")):
                continue
            if value.startswith(("http:", "https:")):
                is_font = key in {"fontUrl", "font_url"}
                label = str(item.get("fontFamily") or item.get("font_family") or "").strip()
                content, mime = resource_loader.load(
                    value,
                    resource_kind="字体" if is_font else "资源",
                    label=label,
                )
            elif key == "src" and value.lstrip().startswith("<"):
                continue
            else:
                resource = Path(value.removeprefix("file://"))
                if not resource.is_absolute():
                    resource = base_directory / resource
                resource = resource.resolve()
                content = resource.read_bytes()
                mime = mimetypes.guess_type(resource.name)[0] or "application/octet-stream"
                if key in {"fontUrl", "font_url"}:
                    print(
                        f"[IMAGE MAP][FONT] 本地字体读取成功："
                        f"{item.get('fontFamily') or item.get('font_family') or resource.name}",
                        flush=True,
                    )
            item[key] = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        # Keep fontUrl/font_url as the authored external reference. The
        # browser renderer loads the embedded copy through fontDataUrl.
        font_key = "fontDataUrl" if item.get("fontDataUrl") else (
            "font_url" if item.get("font_url") else "fontUrl"
        )
        font_value = item.get(font_key)
        if isinstance(font_value, str) and font_value and not font_value.startswith(("data:", "blob:")):
            if font_value.startswith(("http:", "https:")):
                content, mime = resource_loader.load(
                    font_value,
                    resource_kind="字体",
                    label=str(item.get("fontFamily") or item.get("font_family") or "").strip(),
                )
            else:
                resource = Path(font_value.removeprefix("file://"))
                if not resource.is_absolute():
                    resource = base_directory / resource
                resource = resource.resolve()
                content = resource.read_bytes()
                mime = mimetypes.guess_type(resource.name)[0] or "application/octet-stream"
            item["fontDataUrl"] = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        if isinstance(item.get("objects"), list):
            embed_resources(
                item["objects"],
                base_directory,
                resource_loader=resource_loader,
            )


def collect_font_sources(document: Any, base_directory: Path) -> list[dict[str, str]]:
    """Collect external font URLs before embed_resources mutates the JSON."""
    sources: dict[tuple[str, str, str, str], dict[str, str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        family = str(value.get("fontFamily") or value.get("font_family") or "").strip()
        source = str(
            value.get("fontExternalUrl")
            or value.get("font_external_url")
            or value.get("fontUrl")
            or value.get("font_url")
            or ""
        ).strip()
        if family and source and not source.startswith(("data:", "blob:")):
            if source.startswith(("http://", "https://", "file://")):
                url = source
            else:
                path = Path(source)
                if not path.is_absolute():
                    path = base_directory / path
                url = path.resolve().as_uri()
            weight = str(value.get("fontWeight") or value.get("font_weight") or "normal")
            style = str(value.get("fontStyle") or value.get("font_style") or "normal")
            sources[(family, url, weight, style)] = {
                "family": family,
                "url": url,
                "weight": weight,
                "style": style,
            }
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(document)
    return list(sources.values())


def write_data_url(output_path: Path, data_url: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render CatalogTemplate image-map JSON to JPG/PNG"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--jpg", type=Path)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--svg", type=Path)
    parser.add_argument("--text-to-svg", type=Path)
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Write JSON with fitted text fontSize values",
    )
    parser.add_argument("--dpi", type=float, default=300)
    parser.add_argument("--quality", type=float, default=0.95)
    parser.add_argument("--background")
    parser.add_argument("--browser", default="")
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--no-sandbox", action="store_true")
    args = parser.parse_args()
    input_path = args.input.resolve()
    jpg_path = args.jpg.resolve() if args.jpg else None
    png_path = args.png.resolve() if args.png else None
    svg_path = args.svg.resolve() if args.svg else None
    text_to_svg_path = args.text_to_svg.resolve() if args.text_to_svg else None
    output_json_path = args.output_json.resolve() if args.output_json else None
    if (
        jpg_path is None
        and png_path is None
        and svg_path is None
        and text_to_svg_path is None
        and output_json_path is None
    ):
        jpg_path = input_path.with_suffix(".jpg")
        png_path = input_path.with_suffix(".png")
        svg_path = input_path.with_suffix(".svg")
        text_to_svg_path = input_path.with_suffix(".转曲.svg")
    elif jpg_path is not None and svg_path is None:
        svg_path = jpg_path.with_suffix(".svg")
    result = render_json(
        input_path,
        jpg_path,
        png_path,
        dpi=args.dpi,
        quality=args.quality,
        background_color=args.background,
        browser_executable=args.browser,
        no_sandbox=args.no_sandbox,
        base_directory=args.base_dir,
        svg_path=svg_path,
        text_to_svg_path=text_to_svg_path,
        output_json_path=output_json_path,
    )
    printable = {
        key: value
        for key, value in result.items()
        if key not in {"json", "resolvedJson", "svg", "textToSvg"}
    }
    if result.get("svg"):
        printable["svgBytes"] = len(result["svg"].encode("utf-8"))
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
