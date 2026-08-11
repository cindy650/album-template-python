from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from backend.catalog import CatalogRepository


UNIT_TO_INCHES = {
    "in": 1.0,
    "cm": 1 / 2.54,
    "mm": 1 / 25.4,
}


class TemplateImageGenerator:
    """Generate JPEG cover previews using the dimensions stored in SQLite."""

    def __init__(
        self,
        repository: CatalogRepository,
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
            template = self.repository.find_render_template(
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
            self.output_dir.mkdir(parents=True, exist_ok=True)
            order_id = (saved_order or {}).get("id")
            path = self.output_dir / self._filename(order_number, order_id)

            print(f"[图片] 正在渲染：订单号={order_number}", flush=True)
            image, dimensions = self._render_jpeg(
                template,
                order,
                metadata or {},
            )
            print(f"[图片] 正在写入：{path}", flush=True)
            image.save(
                path,
                format="JPEG",
                quality=95,
                subsampling=0,
                dpi=(self.dpi, self.dpi),
                optimize=True,
            )
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
        }
        print(
            f"[图片] 生成成功：订单号={order_number}，路径={path}，"
            f"像素={image.width}x{image.height}",
            flush=True,
        )
        return result

    @staticmethod
    def _filename(order_number: str, order_id: Any):
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", order_number).strip("._-")
        if not normalized:
            normalized = "order"
        suffix = f"-{order_id}" if order_id else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        return f"{normalized}{suffix}-{timestamp}.jpg"

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
        side_width = self._number(template, "single_side_width")
        side_height = self._number(template, "single_side_height")
        bleed = self._number(template, "bleed")
        spine_width = self._number(template, "spine_width")
        spine_bleed = self._number(template, "spine_bleed")
        total_width = side_width * 2 + spine_width + bleed * 2
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
        unit = str(template.get("size_unit") or "in").strip().lower()
        inches = UNIT_TO_INCHES.get(unit)
        if inches is None:
            raise RuntimeError(f"模板单位不受支持：{unit!r}")
        return self.dpi * inches

    @staticmethod
    def _font_candidates():
        windows_fonts = Path("C:/Windows/Fonts")
        return (
            windows_fonts / "simsun.ttc",
            windows_fonts / "msyh.ttc",
            windows_fonts / "arial.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        )

    @classmethod
    def _font(cls, size: float):
        pixel_size = max(10, round(size))
        for candidate in cls._font_candidates():
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
        spine_x = trim_x + side_w
        cover_x = spine_x + spine_w
        content_y = trim_y
        trim_w = side_w * 2 + spine_w
        spine_safe_left = max(trim_x, spine_x - spine_bleed)
        spine_safe_right = min(trim_x + trim_w, spine_x + spine_w + spine_bleed)

        front_center_x = cover_x + side_w / 2
        back_center_x = back_x + side_w / 2
        spine_center_x = spine_x + spine_w / 2

        dark = "#26364f"
        light_blue = "#77c9df"
        orange = "#f2b47f"
        paper = "#fbf7f0"
        line_width = max(2, round(scale / 150))

        image = Image.new("RGB", (canvas_w, canvas_h), paper)
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (trim_x, trim_y, trim_x + trim_w, trim_y + side_h),
            outline=light_blue,
            width=line_width,
        )
        draw.rectangle(
            (bleed / 2, bleed / 2, canvas_w - bleed / 2, canvas_h - bleed / 2),
            outline=orange,
            width=line_width,
        )
        for x in (spine_x, spine_x + spine_w):
            draw.line(
                (x, content_y, x, content_y + side_h),
                fill=light_blue,
                width=line_width,
            )
        for x in (spine_safe_left, spine_safe_right):
            draw.line(
                (x, content_y, x, content_y + side_h),
                fill="#a7ddea",
                width=max(1, line_width // 2),
            )

        font_title = self._font(side_h * 0.055)
        font_body = self._font(side_h * 0.032)
        font_small = self._font(side_h * 0.024)
        font_spine = self._font(max(10, min(spine_w * 0.34, side_h * 0.03)))

        self._draw_centered_text(
            draw,
            (back_center_x, content_y + side_h * 0.52),
            "Forever & Always",
            font_body,
            dark,
        )
        self._draw_centered_text(
            draw,
            (front_center_x, content_y + side_h * 0.18),
            "Wedding Guest Book",
            font_title,
            dark,
        )
        self._draw_centered_text(
            draw,
            (front_center_x, content_y + side_h * 0.43),
            "新人姓名 | 2026.10.01",
            font_small,
            dark,
        )
        self._draw_centered_text(
            draw,
            (front_center_x, content_y + side_h * 0.62),
            "Our Wedding Day",
            font_body,
            dark,
        )
        self._draw_rotated_text(
            image,
            (spine_center_x, content_y + side_h * 0.14),
            "Guest Book",
            font_spine,
            dark,
        )
        self._draw_rotated_text(
            image,
            (spine_center_x, content_y + side_h * 0.55),
            "8x12",
            font_spine,
            dark,
        )
        self._draw_rotated_text(
            image,
            (spine_center_x, content_y + side_h * 0.86),
            "2026",
            font_spine,
            dark,
        )
        self._draw_centered_text(
            draw,
            (back_center_x, canvas_h - bleed * 0.18),
            "封底",
            font_small,
            dark,
        )
        self._draw_centered_text(
            draw,
            (spine_center_x, canvas_h - bleed * 0.18),
            "背脊",
            font_small,
            dark,
        )
        self._draw_centered_text(
            draw,
            (front_center_x, canvas_h - bleed * 0.18),
            "封面",
            font_small,
            dark,
        )
        return image, dimensions
