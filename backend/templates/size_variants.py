from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any


SIZE_UNITS = ("in", "mm", "cm")
SIZE_VALUE_FIELDS = (
    "single_side_width",
    "single_side_height",
    "bleed",
    "spine_width",
    "spine_bleed",
)
SIZE_VARIANT_FIELDS = (
    "size_unit",
    *SIZE_VALUE_FIELDS,
    "spine_width_mode",
    "spine_width_formula",
)
SIZE_OPTION_KEYS = ("id", "label", "select", *SIZE_VARIANT_FIELDS)


def required_size_unit(value: Any) -> str:
    unit = str(value or "").strip().lower()
    if not unit:
        raise ValueError("size_unit 不能为空，拒绝使用默认单位")
    if unit not in SIZE_UNITS:
        raise ValueError("size_unit 只能是 in、mm 或 cm")
    return unit


def normalize_size_options(raw_options: Any) -> list[dict[str, Any]]:
    """Return canonical flat options without derived unit copies."""
    if not isinstance(raw_options, list):
        return []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_options):
        if isinstance(raw, str):
            raw = {"label": raw}
        if isinstance(raw, dict):
            result.append(_flatten_option(raw, index=index, partial=False))
    return result


def merge_size_option(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial editor payload to one canonical size option."""
    base = normalize_size_options([existing])[0]
    patch = _flatten_option(incoming, index=0, partial=True)
    option_id = patch.pop("id", None)
    if option_id not in (None, "") and str(option_id) != str(base["id"]):
        raise ValueError("尺寸方案 id 不允许修改")
    base.update(patch)
    return normalize_size_options([base])[0]


def normalize_size_spec(raw_spec: Any) -> dict[str, Any]:
    """Normalize storage while keeping selection consistent."""
    raw = dict(raw_spec) if isinstance(raw_spec, dict) else {}
    options = normalize_size_options(raw.get("options") or [])
    option_ids = [str(option["id"]) for option in options]
    if len(option_ids) != len(set(option_ids)):
        raise ValueError("size_spec.options 中的 id 不能重复")

    selected = raw.get("selected")
    if isinstance(selected, dict):
        selected = selected.get("id")
    selected_from_option = next(
        (option["id"] for option in options if option.get("select")),
        None,
    )
    if not options:
        selected = None
    elif selected in (None, ""):
        selected = selected_from_option or option_ids[0]
    elif str(selected) not in option_ids:
        raise ValueError(f"size_spec.selected 未匹配尺寸方案: {selected}")
    selected = str(selected) if selected is not None else None
    for option in options:
        option["select"] = str(option["id"]) == selected

    result = {"selected": selected, "options": options}
    display_unit = str(raw.get("display_unit") or "").strip().lower()
    if display_unit:
        if display_unit not in SIZE_UNITS:
            raise ValueError("size_unit 只能是 in、mm 或 cm")
        result["display_unit"] = display_unit
    return result


def selected_size_option(spec: Any) -> dict[str, Any] | None:
    normalized = normalize_size_spec(spec)
    selected_id = normalized.get("selected")
    if selected_id is None:
        return None
    return next(
        option
        for option in normalized["options"]
        if str(option["id"]) == selected_id
    )


def build_size_form(
    fields: dict[str, Any],
    size_option: str | None = None,
    size_unit: str | None = None,
) -> dict[str, Any]:
    """Return a flat compatibility form without generated unit variants."""
    fields = dict(fields or {})
    spec = normalize_size_spec(fields.get("size_spec"))
    if size_option not in (None, ""):
        selected_id = str(size_option)
        if selected_id not in {str(option["id"]) for option in spec["options"]}:
            raise ValueError(f"spec.selected 未匹配尺寸方案: {selected_id}")
        spec["selected"] = selected_id
        for option in spec["options"]:
            option["select"] = str(option["id"]) == selected_id
    option = selected_size_option(spec)
    selected_values = option or fields
    requested_unit = str(size_unit or spec.get("display_unit") or "").lower()
    if requested_unit and requested_unit not in SIZE_UNITS:
        raise ValueError("size_unit 只能是 in、mm 或 cm")
    # Empty options are valid for a newly created editable template. Defer
    # per-option unit validation until the frontend adds a real option.
    if option is None:
        return {
            "size_option": None,
            "size_options": deepcopy(spec["options"]),
            "size_unit": requested_unit or None,
            "size_unit_options": list(SIZE_UNITS),
            **{key: selected_values.get(key, 0) for key in SIZE_VALUE_FIELDS},
            "page_count": fields.get("page_count"),
            "page_count_arr": _page_count_arr(fields.get("page_count_arr")),
            "status": "needs_values",
        }
    unit = required_size_unit(selected_values.get("size_unit"))
    ready = _has_required_values(selected_values)
    return {
        "size_option": str(option["id"]) if option else None,
        "size_options": deepcopy(spec["options"]),
        "size_unit": unit,
        "size_unit_options": list(SIZE_UNITS),
        **{key: selected_values.get(key, 0) for key in SIZE_VALUE_FIELDS},
        "page_count": fields.get("page_count"),
        "page_count_arr": _page_count_arr(fields.get("page_count_arr")),
        "status": "ready" if ready else "needs_values",
    }


def resolve_spine_width(fields: dict[str, Any]) -> float:
    mode = str(fields.get("spine_width_mode") or "fixed").strip().lower()
    if mode == "fixed":
        return _number(fields.get("spine_width"), 0)
    formula = fields.get("spine_width_formula")
    formula = formula if isinstance(formula, dict) else {}
    page_count = _number(fields.get("page_count"), 0)
    base = _number(formula.get("base"), _number(fields.get("spine_width"), 0))
    per_page = _number(formula.get("per_page"), 0)
    reference = _number(formula.get("reference_page_count"), 0)
    width = base + (page_count - reference) * per_page
    if formula.get("min") not in (None, ""):
        width = max(width, _number(formula["min"]))
    if formula.get("max") not in (None, ""):
        width = min(width, _number(formula["max"]))
    return max(0, width)


def resolve_product_spine_width_formula(
    formula: dict[str, Any],
    page_count: Any,
    target_unit: str,
) -> tuple[float, dict[str, Any]] | None:
    """Resolve a product formula and convert its result to target_unit."""
    if not isinstance(formula, dict) or not formula:
        return None
    try:
        source_unit = required_size_unit(formula.get("unit"))
        target_unit = required_size_unit(target_unit)
        pages = float(page_count)
        coefficient = float(formula.get("page_count_coefficient", 0.2))
        thickness = float(formula.get("page_count_thickness", 0.3))
        base = float(formula.get("base_width", 1))
        additional = float(formula.get("additional_width", 0.9))
        spine_bleed = float(formula.get("spine_bleed", 0))
    except (TypeError, ValueError):
        return None
    if pages < 0 or coefficient < 0 or thickness < 0 or base < 0 or additional < 0 or spine_bleed < 0:
        return None
    width = pages * coefficient * thickness + base + additional
    source_factor = {"in": 1.0, "cm": 2.54, "mm": 25.4}[source_unit]
    target_factor = {"in": 1.0, "cm": 2.54, "mm": 25.4}[target_unit]
    converted_width = width / source_factor * target_factor
    converted_bleed = spine_bleed / source_factor * target_factor
    return max(0, converted_width), {
        "method": "product_formula",
        "formula_unit": source_unit,
        "target_unit": target_unit,
        "page_count": pages,
        "page_count_coefficient": coefficient,
        "page_count_thickness": thickness,
        "base_width": base,
        "additional_width": additional,
        "spine_width": max(0, converted_width),
        "spine_bleed": max(0, converted_bleed),
    }


def resolve_product_spine_width_page_table(
    rules: dict[str, Any],
    page_count: Any,
    target_unit: str,
) -> tuple[float, dict[str, Any]] | None:
    """Resolve a product page-count spine table and convert its units."""
    if not isinstance(rules, dict) or not rules:
        return None
    try:
        source_unit = required_size_unit(rules.get("unit"))
        target_unit = required_size_unit(target_unit)
        requested = float(page_count)
    except (TypeError, ValueError):
        return None
    raw_items = rules.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    items: list[tuple[float, float, float]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            page = float(item.get("page_count"))
            width = float(item.get("spine_width"))
            bleed = float(item.get("spine_bleed", 0))
        except (TypeError, ValueError):
            continue
        if page < 0 or width < 0 or bleed < 0:
            continue
        items.append((page, width, bleed))
    if not items:
        return None
    items.sort(key=lambda item: item[0])
    strategy = str(rules.get("match_strategy") or "ceil").strip().lower()
    selected: tuple[float, float, float] | None = None
    lower: tuple[float, float, float] | None = None
    upper: tuple[float, float, float] | None = None
    for item in items:
        if item[0] == requested:
            selected = item
            lower = upper = item
            break
        if item[0] < requested:
            lower = item
        elif upper is None:
            upper = item
    if selected is None:
        if strategy == "exact":
            return None
        if strategy == "linear" and lower is not None and upper is not None:
            span = upper[0] - lower[0]
            ratio = (requested - lower[0]) / span if span > 0 else 0
            selected = (
                requested,
                lower[1] + (upper[1] - lower[1]) * ratio,
                lower[2] + (upper[2] - lower[2]) * ratio,
            )
        elif strategy == "ceil" and upper is not None:
            selected = upper
        else:
            # Below the first node or above the last node: clamp to the
            # nearest configured endpoint for both ceil and linear modes.
            selected = upper or lower
    if selected is None:
        return None
    source_factor = {"in": 1.0, "cm": 2.54, "mm": 25.4}[source_unit]
    target_factor = {"in": 1.0, "cm": 2.54, "mm": 25.4}[target_unit]
    width = selected[1] / source_factor * target_factor
    bleed = selected[2] / source_factor * target_factor
    return max(0, width), {
        "method": "product_page_count_table",
        "match_strategy": strategy,
        "requested_page_count": requested,
        "matched_page_count": selected[0],
        "formula_unit": source_unit,
        "target_unit": target_unit,
        "spine_width": max(0, width),
        "spine_bleed": max(0, bleed),
    }


def resolve_spine_width_for_page_count(
    fields: dict[str, Any],
    page_count: Any,
) -> tuple[float, dict[str, Any]] | None:
    """Resolve spine width from the template page range and width range.

    The smallest configured page count maps to ``min_spine_width`` and the
    largest maps to ``max_spine_width``. Values between or outside those
    endpoints use the same linear interval, clamped to the configured range.
    ``None`` is returned when the template does not contain a complete
    page/width range and the caller should keep its existing width rule.
    """
    source = dict(fields or {})
    options = source.get("page_count_options")
    if not options:
        options = source.get("page_count_arr")
    if not options:
        options = source.get("page_counts")
    if not options:
        options = source.get("page_count_options_json")
    if not options:
        options = source.get("页数") or source.get("页数范围")
    if isinstance(options, str):
        text = options.strip()
        if text:
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            options = decoded if isinstance(decoded, (list, tuple)) else re.findall(
                r"\d+(?:\.\d+)?", text
            )
    if isinstance(options, dict):
        options = (
            options.get("options")
            or options.get("values")
            or options.get("page_counts")
            or options.get("page_count_options")
            or []
        )
    if not isinstance(options, (list, tuple)):
        options = []
    page_values = []
    for value in options:
        if isinstance(value, dict):
            value = (
                value.get("page_count")
                or value.get("pages")
                or value.get("value")
                or value.get("count")
            )
        try:
            number = float(value)
        except (TypeError, ValueError):
            match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
            if not match:
                continue
            number = float(match.group(0))
        if number >= 0:
            page_values.append(number)
    explicit_min = source.get("min_page_count")
    explicit_max = source.get("max_page_count")
    for value, target in ((explicit_min, page_values), (explicit_max, page_values)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            target.append(number)
    if len(page_values) < 2:
        return None
    page_min = min(page_values)
    page_max = max(page_values)
    try:
        requested = float(page_count)
    except (TypeError, ValueError):
        return None
    try:
        width_min = float(
            source.get("min_spine_width", source.get("最小背脊宽"))
        )
        width_max = float(
            source.get("max_spine_width", source.get("最大背脊宽"))
        )
    except (TypeError, ValueError):
        return None
    # A zero/zero pair is the database default for templates that do not
    # configure page-dependent spine widths. Keep the option's existing width
    # in that case instead of silently replacing it with zero.
    if (
        width_min < 0
        or width_max <= 0
        or width_max < width_min
        or page_max <= page_min
    ):
        return None
    ratio = (requested - page_min) / (page_max - page_min)
    ratio = min(1.0, max(0.0, ratio))
    width = width_min + (width_max - width_min) * ratio
    return width, {
        "method": "linear_page_range",
        "requested_page_count": requested,
        "min_page_count": page_min,
        "max_page_count": page_max,
        "min_spine_width": width_min,
        "max_spine_width": width_max,
        "ratio": ratio,
        "spine_width": width,
    }


def apply_selected_size_variant(template: dict[str, Any]) -> dict[str, Any]:
    """Overlay the selected flat option onto a render template."""
    if not isinstance(template, dict):
        return template
    draft = template.get("size_template")
    spec = draft.get("spec") if isinstance(draft, dict) else None
    if not isinstance(spec, dict):
        spec = template.get("size_spec")
    if not isinstance(spec, dict):
        template_fields = template.get("fields")
        spec = template_fields.get("size_spec") if isinstance(template_fields, dict) else None
    option = selected_size_option(spec)
    if option is None:
        return template
    variant = {key: deepcopy(option[key]) for key in SIZE_VARIANT_FIELDS if key in option}
    fields = dict(template.get("fields") or {})
    fields.update(variant)
    fields["size_spec"] = normalize_size_spec(spec)
    template.update(variant)
    template["fields"] = fields
    template["size_spec"] = fields["size_spec"]
    return template


def build_layout_scaling(
    option: dict[str, Any],
    reference_canvas: dict[str, Any],
) -> dict[str, Any]:
    """Build transient render metadata; never store it in size_options."""
    flat = normalize_size_options([option])[0] if isinstance(option, dict) else {}
    width = _number(flat.get("single_side_width"), 0)
    height = _number(flat.get("single_side_height"), 0)
    bleed = _number(flat.get("bleed"), 0)
    spine = resolve_spine_width(flat)
    unit = required_size_unit(flat.get("size_unit"))
    dpi = _number(reference_canvas.get("dpi"), 300)
    unit_to_inches = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4}.get(unit, 1.0)
    spine_bleed = _number(flat.get("spine_bleed"), 0)
    target_width = (
        width * 2 + spine + spine_bleed * 2 + bleed * 2
    ) * unit_to_inches * dpi
    target_height = (height + bleed * 2) * unit_to_inches * dpi
    if target_width <= 0 or target_height <= 0:
        return {
            "mode": "uniform",
            "status": "needs_values",
            "reference_canvas": dict(reference_canvas or {}),
        }
    return {
        "mode": "uniform",
        "status": "ready",
        "reference_canvas": dict(reference_canvas or {}),
        "target_canvas": {
            "width_px": round(target_width, 4),
            "height_px": round(target_height, 4),
            "dpi": dpi,
        },
        "expression": (
            "min(target_canvas.width_px / reference_canvas.width_px, "
            "target_canvas.height_px / reference_canvas.height_px)"
        ),
    }


def size_values_for_unit(fields: dict[str, Any], unit: str) -> dict[str, float]:
    """Convert transiently for rendering without stored unit maps."""
    target_unit = required_size_unit(unit)
    source_unit = required_size_unit(fields.get("size_unit"))
    source_factor = {"in": 1.0, "mm": 25.4, "cm": 2.54}[source_unit]
    target_factor = {"in": 1.0, "mm": 25.4, "cm": 2.54}[target_unit]
    return {
        key: _number(fields.get(key), 0) / source_factor * target_factor
        for key in SIZE_VALUE_FIELDS
    }


def _flatten_option(
    raw: dict[str, Any],
    *,
    index: int,
    partial: bool,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    nested = raw.get("fields")
    if isinstance(nested, dict):
        sources.append(nested)
    sources.append(raw)
    option: dict[str, Any] = {}

    raw_id = raw.get("id", raw.get("value"))
    if raw_id not in (None, ""):
        option["id"] = str(raw_id)
    elif not partial:
        label_for_id = raw.get("label") or raw.get("name") or f"size-{index + 1}"
        option["id"] = _slug(label_for_id, f"size-{index + 1}")
    if "label" in raw or "name" in raw or not partial:
        option["label"] = str(
            raw.get("label") or raw.get("name") or option.get("id") or f"size-{index + 1}"
        ).strip()
    if "select" in raw:
        option["select"] = _boolean(raw.get("select"))
    elif not partial:
        option["select"] = False
    if "layers" in raw:
        if raw.get("layers") is not None and not isinstance(raw.get("layers"), dict):
            raise ValueError("layers 必须是完整图层文档对象")
        if raw.get("layers") is not None:
            option["layers"] = deepcopy(raw["layers"])

    unit = next(
        (
            str(source.get("size_unit")).strip().lower()
            for source in reversed(sources)
            if source.get("size_unit") not in (None, "")
        ),
        "",
    )
    if unit and unit not in SIZE_UNITS:
        raise ValueError("size_unit 只能是 in、mm 或 cm")
    if unit:
        option["size_unit"] = unit
    elif not partial:
        raise ValueError("size_unit 不能为空，拒绝使用默认单位")

    legacy_unit_payload: dict[str, Any] = {}
    if unit:
        for source in sources:
            candidate = source.get(unit)
            if isinstance(candidate, dict):
                legacy_unit_payload.update(candidate)
    for key in SIZE_VALUE_FIELDS:
        found = False
        for source in sources:
            if key in source:
                option[key] = source[key]
                found = True
        if not found and key in legacy_unit_payload:
            option[key] = legacy_unit_payload[key]

    for key in ("spine_width_mode", "spine_width_formula"):
        for source in sources:
            if key in source:
                option[key] = deepcopy(source[key])
    if option.get("spine_width_mode") in (None, "", "fixed"):
        option.pop("spine_width_mode", None)
        option.pop("spine_width_formula", None)
    elif option["spine_width_mode"] != "by_page_count":
        raise ValueError("spine_width_mode 只能是 fixed 或 by_page_count")

    if not partial:
        for key in (
            "single_side_width",
            "single_side_height",
            "bleed",
            "spine_bleed",
        ):
            option.setdefault(key, 0)
        if option.get("spine_width_mode") != "by_page_count":
            option.setdefault("spine_width", 0)
    return option


def _has_required_values(option: dict[str, Any]) -> bool:
    return (
        _number(option.get("single_side_width"), 0) > 0
        and _number(option.get("single_side_height"), 0) > 0
    )


def _page_count_arr(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number >= 0 and number not in result:
            result.append(number)
    return result


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    result = "".join(char if char.isalnum() else "-" for char in text).strip("-")
    return result or fallback
