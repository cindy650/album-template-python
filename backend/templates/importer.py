from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from PIL import Image, ImageOps
import requests

from backend.templates.size_variants import (
    SIZE_VARIANT_FIELDS,
    normalize_size_options,
    normalize_size_spec,
    resolve_spine_width,
    selected_size_option,
)


TEMPLATE_SCHEMA_VERSION = "1.0"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | {".psd"}
KNOWN_RULES = {"initials", "concat", "field", "uppercase", "lowercase", "replace", "date_format"}
SIZE_TEMPLATE_FIELD_KEYS = (
    "size_unit",
    "single_side_width",
    "single_side_height",
    "bleed",
    "spine_width",
    "spine_bleed",
    "page_count",
    "is_fixed",
    "by_page_count",
    "spine_width_mode",
    "spine_width_formula",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: float) -> float:
    return round(float(value), 8)


def _slug(value: Any, fallback: str = "element") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-")
    return text or fallback


def _call(value: Any, *args, default: Any = None) -> Any:
    if callable(value):
        try:
            return value(*args)
        except Exception:
            return default
    return value if not args else default


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name or value).split(".")[-1]


class TemplateImportService:
    """Turn PSD/image uploads and frontend edits into one portable template JSON."""

    def __init__(
        self,
        source_dir: Path,
        catalog_repository=None,
        deepseek_api_url: str = "",
        deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-chat",
        deepseek_timeout: float = 60,
        max_upload_bytes: int = 50 * 1024 * 1024,
        tesseract_command: str = "tesseract",
        ocr_languages: str = "eng+chi_sim",
    ):
        self.source_dir = Path(source_dir)
        self.catalog_repository = catalog_repository
        self.deepseek_api_url = str(deepseek_api_url or "").strip()
        self.deepseek_api_key = str(deepseek_api_key or "").strip()
        self.deepseek_model = str(deepseek_model or "deepseek-chat").strip()
        self.deepseek_timeout = float(deepseek_timeout)
        self.max_upload_bytes = int(max_upload_bytes)
        self.tesseract_command = str(tesseract_command or "tesseract")
        self.ocr_languages = str(ocr_languages or "eng+chi_sim")

    def analyze(
        self,
        filename: str,
        content: bytes,
        content_type: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ValueError("上传文件不能为空")
        if len(content) > self.max_upload_bytes:
            raise ValueError(f"上传文件不能超过 {self.max_upload_bytes} 字节")

        options = deepcopy(options or {})
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError("只支持 PSD、PNG、JPG、JPEG、WEBP、BMP、TIF 或 TIFF 文件")

        digest = sha256(bytes(content)).hexdigest()
        if suffix == ".psd":
            template = self._analyze_psd(
                bytes(content),
                filename,
                content_type,
                digest,
                options,
            )
        else:
            template = self._analyze_image(
                bytes(content),
                filename,
                content_type,
                digest,
                options,
            )

        self._apply_frontend_metadata(template, options)
        if options.get("use_ai"):
            self._enrich_with_deepseek(template)
        return self.finalize(
            template,
            strict_fonts=bool(options.get("strict_fonts", False)),
        )

    def finalize(
        self,
        template: dict[str, Any],
        strict_fonts: bool = True,
        size_template_id: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("template_json 必须是对象")
        result = deepcopy(template)
        result["schema_version"] = str(
            result.get("schema_version") or TEMPLATE_SCHEMA_VERSION
        )
        if result["schema_version"] != TEMPLATE_SCHEMA_VERSION:
            raise ValueError(f"不支持的模板 schema_version: {result['schema_version']}")

        document = result.setdefault("document", {})
        reference = document.get("reference") or {}
        width = _number(reference.get("width"))
        height = _number(reference.get("height"))
        if width <= 0 or height <= 0:
            raise ValueError("document.reference.width 和 height 必须大于 0")
        document["coordinate_system"] = str(
            document.get("coordinate_system") or "top-left"
        )
        document["unit"] = str(document.get("unit") or "px").lower()
        document["reference"] = {
            "width": _round(width),
            "height": _round(height),
            "dpi": _number(reference.get("dpi"), 300),
        }

        regions = self._normalize_regions(document, width, height)
        document["regions"] = regions
        elements = result.get("elements")
        if not isinstance(elements, list):
            raise ValueError("template_json.elements 必须是数组")

        used_ids: set[str] = set()
        normalized_elements = []
        warnings = list(result.get("warnings") or [])
        for index, raw_element in enumerate(elements):
            if not isinstance(raw_element, dict):
                raise ValueError(f"elements[{index}] 必须是对象")
            element = deepcopy(raw_element)
            element_id = self._unique_element_id(element, index, used_ids)
            element["id"] = element_id
            element["description"] = str(element.get("description") or "")
            raw_type = str(element.get("type") or "image").lower()
            element["type"] = "text" if raw_type == "text" else "image"
            element["z_index"] = int(_number(element.get("z_index"), index))
            frame = self._normalize_frame(element.get("frame"), index)
            region_id = str(
                frame.get("region_id")
                or element.get("region_id")
                or "canvas"
            )
            if region_id not in regions:
                warnings.append(f"元素 {element_id} 使用了不存在的区域 {region_id}，已改用 canvas")
                region_id = "canvas"
            region = regions[region_id]
            frame["region_id"] = region_id
            frame["reference_px"] = {
                key: _round((frame.get("reference_px") or frame)[key])
                for key in ("x", "y", "width", "height")
            }
            if frame.get("edited_px"):
                frame["edited_px"] = {
                    key: _round(frame[key])
                    for key in ("x", "y", "width", "height")
                }
            frame["relative"] = self._relative_frame(frame, region)
            frame.pop("formula", None)
            element["frame"] = frame
            element["transform"] = self._normalize_transform(
                element.get("transform"),
                element.get("style"),
            )

            if element["type"] == "text":
                font = self._normalize_font(element.get("font"), document)
                if strict_fonts and not self._font_is_resolved(font):
                    raise ValueError(f"文字元素 {element_id} 尚未指定可用字体")
                element["font"] = font
                raw_content = element.get("content")
                if not isinstance(raw_content, dict):
                    raw_content = {}
                if element.get("rule") and not raw_content.get("rule"):
                    raw_content = {**raw_content, "mode": "rule", "rule": element["rule"]}
                element["content"] = self._normalize_content(raw_content, element.get("text", ""))
                rule = element["content"].get("rule")
                if rule:
                    normalized_rule = self._normalize_rule(rule)
                    element["content"]["rule"] = normalized_rule
                    element["rule"] = deepcopy(normalized_rule)
                else:
                    element["rule"] = {}
                font.pop("size_formula", None)
                style = deepcopy(element.get("style") or {})
                writing_mode = style.get("writing_mode")
                if writing_mode not in {"horizontal", "vertical"}:
                    writing_mode = "vertical" if style.get("vertical") else "horizontal"
                style["writing_mode"] = writing_mode
                style["vertical"] = writing_mode == "vertical"
                element["style"] = style
            if element["type"] == "image":
                element.setdefault("asset", {})
                if not isinstance(element["asset"], dict):
                    raise ValueError(f"元素 {element_id} 的 asset 必须是对象")
                element["asset"].setdefault("asset_id", "")
                element["asset"].setdefault("url", "")
                element["asset"].setdefault(
                    "filename",
                    Path(str(element["asset"].get("path") or "")).name,
                )
            element["frontend"] = self._frontend_descriptor(element)
            normalized_elements.append(element)

        result["elements"] = sorted(normalized_elements, key=lambda item: item["z_index"])
        result["font_layout_templates"] = self._font_layout_templates(
            result["elements"],
            result.get("font_layout_templates"),
            document["reference"],
        )
        result["size_template"] = self._normalize_size_template_draft(
            result.get("size_template"),
            document,
        )
        result["field_index"] = self._field_index(result["elements"])
        result["element_index"] = {
            element["id"]: {
                "type": element["type"],
                "name": element.get("name", ""),
                "source_ref": element.get("source_ref"),
            }
            for element in result["elements"]
        }
        result["warnings"] = list(dict.fromkeys(warnings))
        result["updated_at"] = _now()

        saved = None
        saved_layouts = []
        if size_template_id is not None:
            if self.catalog_repository is None:
                raise ValueError("当前服务未配置模板数据库，不能保存 size_template_id")
            saved, saved_layouts = self._save_to_size_template(
                int(size_template_id),
                result,
            )
        return {
            "template": result,
            "saved_size_template": saved,
            "saved_font_layout_templates": saved_layouts,
        }

    def save(
        self,
        template: dict[str, Any],
        shop_id: int,
        product_names: list[str],
        template_name: str = "",
        strict_fonts: bool = True,
        size_template_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist a finalized draft and create one font-layout row per layout group."""
        if self.catalog_repository is None:
            raise ValueError("当前服务未配置模板数据库，不能保存模板")
        product_names = [str(item).strip() for item in (product_names or []) if str(item).strip()]
        if not product_names:
            raise ValueError("product_names 不能为空")
        finalized = self.finalize(template, strict_fonts=strict_fonts)
        normalized = finalized["template"]
        size_draft = normalized["size_template"]
        self._validate_size_template_for_save(size_draft)
        if template_name:
            size_draft["name"] = template_name
            size_draft["template_name"] = template_name
        size_draft["product_names"] = product_names
        fields = dict(size_draft.get("fields") or {})
        spec = normalize_size_spec(size_draft.get("spec") or {})
        size_payload = {
            "shop_id": int(shop_id),
            "product_names": product_names,
            "name": size_draft.get("template_name") or size_draft.get("name") or "",
            "selected_size_option_id": spec.get("selected"),
            "display_unit": spec.get("display_unit") or "in",
            "page_count": fields.get("page_count"),
            "page_count_options": fields.get("page_count_arr") or [],
            "size_options": spec.get("options") or [],
        }

        if size_template_id is None:
            create_payload = {
                key: value
                for key, value in size_payload.items()
                if key not in {"selected_size_option_id", "size_options"}
            }
            size_record = self.catalog_repository.create_size_template(create_payload)
            for option in spec.get("options") or []:
                size_record = self.catalog_repository.create_size_template_option(
                    int(size_record["id"]),
                    option,
                )
        else:
            size_record = self.catalog_repository.update_size_template(
                int(size_template_id),
                size_payload,
            )

        layout_records = self._save_font_layout_templates(
            int(shop_id),
            size_record.get("product_id"),
            normalized.get("font_layout_templates") or [],
        )
        normalized["size_template"]["id"] = size_record["id"]
        return {
            "template": normalized,
            "size_template": size_record,
            "font_layout_templates": layout_records,
        }

    @staticmethod
    def _validate_size_template_for_save(size_template: dict[str, Any]) -> None:
        """Require production dimensions only when a draft is persisted."""
        fields = size_template.get("fields") if isinstance(size_template, dict) else None
        if not isinstance(fields, dict):
            raise ValueError("size_template.fields 必须是对象")

        unit = fields.get("size_unit")
        if unit is None or not str(unit).strip():
            raise ValueError("保存尺寸模板前必须填写 size_unit")

        required_numeric = {
            "single_side_width": False,
            "single_side_height": False,
            "bleed": True,
            "spine_width": True,
            "spine_bleed": True,
            "page_count": True,
        }
        for key, allow_zero in required_numeric.items():
            value = fields.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"保存尺寸模板前必须填写 {key}")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if numeric < 0 or (not allow_zero and numeric <= 0):
                comparator = ">= 0" if allow_zero else "> 0"
                raise ValueError(f"{key} 必须 {comparator}")

        spec = size_template.get("spec") or {}
        if spec.get("options"):
            option = selected_size_option(spec)
            if option is None:
                raise ValueError("spec.selected is required when size options exist")
            option_fields = option
            option_required = [
                "size_unit",
                "single_side_width",
                "single_side_height",
                "bleed",
                "spine_bleed",
            ]
            option_mode = str(option_fields.get("spine_width_mode") or "fixed").lower()
            if option_mode == "fixed":
                option_required.append("spine_width")
            else:
                formula = option_fields.get("spine_width_formula")
                if not isinstance(formula, dict):
                    raise ValueError("dynamic spine width requires spine_width_formula")
                for formula_key in ("base", "per_page"):
                    if formula.get(formula_key) in (None, ""):
                        raise ValueError(f"spine_width_formula must include {formula_key}")
            for key in option_required:
                value = option_fields.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ValueError(f"selected size option must include {key}")
            return

    def _analyze_image(
        self,
        content: bytes,
        filename: str,
        content_type: str,
        digest: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with Image.open(io.BytesIO(content)) as source:
                image = ImageOps.exif_transpose(source)
                width, height = image.size
                image_format = str(source.format or Path(filename).suffix[1:]).lower()
                dpi_info = source.info.get("dpi") or (300, 300)
                dpi = _number(dpi_info[0] if isinstance(dpi_info, tuple) else dpi_info, 300)
        except Exception as exc:
            raise ValueError("图片文件无法读取或格式无效") from exc

        stored_path = self._store_source(
            filename,
            content,
            digest,
            Path(filename).suffix.lower(),
        )

        background = self._element(
            element_type="image",
            name="source_background",
            frame={"x": 0, "y": 0, "width": width, "height": height, "region_id": "canvas"},
            source_ref={"kind": "uploaded_image", "filename": filename},
            asset={"path": str(stored_path), "role": "reference_only"},
        )
        elements = [background]
        ocr_elements, ocr_status, ocr_warnings = self._ocr_elements(stored_path, width, height, options)
        elements.extend(ocr_elements)
        return self._base_template(
            filename=filename,
            content_type=content_type,
            digest=digest,
            source_kind="image",
            width=width,
            height=height,
            dpi=dpi,
            stored_path=stored_path,
            elements=elements,
            options=options,
            analysis={
                "image_format": image_format,
                "ocr": {"status": ocr_status, "warnings": ocr_warnings},
                "reconstruction_quality": "visual_reference",
            },
        )

    def _analyze_psd(
        self,
        content: bytes,
        filename: str,
        content_type: str,
        digest: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            import photoshopapi as psapi
        except ImportError as exc:
            raise RuntimeError("PSD 解析需要安装 PhotoshopAPI") from exc
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.psd"
            path.write_bytes(content)
            try:
                document = psapi.LayeredFile_8bit.read(path)
            except Exception as exc:
                raise ValueError("PSD 文件无法读取或已损坏") from exc
            width = int(document.width)
            height = int(document.height)
            dpi = _number(document.dpi, 300)
            elements = []
            self._collect_psd_layers(
                document.layers,
                elements,
                width,
                height,
                parent_path=[],
            )
        stored_path = self._store_source(filename, content, digest, ".psd")
        if not elements:
            elements.append(
                self._element(
                    element_type="image",
                    name="psd_canvas",
                    frame={"x": 0, "y": 0, "width": width, "height": height, "region_id": "canvas"},
                    source_ref={"kind": "uploaded_psd", "filename": filename},
                    asset={"path": str(stored_path), "role": "source_psd"},
                )
            )
        return self._base_template(
            filename=filename,
            content_type=content_type,
            digest=digest,
            source_kind="psd",
            width=width,
            height=height,
            dpi=dpi,
            stored_path=stored_path,
            elements=elements,
            options=options,
            analysis={
                "layer_count": len(elements),
                "reconstruction_quality": "layer_exact",
            },
        )

    def _base_template(
        self,
        *,
        filename: str,
        content_type: str,
        digest: str,
        source_kind: str,
        width: int,
        height: int,
        dpi: float,
        stored_path: Path,
        elements: list[dict[str, Any]],
        options: dict[str, Any],
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        unit = str(options.get("unit") or "px").lower()
        regions = options.get("regions")
        template = {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "source": {
                "kind": source_kind,
                "filename": filename,
                "content_type": content_type or "application/octet-stream",
                "sha256": digest,
                "stored_path": str(stored_path),
                "reference_width_px": width,
                "reference_height_px": height,
                "dpi": dpi,
                **analysis,
            },
            "document": {
                "unit": unit,
                "coordinate_system": "top-left",
                "reference": {"width": width, "height": height, "dpi": dpi},
                "regions": regions or {},
            },
            "elements": elements,
            "size_template": self._size_template_draft(
                filename,
                width,
                height,
                dpi,
                options,
            ),
            "metadata": {"created_at": _now(), "name": Path(filename).stem},
            "warnings": [],
        }
        return template

    def _element(
        self,
        *,
        element_type: str,
        name: str,
        frame: dict[str, Any],
        source_ref: dict[str, Any],
        text: str | None = None,
        font: dict[str, Any] | None = None,
        asset: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seed = f"{source_ref}|{name}|{frame.get('x')}|{frame.get('y')}|{text or ''}"
        element = {
            "id": f"{_slug(name)}-{sha256(seed.encode('utf-8')).hexdigest()[:10]}",
            "type": element_type,
            "name": str(name or "element"),
            "description": "",
            "frame": frame,
            "source_ref": source_ref,
            "z_index": 0,
        }
        if text is not None:
            element["text"] = text
            element["content"] = {"mode": "literal", "literal": text, "rule": None}
        if font is not None:
            element["font"] = font
        if asset is not None:
            element["asset"] = asset
        return element

    def _collect_psd_layers(
        self,
        layers: Any,
        output: list[dict[str, Any]],
        canvas_width: int,
        canvas_height: int,
        parent_path: list[str],
    ) -> None:
        for index, layer in enumerate(list(layers or [])):
            name = str(getattr(layer, "name", "") or f"layer-{index}")
            layer_path = [*parent_path, name]
            if hasattr(layer, "layers"):
                self._collect_psd_layers(
                    getattr(layer, "layers"),
                    output,
                    canvas_width,
                    canvas_height,
                    layer_path,
                )
                continue
            frame = self._psd_frame(layer, canvas_width, canvas_height)
            if frame is None:
                continue
            source_ref = {
                "kind": "psd_layer",
                "index": index,
                "path": layer_path,
                "name": name,
            }
            type_name = type(layer).__name__.lower()
            if "textlayer" in type_name:
                text = str(getattr(layer, "text", "") or "")
                element = self._element(
                    element_type="text",
                    name=name,
                    frame=frame,
                    source_ref=source_ref,
                    text=text,
                    font=self._psd_font(layer),
                )
                element["style"] = self._psd_style(layer)
                paragraph_elements = self._split_text_elements(element)
            else:
                element = self._element(
                    element_type="image",
                    name=name,
                    frame=frame,
                    source_ref=source_ref,
                    asset={"role": "source_layer", "layer_type": type_name},
                )
                paragraph_elements = [element]
            for paragraph in paragraph_elements:
                paragraph["z_index"] = len(output)
                output.append(paragraph)

    @staticmethod
    def _psd_frame(layer: Any, canvas_width: int, canvas_height: int) -> dict[str, Any] | None:
        position = _call(getattr(layer, "position", None), default=None)
        if not isinstance(position, (tuple, list)) or len(position) < 2:
            center_x = _number(getattr(layer, "center_x", None), canvas_width / 2)
            center_y = _number(getattr(layer, "center_y", None), canvas_height / 2)
            width = _number(getattr(layer, "width", None))
            height = _number(getattr(layer, "height", None))
            if width <= 0 or height <= 0:
                return None
            return {"x": center_x - width / 2, "y": center_y - height / 2, "width": width, "height": height, "region_id": "canvas"}
        bounds = _call(getattr(layer, "box_bounds", None), default=None)
        width = _number(_call(getattr(layer, "box_width", None), default=None))
        height = _number(_call(getattr(layer, "box_height", None), default=None))
        top = left = 0.0
        if isinstance(bounds, (tuple, list)) and len(bounds) >= 4:
            top, left = _number(bounds[0]), _number(bounds[1])
            width = width or max(0.0, _number(bounds[3]) - left)
            height = height or max(0.0, _number(bounds[2]) - top)
        width = width or _number(getattr(layer, "width", None))
        height = height or _number(getattr(layer, "height", None))
        if width <= 0 or height <= 0:
            return None
        return {
            "x": _number(position[0]) + left,
            "y": _number(position[1]) + top,
            "width": width,
            "height": height,
            "region_id": "canvas",
        }

    @staticmethod
    def _psd_font(layer: Any) -> dict[str, Any]:
        runs = []
        lengths = _call(getattr(layer, "style_run_lengths", None), default=[])
        for index in range(len(lengths or [])):
            size = _number(_call(getattr(layer, "style_run_font_size", None), index, default=None))
            tracking = _number(_call(getattr(layer, "style_run_tracking", None), index, default=None))
            leading = _number(_call(getattr(layer, "style_run_leading", None), index, default=None))
            font_index = _call(getattr(layer, "style_run_font", None), index, default=0)
            font_name = _call(getattr(layer, "font_name", None), int(font_index or 0), default=None)
            runs.append({"font": font_name, "size_px": size, "tracking": tracking, "leading": leading})
        first = runs[0] if runs else {}
        family = str(getattr(layer, "primary_font_name", "") or first.get("font") or "").strip()
        size_px = _number(first.get("size_px"), 0)
        result = {
            "status": "detected" if family else "unassigned",
            "family": family,
            "postscript_name": family,
            "size_px": size_px or None,
            "tracking": _number(first.get("tracking"), 0),
            "leading_px": _number(first.get("leading"), 0) or None,
            "runs": runs,
            "detected_from": "psd",
        }
        for key, attr in (("vertical", "is_vertical"), ("rotation", "rotation_angle")):
            value = getattr(layer, attr, None)
            if value is not None:
                result[key] = value if not callable(value) else _call(value, default=None)
        return result

    @staticmethod
    def _psd_style(layer: Any) -> dict[str, Any]:
        fill = _call(getattr(layer, "style_run_fill_color", None), 0, default=None)
        alignment = _call(
            getattr(layer, "paragraph_run_justification", None),
            0,
            default=None,
        )
        return {
            "fill_color": fill,
            "alignment": _enum_name(alignment),
            "rotation": _number(getattr(layer, "rotation_angle", 0)),
            "vertical": bool(getattr(layer, "is_vertical", False)),
        }

    @staticmethod
    def _split_text_elements(element: dict[str, Any]) -> list[dict[str, Any]]:
        text = str(element.get("text") or "")
        paragraphs = re.split(r"\r\n|\n|\r", text)
        if len(paragraphs) <= 1:
            return [element]
        frame = element["frame"]
        font = element.get("font") or {}
        leading = _number(font.get("leading_px"))
        line_height = leading or frame["height"] / max(1, len(paragraphs))
        result = []
        for index, paragraph_text in enumerate(paragraphs):
            item = deepcopy(element)
            item["id"] = f"{element['id']}-p{index + 1}"
            item["name"] = f"{element.get('name', 'text')} / paragraph {index + 1}"
            item["text"] = paragraph_text
            item["content"] = {
                "mode": "literal",
                "literal": paragraph_text,
                "rule": None,
            }
            item["frame"] = {
                **frame,
                "y": _round(frame["y"] + index * line_height),
                "height": _round(line_height),
            }
            item["source_ref"] = {
                **(element.get("source_ref") or {}),
                "paragraph_index": index,
                "paragraph_count": len(paragraphs),
            }
            result.append(item)
        return result

    def _ocr_elements(
        self,
        path: Path,
        width: int,
        height: int,
        options: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        command = shutil.which(self.tesseract_command) or (
            self.tesseract_command if Path(self.tesseract_command).is_file() else None
        )
        if not command:
            return [], "unavailable", ["未检测到 Tesseract，图片文字需要前端手动确认或配置 OCR 服务"]
        language = str(options.get("ocr_languages") or self.ocr_languages)
        try:
            completed = subprocess.run(
                [command, str(path), "stdout", "--psm", "11", "-l", language, "tsv"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [], "failed", [f"OCR 执行失败: {type(exc).__name__}"]
        if completed.returncode != 0:
            return [], "failed", [completed.stderr.strip() or "OCR 返回错误"]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        reader = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t")
        for row in reader:
            text = str(row.get("text") or "").strip()
            confidence = _number(row.get("conf"), -1)
            if not text or confidence < _number(options.get("ocr_min_confidence"), 35):
                continue
            key = (str(row.get("block_num")), str(row.get("par_num")), str(row.get("line_num")))
            groups.setdefault(key, []).append(row)
        elements = []
        for index, words in enumerate(groups.values()):
            left = min(_number(word.get("left")) for word in words)
            top = min(_number(word.get("top")) for word in words)
            right = max(
                _number(word.get("left")) + _number(word.get("width"))
                for word in words
            )
            bottom = max(
                _number(word.get("top")) + _number(word.get("height"))
                for word in words
            )
            text = " ".join(str(word.get("text") or "").strip() for word in words).strip()
            elements.append(
                self._element(
                    element_type="text",
                    name=f"ocr_text_{index + 1}",
                    frame={"x": left, "y": top, "width": right - left, "height": bottom - top, "region_id": "canvas"},
                    source_ref={"kind": "ocr", "index": index, "confidence": min(_number(w.get("conf"), 0) for w in words)},
                    text=text,
                    font={"status": "unassigned", "family": "", "detected_from": "ocr"},
                )
            )
        return elements, "complete", []

    def _apply_frontend_metadata(self, template: dict[str, Any], options: dict[str, Any]) -> None:
        assignments = options.get("font_assignments") or {}
        rules = options.get("rules") or []
        if not isinstance(assignments, dict):
            assignments = {}
        if not isinstance(rules, list):
            raise ValueError("options.rules 必须是数组")
        for element in template["elements"]:
            keys = [element["id"], element.get("name", "")]
            assignment = next((assignments[key] for key in keys if key in assignments), None)
            if isinstance(assignment, dict) and element.get("type") == "text":
                element["font"] = {**(element.get("font") or {}), **assignment, "status": "assigned"}
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                target = rule.get("element_id") or rule.get("name") or rule.get("layer_name")
                if target in keys and element.get("type") == "text":
                    content = dict(element.get("content") or {})
                    content["rule"] = deepcopy(rule)
                    content["mode"] = "rule"
                    element["content"] = content

    def _enrich_with_deepseek(self, template: dict[str, Any]) -> None:
        if not self.deepseek_api_key or not self.deepseek_api_url:
            template.setdefault("warnings", []).append("未配置 DeepSeek，跳过模板语义识别")
            return
        payload = [
            {"id": e["id"], "name": e.get("name", ""), "type": e.get("type"), "text": e.get("text", "")}
            for e in template.get("elements", [])
            if e.get("type") == "text"
        ]
        if not payload:
            return
        try:
            response = requests.post(
                self.deepseek_api_url,
                headers={"Authorization": f"Bearer {self.deepseek_api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.deepseek_model,
                    "messages": [
                        {"role": "system", "content": "你是模板字段分类器。只返回 JSON 对象，格式为 {\"elements\":[{\"id\":\"...\",\"role\":\"...\",\"binding_key\":\"...\",\"suggested_rule\":null}]}。不要修改坐标、字体或原文。suggested_rule 只能是建议，不能当作已确认规则。"},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
                timeout=self.deepseek_timeout,
            )
            response.raise_for_status()
            body = response.json().get("choices", [])[0].get("message", {}).get("content", "")
            decoded = json.loads(body)
            suggestions = decoded.get("elements", decoded if isinstance(decoded, list) else [])
            by_id = {str(item.get("id")): item for item in suggestions if isinstance(item, dict)}
            for element in template.get("elements", []):
                suggestion = by_id.get(element["id"])
                if suggestion:
                    element["semantic_suggestion"] = {
                        "role": suggestion.get("role"),
                        "binding_key": suggestion.get("binding_key"),
                        "suggested_rule": suggestion.get("suggested_rule"),
                        "confirmed": False,
                    }
        except Exception as exc:
            template.setdefault("warnings", []).append(f"DeepSeek 语义识别失败: {type(exc).__name__}")

    @staticmethod
    def _normalize_regions(document: dict[str, Any], width: float, height: float) -> dict[str, dict[str, float]]:
        source = document.get("regions") or {}
        if isinstance(source, list):
            source = {str(item.get("id")): item for item in source if isinstance(item, dict) and item.get("id")}
        regions = {"canvas": {"x": 0.0, "y": 0.0, "width": width, "height": height}}
        for region_id, raw in source.items():
            if region_id == "canvas" or not isinstance(raw, dict):
                continue
            frame = raw.get("frame") if isinstance(raw.get("frame"), dict) else raw
            values = {key: _number(frame.get(key)) for key in ("x", "y", "width", "height")}
            if values["width"] > 0 and values["height"] > 0:
                regions[str(region_id)] = values
        return regions

    @staticmethod
    def _normalize_frame(raw: Any, index: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError(f"elements[{index}].frame 必须是对象")
        reference_source = raw.get("reference_px") or raw.get("reference") or raw
        if not isinstance(reference_source, dict):
            raise ValueError(f"elements[{index}].frame.reference_px 必须是对象")
        edited_source = raw.get("edited_px") or raw.get("current_px")
        if edited_source is not None and not isinstance(edited_source, dict):
            raise ValueError(f"elements[{index}].frame.edited_px 必须是对象")
        source = edited_source or reference_source
        frame = {key: _number(source.get(key)) for key in ("x", "y", "width", "height")}
        if frame["width"] <= 0 or frame["height"] <= 0:
            raise ValueError(f"elements[{index}] 的 width 和 height 必须大于 0")
        frame["region_id"] = raw.get("region_id") or source.get("region_id") or "canvas"
        frame["reference_px"] = {
            key: _number(reference_source.get(key))
            for key in ("x", "y", "width", "height")
        }
        if edited_source is not None:
            frame["edited_px"] = {
                key: _number(source.get(key))
                for key in ("x", "y", "width", "height")
            }
        return frame

    @staticmethod
    def _normalize_transform(raw: Any, fallback: Any = None) -> dict[str, Any]:
        """Normalize shared rotation and flip controls for every layer type."""
        transform = deepcopy(raw) if isinstance(raw, dict) else {}
        fallback = fallback if isinstance(fallback, dict) else {}
        rotation = transform.get("rotation_deg")
        if rotation is None:
            rotation = transform.get("rotation", fallback.get("rotation", 0))
        transform["rotation_deg"] = _number(rotation, 0)
        transform["flip_horizontal"] = bool(
            transform.get("flip_horizontal", transform.get("flip_x", False))
        )
        transform["flip_vertical"] = bool(
            transform.get("flip_vertical", transform.get("flip_y", False))
        )
        return transform

    @staticmethod
    def _relative_frame(frame: dict[str, Any], region: dict[str, float]) -> dict[str, float]:
        return {
            "x": _round((frame["x"] - region["x"]) / region["width"]),
            "y": _round((frame["y"] - region["y"]) / region["height"]),
            "width": _round(frame["width"] / region["width"]),
            "height": _round(frame["height"] / region["height"]),
        }

    @staticmethod
    def _normalize_content(raw: Any, fallback: Any) -> dict[str, Any]:
        content = deepcopy(raw) if isinstance(raw, dict) else {"mode": "literal", "literal": str(fallback or "")}
        content["mode"] = str(content.get("mode") or "literal")
        content.setdefault("literal", str(fallback or ""))
        if content["mode"] not in {"literal", "binding", "rule"}:
            raise ValueError("文字 content.mode 只能是 literal、binding 或 rule")
        if content["mode"] == "binding" and not content.get("binding_key"):
            raise ValueError("binding 模式必须填写 binding_key")
        return content

    def _normalize_font(self, raw: Any, document: dict[str, Any]) -> dict[str, Any]:
        font = deepcopy(raw) if isinstance(raw, dict) else {}
        font["status"] = str(font.get("status") or "unassigned")
        font["family"] = str(font.get("family") or font.get("font_family") or "").strip()
        font["postscript_name"] = str(font.get("postscript_name") or font.get("font_name") or "").strip()
        if font.get("font_id") is None:
            matched = self._match_detected_font(font["postscript_name"] or font["family"])
            if matched:
                font["font_id"] = matched["id"]
                font["font_name"] = matched.get("font_name", "")
                font["font_family"] = matched.get("font_family", "")
                font["file_path"] = matched.get("file_path", "")
                font["status"] = "matched"
        if font.get("font_id") is not None:
            font["font_id"] = int(font["font_id"])
            if self.catalog_repository is not None:
                try:
                    record = self.catalog_repository.get_font(font["font_id"])
                except LookupError as exc:
                    raise ValueError(f"字体 ID 不存在: {font['font_id']}") from exc
                if not record.get("enabled", True):
                    raise ValueError(f"字体已禁用: {font['font_id']}")
                font["family"] = font["family"] or record.get("font_family") or record.get("font_name", "")
                font["font_name"] = font.get("font_name") or record.get("font_name", "")
                font["font_family"] = font.get("font_family") or record.get("font_family", "")
                font["file_path"] = font.get("file_path") or record.get("file_path", "")
        if font.get("size_px") is not None:
            font["size_px"] = _number(font["size_px"])
        elif font.get("size") is not None:
            font["size_px"] = _number(font["size"])
        for key in ("tracking", "leading_px"):
            if font.get(key) is not None:
                font[key] = _number(font[key])
        font["font_name"] = str(font.get("font_name") or font["postscript_name"] or font["family"])
        font["font_family"] = str(font.get("font_family") or font["family"] or font["font_name"])
        font.setdefault("font_id", None)
        font["file_path"] = str(font.get("file_path") or "")
        style_name = f"{font['font_name']} {font['postscript_name']}".casefold()
        font["bold"] = bool(font.get("bold", "bold" in style_name))
        font["italic"] = bool(
            font.get("italic", "italic" in style_name or "oblique" in style_name)
        )
        return font

    def _match_detected_font(self, detected_name: str) -> dict[str, Any] | None:
        if not detected_name or self.catalog_repository is None:
            return None
        list_fonts = getattr(self.catalog_repository, "list_fonts", None)
        if not callable(list_fonts):
            return None
        try:
            candidates = list_fonts(100, 0, True, detected_name).get("items", [])
        except (LookupError, TypeError, ValueError):
            return None
        normalized = detected_name.casefold()
        return next(
            (
                item
                for item in candidates
                if normalized
                in {
                    str(item.get("font_name") or "").casefold(),
                    str(item.get("font_family") or "").casefold(),
                }
            ),
            None,
        )

    @staticmethod
    def _font_is_resolved(font: dict[str, Any]) -> bool:
        return bool(font.get("font_id") or font.get("file_path") or font.get("postscript_name"))

    @staticmethod
    def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(rule)
        kind = str(result.get("kind") or result.get("type") or "custom").strip().lower()
        result["kind"] = kind
        result["source_fields"] = [str(field) for field in (result.get("source_fields") or result.get("fields") or [])]
        result["description"] = str(result.get("description") or "").strip()
        result["parameters"] = deepcopy(result.get("parameters") or {})
        result["executable"] = kind in KNOWN_RULES
        if kind == "initials" and not result["source_fields"]:
            raise ValueError("initials 规则必须填写 source_fields")
        if kind not in KNOWN_RULES and not result["description"]:
            raise ValueError("自定义文字规则必须填写 description")
        if kind in KNOWN_RULES:
            result["expression"] = TemplateImportService._rule_expression(kind, result["source_fields"], result["parameters"])
        return result

    @staticmethod
    def _rule_expression(kind: str, source_fields: list[str], parameters: dict[str, Any]) -> str:
        source = ", ".join(f'fields.{field}' for field in source_fields)
        if kind == "initials":
            separator = json.dumps(str(parameters.get("separator", "|")), ensure_ascii=False)
            return f"initials({source}, separator={separator})"
        if kind == "concat":
            separator = json.dumps(str(parameters.get("separator", "")), ensure_ascii=False)
            return f"concat({source}, separator={separator})"
        return f"{kind}({source})"

    @staticmethod
    def _frontend_descriptor(element: dict[str, Any]) -> dict[str, Any]:
        return {
            "element_id": element["id"],
            "label": element.get("name", element["id"]),
            "type": element["type"],
            "source_layer": (element.get("source_ref") or {}).get("path") or (element.get("source_ref") or {}).get("name"),
            "editable": True,
            "selectable_by": [element["id"], str(element.get("name", ""))],
        }

    @staticmethod
    def _size_template_draft(
        filename: str,
        width: int,
        height: int,
        dpi: float,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        raw_options = options.get("size_options") or []
        size_options = normalize_size_options(raw_options)
        fields = {
            key: deepcopy((options.get("size_fields") or {}).get(key))
            for key in SIZE_TEMPLATE_FIELD_KEYS
        }
        return {
            "id": None,
            "name": str(options.get("template_name") or Path(filename).stem),
            "template_name": str(options.get("template_name") or Path(filename).stem),
            "product_names": list(options.get("product_names") or []),
            "spec": {
                "selected": deepcopy(options.get("size_spec")),
                "options": size_options,
                "selection_required": True,
            },
            "fields": fields,
            "visual": {
                "reference_canvas": {
                    "width_px": width,
                    "height_px": height,
                    "dpi": dpi,
                },
                "background": deepcopy(options.get("background") or {}),
                "colors": deepcopy(options.get("colors") or []),
            },
        }

    @staticmethod
    def _normalize_size_template_draft(
        raw: Any,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        draft = deepcopy(raw) if isinstance(raw, dict) else {}
        draft.setdefault("id", None)
        draft.setdefault("name", "未命名尺寸模板")
        draft.setdefault("template_name", draft["name"])
        draft["product_names"] = list(draft.get("product_names") or [])
        spec = normalize_size_spec(draft.get("spec"))
        draft["spec"] = spec
        fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
        draft["fields"] = {
            key: deepcopy(fields.get(key))
            for key in SIZE_TEMPLATE_FIELD_KEYS
        }
        option = selected_size_option(spec)
        if option:
            option_fields = option
            for key in SIZE_VARIANT_FIELDS:
                if option_fields.get(key) is not None:
                    draft["fields"][key] = deepcopy(option_fields[key])
            if draft["fields"].get("spine_width_mode") == "by_page_count":
                draft["fields"]["spine_width"] = resolve_spine_width(draft["fields"])
        visual = draft.get("visual") if isinstance(draft.get("visual"), dict) else {}
        reference = document.get("reference") or {}
        visual.setdefault(
            "reference_canvas",
            {
                "width_px": reference.get("width"),
                "height_px": reference.get("height"),
                "dpi": reference.get("dpi"),
            },
        )
        visual.setdefault("background", {})
        visual.setdefault("colors", [])
        draft["visual"] = visual
        return draft

    @staticmethod
    def _font_layout_templates(
        elements: list[dict[str, Any]],
        existing: Any = None,
        reference_canvas: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        existing_by_group = {}
        if isinstance(existing, list):
            existing_by_group = {
                str(
                    item.get("group_key")
                    or (item.get("options") or {}).get("group_key")
                ): item
                for item in existing
                if isinstance(item, dict)
                and (
                    item.get("group_key")
                    or (item.get("options") or {}).get("group_key")
                )
            }
        groups: dict[str, list[dict[str, Any]]] = {}
        for element in elements:
            group_key = str(
                element.get("font_layout_group")
                or element.get("layout_group")
                or (element.get("frame") or {}).get("region_id")
                or "default"
            )
            groups.setdefault(group_key, []).append(element)

        result = []
        for group_key, group_elements in groups.items():
            previous = existing_by_group.get(group_key) or {}
            previous_options = (
                dict(previous.get("options") or {})
                if isinstance(previous.get("options"), dict)
                else {}
            )
            legacy_layout = (
                dict(previous.get("layout") or {})
                if isinstance(previous.get("layout"), dict)
                else {}
            )
            region_id = str((group_elements[0].get("frame") or {}).get("region_id") or "canvas")
            default_names = {
                "front_cover": "封面文字布局",
                "back_cover": "封底文字布局",
                "spine": "书脊文字布局",
                "canvas": "默认文字布局",
            }
            layout_elements = []
            binding_keys = []
            for element in group_elements:
                content = element.get("content") or {}
                if element.get("type") == "text":
                    binding_key = content.get("binding_key")
                    if binding_key and binding_key not in binding_keys:
                        binding_keys.append(binding_key)
                layout_elements.append(TemplateImportService._font_layout_element(element))
            result.append(
                {
                    "id": previous.get("id"),
                    "name": previous.get("name") or default_names.get(region_id, f"{region_id}文字布局"),
                    "description": previous.get("description") or "",
                    "safe_distance": _number(previous.get("safe_distance"), 0),
                    "elements": layout_elements,
                    "options": {
                        **previous_options,
                        "group_key": group_key,
                        "region_id": region_id,
                        "scope": {"region_id": region_id},
                        "coordinate_system": previous_options.get(
                            "coordinate_system",
                            legacy_layout.get("coordinate_system", "top-left"),
                        ),
                        "responsive": previous_options.get(
                            "responsive",
                            legacy_layout.get("responsive", True),
                        ),
                        "scaling": {
                            **(
                                legacy_layout.get("scaling")
                                if isinstance(legacy_layout.get("scaling"), dict)
                                else {}
                            ),
                            **(
                                previous_options.get("scaling")
                                if isinstance(previous_options.get("scaling"), dict)
                                else {}
                            ),
                            "mode": "uniform",
                            "reference_canvas": deepcopy(reference_canvas or {}),
                            "expression": (
                                "min(target_canvas.width_px / reference_canvas.width_px, "
                                "target_canvas.height_px / reference_canvas.height_px)"
                            ),
                        },
                        "element_ids": [element["id"] for element in group_elements],
                        "binding_keys": binding_keys,
                    },
                    "frontend": {
                        "template_id": previous.get("id"),
                        "element_ids": [element["id"] for element in group_elements],
                    },
                }
            )
        return result

    @staticmethod
    def _font_layout_element(element: dict[str, Any]) -> dict[str, Any]:
        element_type = "text" if element.get("type") == "text" else "image"
        result = {
            "id": element["id"],
            "name": element.get("name", element["id"]),
            "description": element.get("description", ""),
            "type": element_type,
            "frame": deepcopy(element.get("frame") or {}),
            "transform": deepcopy(element.get("transform") or {}),
        }
        if element_type == "text":
            content = element.get("content") or {}
            result.update(
                {
                    "text": element.get("text", content.get("literal", "")),
                    "rule": deepcopy(element.get("rule") or content.get("rule") or {}),
                    "font": deepcopy(element.get("font") or {}),
                    "style": deepcopy(element.get("style") or {}),
                }
            )
        else:
            asset = deepcopy(element.get("asset") or {})
            asset.setdefault("asset_id", "")
            asset.setdefault("url", "")
            asset.setdefault("filename", Path(str(asset.get("path") or "")).name)
            result["asset"] = asset
        return result

    @staticmethod
    def _font_layout_storage_payload(layout: dict[str, Any]) -> dict[str, Any]:
        legacy_layout = (
            dict(layout.get("layout") or {})
            if isinstance(layout.get("layout"), dict)
            else {}
        )
        elements = layout.get("elements")
        if not isinstance(elements, list):
            elements = legacy_layout.get("elements")
        if not isinstance(elements, list):
            elements = []

        options = (
            deepcopy(layout.get("options") or {})
            if isinstance(layout.get("options"), dict)
            else {}
        )
        for key in ("coordinate_system", "responsive", "scaling"):
            if key in legacy_layout and key not in options:
                options[key] = deepcopy(legacy_layout[key])
        if layout.get("group_key") and "group_key" not in options:
            options["group_key"] = layout["group_key"]
        if isinstance(layout.get("scope"), dict) and "scope" not in options:
            options["scope"] = deepcopy(layout["scope"])
        legacy_fields = layout.get("fields")
        if isinstance(legacy_fields, dict):
            for key in ("element_ids", "binding_keys"):
                if key in legacy_fields and key not in options:
                    options[key] = deepcopy(legacy_fields[key])
        return {
            "name": layout.get("name") or "文字布局",
            "description": layout.get("description") or "",
            "safe_distance": _number(layout.get("safe_distance"), 0),
            "elements": deepcopy(elements),
            "options": options,
        }

    def _save_font_layout_templates(
        self,
        shop_id: int,
        product_id: int | None,
        layouts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = []
        for layout in layouts:
            payload = {
                "shop_id": int(shop_id),
                "product_id": int(product_id) if product_id is not None else None,
                "name": layout.get("name") or "文字布局",
                "layers": {
                    "objects": deepcopy(layout.get("elements") or []),
                    "animations": [],
                    "styles": [],
                    "dataSources": [],
                },
            }
            layout_id = layout.get("id")
            if isinstance(layout_id, int):
                try:
                    record = self.catalog_repository.update_font_layout_library_template(
                        layout_id,
                        payload,
                    )
                except LookupError:
                    record = self.catalog_repository.create_font_layout_library_template(
                        payload
                    )
            else:
                record = self.catalog_repository.create_font_layout_library_template(
                    payload
                )
            layout["id"] = record["id"]
            layout.setdefault("frontend", {})["template_id"] = record["id"]
            records.append(record)
        return records

    @staticmethod
    def _field_index(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for element in elements:
            if element.get("type") != "text":
                continue
            content = element.get("content") or {}
            key = content.get("binding_key")
            if not key and content.get("rule"):
                key = (content["rule"].get("source_fields") or [None])[0]
            if key:
                entry = index.setdefault(str(key), {"key": str(key), "element_ids": [], "type": "text"})
                entry["element_ids"].append(element["id"])
        return list(index.values())

    def _unique_element_id(self, element: dict[str, Any], index: int, used: set[str]) -> str:
        # The frontend owns the layer name; use it as the stable ID source.
        base = str(element.get("name") or element.get("id") or f"element-{index + 1}").strip()
        base = base or f"element-{index + 1}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    def _save_to_size_template(
        self,
        template_id: int,
        template: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        current = self.catalog_repository.get_size_template(template_id)
        layouts = self._save_font_layout_templates(
            int(current["shop_id"]),
            current.get("product_id"),
            template.get("font_layout_templates") or [],
        )
        return self.catalog_repository.get_size_template(template_id), layouts

    def _store_source(self, filename: str, content: bytes, digest: str, suffix: str) -> Path:
        target_dir = self.source_dir / digest[:2] / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = _slug(Path(filename).stem, "source")[:80]
        target = target_dir / f"{stem}{suffix}"
        if not target.exists():
            target.write_bytes(content)
        return target
