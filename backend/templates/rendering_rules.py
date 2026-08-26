"""Code-level contract for converting Fabric layer JSON to output pixels.

Orders keep only matched template and resolved layer JSON. This module is the
stable interpretation used by all renderers and is not serialized into orders.
"""

from __future__ import annotations

from typing import Any


LAYER_RENDERING_RULES_VERSION = "fabric-css-px-to-output-px-v3"


FABRIC_LAYER_CONTRACT = {
    "coordinates": "original Fabric scene px; crop origin comes from serialized workarea",
    "text_origin": "center",
    "image_origin": "originX/originY (center in current Fabric export)",
    "rect_origin": "originX/originY (center in current Fabric export)",
    "rotation": "clockwise_degrees_about_unrotated_center",
    "flip_order": "flip_then_rotate",
}

FONT_CONVERSION_CONTRACT = {
    "css_font_size": "fontSize * abs(scaleY)",
    "output_font_size": "css_font_size * vertical_reference_scale",
    "fit_order": "fit_unrotated_text_then_rotate",
    "scale_mode": "uniform",
    "char_spacing": "Fabric 1/1000 em",
    "word_spacing": "CSS px",
}


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fabric_layer_geometry(element: dict[str, Any]) -> dict[str, Any]:
    """Convert one Fabric object into renderer-neutral reference geometry."""
    if not isinstance(element, dict):
        raise TypeError("Fabric 图层必须是对象")
    try:
        left = float(element["left"])
        top = float(element["top"])
        width = abs(float(element["width"]))
        height = abs(float(element["height"]))
        scale_x = abs(float(element.get("scaleX", 1) or 1))
        scale_y = abs(float(element.get("scaleY", 1) or 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Fabric 图层缺少有效 left/top/width/height") from exc
    if width <= 0 or height <= 0:
        raise ValueError("Fabric 图层 width/height 必须大于 0")
    element_type = str(element.get("type") or "").strip().lower()
    default_origin = "center" if element_type in {"text", "textbox", "i-text", "itext"} else "left"
    origin_x = str(element.get("originX") or default_origin).strip().lower()
    origin_y = str(element.get("originY") or default_origin).strip().lower()
    center_x = left if origin_x in {"center", "middle"} else left + width * scale_x / 2
    center_y = top if origin_y in {"center", "middle"} else top + height * scale_y / 2
    transform = element.get("transform") or {}
    try:
        angle = float(transform.get("rotation_deg", transform.get("rotation", element.get("angle", 0))) or 0)
    except (TypeError, ValueError):
        angle = 0.0
    return {
        "type": element_type,
        "center_x": center_x,
        "center_y": center_y,
        "width": width * scale_x,
        "height": height * scale_y,
        "angle": angle,
        "flip_x": bool(transform.get("flip_horizontal", transform.get("flipX", element.get("flipX", False)))),
        "flip_y": bool(transform.get("flip_vertical", transform.get("flipY", element.get("flipY", False)))),
        "font_size": _number_or_none(element.get("fontSize")),
        "scale_x": scale_x,
        "scale_y": scale_y,
    }
