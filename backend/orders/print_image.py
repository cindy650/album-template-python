from __future__ import annotations

from base64 import b64encode
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
import requests


class DeepSeekProductInformationTranslator:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def translate(self, product_information: dict[str, Any]) -> list[str]:
        source_lines = self._source_lines(product_information)
        if not source_lines:
            return []
        if not re.search(r"[A-Za-z]", " ".join(source_lines)):
            return source_lines
        if not self.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法翻译商品信息")

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
                            "你是印刷订单翻译助手。把每条英文商品信息翻译成简洁、"
                            "准确的中文。保留姓名、日期、数量、尺寸、色号、字体编号、"
                            "专有名词和无法确定含义的代码，不补充原文没有的信息。"
                            "只返回 JSON，不要 Markdown。返回格式必须是："
                            '{"translations":["第一条", "第二条"]}。'
                            "译文数量和顺序必须与输入完全一致。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"product_information": source_lines},
                            ensure_ascii=False,
                        ),
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
            payload = json.loads(content)
            translations = payload["translations"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek 返回的商品信息译文格式不正确") from exc
        if (
            not isinstance(translations, list)
            or len(translations) != len(source_lines)
            or not all(isinstance(line, str) for line in translations)
        ):
            raise ValueError("DeepSeek 返回的商品信息译文数量或类型不正确")
        return [line.strip() for line in translations]

    @staticmethod
    def _source_lines(product_information: dict[str, Any]) -> list[str]:
        return [
            f"{str(label).strip()}: {str(value).strip()}"
            for label, value in (product_information or {}).items()
            if str(label).strip() and str(value).strip()
        ]


class OrderPrintImageGenerator:
    TABLE_ROWS = (
        "尺寸",
        "皮码",
        "字体",
        "文字",
        "工艺",
        "烫金颜色",
        "内页工艺",
        "内页数量",
        "外观",
    )

    def __init__(
        self,
        order_repository,
        template_image_generator,
        translator,
        dpi: int = 300,
        jpeg_quality: int = 92,
    ):
        self.order_repository = order_repository
        self.template_image_generator = template_image_generator
        self.translator = translator
        self.dpi = int(dpi)
        self.jpeg_quality = int(jpeg_quality)
        if self.dpi <= 0:
            raise ValueError("A4 图片 DPI 必须大于 0")

    def generate(self, order_id: int, order_number: str) -> dict[str, Any]:
        order = self.order_repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        translated_lines = self.translator.translate(
            order.get("product_information") or {}
        )
        preview_path = self._find_or_generate_preview(order)
        image = self._render(order, translated_lines, preview_path)

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            subsampling=0,
            dpi=(self.dpi, self.dpi),
            optimize=True,
        )
        encoded = b64encode(output.getvalue()).decode("ascii")
        return {
            "order_id": order["id"],
            "order_number": order["order_number"],
            "image_base64": encoded,
            "mime_type": "image/jpeg",
            "encoding": "base64",
            "dpi": self.dpi,
            "pixel_width": image.width,
            "pixel_height": image.height,
        }

    def _find_or_generate_preview(self, order: dict[str, Any]) -> Path:
        output_dir = Path(self.template_image_generator.output_dir)
        normalized = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            str(order.get("order_number") or "order"),
        ).strip("._-") or "order"
        candidates = sorted(
            output_dir.glob(f"{normalized}-{order['id']}-*.jpg"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]

        payload = self.order_repository.order_payload(order["id"])
        result = self.template_image_generator.generate_for_order(
            payload,
            saved_order=order,
        )
        return Path(result["path"])

    def _render(
        self,
        order: dict[str, Any],
        translated_lines: list[str],
        preview_path: Path,
    ) -> Image.Image:
        width = round(210 / 25.4 * self.dpi)
        height = round(297 / 25.4 * self.dpi)
        scale = self.dpi / 300
        px = lambda value: max(1, round(value * scale))

        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        ink = "#222222"
        muted = "#6a6a6a"
        red = "#b43b3b"
        line = "#444444"
        margin = px(130)
        left_width = px(940)
        gap = px(90)
        right_x = margin + left_width + gap
        right_width = width - margin - right_x

        title_font = self._font(px(46), bold=True)
        heading_font = self._font(px(29), bold=True)
        body_font = self._font(px(29))
        small_font = self._font(px(24))
        red_font = self._font(px(36), bold=True)
        shop_font = self._font(px(68), bold=True)

        y = px(145)
        draw.text(
            (margin, y),
            f"Order #{order.get('order_number') or ''}",
            font=title_font,
            fill=ink,
        )
        y += px(65)
        y = self._draw_block(
            draw,
            order.get("product") or "",
            (margin, y),
            left_width,
            body_font,
            muted,
            px(39),
            max_lines=3,
        )

        y += px(46)
        draw.text((margin, y), "Ship to", font=heading_font, fill=ink)
        y += px(43)
        y = self._draw_block(
            draw,
            order.get("shipping_address") or "",
            (margin, y),
            left_width,
            body_font,
            ink,
            px(40),
            max_lines=7,
        )
        y += px(45)
        draw.text((margin, y), "Shop", font=heading_font, fill=ink)
        y += px(42)
        draw.text(
            (margin, y),
            order.get("shop") or "",
            font=body_font,
            fill=ink,
        )
        y += px(72)
        draw.text((margin, y), "Order date", font=heading_font, fill=ink)
        y += px(42)
        draw.text(
            (margin, y),
            self._format_date(order.get("created_at")),
            font=body_font,
            fill=ink,
        )
        y += px(72)
        draw.text((margin, y), "Payment method", font=heading_font, fill=ink)
        y += px(42)
        self._draw_block(
            draw,
            order.get("payment_method") or "",
            (margin, y),
            left_width,
            body_font,
            ink,
            px(40),
            max_lines=2,
        )

        shop_label = order.get("shop_name") or order.get("shop") or ""
        shop_bbox = draw.textbbox((0, 0), shop_label, font=shop_font)
        draw.text(
            (width - margin - (shop_bbox[2] - shop_bbox[0]), px(145)),
            shop_label,
            font=shop_font,
            fill=red,
        )
        draw.text(
            (right_x, px(265)),
            f"{order.get('quantity') or 1} item",
            font=heading_font,
            fill=ink,
        )
        draw.line(
            (right_x, px(315), width - margin, px(315)),
            fill=line,
            width=px(2),
        )
        right_y = px(350)
        right_y = self._draw_block(
            draw,
            order.get("product") or "",
            (right_x, right_y),
            right_width,
            heading_font,
            ink,
            px(42),
            max_lines=3,
        )
        right_y += px(20)
        original_lines = DeepSeekProductInformationTranslator._source_lines(
            order.get("product_information") or {}
        )
        for index, text in enumerate(original_lines, start=1):
            available_lines = int((px(1350) - right_y) // px(39))
            if available_lines <= 0:
                break
            right_y = self._draw_block(
                draw,
                f"{index}. {text}",
                (right_x, right_y),
                right_width,
                body_font,
                ink,
                px(39),
                max_lines=min(3, available_lines),
            )
            right_y += px(7)
        draw.line(
            (right_x, px(1400), width - margin, px(1400)),
            fill=line,
            width=px(2),
        )

        translation_y = px(1490)
        for index, text in enumerate(translated_lines, start=1):
            available_lines = int((px(2210) - translation_y) // px(49))
            if available_lines <= 0:
                break
            translation_y = self._draw_block(
                draw,
                f"{index}. {text}",
                (margin, translation_y),
                left_width,
                red_font,
                red,
                px(49),
                max_lines=min(3, available_lines),
            )
            translation_y += px(4)

        preview_box = (
            margin,
            px(2300),
            margin + left_width,
            px(3190),
        )
        self._paste_preview(image, preview_path, preview_box)
        self._draw_check_table(
            draw,
            (right_x, px(1850), width - margin, px(3190)),
            body_font,
            heading_font,
            line,
            px(2),
        )

        draw.line(
            (margin, px(3270), width - margin, px(3270)),
            fill=line,
            width=px(2),
        )
        draw.text(
            (margin, px(3310)),
            f"Order #{order.get('order_number') or ''}",
            font=small_font,
            fill=muted,
        )
        return image

    @classmethod
    def _draw_check_table(
        cls,
        draw,
        box,
        body_font,
        heading_font,
        fill,
        line_width,
    ):
        x1, y1, x2, y2 = box
        headers = ("项目名称", "工艺制作", "装订制作", "发货检验")
        row_count = len(cls.TABLE_ROWS) + 1
        row_height = (y2 - y1) / row_count
        first_col = (x2 - x1) * 0.28
        other_col = (x2 - x1 - first_col) / 3
        columns = (x1, x1 + first_col, x1 + first_col + other_col,
                   x1 + first_col + other_col * 2, x2)
        for x in columns:
            draw.line((x, y1, x, y2), fill=fill, width=line_width)
        for row_index in range(row_count + 1):
            y = y1 + row_height * row_index
            draw.line((x1, y, x2, y), fill=fill, width=line_width)
        for column_index, header in enumerate(headers):
            cls._draw_centered(draw, header, columns[column_index], columns[column_index + 1], y1, y1 + row_height, heading_font, fill)
        for row_index, label in enumerate(cls.TABLE_ROWS, start=1):
            cls._draw_centered(draw, label, columns[0], columns[1], y1 + row_height * row_index, y1 + row_height * (row_index + 1), body_font, fill)

    @staticmethod
    def _draw_centered(draw, text, x1, x2, y1, y2, font, fill):
        draw.text(
            ((x1 + x2) / 2, (y1 + y2) / 2),
            text,
            font=font,
            fill=fill,
            anchor="mm",
        )

    @staticmethod
    def _paste_preview(image: Image.Image, path: Path, box):
        x1, y1, x2, y2 = box
        with Image.open(path) as source:
            preview = ImageOps.contain(
                source.convert("RGB"),
                (x2 - x1, y2 - y1),
                Image.Resampling.LANCZOS,
            )
        x = round(x1 + (x2 - x1 - preview.width) / 2)
        y = round(y1 + (y2 - y1 - preview.height) / 2)
        image.paste(preview, (x, y))

    @classmethod
    def _font(cls, size: int, bold: bool = False):
        filenames = (
            ("msyhbd.ttc", "simhei.ttf", "arialbd.ttf")
            if bold
            else ("msyh.ttc", "simsun.ttc", "arial.ttf")
        )
        candidates = [Path("C:/Windows/Fonts") / name for name in filenames]
        candidates.extend(
            [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            ]
        )
        for candidate in candidates:
            if candidate.exists():
                try:
                    return ImageFont.truetype(str(candidate), size)
                except OSError:
                    continue
        return ImageFont.load_default(size=size)

    @classmethod
    def _draw_block(
        cls,
        draw,
        text,
        position,
        max_width,
        font,
        fill,
        line_height,
        max_lines,
    ):
        x, y = position
        lines = cls._wrap_text(draw, str(text or ""), font, max_width)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = cls._ellipsize(draw, lines[-1], font, max_width)
        for line_text in lines:
            draw.text((x, y), line_text, font=font, fill=fill)
            y += line_height
        return y

    @staticmethod
    def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
        lines = []
        for paragraph in text.replace("\r", "").split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current = ""
            for character in paragraph:
                candidate = current + character
                if current and draw.textlength(candidate, font=font) > max_width:
                    lines.append(current.rstrip())
                    current = character.lstrip()
                else:
                    current = candidate
            if current or not lines:
                lines.append(current.rstrip())
        return lines or [""]

    @staticmethod
    def _ellipsize(draw, text: str, font, max_width: int) -> str:
        suffix = "..."
        text = text.rstrip()
        while text and draw.textlength(text + suffix, font=font) > max_width:
            text = text[:-1]
        return text + suffix

    @staticmethod
    def _format_date(value: Any) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return parsed.strftime("%b %d, %Y")
