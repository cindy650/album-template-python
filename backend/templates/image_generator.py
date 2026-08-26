from __future__ import annotations

from copy import deepcopy
import base64
import binascii
import json
from io import BytesIO
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlparse
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
import requests

from backend.catalog import CatalogRepository
from backend.storage import (
    order_filename,
    order_resource_dir,
    order_resource_filename,
)
from backend.templates.size_variants import apply_selected_size_variant, resolve_spine_width
from backend.templates.order_resolver import OrderTemplateResolver
from backend.templates.rendering_rules import fabric_layer_geometry


UNIT_TO_INCHES = {
    "in": 1.0,
    "cm": 1 / 2.54,
    "mm": 1 / 25.4,
}


class DeepSeekTemplateRuleResolver:
    """Resolve template text rules with DeepSeek, keeping drawing local.

    The backend selects the size option and its size child layout locally.
    DeepSeek only returns structured text values for the already-selected
    layout (and may identify a font layout when local matching has no result).
    Pillow remains responsible for all geometry, font measurement, fitting and
    rasterization, so an LLM response cannot alter the canvas directly.
    """

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        model: str = "deepseek-chat",
        timeout_seconds: float = 60,
    ):
        self.api_url = str(api_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "deepseek-chat").strip()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.api_key)

    @staticmethod
    def _layout_elements(layout: dict[str, Any], selected_size: Any = None):
        def normalize(items):
            return [
                # The resolver owns selection, not a Fabric compatibility
                # migration.  DeepSeek must see the exact current layer JSON.
                deepcopy(item)
                for item in (items or [])
                if isinstance(item, dict)
            ]

        def objects(value: Any):
            if isinstance(value, dict):
                return normalize(value.get("objects") or [])
            return []

        if not isinstance(layout, dict):
            return []
        option_id = str(selected_size or "")
        size_layouts = layout.get("_size_option_layouts")
        if isinstance(size_layouts, dict) and option_id:
            option_layout = size_layouts.get(option_id)
            if isinstance(option_layout, dict):
                return normalize(
                    option_layout.get("objects")
                    or option_layout.get("elements")
                    or objects(option_layout.get("layers"))
                )
        raw_size_layouts = layout.get("size_layouts") or layout.get("sizeLayouts")
        if isinstance(raw_size_layouts, list) and option_id:
            for option_layout in raw_size_layouts:
                if isinstance(option_layout, dict) and str(option_layout.get("size_option_id") or option_layout.get("sizeOptionId") or "") == option_id:
                    return normalize(
                        option_layout.get("objects")
                        or option_layout.get("elements")
                        or objects(option_layout.get("layers"))
                    )
        return normalize(
            layout.get("objects")
            or layout.get("elements")
            or objects(layout.get("layers"))
        )

    def resolve(
        self,
        product_information: dict[str, Any],
        size_options: list[dict[str, Any]],
        layouts: list[dict[str, Any]],
        selected_size: Any = None,
        selected_layout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("未配置 DeepSeek，跳过模板规则解析")
        layout_payload = []
        for layout in layouts:
            if not isinstance(layout, dict):
                continue
            rules = []
            for element in self._layout_elements(layout, selected_size):
                if not isinstance(element, dict) or str(element.get("type") or "text").lower() not in {
                    "text", "textbox", "i-text", "itext"
                }:
                    continue
                content = element.get("content") or {}
                rule = element.get("rule") or content.get("rule") or element.get("rules") or {}
                rule_payload = (
                    rule
                    if isinstance(rule, dict)
                    else {"description": str(rule)}
                )
                if not str(rule_payload.get("description") or "").strip():
                    continue
                rules.append({
                    "id": str(element.get("id") or element.get("name") or ""),
                    "name": str(element.get("name") or ""),
                    "text": str(element.get("text") or content.get("literal") or ""),
                    "rule": rule_payload,
                })
            layout_payload.append({
                "id": layout.get("id"),
                "name": layout.get("name"),
                "elements": rules,
            })
        payload = {
            "product_information": product_information or {},
            # Size matching is deliberately local. Do not expose the complete
            # option list to the model as a selection surface.
            "selected_size": selected_size,
            "selected_layout": (selected_layout or {}).get("name"),
            "font_layouts": layout_payload,
        }
        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是印刷模板规则解析器。根据订单商品信息阅读每个字体布局元素的 rule.description、"
                            "source_fields、binding_key 和 literal，将订单字段转换为最终要绘制的文字。"
                            "尺寸方案和尺寸子布局已经由后端本地规则选定，selected_size 是最终值，禁止修改尺寸、"
                            "禁止选择其他尺寸子布局，也不要返回尺寸选择建议。只处理 selected_size 对应的当前字体布局中的"
                            "自然语言 rule.description；如果本地没有匹配字体布局，可以返回字体布局名称供后端匹配。"
                            "只返回 JSON，不要 Markdown，格式必须为："
                            '{"font_layout_name":null,"elements":[{"id":"","text":""}]}。'
                            "elements 只能包含模板中已有的文字元素；没有值时 text 返回空字符串。"
                            "保留姓名、日期、尺寸、编号和用户原文，不要添加省略号，不要编造内容。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek 返回的模板规则 JSON 格式不正确") from exc
        if not isinstance(decoded, dict):
            raise ValueError("DeepSeek 返回的模板规则必须是对象")
        elements = decoded.get("elements") or []
        if not isinstance(elements, list):
            raise ValueError("DeepSeek 返回的 elements 必须是数组")
        valid_ids = {
            str(item.get("id") or item.get("name"))
            for layout in layouts if isinstance(layout, dict)
            for item in self._layout_elements(layout, selected_size)
            if isinstance(item, dict) and (item.get("id") or item.get("name"))
        }
        values = {}
        for item in elements:
            if not isinstance(item, dict) or str(item.get("id") or "") not in valid_ids:
                continue
            text = item.get("text")
            if text is not None and not isinstance(text, str):
                text = str(text)
            values[str(item["id"])] = (text or "").strip()
        return {
            "font_layout_name": decoded.get("font_layout_name"),
            "text_values": values,
        }


class TemplateImageGenerator:
    """Generate JPEG cover previews using the dimensions stored in MySQL."""

    _font_cache_dir = Path("generated_fonts")

    def __init__(
        self,
        repository: CatalogRepository,
        output_dir: Path,
        dpi: int = 300,
        template_id: int | None = None,
        storage_service=None,
        deepseek_resolver: DeepSeekTemplateRuleResolver | None = None,
        order_repository=None,
        image_map_renderer=None,
    ):
        self.repository = repository
        self.output_dir = Path(output_dir)
        self.dpi = int(dpi)
        self.template_id = template_id
        self.storage_service = storage_service
        self.deepseek_resolver = deepseek_resolver
        self.template_resolver = OrderTemplateResolver(deepseek_resolver)
        self.order_repository = order_repository
        self.image_map_renderer = image_map_renderer
        self.font_cache_dir = self.output_dir.parent / "generated_fonts"

    def _persist_template_resolution(
        self,
        resolved_template: dict[str, Any],
        saved_order: dict[str, Any] | None,
    ) -> dict[str, Any]:
        layers = self._template_resolution_layers(resolved_template)
        snapshot = self._template_snapshot_json(resolved_template)
        order_id = (saved_order or {}).get("id")
        saver = getattr(self.order_repository, "save_template_resolution", None)
        if order_id is None or not callable(saver):
            return layers
        saver(order_id, snapshot, layers)
        return layers

    @staticmethod
    def _template_snapshot_json(resolved_template: dict[str, Any]) -> dict[str, Any]:
        """Return the compact size-template snapshot stored on an order."""
        template = resolved_template or {}
        fields = template.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        size_spec = template.get("size_spec") or (template.get("fields") or {}).get("size_spec") or {}
        selected_size = str(size_spec.get("selected") or "")
        option = template.get("matched_size_option") or template.get("size_template_option")
        if not isinstance(option, dict):
            option = next(
                (
                    item for item in size_spec.get("options") or []
                    if isinstance(item, dict) and str(item.get("id")) == selected_size
                ),
                {},
            )
        # The resolver mutates this selected option with the order-specific
        # spine width. Read the final value from that option first, then use
        # the flattened template/fields only as metadata fallbacks.
        min_spine_width = option.get(
            "min_spine_width",
            template.get("min_spine_width", fields.get("min_spine_width")),
        )
        max_spine_width = option.get(
            "max_spine_width",
            template.get("max_spine_width", fields.get("max_spine_width")),
        )
        page_count_options = (
            template.get("page_count_options")
            or template.get("page_count_arr")
            or fields.get("page_count_options")
            or fields.get("page_count_arr")
            or fields.get("page_count_options_json")
            or []
        )
        return {
            "template_id": template.get("id"),
            "shop": template.get("shop"),
            "shop_name": template.get("shop_name"),
            "product_id": template.get("product_id"),
            "size_unit": option.get("size_unit") or template.get("size_unit"),
            "selected_size": selected_size,
            "single_side_width": option.get("single_side_width", template.get("single_side_width")),
            "single_side_height": option.get("single_side_height", template.get("single_side_height")),
            "bleed": option.get("bleed", template.get("bleed")),
            "spine_width": option.get("spine_width", template.get("spine_width")),
            "spine_bleed": option.get("spine_bleed", template.get("spine_bleed")),
            "background_color": template.get("background_color") or "#ffffff",
            "page_count": template.get("resolved_page_count"),
            "spine_width_resolution": deepcopy(template.get("spine_width_resolution") or {}),
            "min_spine_width": min_spine_width,
            "max_spine_width": max_spine_width,
            "page_count_options": deepcopy(page_count_options),
        }

    @staticmethod
    def _template_resolution_layers(resolved_template: dict[str, Any]) -> dict[str, Any]:
        layout = resolved_template.get("_selected_font_layout") or {}
        # Store the selected font-layout document itself. It is the canonical
        # layer JSON consumed by preview, production-sheet and exporters.
        return deepcopy(layout) if isinstance(layout, dict) else {}

    @staticmethod
    def _attach_template_resolution(
        saved_order: dict[str, Any] | None,
        resolved_template: dict[str, Any],
        layers: dict[str, Any],
    ) -> None:
        if not isinstance(saved_order, dict):
            return
        saved_order["matched_template"] = TemplateImageGenerator._template_snapshot_json(
            resolved_template
        )
        saved_order["resolved_layers"] = deepcopy(layers)

    def generate_for_order(
        self,
        order: dict[str, Any],
        saved_order: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
        template_resolved: bool = False,
        reuse_snapshot: bool = True,
        upload_to_oss: bool = True,
    ):
        order_number = str(
            order.get("订单号")
            or (saved_order or {}).get("order_number")
            or "order"
        )
        print(f"[图片] 开始生成：订单号={order_number}", flush=True)
        try:
            product_name = str(
                order.get("产品")
                or (saved_order or {}).get("product")
                or ""
            ).strip()
            shop = str(
                order.get("店铺")
                or (saved_order or {}).get("shop")
                or ""
            ).strip()
            template_was_provided = template is not None
            template = template or self.repository.find_render_template(
                product_name=product_name,
                shop_id=(saved_order or {}).get("shop_id"),
                shop=shop,
                template_id=(saved_order or {}).get("size_template_id")
                or self.template_id,
            )
            if template is None:
                raise RuntimeError(
                    "新尺寸模板表中未找到匹配模板："
                    f"店铺={shop or '空'}，商品={product_name or '空'}"
                )
            render_order = self._order_context(order, saved_order)
            resolution_layers = None
            if (
                not template_resolved
                and not template_was_provided
                and reuse_snapshot
            ):
                snapshot = self.template_resolver.restore_order_snapshot(saved_order)
                if snapshot is not None:
                    template = snapshot
                    template_resolved = True
                    resolution_layers = self._template_resolution_layers(template)
                    print(
                        f"[模板解析] 复用订单快照：订单号={order_number}",
                        flush=True,
                    )
            if not template_resolved:
                template = self._resolve_order_template(template, render_order)
                selected_layout = template.get("_selected_font_layout")
                if isinstance(selected_layout, dict):
                    template["_selected_font_layout"] = self._adapt_fabric_layout_to_spine_width(
                        template,
                        selected_layout,
                    )
                resolution_layers = self._template_resolution_layers(template)
            # Attach transiently for rendering, but persist only after the
            # image has rendered successfully. Invalid/legacy layers must not
            # be written back to the order snapshot.
            self._attach_template_resolution(saved_order, template, resolution_layers or {})
            if self.dpi <= 0:
                raise RuntimeError(f"图片 DPI 必须大于 0，当前值：{self.dpi}")

            dimensions = self._template_dimensions(template)
            print(
                "[图片] 已读取模板："
                f"ID={template['id']}，尺寸="
                f"{dimensions['total_width']}x{dimensions['total_height']} "
                f"{template['size_unit']}，DPI={self.dpi}",
                flush=True,
            )
            file_order = {**(order or {}), **(saved_order or {})}
            output_dir = order_resource_dir(self.output_dir, file_order)
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / order_resource_filename(
                file_order,
                "jpg",
                artifact="预览图",
            )

            print(f"[图片] 正在渲染：订单号={order_number}", flush=True)
            image, dimensions = self._render_jpeg(
                template,
                render_order,
                metadata or {},
            )
            if not template_resolved:
                resolution_layers = self._persist_template_resolution(template, saved_order)
                self._attach_template_resolution(saved_order, template, resolution_layers)
            print(f"[图片] 正在写入：{path}", flush=True)
            image.save(
                path,
                format="JPEG",
                quality=95,
                subsampling=0,
                dpi=(self.dpi, self.dpi),
                optimize=True,
            )
            native_svg = str(template.get("_fabric_render_svg") or "")
            svg_path = None
            if native_svg:
                svg_path = output_dir / order_resource_filename(file_order, "svg")
                svg_path.write_text(native_svg, encoding="utf-8")
        except Exception as exc:
            print(
                f"[图片] 生成失败：订单号={order_number}，"
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            raise

        result = {
            "ok": True,
            "format": "jpg",
            "path": str(path),
            "template_id": template["id"],
            "dpi": self.dpi,
            "pixel_width": image.width,
            "pixel_height": image.height,
            "width": dimensions["total_width"],
            "height": dimensions["total_height"],
            "unit": template["size_unit"],
            "size_option": template.get("size_spec", {}).get("selected"),
            "font_layout_id": (
                template.get("_selected_font_layout") or {}
            ).get("id"),
            "font_layout_name": (
                template.get("_selected_font_layout") or {}
            ).get("name"),
        }
        if self.storage_service is not None and upload_to_oss:
            result["oss"] = self.storage_service.upload_file(path)
        if svg_path is not None:
            result["svg_path"] = str(svg_path)
        print(
            f"[图片] 生成成功：订单号={order_number}，路径={path}，"
            f"像素={image.width}x{image.height}",
            flush=True,
        )
        return result

    def render_for_order(
        self,
        order: dict[str, Any],
        saved_order: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
        template_resolved: bool = False,
    ):
        """Render the order template in memory without writing a preview file."""
        product_name = str(
            order.get("产品") or (saved_order or {}).get("product") or ""
        ).strip()
        shop = str(
            order.get("店铺") or (saved_order or {}).get("shop") or ""
        ).strip()
        template_was_provided = template is not None
        template = template or self.repository.find_render_template(
            product_name=product_name,
            shop_id=(saved_order or {}).get("shop_id"),
            shop=shop,
            template_id=(saved_order or {}).get("size_template_id")
                or self.template_id,
        )
        if template is None:
            raise RuntimeError(
                "未找到匹配的商品尺寸模板："
                f"店铺={shop or '空'}，商品={product_name or '空'}"
            )
        render_order = self._order_context(order, saved_order)
        resolution_layers = None
        if (
            not template_resolved
            and not template_was_provided
        ):
            snapshot = self.template_resolver.restore_order_snapshot(saved_order)
            if snapshot is not None:
                template = snapshot
                template_resolved = True
                resolution_layers = self._template_resolution_layers(template)
        if not template_resolved:
            template = self._resolve_order_template(template, render_order)
            selected_layout = template.get("_selected_font_layout")
            if isinstance(selected_layout, dict):
                template["_selected_font_layout"] = self._adapt_fabric_layout_to_spine_width(
                    template,
                    selected_layout,
                )
            resolution_layers = self._template_resolution_layers(template)
        self._attach_template_resolution(saved_order, template, resolution_layers or {})
        if self.dpi <= 0:
            raise RuntimeError(f"图片 DPI 必须大于 0，当前值：{self.dpi}")
        image, dimensions = self._render_jpeg(
            template,
            render_order,
            metadata or {},
        )
        if not template_resolved:
            resolution_layers = self._persist_template_resolution(template, saved_order)
            self._attach_template_resolution(saved_order, template, resolution_layers)
        return {
            "image": image,
            "template": template,
            "template_id": template["id"],
            "dpi": self.dpi,
            "pixel_width": image.width,
            "pixel_height": image.height,
            "width": dimensions["total_width"],
            "height": dimensions["total_height"],
            "unit": template["size_unit"],
            "size_option": template.get("size_spec", {}).get("selected"),
            "font_layout_id": (
                template.get("_selected_font_layout") or {}
            ).get("id"),
            "font_layout_name": (
                template.get("_selected_font_layout") or {}
            ).get("name"),
        }

    @staticmethod
    def _order_context(order: dict[str, Any], saved_order: dict[str, Any] | None):
        context = {**(order or {}), **(saved_order or {})}
        information = (
            context.get("product_information")
            or context.get("商品信息")
            or context.get("定制信息")
            or {}
        )
        context["product_information"] = (
            information if isinstance(information, dict) else {}
        )
        context["商品信息"] = context["product_information"]
        return context

    def _resolve_order_template(
        self,
        template: dict[str, Any],
        order: dict[str, Any],
    ) -> dict[str, Any]:
        return self.template_resolver.resolve(template, order)

    @staticmethod
    def _font_layout_for_size_option(
        layout: dict[str, Any],
        size_option: Any,
    ) -> dict[str, Any]:
        return OrderTemplateResolver.font_layout_for_size_option(layout, size_option)

    @staticmethod
    def _natural_rule_element_ids(layout: dict[str, Any]) -> list[str]:
        return OrderTemplateResolver.natural_rule_element_ids(layout)

    @classmethod
    def _layout_requires_deepseek(cls, layout: dict[str, Any] | None, size_option: Any) -> bool:
        return OrderTemplateResolver.layout_requires_deepseek(layout, size_option)

    @classmethod
    def _match_size_option(cls, template: dict[str, Any], information: dict[str, Any]):
        return OrderTemplateResolver.match_size_option(template, information)

    @classmethod
    def _enrich_size_option(cls, template: dict[str, Any], option: dict[str, Any]):
        from backend.templates.size_variants import merge_size_option

        enriched = deepcopy(option)
        option_id = str(enriched.get("id") or enriched.get("value") or "")
        raw_options = template.get("size_options") or []
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("id") or raw.get("value") or "")
            if raw_id != option_id:
                continue
            enriched = merge_size_option(enriched, raw)
            break
        return enriched

    @staticmethod
    def _size_option_has_values(option: dict[str, Any]) -> bool:
        try:
            return all(float(option.get(key)) > 0 for key in ("single_side_width", "single_side_height"))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _size_information_text(information: dict[str, Any]) -> str:
        selected = []
        for key, value in information.items():
            label = str(key or "").casefold()
            if any(token in label for token in ("size", "page", "尺寸", "页数", "大小")):
                selected.append(str(value or ""))
        return " ".join(selected)

    @staticmethod
    def _normalize_size_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold().replace("×", "x"))

    @staticmethod
    def _size_pairs(value: Any) -> set[tuple[float, float]]:
        pairs = set()
        for first, second in re.findall(
            r"(\d+(?:\.\d+)?)\s*(?:x|×|\*)\s*(\d+(?:\.\d+)?)",
            str(value or "").casefold(),
        ):
            left, right = float(first), float(second)
            pairs.add((left, right))
            pairs.add((right, left))
        return pairs

    @classmethod
    def _match_font_layout(cls, layouts: Any, information: dict[str, Any]):
        return OrderTemplateResolver.match_font_layout(layouts, information)

    @staticmethod
    def _layout_name(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().casefold())

    @staticmethod
    def _filename(
        order_number: str,
        order_id: Any = None,
        extension: str = "jpg",
        created_at: Any = None,
    ):
        return order_filename(
            {
                "order_number": order_number,
                "created_at": created_at,
            },
            extension,
            created_at=created_at,
        )

    @staticmethod
    def _number(template: dict[str, Any], key: str):
        value = template.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"模板字段 {key} 不是有效数字：{value!r}") from exc
        if number < 0:
            raise RuntimeError(f"模板字段 {key} 不能小于 0：{value!r}")
        return number

    def _template_dimensions(self, template: dict[str, Any]):
        template = apply_selected_size_variant(template)
        side_width = self._number(template, "single_side_width")
        side_height = self._number(template, "single_side_height")
        bleed = self._number(template, "bleed")
        spine_width = resolve_spine_width(template)
        spine_bleed = self._number(template, "spine_bleed")
        total_width = (
            side_width * 2
            + spine_width
            + spine_bleed * 2
            + bleed * 2
        )
        total_height = side_height + bleed * 2
        if total_width <= 0 or total_height <= 0:
            raise RuntimeError("模板的总宽度和总高度必须大于 0")
        return {
            "side_width": side_width,
            "side_height": side_height,
            "bleed": bleed,
            "spine_width": spine_width,
            "spine_bleed": spine_bleed,
            "total_width": total_width,
            "total_height": total_height,
        }

    def _pixels_per_unit(self, template: dict[str, Any]):
        unit = str(template.get("size_unit") or "").strip().lower()
        if not unit:
            raise RuntimeError("命中的尺寸规格缺少 size_unit，拒绝使用默认单位")
        inches = UNIT_TO_INCHES.get(unit)
        if inches is None:
            raise RuntimeError(f"模板单位不受支持：{unit!r}")
        return self.dpi * inches

    @staticmethod
    def _font_candidates(font_data: dict[str, Any] | None = None):
        windows_fonts = Path("C:/Windows/Fonts")
        candidates = []
        if isinstance(font_data, dict):
            for key in ("file_path", "path", "font_path"):
                value = str(font_data.get(key) or "").strip()
                if value:
                    candidates.append(Path(value))
            for key in ("font_family", "family", "font_name", "postscript_name"):
                value = str(font_data.get(key) or "").strip()
                if value:
                    candidates.extend(
                        windows_fonts / f"{value}{extension}"
                        for extension in (".ttf", ".otf", ".ttc")
                    )
        candidates.extend(
            (
            windows_fonts / "simsun.ttc",
            windows_fonts / "msyh.ttc",
            windows_fonts / "arial.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            )
        )
        return tuple(candidates)

    @classmethod
    def _font(cls, size: float, font_data: dict[str, Any] | None = None):
        pixel_size = max(10, round(size))
        # A template-supplied remote font is authoritative.  Do this before
        # generic system candidates such as Arial; otherwise the fallback is
        # selected and the URL branch is never reached.
        font_url = str((font_data or {}).get("font_url") or "").strip()
        if font_url.startswith(("http://", "https://")):
            try:
                cls._font_cache_dir.mkdir(parents=True, exist_ok=True)
                name = Path(unquote(urlparse(font_url).path)).name or "remote-font.ttf"
                cached = cls._font_cache_dir / name
                if not cached.is_file():
                    response = requests.get(font_url, timeout=20)
                    response.raise_for_status()
                    cached.write_bytes(response.content)
                return ImageFont.truetype(str(cached), pixel_size)
            except (OSError, ValueError, requests.RequestException):
                pass
        for candidate in cls._font_candidates(font_data):
            if candidate.exists():
                try:
                    return ImageFont.truetype(str(candidate), pixel_size)
                except OSError:
                    continue
        return ImageFont.load_default(size=pixel_size)

    @staticmethod
    def _draw_centered_text(draw, position, text, font, fill):
        draw.text(position, text, font=font, fill=fill, anchor="mm")

    @classmethod
    def _draw_rotated_text(cls, image, position, text, font, fill):
        bbox = font.getbbox(text)
        width = max(1, bbox[2] - bbox[0] + 12)
        height = max(1, bbox[3] - bbox[1] + 12)
        text_layer = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        text_draw = ImageDraw.Draw(text_layer)
        text_draw.text(
            (width / 2, height / 2),
            text,
            font=font,
            fill=fill,
            anchor="mm",
        )
        rotated = text_layer.rotate(
            -90,
            expand=True,
            resample=Image.Resampling.BICUBIC,
        )
        x = round(position[0] - rotated.width / 2)
        y = round(position[1] - rotated.height / 2)
        image.paste(rotated, (x, y), rotated)

    def _layout_regions(self, template: dict[str, Any], canvas_w: int, canvas_h: int):
        """Return target regions using the editor's panel geometry.

        The frontend treats the spine bleed adjacent to each side as part of
        that side's panel.  Keeping that convention here is important for
        mapping Fabric objects: the cover centre in the editor must land on
        the cover centre in the exported image.
        """
        dimensions = self._template_dimensions(template)
        scale = self._pixels_per_unit(template)
        bleed = dimensions["bleed"] * scale
        side_w = dimensions["side_width"] * scale
        side_h = dimensions["side_height"] * scale
        spine_w = dimensions["spine_width"] * scale
        spine_bleed = dimensions["spine_bleed"] * scale
        panel_w = side_w + spine_bleed
        back_x = bleed
        spine_x = back_x + panel_w
        cover_x = spine_x + spine_w
        return {
            "canvas": {"x": 0.0, "y": 0.0, "width": float(canvas_w), "height": float(canvas_h)},
            "back": {"x": back_x, "y": bleed, "width": panel_w, "height": side_h},
            "spine": {"x": spine_x, "y": bleed, "width": spine_w, "height": side_h},
            "cover": {"x": cover_x, "y": bleed, "width": panel_w, "height": side_h},
        }

    def _reference_layout_regions(
        self,
        template: dict[str, Any],
        layout: dict[str, Any],
        reference_dimensions: tuple[float, float] | None = None,
    ):
        """Build the base-size regions in the layout reference-canvas units."""
        options = layout.get("options") or {}
        reference_canvas = options.get("reference_canvas") or {}
        try:
            if reference_dimensions:
                reference_w, reference_h = map(float, reference_dimensions)
            else:
                reference_w = float(reference_canvas.get("width") or reference_canvas.get("width_px"))
                reference_h = float(reference_canvas.get("height") or reference_canvas.get("height_px"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "字体布局缺少有效 reference_canvas.width/height，拒绝使用默认画布"
            ) from exc
        if reference_w <= 0 or reference_h <= 0:
            raise RuntimeError("字体布局 reference_canvas.width/height 必须大于 0")
        spec = template.get("size_spec") or (template.get("fields") or {}).get("size_spec") or {}
        reference_id = options.get("reference_size_option")
        option = next(
            (item for item in (spec.get("options") or [])
             if isinstance(item, dict) and str(item.get("id")) == str(reference_id)),
            None,
        )
        fields = option or template.get("fields") or template
        try:
            side_w = float(fields.get("single_side_width"))
            side_h = float(fields.get("single_side_height"))
            spine_w = float(fields.get("spine_width"))
            bleed = float(fields.get("bleed"))
            # The editor's two side panels include the adjacent spine bleed.
            # It is therefore part of the horizontal reference canvas too.
            spine_bleed = float(fields.get("spine_bleed", 0) or 0)
            unit_x = reference_w / max(side_w * 2 + spine_w + spine_bleed * 2 + bleed * 2, 1e-9)
            unit_y = reference_h / max(side_h + bleed * 2, 1e-9)
        except (TypeError, ValueError):
            return {"canvas": {"x": 0.0, "y": 0.0, "width": reference_w, "height": reference_h}}
        bleed_x = bleed * unit_x
        bleed_y = bleed * unit_y
        panel_px = (side_w + float(fields.get("spine_bleed", 0) or 0)) * unit_x
        spine_px = spine_w * unit_x
        return {
            "canvas": {"x": 0.0, "y": 0.0, "width": reference_w, "height": reference_h},
            "back": {"x": bleed_x, "y": bleed_y, "width": panel_px, "height": side_h * unit_y},
            "spine": {"x": bleed_x + panel_px, "y": bleed_y, "width": spine_px, "height": side_h * unit_y},
            "cover": {"x": bleed_x + panel_px + spine_px, "y": bleed_y, "width": panel_px, "height": side_h * unit_y},
        }

    @staticmethod
    def _physical_region_size(template: dict[str, Any], region_name: str, option_id: Any = None):
        spec = template.get("size_spec") or (template.get("fields") or {}).get("size_spec") or {}
        option = next(
            (item for item in (spec.get("options") or [])
             if isinstance(item, dict) and str(item.get("id")) == str(option_id)),
            None,
        ) if option_id is not None else None
        fields = option or template.get("fields") or template
        try:
            side_w = float(fields.get("single_side_width"))
            side_h = float(fields.get("single_side_height"))
            spine_w = float(fields.get("spine_width"))
        except (TypeError, ValueError):
            return {"width": 1.0, "height": 1.0}
        if region_name == "spine":
            return {"width": spine_w, "height": side_h}
        if region_name in {"back", "cover"}:
            return {"width": side_w, "height": side_h}
        return {"width": side_w * 2 + spine_w, "height": side_h}

    @staticmethod
    def _anchor_factor(value: Any, axis: str) -> float:
        value = str(value or "center").strip().lower()
        if axis == "x":
            return {"left": 0.0, "start": 0.0, "right": 1.0, "end": 1.0}.get(value, 0.5)
        return {"top": 0.0, "bottom": 1.0}.get(value, 0.5)

    def _draw_font_layout(
        self,
        image: Image.Image,
        template: dict[str, Any],
        order: dict[str, Any],
        layout: dict[str, Any],
        canvas_w: int,
        canvas_h: int,
    ) -> bool:
        """Draw elements using the Fabric frame/center-rotation contract."""
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        elements = layout.get("objects")
        if not isinstance(elements, list):
            elements = layers.get("objects")
        if not isinstance(elements, list):
            elements = layout.get("elements")
        if not isinstance(elements, list):
            return False
        options = layout.get("options") or {}
        reference_canvas = layout.get("canvas") or layers.get("canvas") or options.get("reference_canvas") or {}
        fabric_objects = any(
            isinstance(item, dict)
            and str(item.get("type") or "").lower() in {"text", "textbox", "i-text", "itext", "rect", "rectangle", "image"}
            and "frame" not in item
            for item in elements
        )
        try:
            reference_w = float(reference_canvas.get("width") or reference_canvas.get("width_px"))
            reference_h = float(reference_canvas.get("height") or reference_canvas.get("height_px"))
        except (TypeError, ValueError):
            # image-map-editor's fixed workarea is expressed in CSS pixels at
            # 96 px/in. When a size variant omits canvas metadata, derive the
            # reference from the matched physical size. Never use a fixed
            # legacy canvas or infer it from object bounds.
            if fabric_objects:
                dimensions = self._template_dimensions(template)
                reference_w = dimensions["total_width"] * self._pixels_per_unit(template) * 96 / max(self.dpi, 1)
                reference_h = dimensions["total_height"] * self._pixels_per_unit(template) * 96 / max(self.dpi, 1)
            else:
                reference_w, reference_h = 0.0, 0.0
        if reference_w <= 0 or reference_h <= 0:
            print(
                "[预览图] 字体布局缺少有效 canvas.width/height，"
                "拒绝按旧 Fabric 坐标或元素范围推导画布",
                flush=True,
            )
            return False

        rule_context = {
            **(template.get("fields") or {}),
            **template,
            **order,
            "product_information": order.get("product_information") or {},
        }
        offset_x, offset_y, layout_scale = self._layout_output_transform(
            template,
            reference_w,
            reference_h,
            canvas_w,
            canvas_h,
        )
        # Fabric's editor uses a horizontally fitted guide but preserves the
        # CSS/text scale from the vertical reference.  Build source region
        # anchors from the guide's horizontal unit, then use the vertical
        # unit for the actual object distance/size.  This is the same
        # non-stretched geometry used by the frontend's physical guide.
        target_regions = self._layout_regions(template, canvas_w, canvas_h)
        source_regions = self._reference_layout_regions(
            template,
            layout,
            (reference_w, reference_h) if fabric_objects else None,
        )
        source_fields = template.get("size_spec") or (template.get("fields") or {}).get("size_spec") or {}
        source_option_id = source_fields.get("selected")
        source_option = next(
            (item for item in (source_fields.get("options") or [])
             if isinstance(item, dict) and str(item.get("id")) == str(source_option_id)),
            None,
        )
        source_values = source_option or template.get("fields") or template
        try:
            source_height_unit = reference_h / max(float(source_values["single_side_height"]) + float(source_values["bleed"]) * 2, 1e-9)
        except (KeyError, TypeError, ValueError):
            source_height_unit = reference_h / max(float(reference_h), 1.0)
        target_height_unit = canvas_h / max(self._template_dimensions(template)["total_height"], 1e-9)
        fabric_x_scale = target_height_unit / max(source_height_unit, 1e-9)
        fabric_y_scale = fabric_x_scale

        def fabric_region(center_x: float, element_type: str):
            # Region selection follows the horizontal guide coordinates, not
            # the virtual width used for text sizing.
            if element_type in {"textbox", "i-text", "itext", "text"} and center_x < reference_w * 0.48:
                return "spine"
            spine_left = float(source_regions["spine"]["x"])
            spine_right = spine_left + float(source_regions["spine"]["width"])
            if center_x < spine_left:
                return "back"
            if center_x <= spine_right:
                return "spine"
            return "cover"

        def fabric_center(center_x: float, center_y: float, element_type: str):
            region_name = fabric_region(center_x, element_type)
            if region_name == "spine" and element_type in {"textbox", "i-text", "itext", "text"}:
                # Spine text is authored in the local spine panel in the
                # template editor; its physical anchor is the real spine
                # centre after the spread is assembled.
                target = target_regions["spine"]
                return (
                    float(target["x"]) + float(target["width"]) / 2,
                    center_y * fabric_y_scale - 48.0,
                    region_name,
                )
            source = source_regions[region_name]
            target = target_regions[region_name]
            source_center_x = float(source["x"]) + float(source["width"]) / 2
            target_center_x = float(target["x"]) + float(target["width"]) / 2
            return (
                target_center_x + (center_x - source_center_x) * fabric_x_scale,
                center_y * fabric_y_scale - 48.0,
                region_name,
            )
        drawn = False
        for element in sorted(
            (item for item in elements if isinstance(item, dict)),
            key=lambda item: item.get("z_index", 0),
        ):
            if fabric_objects and "frame" not in element:
                try:
                    geometry = fabric_layer_geometry(element)
                except (TypeError, ValueError):
                    continue
                element_type = str(element.get("type") or "").lower()
                center_x = geometry["center_x"]
                center_y = geometry["center_y"]
                scale_x = geometry["scale_x"]
                scale_y = geometry["scale_y"]
                center_x, center_y, _ = fabric_center(center_x, center_y, element_type)
                center = (center_x, center_y)
                width = max(1.0, geometry["width"] * fabric_x_scale)
                height = max(1.0, geometry["height"] * fabric_y_scale)
            else:
                frame = self._final_layout_frame(element, reference_w, reference_h)
                if frame is None:
                    print(
                        f"[预览图] 跳过无效图层：id={element.get('id') or element.get('name') or ''}",
                        flush=True,
                    )
                    continue
                x = (float(frame["x"]) - offset_x) * layout_scale
                y = (float(frame["y"]) - offset_y) * layout_scale
                width = max(1.0, float(frame["width"]) * layout_scale)
                height = max(1.0, float(frame["height"]) * layout_scale)
                center = (x + width / 2, y + height / 2)
            transform = element.get("transform") or {}
            if fabric_objects and "frame" not in element:
                angle = geometry["angle"]
                flip_x = geometry["flip_x"]
                flip_y = geometry["flip_y"]
            else:
                try:
                    angle = float(
                        transform.get(
                            "rotation_deg",
                            transform.get("rotation", element.get("angle", 0)),
                        )
                        or 0
                    )
                except (TypeError, ValueError):
                    angle = 0.0
                flip_x = bool(
                    transform.get("flip_horizontal", transform.get("flipX", element.get("flipX", False)))
                )
                flip_y = bool(
                    transform.get("flip_vertical", transform.get("flipY", element.get("flipY", False)))
                )
            element_type = str(element.get("type") or "text").lower()
            appearance = element.get("appearance") or {}
            asset = element.get("asset") or {}
            shape = str(
                appearance.get("shape")
                or asset.get("kind")
                or element.get("shape")
                or ""
            ).lower()

            if element_type in {"text", "textbox", "i-text", "itext"}:
                text = self._font_layout_text(element, rule_context)
                if not text:
                    continue
                style = element.get("style") or {}
                writing_mode = str(style.get("writing_mode") or "").lower()
                if style.get("vertical") or writing_mode == "vertical":
                    text = "\n".join(text.replace("\r", "").replace("\n", ""))
                font_data = element.get("font") or {
                    "size_px": element.get("fontSize"),
                    "font_family": element.get("fontFamily"),
                    "font_weight": element.get("fontWeight"),
                    "font_style": element.get("fontStyle"),
                    "font_url": element.get("fontUrl"),
                }
                try:
                    base_size = float(font_data.get("size_px") or element.get("fontSize"))
                except (TypeError, ValueError):
                    base_size = max(12.0, height * 0.7)
                if fabric_objects and "frame" not in element:
                    # Fabric applies scaleY to the text object's authored CSS
                    # font size before mapping it to the output canvas.
                    font_size = base_size * scale_y * fabric_y_scale
                else:
                    font_size = base_size * layout_scale
                font = self._font(max(10.0, font_size), font_data)
                fill = self._font_layout_color(
                    style.get("fill_color") or element.get("fill"), "#172033"
                )
                tracking = font_data.get(
                    "tracking",
                    font_data.get(
                        "char_spacing",
                        element.get("charSpacing", style.get("char_spacing", style.get("letter_spacing", 0))),
                    ),
                )
                leading = font_data.get("leading_px", style.get("leading_px"))
                try:
                    leading = float(leading) * (fabric_y_scale if fabric_objects and "frame" not in element else layout_scale) if leading is not None else None
                except (TypeError, ValueError):
                    pass
                word_spacing = font_data.get(
                    "word_spacing",
                    font_data.get("wordSpacing", element.get("wordSpacing", style.get("word_spacing", 0))),
                )
                try:
                    if fabric_objects and "frame" not in element:
                        # The current Fabric template stores wordSpacing in
                        # the same 1/1000-em unit as charSpacing. Convert it
                        # after the effective output font size is known;
                        # treating -180 as -180 px makes rotated spine words
                        # overlap.
                        word_spacing = self._fabric_word_spacing(
                            word_spacing,
                            font_size,
                        )
                    else:
                        word_spacing = float(word_spacing)
                        word_spacing *= layout_scale
                except (TypeError, ValueError):
                    pass
                text_render = self._draw_fitted_layout_text(
                    image,
                    text,
                    center,
                    width,
                    height,
                    font,
                    fill,
                    str(style.get("alignment") or element.get("textAlign") or "center").lower(),
                    angle,
                    anchor_x_factor=0.5,
                    anchor_y_factor=0.5,
                    bounds=None,
                    flip_horizontal=flip_x,
                    flip_vertical=flip_y,
                    tracking=tracking,
                    leading=leading,
                    word_spacing=word_spacing,
                    vertical_tracking=(
                        self._tracking_pixels(tracking, font)
                        if style.get("vertical") or writing_mode == "vertical"
                        else 0
                    ),
                )
                drawn = True
                continue

            if shape != "rectangle" and element_type not in {"rectangle", "shape", "rect"}:
                source = asset.get("data_url") or asset.get("dataUrl") or asset.get("path") or asset.get("url")
                source_image = self._load_layout_image(source)
                if source_image is None:
                    continue
                layer = source_image.convert("RGBA").resize(
                    (max(1, round(width)), max(1, round(height))),
                    Image.Resampling.LANCZOS,
                )
                layer = self._transform_layout_layer(layer, angle, flip_x, flip_y)
                image.paste(
                    layer,
                    (round(center[0] - layer.width / 2), round(center[1] - layer.height / 2)),
                    layer,
                )
                drawn = True
                continue
            color = self._font_layout_color(
                appearance.get("fill_color") or element.get("fill"), "#000000"
            )
            try:
                opacity = max(0, min(255, round(float(appearance.get("opacity", 100) or 0) * 2.55)))
            except (TypeError, ValueError):
                opacity = 255
            layer = Image.new("RGBA", (max(1, round(width)), max(1, round(height))), (0, 0, 0, 0))
            layer_draw = ImageDraw.Draw(layer)
            layer_draw.rectangle(
                (0, 0, layer.width - 1, layer.height - 1),
                fill=(*ImageColor.getrgb(color), opacity),
            )
            layer = self._transform_layout_layer(layer, angle, flip_x, flip_y)
            image.paste(
                layer,
                (round(center[0] - layer.width / 2), round(center[1] - layer.height / 2)),
                layer,
            )
            drawn = True
        return drawn

    @staticmethod
    def _final_layout_frame(element: dict[str, Any], reference_w: float, reference_h: float):
        frame = element.get("frame") or {}
        base = frame.get("reference_px") or frame.get("reference") or frame
        override = frame.get("edited_px") or frame.get("current_px") or frame.get("preview_px") or {}
        if not isinstance(base, dict) or not isinstance(override, dict):
            return None
        final = {**base, **override}
        try:
            values = {key: float(final[key]) for key in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            relative = frame.get("relative") or {}
            try:
                values = {
                    "x": float(relative["x"]) * reference_w,
                    "y": float(relative["y"]) * reference_h,
                    "width": float(relative["width"]) * reference_w,
                    "height": float(relative["height"]) * reference_h,
                }
            except (KeyError, TypeError, ValueError):
                return None
        if values["width"] <= 0 or values["height"] <= 0:
            return None
        return values

    def _layout_output_transform(
        self,
        template: dict[str, Any],
        reference_w: float,
        reference_h: float,
        canvas_w: int,
        canvas_h: int,
    ):
        """Map the centered Fabric guide canvas onto the production canvas."""
        dimensions = self._template_dimensions(template)
        trim_width = (
            dimensions["side_width"] * 2
            + dimensions["spine_width"]
            + dimensions["spine_bleed"] * 2
        )
        trim_height = dimensions["side_height"]
        reference_scale = min(
            reference_w / max(dimensions["total_width"], 1e-9),
            reference_h / max(dimensions["total_height"], 1e-9),
        )
        trim_x = (reference_w - trim_width * reference_scale) / 2
        trim_y = (reference_h - trim_height * reference_scale) / 2
        outer_x = trim_x - dimensions["bleed"] * reference_scale
        outer_y = trim_y - dimensions["bleed"] * reference_scale
        output_scale = min(
            canvas_w / max(dimensions["total_width"], 1e-9),
            canvas_h / max(dimensions["total_height"], 1e-9),
        )
        return outer_x, outer_y, output_scale / max(reference_scale, 1e-9)

    @staticmethod
    def _transform_layout_layer(layer: Image.Image, angle: float, flip_x: bool, flip_y: bool):
        if flip_x:
            layer = ImageOps.mirror(layer)
        if flip_y:
            layer = ImageOps.flip(layer)
        if angle:
            # Fabric's positive angle is clockwise; Pillow's positive angle
            # is counter-clockwise, hence the negative sign.
            layer = layer.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)
        return layer

    @staticmethod
    def _load_layout_image(source: Any):
        value = str(source or "").strip()
        if not value:
            return None
        try:
            if value.startswith("data:") and "," in value:
                encoded = value.split(",", 1)[1]
                return Image.open(BytesIO(base64.b64decode(encoded)))
            path = Path(value)
            if path.exists():
                return Image.open(path)
            if value.startswith(("http://", "https://")):
                response = requests.get(value, timeout=20)
                response.raise_for_status()
                return Image.open(BytesIO(response.content))
        except (OSError, ValueError, TypeError, binascii.Error, requests.RequestException):
            return None
        return None

    def _font_layout_text(cls, element: dict[str, Any], order: dict[str, Any]) -> str:
        deepseek_values = order.get("_deepseek_text_values") or {}
        element_id = str(element.get("id") or element.get("name") or "")
        if element_id in deepseek_values:
            return str(deepseek_values[element_id] or "").strip()
        content = element.get("content") or {}
        rule = element.get("rule") or content.get("rule") or element.get("rules") or {}
        if (isinstance(rule, dict) and str(rule.get("description") or "").strip()) or (
            isinstance(rule, str) and rule.strip()
        ):
            # The resolver has already materialized this object in the
            # current Fabric document.  Use that stored value during drawing;
            # unresolved documents are rejected before rendering.
            return str(element.get("text") or "").strip()
        if isinstance(rule, dict) and rule:
            value = cls._evaluate_text_rule(rule, order)
            if value:
                return value
        binding_key = content.get("binding_key") or element.get("binding_key")
        if binding_key:
            value = cls._resolve_order_field(binding_key, order)
            if value:
                return value
        literal = content.get("literal")
        return str(literal if literal not in (None, "") else element.get("text") or "").strip()

    @classmethod
    def _evaluate_text_rule(cls, rule: dict[str, Any], order: dict[str, Any]) -> str:
        if not isinstance(rule, dict):
            description = str(rule or "")
            # Fabric layouts keep the natural-language rule in `rules` as
            # plain text. DeepSeek supplies the materialized value; this
            # branch only handles an explicit field name for local fallback.
            match = re.search(r"(?:订单信息|order information)\s*([^字段]+?)字段", description, re.I)
            if match:
                return cls._resolve_order_field(match.group(1), order)
            return ""
        source_fields = rule.get("source_fields") or rule.get("fields") or []
        if isinstance(source_fields, str):
            source_fields = [source_fields]
        values = [
            value
            for field in source_fields
            if (value := cls._resolve_order_field(field, order))
        ]
        if not values:
            description = str(rule.get("description") or "")
            quoted = re.findall(r"[\"']([^\"']+)[\"']", description)
            for field in quoted:
                value = cls._resolve_order_field(field, order)
                if value:
                    values.append(value)
            if not values:
                return str(rule.get("literal") or "").strip()
        kind = str(rule.get("kind") or rule.get("type") or "value").lower()
        parameters = rule.get("parameters") or {}
        separator = str(parameters.get("separator") or rule.get("separator") or " ")
        if kind in {"initial", "initials"}:
            words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", " ".join(values))
            chunks = [word[0] for word in words if word]
            return separator.join(chunks)
        if kind in {"join", "concat", "combine"}:
            return separator.join(values)
        return values[0]

    @classmethod
    def _resolve_order_field(cls, field: Any, order: dict[str, Any]) -> str:
        key = str(field or "").strip()
        if not key:
            return ""
        key = re.sub(r"^(?:product_information|商品信息|fields)[.:]", "", key, flags=re.IGNORECASE)
        information = order.get("product_information") or order.get("商品信息") or {}
        normalized = cls._layout_name(key)
        for candidate_key, value in information.items():
            if cls._layout_name(candidate_key) == normalized:
                return str(value or "").strip()
        for candidate_key, value in order.items():
            if cls._layout_name(candidate_key) == normalized:
                return str(value or "").strip()
        return ""

    @staticmethod
    def _font_layout_color(value: Any, fallback: str) -> str:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("#") or text.lower().startswith(("rgb(", "rgba(")):
                return text
            # Legacy PSD colour arrays serialized as comma text are not valid
            # Fabric/CSS colours.  Fabric falls back to the configured text
            # colour, so the backend must do the same.
            return fallback
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            values = list(value)
        else:
            try:
                values = [float(part.strip()) for part in str(value or "").split(",")]
            except (TypeError, ValueError):
                return fallback
        if len(values) >= 4 and float(values[0]) <= 1:
            values = values[1:4]
        if not values or max(float(item) for item in values[:3]) <= 1:
            values = [float(item) * 255 for item in values[:3]]
        return "#%02x%02x%02x" % tuple(max(0, min(255, round(float(item)))) for item in values[:3])

    @staticmethod
    def _draw_fitted_layout_text(
        image: Image.Image,
        text: str,
        center: tuple[float, float],
        box_w: float,
        box_h: float,
        font,
        fill: str,
        alignment: str,
        rotation: float,
        anchor_x_factor: float = 0.5,
        anchor_y_factor: float = 0.5,
        bounds: dict[str, float] | None = None,
        safe_distance: float = 0,
        spine_region: bool = False,
        flip_horizontal: bool = False,
        flip_vertical: bool = False,
        tracking: Any = 0,
        leading: Any = None,
        word_spacing: Any = 0,
        vertical_tracking: float = 0,
    ):
        draw = ImageDraw.Draw(image)
        tracking_px = TemplateImageGenerator._tracking_pixels(tracking, font)
        leading_px = TemplateImageGenerator._leading_pixels(leading, font)
        word_spacing_px = TemplateImageGenerator._word_spacing_pixels(word_spacing)
        if vertical_tracking and leading_px is None:
            leading_px = float(getattr(font, "size", 0) or 0) + vertical_tracking
        text_w, text_h, bbox = TemplateImageGenerator._spaced_text_metrics(
            text, font, tracking_px, leading_px, word_spacing_px
        )
        # The frame is the unrotated local rectangle.  Fit text in that
        # rectangle first; rotation must not swap the dimensions used for
        # font fitting.
        # Fabric's fitTextObjectToFrame only shrinks multi-glyph text.  A
        # large frame is not permission to enlarge the CSS font size.
        text_length = len(str(text or "").replace("\n", "").replace("\r", "").replace(" ", ""))
        factor = min(1.0, box_w / max(text_w, 1), box_h / max(text_h, 1))
        if text_length <= 1:
            factor = 1.0
        if abs(factor - 1.0) > 0.001:
            font = ImageFont.truetype(font.path, max(1, round(font.size * factor))) if getattr(font, "path", None) else font
            tracking_px = TemplateImageGenerator._tracking_pixels(tracking, font)
            leading_px = TemplateImageGenerator._leading_pixels(leading, font)
            text_w, text_h, bbox = TemplateImageGenerator._spaced_text_metrics(
                text, font, tracking_px, leading_px, word_spacing_px
            )
        layer = Image.new("RGBA", (text_w + 12, text_h + 12), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        TemplateImageGenerator._draw_spaced_text(
            layer_draw,
            (6 - bbox[0], 6 - bbox[1]),
            text,
            font,
            fill,
            tracking_px,
            leading_px,
            word_spacing_px,
        )
        layer = TemplateImageGenerator._transform_layout_layer(
            layer,
            rotation,
            flip_horizontal,
            flip_vertical,
        )
        # Fabric text objects rotate around the center of their unrotated
        # frame.  The new frame contract always uses a centered composite.
        x = round(center[0] - layer.width / 2)
        y = round(center[1] - layer.height / 2)
        if bounds:
            left = float(bounds.get("x", 0)) + (0 if spine_region else safe_distance)
            right = float(bounds.get("x", 0)) + float(bounds.get("width", image.width)) - (0 if spine_region else safe_distance)
            top = float(bounds.get("y", 0)) + (0 if spine_region else safe_distance)
            bottom = float(bounds.get("y", 0)) + float(bounds.get("height", image.height)) - (0 if spine_region else safe_distance)
            if spine_region:
                if y < top:
                    y = round(top)
                if y + layer.height > bottom:
                    y = round(bottom - layer.height)
            else:
                if x < left:
                    x = round(left)
                if x + layer.width > right:
                    x = round(right - layer.width)
                if y < top:
                    y = round(top)
                if y + layer.height > bottom:
                    y = round(bottom - layer.height)
        image.paste(layer, (x, y), layer)
        return {
            "font_size": float(getattr(font, "size", 0) or 0),
            "tracking_px": float(tracking_px),
            "leading_px": None if leading_px is None else float(leading_px),
            "word_spacing_px": float(word_spacing_px),
            "width": int(layer.width),
            "height": int(layer.height),
        }

    @staticmethod
    def _tracking_pixels(value: Any, font) -> float:
        """Convert PSD/Fabric tracking (1/1000 em) to pixels.

        Explicit pixel fields are accepted for templates that already store
        physical spacing. Legacy ``tracking`` and Fabric ``charSpacing`` use
        the standard 1/1000-em convention.
        """
        raw = str(value or "").strip().lower()
        explicit_pixels = raw.endswith("px")
        try:
            number = float(raw[:-2] if explicit_pixels else raw)
        except (TypeError, ValueError):
            return 0.0
        return number if explicit_pixels else number * float(getattr(font, "size", 0) or 0) / 1000.0

    @staticmethod
    def _word_spacing_pixels(value: Any) -> float:
        """Fabric's custom wordSpacing extension stores CSS pixels directly."""
        raw = str(value or "").strip().lower()
        if raw.endswith("px"):
            raw = raw[:-2].strip()
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fabric_word_spacing(value: Any, font_size: float) -> float:
        """Convert Fabric wordSpacing to output pixels.

        Fabric layouts use the same 1/1000-em convention as charSpacing for
        numeric word spacing. An explicit ``px`` suffix remains an absolute
        pixel value for templates that intentionally use CSS units.
        """
        raw = str(value or "").strip().lower()
        if raw.endswith("px"):
            try:
                return float(raw[:-2].strip())
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(raw or 0) * float(font_size) / 1000.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _leading_pixels(value: Any, font) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _spaced_text_metrics(
        text: str,
        font,
        tracking_px: float,
        leading_px: float | None,
        word_spacing_px: float = 0,
    ):
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        lines = str(text or "").split("\n") or [""]
        line_metrics = [draw.textbbox((0, 0), line or " ", font=font) for line in lines]
        widths = [
            max(1, box[2] - box[0])
            + max(0, len(line) - 1) * tracking_px
            + line.count(" ") * word_spacing_px
            for line, box in zip(lines, line_metrics)
        ]
        heights = [max(1, box[3] - box[1]) for box in line_metrics]
        line_step = leading_px or (max(heights) if heights else 1)
        return (
            max(1, round(max(widths or [1]))),
            max(1, round(sum(heights) + line_step * max(0, len(lines) - 1))),
            line_metrics[0] if line_metrics else (0, 0, 1, 1),
        )

    @staticmethod
    def _draw_spaced_text(
        draw,
        origin,
        text,
        font,
        fill,
        tracking_px,
        leading_px,
        word_spacing_px: float = 0,
    ):
        x0, y0 = origin
        lines = str(text or "").split("\n") or [""]
        default_box = draw.textbbox((0, 0), "Ag", font=font)
        line_step = leading_px or max(1, default_box[3] - default_box[1])
        for line_index, line in enumerate(lines):
            x = x0
            for char_index, char in enumerate(line):
                draw.text((x, y0 + line_index * line_step), char, font=font, fill=fill)
                char_box = draw.textbbox((x, y0 + line_index * line_step), char, font=font)
                x += (char_box[2] - char_box[0])
                if char_index < len(line) - 1:
                    x += tracking_px
                if char in {" ", "\t"}:
                    x += word_spacing_px

    def _render_jpeg(
        self,
        template: dict[str, Any],
        order: dict[str, Any],
        metadata: dict[str, Any],
    ):
        dimensions = self._template_dimensions(template)
        scale = self._pixels_per_unit(template)
        canvas_w = max(1, round(dimensions["total_width"] * scale))
        canvas_h = max(1, round(dimensions["total_height"] * scale))
        bleed = dimensions["bleed"] * scale
        side_w = dimensions["side_width"] * scale
        side_h = dimensions["side_height"] * scale
        spine_w = dimensions["spine_width"] * scale
        spine_bleed = dimensions["spine_bleed"] * scale

        trim_x = bleed
        trim_y = bleed
        back_x = trim_x
        back_right = trim_x + side_w
        spine_x = back_right + spine_bleed
        cover_x = spine_x + spine_w + spine_bleed
        content_y = trim_y
        trim_w = side_w * 2 + spine_w + spine_bleed * 2
        spine_safe_left = max(trim_x, spine_x - spine_bleed)
        spine_safe_right = min(trim_x + trim_w, spine_x + spine_w + spine_bleed)

        front_center_x = cover_x + side_w / 2
        back_center_x = back_x + side_w / 2
        spine_center_x = spine_x + spine_w / 2

        dark = "#26364f"
        light_blue = "#77c9df"
        orange = "#dc8b45"
        raw_background = (
            template.get("background_color")
            or (template.get("fields") or {}).get("background_color")
            or ""
        )
        try:
            paper = ImageColor.getrgb(str(raw_background)) if raw_background else (255, 255, 255)
        except (TypeError, ValueError):
            paper = (255, 255, 255)
        line_width = max(2, round(scale / 150))

        selected_layout = template.get("_selected_font_layout")
        if selected_layout and self._fabric_layout_objects(selected_layout):
            if self.image_map_renderer is None or not getattr(self.image_map_renderer, "enabled", False):
                raise RuntimeError("Fabric 图层预览必须启用 Image Map 无头渲染器")
            return (
                self._render_fabric_preview_with_image_map(
                    template,
                    selected_layout,
                    canvas_w,
                    canvas_h,
                    paper,
                ),
                dimensions,
            )

        image = Image.new("RGB", (canvas_w, canvas_h), paper)
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (trim_x, trim_y, trim_x + trim_w, trim_y + side_h),
            outline=light_blue,
            width=line_width,
        )
        self._draw_dashed_rectangle(
            draw,
            (bleed / 2, bleed / 2, canvas_w - bleed / 2, canvas_h - bleed / 2),
            fill=orange,
            width=line_width,
            dash_length=max(8, round(scale * 0.08)),
            gap_length=max(6, round(scale * 0.05)),
        )
        for x in (spine_x, spine_x + spine_w):
            draw.line(
                (x, content_y, x, content_y + side_h),
                fill=light_blue,
                width=line_width,
            )
        for x in (spine_safe_left, spine_safe_right):
            self._draw_dashed_line(
                draw,
                (x, content_y),
                (x, content_y + side_h),
                fill="#62b7cf",
                width=max(1, line_width // 2),
                dash_length=max(8, round(scale * 0.08)),
                gap_length=max(6, round(scale * 0.05)),
            )

        font_title = self._font(side_h * 0.055)
        font_body = self._font(side_h * 0.032)
        font_small = self._font(side_h * 0.024)
        font_spine = self._font(max(10, min(spine_w * 0.34, side_h * 0.03)))

        def field(name: str, fallback: str = ""):
            if not template.get("_render_editable_text", True) and name in {
                "cover_text",
                "cover_names",
                "cover_wedding_date",
                "cover_subtitle",
                "spine_top",
                "spine_middle",
                "spine_bottom",
                "back_cover_content",
            }:
                return ""
            value = template.get(name)
            if value in (None, ""):
                value = (template.get("fields") or {}).get(name, fallback)
            return str(value or fallback)

        rendered_layout = False
        if selected_layout:
            rendered_layout = bool(
                self._draw_font_layout(
                    image,
                    template,
                    order,
                    selected_layout,
                    canvas_w,
                    canvas_h,
                )
            )
            if not rendered_layout:
                raise RuntimeError(
                    "当前字体布局不是新图层格式或没有可绘制图层；"
                    "预览图不会回退到旧 Fabric/默认文字逻辑"
                )
        if not rendered_layout:
            self._draw_centered_text(
                draw,
                (back_center_x, content_y + side_h * 0.52),
                field("back_cover_content"),
                font_body,
                dark,
            )
            self._draw_centered_text(
                draw,
                (front_center_x, content_y + side_h * 0.18),
                field("cover_text"),
                font_title,
                dark,
            )
            self._draw_centered_text(
                draw,
                (front_center_x, content_y + side_h * 0.43),
                " | ".join(
                    value
                    for value in (
                        field("cover_names"),
                        field("cover_wedding_date"),
                    )
                    if value
                ),
                font_small,
                dark,
            )
            self._draw_centered_text(
                draw,
                (front_center_x, content_y + side_h * 0.62),
                field("cover_subtitle"),
                font_body,
                dark,
            )
            self._draw_rotated_text(
                image,
                (spine_center_x, content_y + side_h * 0.14),
                field("spine_top"),
                font_spine,
                dark,
            )
            self._draw_rotated_text(
                image,
                (spine_center_x, content_y + side_h * 0.55),
                field("spine_middle"),
                font_spine,
                dark,
            )
            self._draw_rotated_text(
                image,
                (spine_center_x, content_y + side_h * 0.86),
                field("spine_bottom"),
                font_spine,
                dark,
            )
        return image, dimensions

    @staticmethod
    def _fabric_layout_objects(layout: dict[str, Any]) -> list[dict[str, Any]]:
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        objects = layout.get("objects")
        if not isinstance(objects, list):
            objects = layers.get("objects")
        if not isinstance(objects, list):
            return []
        return [
            item
            for item in objects
            if isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() != "workarea"
            and "frame" not in item
        ]

    @staticmethod
    def _fabric_reference_canvas(layout: dict[str, Any]) -> dict[str, Any]:
        """Use the exported Fabric workarea as the scene reference."""
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        explicit = (
            layout.get("canvas")
            or layers.get("canvas")
            or (layout.get("options") or {}).get("reference_canvas")
            or {}
        )
        reference = dict(explicit) if isinstance(explicit, dict) else {}
        objects = layout.get("objects")
        if not isinstance(objects, list):
            objects = layers.get("objects")
        workarea = next(
            (
                item for item in (objects or [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().lower() == "workarea"
            ),
            None,
        )
        if not isinstance(workarea, dict):
            return reference
        # The browser renderer reads the serialized workarea itself. Keep the
        # canvas object as plain reference metadata and never add a legacy
        # coordinate-space marker or derived scene origin to the request.
        try:
            width = float(workarea.get("workareaWidth") or workarea["width"])
            height = float(workarea.get("workareaHeight") or workarea["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Fabric workarea 缺少有效的尺寸") from exc
        if width <= 0 or height <= 0:
            raise RuntimeError("Fabric workarea 缺少有效的尺寸")
        # The serialized workarea is the complete Fabric reference canvas;
        # stale top-level canvas dimensions must not override it.
        reference.update({"width": width, "height": height})
        return reference

    def _adapt_fabric_layout_to_spine_width(
        self,
        template: dict[str, Any],
        layout: dict[str, Any],
    ) -> dict[str, Any]:
        """Expand only the spine when the order page count changes its width.

        Fabric stores the size-variant scene in CSS pixels (96 px/in). The
        matched order may calculate a different spine width from its page
        count, while both side panels, bleed values and authored layer sizes
        remain unchanged. Mutate a render-only copy so the canonical JSON
        saved on the order is never rewritten with derived coordinates.
        """
        adapted = deepcopy(layout)
        layers = adapted.get("layers") if isinstance(adapted.get("layers"), dict) else {}
        objects = adapted.get("objects")
        if not isinstance(objects, list):
            objects = layers.get("objects")
        if not isinstance(objects, list):
            return adapted

        workarea = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().lower() == "workarea"
            ),
            None,
        )
        if not isinstance(workarea, dict):
            return adapted

        dimensions = self._template_dimensions(template)
        unit = str(template.get("size_unit") or "").strip().lower()
        unit_inches = UNIT_TO_INCHES.get(unit)
        if unit_inches is None:
            return adapted
        css_per_unit = 96.0 * unit_inches

        try:
            raw_width = float(workarea.get("width") or workarea["workareaWidth"])
            scale_x = float(workarea.get("scaleX", 1) or 1)
            source_width = abs(raw_width * scale_x)
            workarea_left = float(workarea.get("left", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return adapted
        if source_width <= 0 or scale_x == 0:
            return adapted

        origin_x = str(workarea.get("originX") or "left").strip().lower()
        origin_factor = 0.5 if origin_x in {"center", "middle"} else (1.0 if origin_x == "right" else 0.0)
        bounds_left = workarea_left - source_width * origin_factor
        fixed_width = (
            dimensions["side_width"] * 2
            + dimensions["bleed"] * 2
            + dimensions["spine_bleed"] * 2
        ) * css_per_unit
        source_spine_width = source_width - fixed_width
        target_spine_width = dimensions["spine_width"] * css_per_unit
        delta = target_spine_width - source_spine_width
        if source_spine_width <= 0 or abs(delta) < 0.01:
            return adapted

        spine_left = bounds_left + (
            dimensions["bleed"]
            + dimensions["side_width"]
            + dimensions["spine_bleed"]
        ) * css_per_unit
        spine_right = spine_left + source_spine_width

        for element in objects:
            if not isinstance(element, dict) or element is workarea:
                continue
            try:
                center_x = float(fabric_layer_geometry(element)["center_x"])
                left = float(element.get("left", 0) or 0)
            except (TypeError, ValueError):
                continue
            if center_x < spine_left:
                continue
            if center_x <= spine_right:
                ratio = (center_x - spine_left) / source_spine_width
                target_center_x = spine_left + ratio * target_spine_width
                element["left"] = left + target_center_x - center_x
            else:
                element["left"] = left + delta

        target_width = source_width + delta
        workarea["width"] = target_width / abs(scale_x)
        workarea["left"] = bounds_left + target_width * origin_factor
        workarea.update(
            {
                "workareaWidth": target_width,
                "workareaHeight": dimensions["total_height"] * css_per_unit,
                "unit": unit,
                "sideWidth": dimensions["side_width"],
                "sideHeight": dimensions["side_height"],
                "bleed": dimensions["bleed"],
                "spineWidth": dimensions["spine_width"],
                "spineBleed": dimensions["spine_bleed"],
            }
        )
        bleed_css = dimensions["bleed"] * css_per_unit
        side_css = dimensions["side_width"] * css_per_unit
        side_height_css = dimensions["side_height"] * css_per_unit
        spine_bleed_css = dimensions["spine_bleed"] * css_per_unit
        vertical_guides = [
            0.0,
            bleed_css,
            bleed_css + side_css,
            bleed_css + side_css + spine_bleed_css,
            bleed_css + side_css + spine_bleed_css + target_spine_width,
            bleed_css + side_css + spine_bleed_css + target_spine_width + spine_bleed_css,
            bleed_css + side_css * 2 + spine_bleed_css * 2 + target_spine_width,
            target_width,
        ]
        horizontal_guides = [
            0.0,
            bleed_css,
            bleed_css + side_height_css,
            dimensions["total_height"] * css_per_unit,
        ]
        workarea["printGuides"] = [
            *(
                {"orientation": "vertical", "position": position}
                for position in vertical_guides
            ),
            *(
                {"orientation": "horizontal", "position": position}
                for position in horizontal_guides
            ),
        ]
        print(
            "[IMAGE MAP] 按订单页数调整背脊工作区："
            f"原背脊={source_spine_width / css_per_unit:g}{unit}，"
            f"目标背脊={dimensions['spine_width']:g}{unit}，"
            f"画布={source_width:g}->{target_width:g} CSS px",
            flush=True,
        )
        return adapted

    def _render_fabric_preview_with_image_map(
        self,
        template: dict[str, Any],
        layout: dict[str, Any],
        canvas_w: int,
        canvas_h: int,
        paper: tuple[int, int, int],
    ) -> Image.Image:
        objects = self._fabric_layout_objects(layout)
        render_layout = self._adapt_fabric_layout_to_spine_width(template, layout)
        # Keep the serialized workarea in the browser request. The frontend
        # exports content in scene coordinates; the renderer uses this object
        # to calculate the exact crop origin instead of relying on metadata.
        all_layout_objects = render_layout.get("objects")
        if not isinstance(all_layout_objects, list):
            all_layout_objects = (
                render_layout.get("layers", {}).get("objects")
                if isinstance(render_layout.get("layers"), dict)
                else None
            )
        workarea = next(
            (
                item for item in (all_layout_objects or [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().lower() == "workarea"
            ),
            None,
        )
        if not isinstance(workarea, dict):
            raise RuntimeError("Fabric JSON 缺少 workarea，无法按前端源码渲染器绘制")
        # Preserve the exact editor object order and include workarea so the
        # browser can calculate the crop origin. Workarea is consumed as
        # metadata and is never painted as a content layer.
        image_map_objects = [
            item for item in (all_layout_objects or [])
            if isinstance(item, dict) and "frame" not in item
        ]
        layers = render_layout.get("layers") if isinstance(render_layout.get("layers"), dict) else {}
        stored_reference = self._fabric_reference_canvas(layout)
        if not isinstance(stored_reference, dict):
            stored_reference = {}
        dimensions = self._template_dimensions(template)
        unit = str(template.get("size_unit") or "").strip().lower()
        inches = UNIT_TO_INCHES.get(unit)
        if inches is None:
            raise RuntimeError(f"命中的尺寸规格单位不受支持：{unit!r}")
        reference = {
            **stored_reference,
            # Reference dimensions come from the complete Fabric JSON. The
            # output dimensions above still come exclusively from the matched
            # size option and requested output DPI.
        }
        print_spec = {
            "size_unit": unit,
            "single_side_width": dimensions["side_width"],
            "single_side_height": dimensions["side_height"],
            "bleed": dimensions["bleed"],
            "spine_width": dimensions["spine_width"],
            "spine_bleed": dimensions["spine_bleed"],
        }
        with tempfile.TemporaryDirectory(prefix="image-map-preview-") as directory:
            output = Path(directory) / "preview.jpg"
            result = self.image_map_renderer.render(
                width=canvas_w,
                height=canvas_h,
                output_path=output,
                layout={"objects": image_map_objects, "version": render_layout.get("version") or "7.4.0"},
                reference_canvas=reference,
                background_color=f"rgb({paper[0]},{paper[1]},{paper[2]})",
                font_files=self._image_map_font_files(objects),
                output_format="jpg",
                include_svg=True,
                print_spec=print_spec,
            )
            # Transient browser geometry for the PSD conversion. Keep it out
            # of the selected layout so resolved_layers_json remains exactly
            # the current Fabric document stored for the order.
            template["_fabric_render_geometry"] = deepcopy(
                result.get("layer_geometry") or []
            )
            template["_fabric_render_svg"] = result.get("svg") or ""
            with Image.open(output) as rendered:
                image = rendered.convert("RGB").copy()
        print(
            "[IMAGE MAP] Fabric 预览图生成成功："
            f"图层={len(objects)}，尺寸="
            f"{result.get('width')}x{result.get('height')}，"
            f"工作区原点={result.get('workarea_origin')}",
            flush=True,
        )
        return image

    def _draw_font_layout_with_image_map(
        self,
        image: Image.Image,
        template: dict[str, Any],
        layout: dict[str, Any],
        canvas_w: int,
        canvas_h: int,
    ) -> bool:
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        objects = layout.get("objects")
        if not isinstance(objects, list):
            objects = layers.get("objects")
        if not isinstance(objects, list):
            objects = layout.get("elements")
        objects = [item for item in (objects or []) if isinstance(item, dict)]
        if not objects:
            return False
        reference = (
            layout.get("canvas")
            or layers.get("canvas")
            or (layout.get("options") or {}).get("reference_canvas")
        )
        try:
            reference_width = float(reference.get("width") or reference.get("width_px") or 0) if isinstance(reference, dict) else 0
            reference_height = float(reference.get("height") or reference.get("height_px") or 0) if isinstance(reference, dict) else 0
        except (TypeError, ValueError):
            reference_width = reference_height = 0
        if reference_width <= 0 or reference_height <= 0:
            raise RuntimeError(
                "Fabric 图层缺少 reference_canvas.width/height，拒绝使用默认画布"
            )
        with tempfile.TemporaryDirectory(prefix="image-map-layers-") as directory:
            output = Path(directory) / "layers.png"
            self.image_map_renderer.render(
                width=canvas_w,
                height=canvas_h,
                output_path=output,
                layout={"objects": objects, "version": layout.get("version") or "7.4.0"},
                reference_canvas=reference,
                background_color="rgba(0,0,0,0)",
                font_files=self._image_map_font_files(objects),
                output_format="png",
            )
            with Image.open(output) as rendered:
                layer = rendered.convert("RGBA")
                image.paste(layer, (0, 0), layer)
        print(f"[IMAGE MAP] Fabric 图层渲染成功：图层={len(objects)}，尺寸={canvas_w}x{canvas_h}", flush=True)
        return True

    def _image_map_font_files(
        self,
        elements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        files = []
        seen = set()
        for element in elements:
            font = element.get("font") or element
            family = str(font.get("fontFamily") or font.get("font_family") or "").strip()
            if not family or family in seen:
                continue
            url = str(font.get("fontUrl") or font.get("font_url") or "").strip()
            filename = Path(unquote(urlparse(url).path)).name if url else ""
            # The template URL is authoritative. A same-named file in the
            # persistent cache may belong to an older font upload, while the
            # renderer's resource loader already provides a bounded in-memory
            # cache for remote resources.
            candidates = [] if url else ([self.font_cache_dir / filename] if filename else [])
            if not url:
                candidates.extend(self._font_candidates(font))
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if path is None and not url:
                continue
            item = {
                "family": family,
                "weight": str(font.get("fontWeight") or "normal"),
                "style": str(font.get("fontStyle") or "normal"),
            }
            if url:
                item["url"] = url
            elif path is not None:
                item["path"] = str(path.resolve())
            files.append(item)
            seen.add(family)
        return files

    @staticmethod
    def _draw_dashed_rectangle(
        draw,
        box,
        fill,
        width=1,
        dash_length=12,
        gap_length=8,
    ):
        """Draw a rectangular guide as a consistent dashed line."""
        x1, y1, x2, y2 = box
        for start, end in (
            ((x1, y1), (x2, y1)),
            ((x2, y1), (x2, y2)),
            ((x2, y2), (x1, y2)),
            ((x1, y2), (x1, y1)),
        ):
            TemplateImageGenerator._draw_dashed_line(
                draw,
                start,
                end,
                fill=fill,
                width=width,
                dash_length=dash_length,
                gap_length=gap_length,
            )

    @staticmethod
    def _draw_dashed_line(
        draw,
        start,
        end,
        fill,
        width=1,
        dash_length=12,
        gap_length=8,
    ):
        x1, y1 = start
        x2, y2 = end
        dx = x2 - x1
        dy = y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length <= 0:
            return
        dash_length = max(1, float(dash_length))
        gap_length = max(0, float(gap_length))
        cursor = 0.0
        while cursor < length:
            dash_end = min(length, cursor + dash_length)
            ratio_start = cursor / length
            ratio_end = dash_end / length
            draw.line(
                (
                    x1 + dx * ratio_start,
                    y1 + dy * ratio_start,
                    x1 + dx * ratio_end,
                    y1 + dy * ratio_end,
                ),
                fill=fill,
                width=width,
            )
            cursor += dash_length + gap_length
