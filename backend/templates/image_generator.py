from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
import re
from typing import Any

from backend.templates.repository import TemplateRepository


SVG_NS = "http://www.w3.org/2000/svg"


class TemplateImageGenerator:
    """Generate printable cover preview images from template dimensions."""

    def __init__(
        self,
        repository: TemplateRepository,
        output_dir: Path,
        dpi: int = 300,
        template_id: int | None = None,
    ):
        self.repository = repository
        self.output_dir = Path(output_dir)
        self.dpi = int(dpi)
        self.template_id = template_id

    def generate_for_order(
        self,
        order: dict[str, Any],
        saved_order: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        template = self.repository.get_default(self.template_id)
        if template is None:
            raise RuntimeError("模板数据库中没有可用于生成图片的模板")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        order_number = str(
            order.get("订单号")
            or (saved_order or {}).get("order_number")
            or "order"
        )
        order_id = (saved_order or {}).get("id")
        filename = self._filename(order_number, order_id)
        path = self.output_dir / filename
        svg = self._render_svg(template, order, metadata or {})
        path.write_text(svg, encoding="utf-8")

        dimensions = self._template_dimensions(template)
        return {
            "ok": True,
            "format": "svg",
            "path": str(path),
            "template_id": template["id"],
            "dpi": self.dpi,
            "width": dimensions["total_width"],
            "height": dimensions["total_height"],
            "unit": template["size_unit"],
        }

    @staticmethod
    def _filename(order_number: str, order_id: Any):
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", order_number).strip("._-")
        if not normalized:
            normalized = "order"
        suffix = f"-{order_id}" if order_id else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{normalized}{suffix}-{timestamp}.svg"

    @staticmethod
    def _number(template: dict[str, Any], key: str):
        value = template.get(key)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"模板字段 {key} 不是有效数字：{value!r}") from exc

    def _template_dimensions(self, template: dict[str, Any]):
        side_width = self._number(template, "single_side_width")
        side_height = self._number(template, "single_side_height")
        bleed = self._number(template, "bleed")
        spine_width = self._number(template, "spine_width")
        spine_bleed = self._number(template, "spine_bleed")
        total_width = side_width * 2 + spine_width + bleed * 2
        total_height = side_height + bleed * 2
        return {
            "side_width": side_width,
            "side_height": side_height,
            "bleed": bleed,
            "spine_width": spine_width,
            "spine_bleed": spine_bleed,
            "total_width": total_width,
            "total_height": total_height,
        }

    def _render_svg(
        self,
        template: dict[str, Any],
        order: dict[str, Any],
        metadata: dict[str, Any],
    ):
        dimensions = self._template_dimensions(template)
        unit = str(template.get("size_unit") or "in")
        scale = self.dpi
        total_width = dimensions["total_width"]
        total_height = dimensions["total_height"]
        canvas_w = total_width * scale
        canvas_h = total_height * scale
        bleed = dimensions["bleed"] * scale
        side_w = dimensions["side_width"] * scale
        side_h = dimensions["side_height"] * scale
        spine_w = dimensions["spine_width"] * scale
        spine_bleed = dimensions["spine_bleed"] * scale

        trim_x = bleed
        trim_y = bleed
        back_x = trim_x
        spine_x = trim_x + side_w
        cover_x = spine_x + spine_w
        content_y = trim_y
        trim_w = side_w * 2 + spine_w

        spine_safe_left = max(trim_x, spine_x - spine_bleed)
        spine_safe_right = min(trim_x + trim_w, spine_x + spine_w + spine_bleed)

        front_center_x = cover_x + side_w / 2
        back_center_x = back_x + side_w / 2
        spine_center_x = spine_x + spine_w / 2

        title = "Wedding Guest Book"
        names = "新人姓名"
        date = "2026.10.01"
        subtitle = "Our Wedding Day"
        back_text = "Forever & Always"
        spine_top = "Guest Book"
        spine_middle = "8x12"
        spine_bottom = "2026"

        dark = "#26364f"
        light_blue = "#77c9df"
        orange = "#f2b47f"
        paper = "#fbf7f0"

        def text_line(x, y, text, size, weight="400", anchor="middle"):
            return (
                f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" '
                f'font-family="Georgia, \'Times New Roman\', SimSun, serif" '
                f'font-size="{size:.2f}" font-weight="{weight}" '
                f'fill="{dark}">{escape(text)}</text>'
            )

        def rotated_text(x, y, text, size):
            return (
                f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="middle" '
                f'font-family="Georgia, \'Times New Roman\', SimSun, serif" '
                f'font-size="{size:.2f}" fill="{dark}" '
                f'transform="rotate(90 {x:.2f} {y:.2f})">{escape(text)}</text>'
            )

        font_title = side_h * 0.055
        font_body = side_h * 0.032
        font_small = side_h * 0.024
        font_spine = max(7.0, min(spine_w * 0.34, side_h * 0.03))

        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            (
                f'<svg xmlns="{SVG_NS}" width="{total_width:.4f}{unit}" '
                f'height="{total_height:.4f}{unit}" '
                f'viewBox="0 0 {canvas_w:.2f} {canvas_h:.2f}" '
                f'role="img" aria-label="Wedding guest book cover preview">'
            ),
            f'<metadata>Generated at {datetime.now(timezone.utc).isoformat()}</metadata>',
            f'<rect x="0" y="0" width="{canvas_w:.2f}" height="{canvas_h:.2f}" fill="{paper}"/>',
            (
                f'<rect x="{trim_x:.2f}" y="{trim_y:.2f}" width="{trim_w:.2f}" '
                f'height="{side_h:.2f}" fill="none" stroke="{light_blue}" '
                f'stroke-width="2"/>'
            ),
            (
                f'<rect x="{bleed / 2:.2f}" y="{bleed / 2:.2f}" '
                f'width="{canvas_w - bleed:.2f}" height="{canvas_h - bleed:.2f}" '
                f'fill="none" stroke="{orange}" stroke-width="2" '
                f'stroke-dasharray="14 10"/>'
            ),
            (
                f'<line x1="{spine_x:.2f}" y1="{content_y:.2f}" '
                f'x2="{spine_x:.2f}" y2="{content_y + side_h:.2f}" '
                f'stroke="{light_blue}" stroke-width="2" stroke-dasharray="8 8"/>'
            ),
            (
                f'<line x1="{spine_x + spine_w:.2f}" y1="{content_y:.2f}" '
                f'x2="{spine_x + spine_w:.2f}" y2="{content_y + side_h:.2f}" '
                f'stroke="{light_blue}" stroke-width="2" stroke-dasharray="8 8"/>'
            ),
            (
                f'<line x1="{spine_safe_left:.2f}" y1="{content_y:.2f}" '
                f'x2="{spine_safe_left:.2f}" y2="{content_y + side_h:.2f}" '
                f'stroke="{light_blue}" stroke-width="1" stroke-dasharray="4 8" opacity="0.55"/>'
            ),
            (
                f'<line x1="{spine_safe_right:.2f}" y1="{content_y:.2f}" '
                f'x2="{spine_safe_right:.2f}" y2="{content_y + side_h:.2f}" '
                f'stroke="{light_blue}" stroke-width="1" stroke-dasharray="4 8" opacity="0.55"/>'
            ),
            text_line(back_center_x, content_y + side_h * 0.52, back_text, font_body),
            text_line(front_center_x, content_y + side_h * 0.18, title, font_title),
            text_line(front_center_x, content_y + side_h * 0.43, f"{names} | {date}", font_small, weight="700"),
            text_line(front_center_x, content_y + side_h * 0.62, subtitle, font_body),
            rotated_text(spine_center_x, content_y + side_h * 0.14, spine_top, font_spine),
            rotated_text(spine_center_x, content_y + side_h * 0.55, spine_middle, font_spine),
            rotated_text(spine_center_x, content_y + side_h * 0.86, spine_bottom, font_spine),
            text_line(back_center_x, canvas_h - bleed * 0.18, "封底", font_small),
            text_line(spine_center_x, canvas_h - bleed * 0.18, "背脊", font_small),
            text_line(front_center_x, canvas_h - bleed * 0.18, "封面", font_small),
            "</svg>",
        ]
        return "\n".join(parts)
