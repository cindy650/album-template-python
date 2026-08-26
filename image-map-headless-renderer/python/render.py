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
        include_svg: bool = False,
        dpi: float = 300,
        quality: float = 0.95,
        background_color: str | None = None,
        base_directory: Path | None = None,
    ) -> dict[str, Any]:
        if not jpg_path and not png_path and not svg_path and not include_svg:
            raise ValueError("至少需要 jpg_path、png_path 或 svg_path")
        document = deepcopy(document)
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
            )
            if path_value
        ]
        payload = {
            "json": document,
            "dpi": dpi,
            "quality": quality,
            "backgroundColor": background_color,
            "formats": formats,
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
                error = exc
                self._close_browser()
                if attempt >= self.render_retry_attempts:
                    raise RuntimeError(
                        f"Chromium 渲染失败，已重试 {self.render_retry_attempts} 次：{exc}"
                    ) from exc
        self.render_count += 1
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
                write_data_url(Path(output_path), result["images"][format_name])
        if svg_path:
            svg_path = Path(svg_path)
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            svg_path.write_text(result["svg"], encoding="utf-8")
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


def render_json(
    input_path: Path,
    jpg_path: Path | None,
    png_path: Path | None,
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
        return session.render_document(
            document,
            jpg_path=jpg_path,
            png_path=png_path,
            svg_path=svg_path,
            dpi=dpi,
            quality=quality,
            background_color=background_color,
            base_directory=base_directory or input_path.parent,
        )
    finally:
        session.close()


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
        for key in ("src", "fontUrl", "font_url"):
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
        if isinstance(item.get("objects"), list):
            embed_resources(
                item["objects"],
                base_directory,
                resource_loader=resource_loader,
            )


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
    if jpg_path is None and png_path is None and svg_path is None:
        jpg_path = input_path.with_suffix(".jpg")
        png_path = input_path.with_suffix(".png")
        svg_path = input_path.with_suffix(".svg")
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
    )
    printable = {key: value for key, value in result.items() if key != "svg"}
    if result.get("svg"):
        printable["svgBytes"] = len(result["svg"].encode("utf-8"))
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
