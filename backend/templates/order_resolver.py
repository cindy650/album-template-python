from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any



class FontLayoutNotConfiguredError(RuntimeError):
    """The matched size template has no active font layout to render."""


class OrderTemplateResolver:
    """Resolve one order into the canonical template used by every renderer.

    The resolver owns selection only: the shop/product association is already
    stored on the order, size options and child layouts are selected locally,
    and DeepSeek may only supply values for natural-language text rules.
    Renderers consume the returned template and must not repeat these choices.
    """

    def __init__(self, deepseek_resolver=None):
        self.deepseek_resolver = deepseek_resolver

    @staticmethod
    def restore_order_snapshot(
        saved_order: dict[str, Any] | None,
        current_template: dict[str, Any] | None = None,
    ):
        snapshot = (saved_order or {}).get("matched_template")
        # API rows expose the editable document as ``template_json``. Keep a
        # fallback for in-memory rows produced by older internal callers.
        layers = (saved_order or {}).get("template_json")
        if not isinstance(layers, dict):
            layers = (saved_order or {}).get("resolved_layers")
        if not isinstance(snapshot, dict) or not isinstance(layers, dict):
            return None
        saved_template_id = snapshot.get("template_id")
        current_template_id = (current_template or {}).get("id")
        associated_template_id = (saved_order or {}).get("size_template_id")
        if (
            associated_template_id not in (None, "")
            and str(saved_template_id) != str(associated_template_id)
        ) or (
            current_template_id not in (None, "")
            and str(saved_template_id) != str(current_template_id)
        ):
            print("[模板解析] 订单快照的尺寸模板已变化，已作废并重新解析", flush=True)
            return None
        current_layout_id = (current_template or {}).get("selected_font_layout_id")
        saved_layout_id = layers.get("layout_id", layers.get("id"))
        if current_template is not None and not snapshot.get("manual_selection") and (
            current_layout_id in (None, "")
            or str(saved_layout_id) != str(current_layout_id)
        ):
            print("[模板解析] 尺寸模板当前字体布局已变化，订单快照已作废", flush=True)
            return None
        elements = OrderTemplateResolver._layout_objects(layers)
        if not elements:
            return None
        required_size_fields = (
            "size_unit",
            "single_side_width",
            "single_side_height",
            "bleed",
            "spine_width",
            "spine_bleed",
        )
        bleed_details = snapshot.get("bleed_details") or {}
        snapshot_bleed = snapshot.get("bleed")
        if snapshot_bleed in (None, ""):
            snapshot_bleed = bleed_details.get("left", bleed_details.get("top"))
        if any(
            (snapshot_bleed if key == "bleed" else snapshot.get(key)) in (None, "")
            for key in required_size_fields
        ):
            print(
                "[模板解析] 订单快照缺少命中规格字段，已作废并重新读取尺寸模板",
                flush=True,
            )
            return None
        if not all(OrderTemplateResolver._is_current_layer_element(item) for item in elements):
            print(
                "[模板解析] 订单快照使用旧 Fabric 图层格式，已作废并重新读取尺寸模板",
                flush=True,
            )
            return None
        # The current snapshot contract is the complete Fabric document. A
        # workarea is mandatory because the frontend renderer derives the crop
        # origin from it; no coordinate-space marker or legacy origin fields
        # are interpreted here.
        object_source = layers.get("objects") if isinstance(layers.get("objects"), list) else elements
        if not any(
            isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() == "workarea"
            for item in object_source
        ):
            print("[模板解析] 订单快照缺少 workarea，已作废并重新读取字体布局", flush=True)
            return None
        if "layout_id" not in layers and ("id" in layers or "objects" in layers or "layers" in layers):
            # New compact format: matched_template is the size-only snapshot
            # and resolved_layers is the selected font-layout JSON itself.
            restored = {
                "id": snapshot.get("template_id"),
                "shop_id": snapshot.get("shop_id"),
                "shop": snapshot.get("shop"),
                "shop_name": snapshot.get("shop_name"),
                "product_id": snapshot.get("product_id"),
                "size_unit": snapshot.get("size_unit"),
                "single_side_width": snapshot.get("single_side_width"),
                "single_side_height": snapshot.get("single_side_height"),
                "bleed": snapshot_bleed,
                "spine_width": snapshot.get("spine_width"),
                "spine_bleed": snapshot.get("spine_bleed"),
                "background_color": snapshot.get("background_color") or "#ffffff",
                "size_spec": {
                    "selected": snapshot.get("selected_size"),
                    "options": [deepcopy({
                        "id": snapshot.get("selected_size"),
                        "label": snapshot.get("selected_size"),
                        "size_unit": snapshot.get("size_unit"),
                        "single_side_width": snapshot.get("single_side_width"),
                        "single_side_height": snapshot.get("single_side_height"),
                        "bleed": snapshot_bleed,
                        "spine_width": snapshot.get("spine_width"),
                        "spine_bleed": snapshot.get("spine_bleed"),
                    })],
                },
            }
            restored["_selected_font_layout"] = deepcopy(layers)
        else:
            # Backward compatibility for rows written before the compact
            # snapshot migration. These rows are cleared by the migration.
            restored = deepcopy(snapshot)
            restored["_selected_font_layout"] = {
                "id": layers.get("layout_id"),
                "name": layers.get("layout_name"),
                "size_option": layers.get("size_option"),
                "layers_source": layers.get("layers_source"),
                "layers": deepcopy(layers.get("layers") or {}),
                "canvas": deepcopy(layers.get("canvas") or {}),
                "elements": deepcopy(elements),
                "options": {
                    "reference_canvas": deepcopy(layers.get("canvas") or {})
                },
            }
        # Order snapshots already contain the canonical layer contract.  Do
        # not run them through the legacy Fabric left/top/scale converter: it
        # changes the stored frame and makes a second render depend on the
        # previous render.
        restored["_selected_font_layout"] = OrderTemplateResolver._prepare_layout_elements(
            restored["_selected_font_layout"]
        )
        return restored

    def resolve(self, template: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        resolved = deepcopy(template)
        information = order.get("product_information") or order.get("商品信息") or {}
        if not isinstance(information, dict):
            information = {}
        option = self.match_size_option(resolved, information)
        page_count = self.extract_page_count(information)
        if option is not None:
            fields = dict(resolved.get("fields") or {})
            spec = dict(fields.get("size_spec") or resolved.get("size_spec") or {})
            spec["selected"] = str(option.get("id"))
            spec_options = []
            for candidate in spec.get("options") or []:
                if isinstance(candidate, dict) and str(candidate.get("id")) == str(option.get("id")):
                    spec_options.append(option)
                else:
                    spec_options.append(candidate)
            if spec_options:
                spec["options"] = spec_options
            fields["size_spec"] = spec
            resolved["fields"] = fields
            resolved["size_spec"] = spec
            # Keep the complete unit-aware row that was matched from
            # size_template_options in the order snapshot.
            self._apply_order_page_count_spine_width(resolved, option, page_count)
            resolved["matched_size_option"] = deepcopy(option)
            resolved["size_template_option"] = deepcopy(option)
            print(
                f"[模板解析] 订单尺寸匹配成功：订单号={order.get('order_number') or order.get('订单号') or ''}，"
                f"尺寸={option.get('label') or option.get('id')}",
                flush=True,
            )
        else:
            print(
                f"[模板解析] 未从订单文字识别到尺寸，使用模板当前选中规格：订单号={order.get('order_number') or order.get('订单号') or ''}，"
                f"尺寸={((resolved.get('size_spec') or {}).get('selected') or '默认')}",
                flush=True,
            )
            selected_id = (resolved.get("size_spec") or {}).get("selected")
            selected_option = next(
                (
                    item for item in (resolved.get("size_spec") or {}).get("options") or []
                    if isinstance(item, dict) and str(item.get("id")) == str(selected_id)
                ),
                None,
            )
            if selected_option is not None:
                self._apply_order_page_count_spine_width(
                    resolved,
                    selected_option,
                    page_count,
                )
                resolved["matched_size_option"] = deepcopy(selected_option)
                resolved["size_template_option"] = deepcopy(selected_option)

        # The selected size option is the single source of truth for rendering.
        # Its layers document may contain natural-language rules directly;
        # never resolve rules from a separate font-layout JSON.
        selected_layers = None
        if isinstance(option, dict):
            selected_layers = option.get("layers")
        if not isinstance(selected_layers, dict):
            selected_id = (resolved.get("size_spec") or {}).get("selected")
            selected_option = next(
                (item for item in (resolved.get("size_spec") or {}).get("options") or []
                 if isinstance(item, dict) and str(item.get("id")) == str(selected_id)),
                None,
            )
            if isinstance(selected_option, dict):
                selected_layers = selected_option.get("layers")
        layouts = [selected_layers] if isinstance(selected_layers, dict) else []
        selected_size = (resolved.get("size_spec") or {}).get("selected")
        layout = next(
            (item for item in layouts if isinstance(item, dict)),
            None,
        )
        if layout is None:
            raise FontLayoutNotConfiguredError(
                "命中的尺寸模板未配置字体布局，已跳过订单图片"
            )
        requires_deepseek = self.layout_requires_deepseek(layout, selected_size)
        resolver = self.deepseek_resolver
        if requires_deepseek and (resolver is None or not resolver.enabled):
            raise RuntimeError(
                "模板包含自然语言图层规则，必须配置并调用 DeepSeek，不能使用模板示例文字回退"
            )
        if resolver is not None and resolver.enabled:
            try:
                deepseek_result = resolver.resolve(
                    information,
                    (resolved.get("size_spec") or {}).get("options") or [],
                    layouts,
                    selected_size=selected_size,
                    selected_layout=layout,
                )
                returned_values = deepseek_result.get("text_values") or {}
                rule_layout = self.font_layout_for_size_option(layout, selected_size)
                rule_ids = self.natural_rule_element_ids(rule_layout)
                missing_ids = [
                    element_id for element_id in rule_ids
                    if element_id not in returned_values
                ]
                empty_ids = [
                    element_id for element_id in rule_ids
                    if element_id in returned_values
                    and not str(returned_values.get(element_id) or "").strip()
                ]
                allowed_empty_ids = {
                    element_id
                    for element_id in empty_ids
                    if self.rule_allows_empty(rule_layout, element_id)
                }
                invalid_empty_ids = [
                    element_id for element_id in empty_ids
                    if element_id not in allowed_empty_ids
                ]
                if missing_ids or invalid_empty_ids:
                    error_ids = missing_ids + invalid_empty_ids
                    raise RuntimeError(
                        "DeepSeek 未返回全部自然语言图层文字：" + ", ".join(error_ids)
                    )
                values = {
                    element_id: returned_values[element_id]
                    for element_id in rule_ids
                    if element_id not in allowed_empty_ids
                }
                resolved["_deepseek_text_values"] = values
                resolved["_empty_rule_element_ids"] = list(allowed_empty_ids)
                if allowed_empty_ids:
                    print(
                        "[模板解析] 自然语言图层文字为空，已删除图层："
                        + ", ".join(sorted(allowed_empty_ids)),
                        flush=True,
                    )
                print(
                    f"[模板解析] DeepSeek 规则完成：布局={layout.get('name') or layout.get('id')}",
                    flush=True,
                )
                rule_text_summary = {
                    element_id: str(values[element_id])
                    for element_id in values
                }
                if rule_text_summary:
                    print(
                        "[模板解析] [DeepSeek] 规则图层最终文字："
                        + json.dumps(rule_text_summary, ensure_ascii=False),
                        flush=True,
                    )
            except Exception as exc:
                if requires_deepseek:
                    print(
                        f"[模板解析] DeepSeek 规则失败：{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    raise
                print(
                    f"[模板解析] DeepSeek 非必需规则失败，继续本地渲染：{type(exc).__name__}: {exc}",
                    flush=True,
                )
        layout = self.font_layout_for_size_option(layout, selected_size)
        layout = self._remove_layout_elements(
            layout,
            resolved.get("_empty_rule_element_ids") or [],
        )
        layout = self._replace_text_values(layout, resolved.get("_deepseek_text_values") or {})
        resolved["_selected_font_layout"] = layout
        print(
            f"[模板解析] 使用尺寸模板配置的字体布局：订单号={order.get('order_number') or order.get('订单号') or ''}，"
            f"布局={layout.get('name') or layout.get('id')}，图层来源={layout.get('layers_source')}",
            flush=True,
        )
        resolved["_template_resolution"] = {
            "size_option": selected_size,
            "font_layout_id": (layout or {}).get("id"),
            "font_layout_name": (layout or {}).get("name"),
            "layers_source": (layout or {}).get("layers_source"),
        }
        return resolved

    @staticmethod
    def extract_page_count(information: dict[str, Any]) -> float | None:
        """Extract page count while ignoring dimensions such as ``9*6``."""
        if not isinstance(information, dict):
            return None
        for key, raw_value in information.items():
            label = str(key or "").casefold()
            if not any(token in label for token in ("page", "pages", "页数", "页", "sheet")):
                continue
            value = str(raw_value or "")
            without_dimensions = re.sub(
                r"\d+(?:\.\d+)?\s*(?:x|×|\*)\s*\d+(?:\.\d+)?",
                " ",
                value,
                flags=re.I,
            )
            numbers = re.findall(r"\d+(?:\.\d+)?", without_dimensions)
            if not numbers:
                continue
            try:
                return float(numbers[0])
            except ValueError:
                continue
        return None

    @staticmethod
    def _apply_order_page_count_spine_width(
        template: dict[str, Any],
        option: dict[str, Any],
        page_count: float | None,
    ) -> None:
        if not isinstance(option, dict) or page_count is None:
            return
        from backend.templates.size_variants import (
            resolve_product_spine_width_formula,
            resolve_product_spine_width_page_table,
            resolve_spine_width_for_page_count,
        )

        template["resolved_page_count"] = page_count
        fields = {
            key: deepcopy(template.get(key))
            for key in (
                "min_spine_width",
                "max_spine_width",
                "min_page_count",
                "max_page_count",
                "page_count",
                "page_count_options",
                "page_count_arr",
                "product_spine_width_mode",
                "product_spine_width_formula",
                "product_spine_width_page_rules",
            )
            if key in template
        }
        template_fields = template.get("fields") or {}
        if isinstance(template_fields, dict):
            fields.update(template_fields)
        fields.update(option)
        product_formula = fields.get("product_spine_width_formula")
        product_page_rules = fields.get("product_spine_width_page_rules")
        has_product_mode = bool(
            str(fields.get("product_spine_width_mode") or "").strip()
        )
        product_mode = str(fields.get("product_spine_width_mode") or "").strip().lower()
        if not product_mode:
            if isinstance(product_page_rules, dict) and product_page_rules:
                product_mode = "page_count_table"
            elif isinstance(product_formula, dict) and product_formula:
                product_mode = "formula"
            else:
                product_mode = "range"
        page_mode_enabled = (
            (has_product_mode and product_mode in {
                "range",
                "formula",
                "page_count_table",
            })
            or (
                not has_product_mode
                and str(fields.get("spine_width_basis") or "").strip().lower()
                in {"range", "1"}
            )
            or option.get("spine_width_mode") == "by_page_count"
        )
        if (
            page_mode_enabled
            and product_mode == "page_count_table"
            and isinstance(product_page_rules, dict)
        ):
            result = resolve_product_spine_width_page_table(
                product_page_rules,
                page_count,
                option.get("size_unit") or fields.get("size_unit") or "in",
            )
            if result is not None:
                width, resolution = result
                option["spine_width"] = width
                option["spine_bleed"] = resolution["spine_bleed"]
                option["spine_width_mode"] = "fixed"
                template["spine_width_resolution"] = resolution
                print(
                    "[模板解析] 使用产品级页数分段背脊配置："
                    f"页数={page_count:g}，匹配页数={resolution['matched_page_count']:g}，"
                    f"背脊宽={width:.6g}，单位={resolution['target_unit']}，"
                    f"背脊出血={option['spine_bleed']:.6g}",
                    flush=True,
                )
                return
        if (
            page_mode_enabled
            and product_mode == "formula"
            and isinstance(product_formula, dict)
            and product_formula
        ):
            result = resolve_product_spine_width_formula(
                product_formula,
                page_count,
                option.get("size_unit") or fields.get("size_unit") or "in",
            )
            if result is not None:
                width, resolution = result
                option["spine_width"] = width
                option["spine_bleed"] = resolution["spine_bleed"]
                option["spine_width_mode"] = "fixed"
                template["spine_width_resolution"] = resolution
                print(
                    "[模板解析] 使用产品级页数背脊公式："
                    f"页数={page_count:g}，背脊宽={width:.6g}，"
                    f"单位={resolution['target_unit']}，背脊出血={option['spine_bleed']:.6g}",
                    flush=True,
                )
                return
        result = resolve_spine_width_for_page_count(fields, page_count)
        if result is None:
            print(
                "[模板解析] 未找到完整页数/背脊宽范围，保留模板原背脊宽："
                f"页数={page_count:g}，背脊宽={option.get('spine_width', 0)}",
                flush=True,
            )
            return
        width, resolution = result
        option["spine_width"] = width
        option["spine_width_mode"] = "fixed"
        template["spine_width_resolution"] = resolution
        print(
            "[模板解析] 订单页数调整背脊宽："
            f"页数={page_count:g}，背脊宽={width:.6g}，"
            f"页数范围={resolution['min_page_count']:g}-{resolution['max_page_count']:g}，"
            f"背脊范围={resolution['min_spine_width']:.6g}-{resolution['max_spine_width']:.6g}",
            flush=True,
        )

    @staticmethod
    def _remove_layout_elements(
        layout: dict[str, Any],
        element_ids: list[str] | set[str],
    ) -> dict[str, Any]:
        """Remove rule layers whose resolved text is intentionally empty."""
        if not isinstance(layout, dict) or not element_ids:
            return layout
        ids = {
            str(element_id).strip()
            for element_id in element_ids
            if str(element_id).strip()
        }
        if not ids:
            return layout

        def remove_from(collection):
            if not isinstance(collection, list):
                return collection
            return [
                element for element in collection
                if not (
                    isinstance(element, dict)
                    and str(element.get("id") or element.get("name") or "").strip() in ids
                )
            ]

        if isinstance(layout.get("objects"), list):
            layout["objects"] = remove_from(layout["objects"])
        if isinstance(layout.get("elements"), list):
            layout["elements"] = remove_from(layout["elements"])
        layers = layout.get("layers")
        if isinstance(layers, dict) and isinstance(layers.get("objects"), list):
            layers["objects"] = remove_from(layers["objects"])
        return layout

    @classmethod
    def rule_allows_empty(cls, layout: dict[str, Any], element_id: str) -> bool:
        """Return whether a rule explicitly permits clearing an unmatched value."""
        for element in cls._layout_objects(layout):
            if not isinstance(element, dict):
                continue
            current_id = str(element.get("id") or element.get("name") or "").strip()
            if current_id != str(element_id).strip():
                continue
            rule = (
                element.get("rule")
                or (element.get("content") or {}).get("rule")
                or element.get("rules")
                or ""
            )
            if isinstance(rule, dict):
                for key in ("allow_empty", "optional", "nullable", "clear_when_unmatched"):
                    if rule.get(key) is True:
                        return True
                description = str(rule.get("description") or "")
            else:
                description = str(rule or "")
            normalized = re.sub(r"\s+", "", description).casefold()
            has_no_match_condition = any(
                token in normalized
                for token in (
                    "没有匹配",
                    "未匹配",
                    "匹配不到",
                    "匹配不上",
                    "没有找到",
                    "未找到",
                    "找不到",
                    "无匹配",
                    "nomatch",
                    "notfound",
                    "missing",
                    "unavailable",
                )
            ) or bool(
                re.search(r"(?:没有|未|无).{0,24}(?:时|的话|则|就)", normalized)
            )
            has_clear_instruction = any(
                token in normalized
                for token in (
                    "置空",
                    "清空",
                    "留空",
                    "为空",
                    "空白",
                    "不填",
                    "blank",
                    "empty",
                    "clear",
                    "omit",
                    "remove",
                )
            )
            return has_no_match_condition and has_clear_instruction
        return False

    @staticmethod
    def _replace_text_values(layout: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        """Materialize DeepSeek values into the selected layer JSON."""
        if not isinstance(layout, dict) or not isinstance(values, dict):
            return layout
        # Materialize into every view of the document. The catalog response
        # temporarily exposes `elements` as an alias, while the stored Fabric
        # document uses `objects`; keeping both in sync prevents PSD/export
        # code from seeing stale sample text.
        layers = layout.get("layers")
        collections = [layout.get("objects"), layout.get("elements")]
        if isinstance(layers, dict):
            collections.append(layers.get("objects"))
        for collection in collections:
            if not isinstance(collection, list):
                continue
            for element in collection:
                if not isinstance(element, dict):
                    continue
                element_id = str(element.get("id") or element.get("name") or "")
                if element_id not in values:
                    continue
                text = str(values[element_id] or "")
                element["text"] = text
                content = element.get("content")
                if isinstance(content, dict) and "literal" in content:
                    content["literal"] = text
        return layout

    @staticmethod
    def _layout_objects(layout: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Return the current Fabric object list without changing its shape.

        New layouts are stored as a Fabric document (`objects`), commonly
        nested under `layers`.  `elements` is accepted only for rows created
        by the transitional catalog API; no geometry conversion is performed.
        """
        if not isinstance(layout, dict):
            return []
        for candidate in (
            layout.get("objects"),
            (layout.get("layers") or {}).get("objects")
            if isinstance(layout.get("layers"), dict)
            else None,
            layout.get("elements"),
        ):
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    @staticmethod
    def _is_current_layer_element(element: Any) -> bool:
        """Return whether an element follows the current Fabric document contract."""
        if not isinstance(element, dict):
            return False
        element_type = str(element.get("type") or "").strip().lower()
        if not element_type or isinstance(element.get("frame"), dict):
            return False
        # Fabric serializes every drawable with scene left/top. Width/height
        # are required for normal objects; line-like objects may instead carry
        # their point coordinates and are accepted by the frontend renderer.
        if "left" not in element or "top" not in element:
            return False
        if element_type in {"line", "arrow", "polygon", "polyline"}:
            return True
        return "width" in element or "height" in element or element_type == "group"

    @classmethod
    def _prepare_layout_elements(cls, layout: dict[str, Any]) -> dict[str, Any]:
        # Keep the current Fabric document unchanged. In particular, do not
        # manufacture frame values or target coordinates from Fabric fields.
        objects = cls._layout_objects(layout)
        if isinstance(layout.get("objects"), list):
            layout["objects"] = [deepcopy(item) for item in objects]
        elif isinstance(layout.get("layers"), dict) and isinstance(layout["layers"].get("objects"), list):
            layout["layers"]["objects"] = [deepcopy(item) for item in objects]
        elif isinstance(layout.get("elements"), list):
            layout["elements"] = [deepcopy(item) for item in objects]
        # A missing canvas is invalid for the current contract.  Never infer
        # one from element extents: that makes every layer move when a text
        # value changes and cannot match the frontend canvas.
        layout["options"] = dict(layout.get("options") or {})
        return layout

    @classmethod
    def match_size_option(cls, template: dict[str, Any], information: dict[str, Any]):
        fields = template.get("fields") or {}
        spec = fields.get("size_spec") or template.get("size_spec") or {}
        options = spec.get("options") if isinstance(spec, dict) else None
        if not isinstance(options, list) or len(options) <= 1:
            return None
        source = cls._size_information_text(information)
        if not source:
            return None
        source_normalized = cls._normalize_size_text(source)
        source_pairs = cls._size_pairs(source)
        for raw_option in options:
            if not isinstance(raw_option, dict):
                continue
            option = cls._enrich_size_option(template, raw_option)
            if option.get("disabled") or (
                str(option.get("status") or "ready") != "ready"
                and not cls._size_option_has_values(option)
            ):
                continue
            alias_text = " ".join(
                str(value or "")
                for value in (option.get("id"), option.get("value"), option.get("label"))
            )
            normalized_alias = cls._normalize_size_text(alias_text)
            if normalized_alias and normalized_alias in source_normalized:
                return option
            option_pairs = cls._size_pairs(alias_text)
            if option_pairs and any(pair in source_pairs for pair in option_pairs):
                return option
        return None

    @classmethod
    def _enrich_size_option(cls, template: dict[str, Any], option: dict[str, Any]):
        from backend.templates.size_variants import merge_size_option

        enriched = deepcopy(option)
        option_id = str(enriched.get("id") or enriched.get("value") or "")
        for raw in template.get("size_options") or []:
            if isinstance(raw, dict) and str(raw.get("id") or raw.get("value") or "") == option_id:
                return merge_size_option(enriched, raw)
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
    def font_layout_for_size_option(cls, layout: dict[str, Any], size_option: Any) -> dict[str, Any]:
        resolved = deepcopy(layout)
        layouts = resolved.pop("_size_option_layouts", {})
        if not layouts:
            raw_size_layouts = resolved.pop("size_layouts", resolved.pop("sizeLayouts", []))
            if isinstance(raw_size_layouts, list):
                layouts = {
                    str(item.get("size_option_id") or item.get("sizeOptionId")): item
                    for item in raw_size_layouts
                    if isinstance(item, dict)
                    and item.get("size_option_id", item.get("sizeOptionId")) not in (None, "")
                }
        option_id = str(size_option or "")
        option_layout = layouts.get(option_id) if isinstance(layouts, dict) else None
        if not isinstance(option_layout, dict):
            resolved["size_option"] = option_id or None
            resolved["layers_source"] = "base"
            resolved["using_base_layers"] = True
            return cls._prepare_layout_elements(resolved)
        layers = option_layout.get("layers")
        option_objects = option_layout.get("objects")
        if not isinstance(option_objects, list) and isinstance(layers, dict):
            option_objects = layers.get("objects")
        if not isinstance(option_objects, list):
            option_objects = option_layout.get("elements") or []
        resolved["objects"] = deepcopy(option_objects)
        # Keep the old flattened catalog response readable for clients that
        # still inspect `elements`; rendering always prefers `objects`.
        # `elements` remains a read-only compatibility alias for existing
        # callers; all renderers select `objects` first and never transform it.
        resolved["elements"] = deepcopy(option_objects)
        if isinstance(layers, dict):
            resolved["layers"] = deepcopy(layers)
        resolved["size_option"] = option_id
        resolved["layers_source"] = option_layout.get(
            "layers_source", "size_variant"
        )
        resolved["using_base_layers"] = False
        options = dict(resolved.get("options") or {})
        options.pop("responsive_version", None)
        options.pop("reference_size_option", None)
        options.pop("responsive_layout", None)
        option_canvas = option_layout.get("canvas")
        if not isinstance(option_canvas, dict) and isinstance(layers, dict):
            option_canvas = layers.get("canvas")
        if isinstance(option_canvas, dict):
            resolved["canvas"] = deepcopy(option_canvas)
            options["reference_canvas"] = deepcopy(option_canvas)
        resolved["options"] = options
        return cls._prepare_layout_elements(resolved)

    @classmethod
    def natural_rule_element_ids(cls, layout: dict[str, Any]) -> list[str]:
        ids = []
        for element in cls._layout_objects(layout):
            if not isinstance(element, dict) or str(element.get("type") or "text").lower() not in {
                "text", "textbox", "i-text", "itext"
            }:
                continue
            rule = (
                element.get("rule")
                or (element.get("content") or {}).get("rule")
                or element.get("rules")
                or ""
            )
            element_id = str(element.get("id") or element.get("name") or "").strip()
            description = (
                str(rule.get("description") or "").strip()
                if isinstance(rule, dict)
                else str(rule or "").strip()
            )
            if description and element_id:
                ids.append(element_id)
        return ids

    @classmethod
    def layout_requires_deepseek(cls, layout: dict[str, Any] | None, size_option: Any) -> bool:
        return bool(layout and cls.natural_rule_element_ids(cls.font_layout_for_size_option(layout, size_option)))
