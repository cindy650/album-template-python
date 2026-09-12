from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import tempfile
from threading import BoundedSemaphore, Lock
from typing import Any, Literal

from PIL import Image, ImageColor


class ImageMapHeadlessRenderer:
    """Reuse one Python Playwright/Chromium session on its owning thread."""

    def __init__(
        self,
        project_root: Path,
        *,
        enabled: bool = True,
        timeout_seconds: float = 120,
        no_sandbox: bool = False,
        browser_executable: str = "",
        dpi: float = 300,
        recycle_after: int = 100,
        render_retry_attempts: int = 2,
        resource_retry_attempts: int = 3,
        resource_retry_delay_seconds: float = 1,
        resource_cache_max_bytes: int = 64 * 1024 * 1024,
        max_pending: int = 4,
        max_output_pixels: int = 50_000_000,
        font_error_log_path: Path | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.renderer_dir = self.project_root / "image-map-headless-renderer"
        self.runtime_path = self.renderer_dir / "python" / "render.py"
        self.enabled = bool(enabled)
        self.timeout_seconds = float(timeout_seconds)
        self.no_sandbox = bool(no_sandbox)
        self.browser_executable = str(browser_executable or "").strip()
        self.dpi = float(dpi)
        self.recycle_after = max(1, int(recycle_after))
        self.render_retry_attempts = max(1, int(render_retry_attempts))
        self.resource_retry_attempts = max(1, int(resource_retry_attempts))
        self.resource_retry_delay_seconds = max(
            0,
            float(resource_retry_delay_seconds),
        )
        self.resource_cache_max_bytes = max(0, int(resource_cache_max_bytes))
        self.max_pending = max(1, int(max_pending))
        self.max_output_pixels = max(1, int(max_output_pixels))
        self.font_error_log_path = Path(
            font_error_log_path or self.project_root / "logs" / "font_errors.log"
        ).resolve()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="image-map-renderer",
        )
        self._session = None
        self._close_lock = Lock()
        self._font_error_log_lock = Lock()
        self._pending_slots = BoundedSemaphore(self.max_pending)
        self._closed = False

    def render(
        self,
        *,
        output_path: Path,
        layout: dict[str, Any] | list[dict[str, Any]],
        output_format: Literal["jpg", "png"] = "png",
        background_color: str | None = None,
        font_files: list[dict[str, Any]] | None = None,
        dpi: float | None = None,
        quality: float = 0.95,
        include_svg: bool = False,
        include_text_to_svg: bool = False,
        safe_distances: dict[str, Any] | None = None,
        use_safe_distance: bool | None = None,
        order_number: str = "",
        **_legacy_options: Any,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Image Map 无头渲染器未启用")
        if self._closed:
            raise RuntimeError("Image Map 无头渲染器已经关闭")
        if not self.runtime_path.is_file():
            raise RuntimeError(f"Image Map Python 渲染入口不存在：{self.runtime_path}")
        if output_format not in {"jpg", "png"}:
            raise ValueError("Image Map 无头渲染只支持 jpg 或 png")

        render_dpi = float(self.dpi if dpi is None else dpi)
        if not 1 <= render_dpi <= 1200:
            raise ValueError("dpi 必须在 1 到 1200 之间")
        quality = float(quality)
        if not 0 <= quality <= 1:
            raise ValueError("quality 必须在 0 到 1 之间")

        document = deepcopy(layout)
        self._inject_font_resources(document, font_files or [])
        self._validate_output_size(document, render_dpi)
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._pending_slots.acquire(blocking=False):
            raise RuntimeError(
                f"Image Map 渲染队列已满：最多允许 {self.max_pending} 个等待或执行中的任务"
            )
        future = None

        def submit_render():
            return self._executor.submit(
                self._render_on_worker,
                document,
                output_path,
                output_format,
                render_dpi,
                quality,
                background_color,
                bool(include_svg),
                bool(include_text_to_svg),
                deepcopy(safe_distances) if isinstance(safe_distances, dict) else None,
                use_safe_distance,
            )

        try:
            future = submit_render()
            raw_result = future.result(timeout=self.timeout_seconds)
            if self._is_unexpected_solid_black(output_path, background_color):
                print(
                    "[IMAGE MAP] 检测到异常全黑图，正在重启 Chromium 并重试",
                    flush=True,
                )
                self._executor.submit(self._close_on_worker).result(
                    timeout=self.timeout_seconds
                )
                output_path.unlink(missing_ok=True)
                future = submit_render()
                raw_result = future.result(timeout=self.timeout_seconds)
                if self._is_unexpected_solid_black(output_path, background_color):
                    raise RuntimeError(
                        "Image Map 重启 Chromium 后仍生成全黑图，已拒绝保存异常产物"
                    )
            svg = str(raw_result.get("svg") or "")
            if include_svg and "data:font" in svg:
                output_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "SVG 检测到嵌入字体二进制，已拒绝保存和上传；"
                    "SVG 只允许使用外部字体 URL"
                )
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                f"Image Map 无头渲染超时：超过 {self.timeout_seconds:g} 秒"
            ) from exc
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "Image Map Python 运行时不可用；请安装 requirements.txt 中的 playwright"
            ) from exc
        except RuntimeError as exc:
            if "字体加载失败" in str(exc):
                output_path.unlink(missing_ok=True)
                message = self.record_font_failure(order_number, str(exc))
                raise RuntimeError(message) from exc
            raise
        except Exception as exc:
            raise RuntimeError(f"Image Map 无头渲染失败：{exc}") from exc
        finally:
            self._pending_slots.release()
        if not output_path.is_file():
            raise RuntimeError(f"Image Map 无头渲染未生成输出文件：{output_path}")

        return {
            "ok": True,
            "renderer": "image-map-headless-python",
            "format": output_format,
            "path": str(output_path),
            "width": raw_result.get("width"),
            "height": raw_result.get("height"),
            "css_width": raw_result.get("cssWidth"),
            "css_height": raw_result.get("cssHeight"),
            "dpi": raw_result.get("dpi"),
            "object_count": raw_result.get("objectCount"),
            "layer_geometry": [
                self._normalize_geometry(item)
                for item in (raw_result.get("geometry") or [])
                if isinstance(item, dict)
            ],
            "workarea_origin": raw_result.get("workareaOrigin"),
            "crop": raw_result.get("crop"),
            "warnings": raw_result.get("warnings") or [],
            "font_status": raw_result.get("fontStatus") or [],
            "fabric_version": raw_result.get("fabricVersion"),
            "resolved_json": raw_result.get("resolvedJson"),
            "svg": raw_result.get("svg"),
            "text_to_svg": raw_result.get("textToSvg"),
        }

    def record_font_failure(self, order_number: str, detail: str) -> str:
        order_label = str(order_number or "未提供").strip() or "未提供"
        message = f"订单字体加载失败：订单号={order_label}，{detail}"
        print(f"[IMAGE MAP][FONT][ERROR] {message}", flush=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            with self._font_error_log_lock:
                self.font_error_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.font_error_log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(f"{timestamp} [IMAGE MAP][FONT][ERROR] {message}\n")
        except OSError as exc:
            print(
                f"[IMAGE MAP][FONT][ERROR] 字体错误日志写入失败："
                f"路径={self.font_error_log_path}，错误={type(exc).__name__}: {exc}",
                flush=True,
            )
        return message

    @staticmethod
    def _is_unexpected_solid_black(
        output_path: Path,
        background_color: str | None,
    ) -> bool:
        if not output_path.is_file():
            return False
        try:
            expected_background = ImageColor.getrgb(
                str(background_color or "#ffffff")
            )
        except (TypeError, ValueError):
            expected_background = (255, 255, 255)
        if max(expected_background[:3]) <= 8:
            return False
        try:
            with Image.open(output_path) as image:
                extrema = image.convert("RGB").getextrema()
        except (OSError, ValueError):
            return False
        return all(channel_max <= 1 for _channel_min, channel_max in extrema)

    def render_bytes(
        self,
        layout: dict[str, Any] | list[dict[str, Any]],
        *,
        output_format: Literal["jpg", "png"],
        dpi: float | None = None,
        quality: float = 0.95,
        background_color: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="image-map-response-") as directory:
            output_path = Path(directory) / f"render.{output_format}"
            result = self.render(
                output_path=output_path,
                layout=layout,
                output_format=output_format,
                dpi=dpi,
                quality=quality,
                background_color=background_color,
            )
            return output_path.read_bytes(), result

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._executor.submit(self._close_on_worker).result(
                    timeout=self.timeout_seconds
                )
            except (FutureTimeoutError, RuntimeError):
                pass
            finally:
                self._executor.shutdown(wait=False, cancel_futures=True)

    def _render_on_worker(
        self,
        document: dict[str, Any] | list[dict[str, Any]],
        output_path: Path,
        output_format: str,
        dpi: float,
        quality: float,
        background_color: str | None,
        include_svg: bool,
        include_text_to_svg: bool,
        safe_distances: dict[str, Any] | None,
        use_safe_distance: bool | None,
    ) -> dict[str, Any]:
        if self._session is None:
            runtime = _load_runtime(self.runtime_path)
            self._session = runtime.ImageMapRendererSession(
                browser_executable=self.browser_executable,
                no_sandbox=self.no_sandbox,
                recycle_after=self.recycle_after,
                render_retry_attempts=self.render_retry_attempts,
                resource_retry_attempts=self.resource_retry_attempts,
                resource_retry_delay_seconds=self.resource_retry_delay_seconds,
                resource_cache_max_bytes=self.resource_cache_max_bytes,
            )
        paths = {
            "jpg_path": output_path if output_format == "jpg" else None,
            "png_path": output_path if output_format == "png" else None,
        }
        return self._session.render_document(
            document,
            **paths,
            dpi=dpi,
            quality=quality,
            background_color=background_color,
            include_svg=include_svg,
            include_text_to_svg=include_text_to_svg,
            safe_distances=safe_distances,
            use_safe_distance=use_safe_distance,
            base_directory=self.project_root,
        )

    def _close_on_worker(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    @classmethod
    def _inject_font_resources(
        cls,
        document: dict[str, Any] | list[dict[str, Any]],
        font_files: list[dict[str, Any]],
    ) -> None:
        fonts: dict[str, str] = {}
        for font in font_files:
            if not isinstance(font, dict):
                continue
            family = str(font.get("family") or "").strip()
            source = str(font.get("path") or font.get("url") or "").strip()
            if not family or not source:
                continue
            if font.get("path") and not Path(source).is_file():
                raise RuntimeError(f"本地字体读取失败：{source}")
            fonts[family.casefold()] = source

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            family = str(value.get("fontFamily") or value.get("font_family") or "").strip()
            source = fonts.get(family.casefold())
            if source:
                external_source = next(
                    (
                        str(font.get("url") or "").strip()
                        for font in font_files
                        if isinstance(font, dict)
                        and str(font.get("family") or "").strip().casefold()
                        == family.casefold()
                        and str(font.get("url") or "").strip()
                    ),
                    "",
                )
                if external_source:
                    value["fontExternalUrl"] = external_source
                    value["fontUrl"] = external_source
                else:
                    value["fontUrl"] = str(value.get("fontUrl") or value.get("font_url") or source)
                value["fontDataUrl"] = source
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)

        visit(document)

    @staticmethod
    def _normalize_geometry(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_index": value.get("sourceIndex"),
            "id": value.get("id"),
            "type": value.get("type"),
            "left": value.get("left"),
            "top": value.get("top"),
            "center_x": value.get("centerX"),
            "center_y": value.get("centerY"),
            "width": value.get("width"),
            "height": value.get("height"),
            "scale_x": value.get("scaleX"),
            "scale_y": value.get("scaleY"),
            "angle": value.get("angle"),
            "bounding_rect": value.get("boundingRect"),
            "text_lines": value.get("textLines"),
            "character_bounds": value.get("characterBounds"),
        }

    def _validate_output_size(
        self,
        document: dict[str, Any] | list[dict[str, Any]],
        dpi: float,
    ) -> None:
        if isinstance(document, list):
            objects = document
        elif isinstance(document, dict):
            layers = document.get("layers") if isinstance(document.get("layers"), dict) else {}
            objects = document.get("objects") or layers.get("objects") or []
        else:
            objects = []
        workarea = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().casefold() == "workarea"
            ),
            None,
        )
        if workarea is None:
            return
        try:
            raw_width = float(workarea.get("width") or workarea["workareaWidth"])
            raw_height = float(workarea.get("height") or workarea["workareaHeight"])
            if workarea.get("canvasRows") == 2:
                def nonnegative(field: str) -> float | None:
                    try:
                        value = float(workarea.get(field))
                    except (TypeError, ValueError):
                        return None
                    return value if value >= 0 else None

                unit_factor = {
                    "in": 96.0,
                    "cm": 96.0 / 2.54,
                    "mm": 96.0 / 25.4,
                }.get(str(workarea.get("unit") or "").strip().lower())
                side_height = nonnegative("sideHeight")
                bleed = nonnegative("bleed")
                if unit_factor is not None and side_height is not None and bleed is not None:
                    raw_height = (side_height + bleed * 2) * unit_factor * 2 + 32
                    width_values = (
                        nonnegative("sideWidth"),
                        nonnegative("spineBleed"),
                        nonnegative("spineWidth"),
                    )
                    if all(value is not None for value in width_values):
                        raw_width = (
                            width_values[0] * 2
                            + bleed * 2
                            + width_values[1] * 2
                            + width_values[2]
                        ) * unit_factor
            width = abs(
                raw_width * float(workarea.get("scaleX", 1) or 1)
            )
            height = abs(
                raw_height * float(workarea.get("scaleY", 1) or 1)
            )
        except (KeyError, TypeError, ValueError):
            return
        pixels = int(width * dpi / 96) * int(height * dpi / 96)
        if pixels > self.max_output_pixels:
            raise ValueError(
                f"输出图片预计 {pixels} 像素，超过上限 {self.max_output_pixels}；"
                "请降低 DPI 或画布尺寸"
            )


_runtime_modules: dict[Path, Any] = {}
_runtime_lock = Lock()


def _load_runtime(path: Path):
    path = Path(path).resolve()
    with _runtime_lock:
        cached = _runtime_modules.get(path)
        if cached is not None:
            return cached
        module_name = f"_image_map_runtime_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 Image Map Python 渲染入口：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _runtime_modules[path] = module
        return module
