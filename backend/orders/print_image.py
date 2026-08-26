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

from backend.storage import (
    order_filename,
    order_resource_dir,
    order_resource_filename,
    safe_filename_part,
)


_USER_MESSAGE_LABEL_PATTERNS = (
    re.compile(r"\bmessage\b", re.IGNORECASE),
    re.compile(r"\bpersonalization\b|\bpersonalisation\b", re.IGNORECASE),
    re.compile(r"\binscription\b", re.IGNORECASE),
    re.compile(r"\b(?:buyer|customer|user)(?:'s)?\s+note\b", re.IGNORECASE),
    re.compile(r"\bcustom(?:ized|ised)?\s+(?:text|wording)\b", re.IGNORECASE),
    re.compile(r"\b(?:name|names|text)(?:\s*/[^:]*)?\s+for\s+the\s+cover\b", re.IGNORECASE),
)
_USER_MESSAGE_LABEL_WORDS = (
    "用户留言",
    "客户留言",
    "顾客留言",
    "买家留言",
    "用户备注",
    "客户备注",
    "顾客备注",
    "买家备注",
    "定制内容",
    "定制文字",
    "个性化内容",
    "刻字内容",
    "封面文字",
)


def is_user_message_label(label: Any) -> bool:
    """Return whether a product-information field contains buyer-entered text."""
    normalized = " ".join(str(label or "").strip().split())
    if not normalized:
        return False
    return any(word in normalized for word in _USER_MESSAGE_LABEL_WORDS) or any(
        pattern.search(normalized) for pattern in _USER_MESSAGE_LABEL_PATTERNS
    )


_NATURAL_MESSAGE_ENGLISH = re.compile(
    r"\b(?:please|kindly|i(?:'ll|'d|'m| am| would| want| need)?|"
    r"we(?:'ll|'d|'re| are| would| want| need)?|can you|could you|would you|"
    r"do not|don't|if|leave|use|send|sending|exclude|include|add|remove|"
    r"keep|place|put|change|make|should|will)\b",
    re.IGNORECASE,
)
_NATURAL_MESSAGE_CHINESE = re.compile(
    r"(?:请|麻烦|希望|可以|能否|不要|无需|需要|想要|帮我|我们|如果|"
    r"请把|请用|保留|去掉|排除|添加|修改|放在|改成|留下)",
)


def is_natural_language_user_message(value: Any) -> bool:
    """Return whether a value looks like a buyer's free-form request.

    Structured choices such as colors, sizes, names, and dates must not be
    highlighted just because their field label contains "message" or
    "personalization". DeepSeek indexes are filtered through this check too.
    """
    normalized = " ".join(str(value or "").replace("\r", " ").split())
    if not normalized:
        return False
    latin_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", normalized)
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
    if latin_words and len(latin_words) >= 2 and _NATURAL_MESSAGE_ENGLISH.search(normalized):
        return True
    if len(chinese_chars) >= 4 and _NATURAL_MESSAGE_CHINESE.search(normalized):
        return True
    # For other languages, a long punctuated sentence is a safer signal than
    # a short option value such as a color, size, date, or person's name.
    return len(latin_words) >= 8 and bool(re.search(r"[,.;:!?]", normalized))


def product_information_entries(
    product_information: dict[str, Any],
    user_message_indexes: set[int] | None = None,
) -> list[tuple[str, str, bool]]:
    entries = []
    for label, value in (product_information or {}).items():
        label_text = str(label).strip()
        value_text = str(value).strip()
        if label_text and value_text:
            index = len(entries) + 1
            entries.append((
                label_text,
                value_text,
                is_natural_language_user_message(value_text)
                and (
                    is_user_message_label(label_text)
                    or index in (user_message_indexes or set())
                ),
            ))
    return entries


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
        translations, _ = self.analyze(product_information)
        return translations

    def analyze(
        self,
        product_information: dict[str, Any],
    ) -> tuple[list[str], set[int]]:
        source_lines = self._source_lines(product_information)
        if not source_lines:
            return [], set()
        fallback_indexes = self._fallback_user_message_indexes(product_information)
        if not self.api_key:
            if re.search(r"[A-Za-z]", " ".join(source_lines)):
                raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法翻译商品信息")
            return source_lines, fallback_indexes

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
                            "同时识别哪些条目是买家直接写下的自然语言留言或完整请求，"
                            "例如请使用我发送的 logo、请把日期放在底部、不要显示姓名。"
                            "只有自然语言句子或明确请求才算 user_message；颜色、尺寸、"
                            "页数、字体、姓名、日期、地点、代码、单词短语和选项值都不是。"
                            "只返回 JSON，不要 Markdown。返回格式必须是："
                            '{"translations":["第一条", "第二条"],'
                            '"user_message_indexes":[2]}。索引从 1 开始。'
                            "译文数量和顺序必须与输入完全一致。每条译文只能是单行文本，"
                            "不要添加序号、换行、项目符号或省略号。"
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
            indexes = payload.get("user_message_indexes", [])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek 返回的商品信息译文格式不正确") from exc
        if (
            not isinstance(translations, list)
            or len(translations) != len(source_lines)
            or not all(isinstance(line, str) for line in translations)
        ):
            raise ValueError("DeepSeek 返回的商品信息译文数量或类型不正确")
        if not isinstance(indexes, list) or not all(
            isinstance(index, int) and not isinstance(index, bool)
            for index in indexes
        ):
            raise ValueError("DeepSeek 返回的用户留言索引格式不正确")
        values = [
            str(value).strip()
            for label, value in (product_information or {}).items()
            if str(label).strip() and str(value).strip()
        ]
        valid_indexes = {
            index
            for index in indexes
            if 1 <= index <= len(source_lines)
            and index <= len(values)
            and is_natural_language_user_message(values[index - 1])
        }
        return [line.strip() for line in translations], (
            valid_indexes | fallback_indexes
        )

    def identify_user_message_indexes(
        self,
        product_information: dict[str, Any],
    ) -> set[int]:
        source_lines = self._source_lines(product_information)
        fallback_indexes = self._fallback_user_message_indexes(product_information)
        if not source_lines or not self.api_key:
            return fallback_indexes
        _, indexes = self.analyze(product_information)
        return indexes

    @staticmethod
    def _fallback_user_message_indexes(
        product_information: dict[str, Any],
    ) -> set[int]:
        return {
            index
            for index, (_, _, is_user_message) in enumerate(
                product_information_entries(product_information),
                start=1,
            )
            if is_user_message
        }

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
        output_dir: Path | None = None,
        storage_service=None,
    ):
        self.order_repository = order_repository
        self.template_image_generator = template_image_generator
        self.translator = translator
        self.dpi = int(dpi)
        self.jpeg_quality = int(jpeg_quality)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.storage_service = storage_service
        if self.dpi <= 0:
            raise ValueError("A4 图片 DPI 必须大于 0")

    def generate(
        self,
        order_id: int,
        order_number: str,
        upload_to_oss: bool = True,
        preview_path: str | Path | None = None,
    ) -> dict[str, Any]:
        order = self.order_repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        translated_lines, user_message_indexes = self._analyze_product_information(
            order.get("product_information") or {}
        )
        preview_path = self._find_or_generate_preview(order, preview_path=preview_path)
        image = self._render(
            order,
            translated_lines,
            preview_path,
            user_message_indexes=user_message_indexes,
        )

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            subsampling=0,
            dpi=(self.dpi, self.dpi),
            optimize=True,
        )
        image_bytes = output.getvalue()
        filename = self._filename(order)
        result = {
            "order_id": order["id"],
            "order_number": order["order_number"],
            "filename": filename,
            "image_base64": b64encode(image_bytes).decode("ascii"),
            "mime_type": "image/jpeg",
            "encoding": "base64",
            "dpi": self.dpi,
            "pixel_width": image.width,
            "pixel_height": image.height,
        }
        if self.output_dir is not None:
            if self.storage_service is not None and upload_to_oss:
                saved = self.storage_service.save_bytes(
                    order_resource_dir(self.output_dir, order),
                    filename,
                    image_bytes,
                )
                result["path"] = saved["path"]
                result["oss"] = saved["oss"]
            else:
                output_dir = order_resource_dir(self.output_dir, order)
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir / filename
                path.write_bytes(image_bytes)
                result["path"] = str(path)
                print(
                    f"[FILE] A4 生产单本地保存成功：文件={filename}，路径={path}",
                    flush=True,
                )
        return result

    def _analyze_product_information(
        self,
        product_information: dict[str, Any],
    ) -> tuple[list[str], set[int]]:
        analyze = getattr(self.translator, "analyze", None)
        if callable(analyze):
            return analyze(product_information)
        return (
            self.translator.translate(product_information),
            DeepSeekProductInformationTranslator._fallback_user_message_indexes(
                product_information
            ),
        )

    @staticmethod
    def _filename(order: dict[str, Any]):
        return order_resource_filename(order, "jpg", artifact="生产单")

    def _find_or_generate_preview(
        self,
        order: dict[str, Any],
        preview_path: str | Path | None = None,
    ) -> Path:
        if preview_path:
            explicit_path = Path(preview_path)
            if explicit_path.is_file():
                return explicit_path
        preview_path = order_resource_dir(
            self.template_image_generator.output_dir,
            order,
        ) / order_resource_filename(order, "jpg", artifact="预览图")
        if preview_path.is_file():
            return preview_path

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
        user_message_indexes: set[int] | None = None,
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
        gap = px(70)
        usable_width = width - margin * 2
        left_width = (usable_width - gap) // 2
        right_x = margin + left_width + gap
        right_width = width - margin - right_x

        # Keep the production sheet predictable: order details, preview and
        # check table each receive exactly one third of the main A4 area.
        content_top = px(110)
        content_bottom = px(3270)
        section_height = (content_bottom - content_top) / 3
        first_bottom = content_top + section_height
        second_bottom = content_top + section_height * 2

        title_font = self._font(px(56), bold=True)
        heading_font = self._font(px(35), bold=True)
        body_font = self._font(px(34))
        small_font = self._font(px(24))
        shop_font = self._font(px(76), bold=True)

        y = content_top + px(25)
        draw.text(
            (margin, y),
            f"Order #{order.get('order_number') or ''}",
            font=title_font,
            fill=ink,
        )
        y += px(72)
        y = self._draw_block(
            draw,
            order.get("product") or "",
            (margin, y),
            left_width,
            body_font,
            muted,
            px(44),
            max_lines=3,
        )

        y += px(20)
        draw.text((margin, y), "Ship to", font=heading_font, fill=ink)
        y += px(42)
        y = self._draw_block(
            draw,
            order.get("shipping_address") or "",
            (margin, y),
            left_width,
            body_font,
            ink,
            px(42),
            max_lines=7,
        )
        y += px(20)
        draw.text((margin, y), "Shop", font=heading_font, fill=ink)
        y += px(42)
        draw.text(
            (margin, y),
            order.get("shop") or "",
            font=body_font,
            fill=ink,
        )
        y += px(58)
        draw.text((margin, y), "Order date", font=heading_font, fill=ink)
        y += px(42)
        draw.text(
            (margin, y),
            self._format_date(order.get("created_at")),
            font=body_font,
            fill=ink,
        )
        y += px(58)
        draw.text((margin, y), "Payment method", font=heading_font, fill=ink)
        y += px(42)
        y = self._draw_block(
            draw,
            order.get("payment_method") or "",
            (margin, y),
            left_width,
            body_font,
            ink,
            px(42),
            max_lines=2,
        )

        shop_label = order.get("shop_name") or order.get("shop") or ""
        shop_bbox = draw.textbbox((0, 0), shop_label, font=shop_font)
        draw.text(
            (width - margin - (shop_bbox[2] - shop_bbox[0]), content_top + px(25)),
            shop_label,
            font=shop_font,
            fill=red,
        )
        draw.text(
            (right_x, content_top + px(110)),
            f"{order.get('quantity') or 1} item",
            font=heading_font,
            fill=ink,
        )
        draw.line(
            (right_x, content_top + px(158), width - margin, content_top + px(158)),
            fill=line,
            width=px(2),
        )
        right_y = content_top + px(185)
        right_y = self._draw_block(
            draw,
            order.get("product") or "",
            (right_x, right_y),
            right_width,
            heading_font,
            ink,
            px(46),
            max_lines=3,
        )
        right_y += px(14)
        original_entries = product_information_entries(
            order.get("product_information") or {},
            user_message_indexes,
        )

        # Product details remain in the first third. Long lines are reduced
        # only as needed, while the preferred sizes are larger than before.
        for index, (label_text, value_text, is_user_message) in enumerate(
            original_entries,
            start=1,
        ):
            translation = self._normalize_translation(
                translated_lines[index - 1]
                if index <= len(translated_lines)
                else ""
            )
            right_y = self._draw_fitted_segments_single_line(
                draw,
                [
                    (
                        f"{index}. {self._normalize_source(label_text)}: ",
                        ink,
                    ),
                    (
                        self._normalize_single_line(value_text),
                        red if is_user_message else ink,
                    ),
                ],
                (right_x, right_y),
                right_width,
                preferred_size=px(34),
                minimum_size=px(17),
                line_height=px(40),
            )
            if translation:
                right_y = self._draw_fitted_single_line(
                    draw,
                    translation,
                    (right_x + px(38), right_y),
                    right_width - px(38),
                    preferred_size=px(40),
                    minimum_size=px(19),
                    fill=red,
                    line_height=px(47),
                    bold=True,
                )
            right_y += px(5)

        preview_box = (
            margin,
            first_bottom + px(35),
            width - margin,
            second_bottom - px(35),
        )
        self._paste_preview(image, preview_path, preview_box)
        self._draw_check_table(
            draw,
            (
                margin,
                second_bottom + px(22),
                width - margin,
                content_bottom - px(22),
            ),
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
    def _draw_fitted_single_line(
        cls,
        draw,
        text,
        position,
        max_width,
        preferred_size,
        minimum_size,
        fill,
        line_height,
        bold=False,
    ):
        x, y = position
        normalized = cls._normalize_single_line(text)
        size = max(1, int(preferred_size))
        minimum_size = max(1, min(size, int(minimum_size)))
        font = cls._font(size, bold=bold)
        while size > minimum_size and draw.textlength(normalized, font=font) > max_width:
            size -= 1
            font = cls._font(size, bold=bold)
        text_width = max(1, round(draw.textlength(normalized, font=font)))
        if text_width <= max_width:
            draw.text((x, y), normalized, font=font, fill=fill)
            return y + line_height

        bbox = draw.textbbox((0, 0), normalized, font=font)
        text_height = max(1, bbox[3] - bbox[1])
        text_image = Image.new(
            "RGBA",
            (text_width + 4, text_height + 4),
            (0, 0, 0, 0),
        )
        text_draw = ImageDraw.Draw(text_image)
        text_draw.text((0, -bbox[1]), normalized, font=font, fill=fill)
        compressed = text_image.resize(
            (max(1, int(max_width)), text_image.height),
            Image.Resampling.LANCZOS,
        )
        draw._image.paste(compressed, (round(x), round(y)), compressed)
        return y + line_height

    @classmethod
    def _draw_fitted_segments_single_line(
        cls,
        draw,
        segments: list[tuple[str, str]],
        position,
        max_width,
        preferred_size,
        minimum_size,
        line_height,
        bold=False,
    ):
        """Draw one fitted line while allowing only the message value to be red."""
        x, y = position
        normalized_segments = [
            (cls._normalize_single_line(text), fill)
            for text, fill in segments
            if cls._normalize_single_line(text)
        ]
        size = max(1, int(preferred_size))
        minimum_size = max(1, min(size, int(minimum_size)))
        font = cls._font(size, bold=bold)
        width = sum(draw.textlength(text, font=font) for text, _ in normalized_segments)
        while size > minimum_size and width > max_width:
            size -= 1
            font = cls._font(size, bold=bold)
            width = sum(
                draw.textlength(text, font=font)
                for text, _ in normalized_segments
            )

        bbox = font.getbbox("Ag")
        text_height = max(1, bbox[3] - bbox[1])
        rendered_width = max(1, round(width))
        text_image = Image.new(
            "RGBA",
            (rendered_width + 4, text_height + 4),
            (0, 0, 0, 0),
        )
        text_draw = ImageDraw.Draw(text_image)
        cursor = 0
        for text, fill in normalized_segments:
            text_draw.text((cursor, -bbox[1]), text, font=font, fill=fill)
            cursor += text_draw.textlength(text, font=font)
        if rendered_width <= max_width:
            draw._image.paste(text_image, (round(x), round(y)), text_image)
        else:
            compressed = text_image.resize(
                (max(1, int(max_width)), text_image.height),
                Image.Resampling.LANCZOS,
            )
            draw._image.paste(compressed, (round(x), round(y)), compressed)
        return y + line_height

    @staticmethod
    def _normalize_single_line(text: Any) -> str:
        return " ".join(str(text or "").replace("\r", " ").split())

    @classmethod
    def _normalize_source(cls, text: Any) -> str:
        normalized = cls._normalize_single_line(text)
        return re.sub(
            r"^(?:\d+\s*(?:\.\s+|[、:：)）\]]\s*))+",
            "",
            normalized,
        ).lstrip()

    @classmethod
    def _normalize_translation(cls, text: Any) -> str:
        normalized = cls._normalize_single_line(text)
        normalized = re.sub(
            r"^(?:(?:第\s*)?\d+\s*(?:\.\s*|[、:：)）\]]\s*))+",
            "",
            normalized,
        )
        normalized = re.sub(r"(?:\.{3,}|…+)", "", normalized)
        return cls._normalize_single_line(normalized)

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
        box_width = max(1, round(x2 - x1))
        box_height = max(1, round(y2 - y1))
        with Image.open(path) as source:
            preview = ImageOps.contain(
                source.convert("RGB"),
                (box_width, box_height),
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
            # Keep Latin words together while retaining character-level
            # wrapping for CJK text and unusually long unbroken values.
            tokens = re.findall(r"[A-Za-z0-9]+(?:['’._/&|:-][A-Za-z0-9]+)*\s*|.", paragraph)
            for token in tokens:
                candidate = current + token
                if not current or draw.textlength(candidate, font=font) <= max_width:
                    current = candidate
                    continue
                lines.append(current.rstrip())
                current = token.lstrip()
                if draw.textlength(current, font=font) <= max_width:
                    continue
                oversized = current
                current = ""
                for character in oversized:
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


class WeComOrderInfoImageGenerator:
    """Generate the first WeCom image: order details plus the current preview."""

    WIDTH = 1600
    TOP_HEIGHT = 760
    PREVIEW_MAX_HEIGHT = 980

    def __init__(
        self,
        order_repository,
        template_image_generator,
        product_information_analyzer=None,
        jpeg_quality: int = 90,
        output_dir: Path | None = None,
        storage_service=None,
    ):
        self.order_repository = order_repository
        self.template_image_generator = template_image_generator
        self.product_information_analyzer = product_information_analyzer
        self.jpeg_quality = int(jpeg_quality)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.storage_service = storage_service

    def generate(
        self,
        order_id: int,
        order_number: str,
        preview_result: dict[str, Any] | None = None,
        upload_to_oss: bool = True,
    ) -> dict[str, Any]:
        order = self.order_repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        user_message_indexes = self._user_message_indexes(
            order.get("product_information") or {}
        )
        preview_path = self._preview_path(order, preview_result)
        image = self._render(
            order,
            preview_path,
            user_message_indexes=user_message_indexes,
        )

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            subsampling=0,
            optimize=True,
        )
        image_bytes = output.getvalue()
        filename = order_resource_filename(order, "jpg", artifact="企业微信")
        result = {
            "order_id": order["id"],
            "order_number": order["order_number"],
            "filename": filename,
            "image_base64": b64encode(image_bytes).decode("ascii"),
            "mime_type": "image/jpeg",
            "encoding": "base64",
            "pixel_width": image.width,
            "pixel_height": image.height,
            "source": "wecom_order_info",
        }
        if self.output_dir is not None:
            output_dir = order_resource_dir(self.output_dir, order)
            if self.storage_service is not None and upload_to_oss:
                saved = self.storage_service.save_bytes(
                    output_dir,
                    filename,
                    image_bytes,
                )
                result["path"] = saved["path"]
                result["oss"] = saved["oss"]
            else:
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir / filename
                path.write_bytes(image_bytes)
                result["path"] = str(path)
        return result

    def _user_message_indexes(
        self,
        product_information: dict[str, Any],
    ) -> set[int]:
        fallback = DeepSeekProductInformationTranslator._fallback_user_message_indexes(
            product_information
        )
        analyzer = self.product_information_analyzer
        if analyzer is None:
            return fallback
        try:
            return analyzer.identify_user_message_indexes(product_information)
        except Exception as exc:
            print(
                "[企业微信] DeepSeek 用户留言识别失败，使用字段规则继续生成："
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return fallback

    def _preview_path(
        self,
        order: dict[str, Any],
        preview_result: dict[str, Any] | None,
    ) -> Path:
        explicit_path = Path(str((preview_result or {}).get("path") or ""))
        if explicit_path.is_file():
            return explicit_path

        output_dir = Path(self.template_image_generator.output_dir)
        order_folder = order_resource_dir(output_dir, order)
        order_part = safe_filename_part(order.get("order_number"), "order")
        candidates = sorted(
            [
                *order_folder.glob("*-预览图.jpg"),
                *output_dir.glob(f"*{order_part}*.jpg"),
                *output_dir.glob(f"{order_part}_*.jpg"),
                *output_dir.glob(f"{order_part}-{order['id']}-*.jpg"),
            ],
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
        preview_path: Path,
        user_message_indexes: set[int] | None = None,
    ) -> Image.Image:
        width = self.WIDTH
        margin = 60
        gap = 50
        column_width = (width - margin * 2 - gap) // 2
        right_x = margin + column_width + gap

        with Image.open(preview_path) as source:
            preview = ImageOps.contain(
                ImageOps.exif_transpose(source).convert("RGB"),
                (width - margin * 2, self.PREVIEW_MAX_HEIGHT),
                Image.Resampling.LANCZOS,
            )

        title_font = self._font(44, bold=True)
        label_font = self._font(30, bold=True)
        body_font = self._font(30)
        small_font = self._font(26)
        product_y = margin + 34 + 72
        measure_image = Image.new("RGB", (width, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)
        product_bottom = self._measure_product_information(
            draw=measure_draw,
            order=order,
            user_message_indexes=user_message_indexes,
            max_width=column_width - 64,
            font=body_font,
        ) + product_y
        panel_bottom = max(self.TOP_HEIGHT, product_bottom + 30)
        preview_y = panel_bottom + 70
        height = preview_y + 58 + preview.height + margin
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)

        ink = "#222222"
        muted = "#666666"
        accent = "#B43B3B"
        border = "#D9D9D9"
        panel = "#F7F8FA"

        draw.rounded_rectangle(
            (margin, margin, width - margin, panel_bottom),
            radius=20,
            fill=panel,
            outline=border,
            width=2,
        )
        draw.line(
            (right_x - gap // 2, margin + 30, right_x - gap // 2, panel_bottom - 30),
            fill=border,
            width=2,
        )

        y = margin + 34
        shop_line = self._shop_line(order)
        draw.text((margin + 32, y), shop_line, font=title_font, fill=accent)
        y += 68
        y = self._draw_field(
            draw,
            "邮寄地址",
            order.get("shipping_address") or "",
            margin + 32,
            y,
            column_width - 64,
            label_font,
            body_font,
            ink,
            muted,
            max_lines=4,
        )
        y = self._draw_field(
            draw,
            "付款方式",
            order.get("payment_method") or "",
            margin + 32,
            y + 16,
            column_width - 64,
            label_font,
            body_font,
            ink,
            muted,
            max_lines=2,
        )
        y = self._draw_field(
            draw,
            "付款信息",
            self._payment_info(order),
            margin + 32,
            y + 16,
            column_width - 64,
            label_font,
            body_font,
            ink,
            muted,
            max_lines=2,
        )
        self._draw_field(
            draw,
            "订单日期",
            OrderPrintImageGenerator._format_date(order.get("created_at")),
            margin + 32,
            y + 16,
            column_width - 64,
            label_font,
            body_font,
            ink,
            muted,
            max_lines=1,
        )

        product_y = margin + 34
        draw.text((right_x + 32, product_y), "商品信息", font=title_font, fill=ink)
        product_y += 72
        self._draw_product_information(
            draw=draw,
            order=order,
            user_message_indexes=user_message_indexes,
            position=(right_x + 32, product_y),
            max_width=column_width - 64,
            font=body_font,
            ink=ink,
            accent=accent,
        )

        draw.text((margin, preview_y), "订单预览图", font=label_font, fill=ink)
        preview_top = preview_y + 58
        preview_x = round((width - preview.width) / 2)
        draw.rounded_rectangle(
            (
                preview_x - 12,
                preview_top - 12,
                preview_x + preview.width + 12,
                preview_top + preview.height + 12,
            ),
            radius=14,
            fill="white",
            outline=border,
            width=2,
        )
        image.paste(preview, (preview_x, preview_top))
        draw.text(
            (margin, height - 42),
            f"Order #{order.get('order_number') or ''}",
            font=small_font,
            fill=muted,
        )
        return image

    @staticmethod
    def _shop_line(order: dict[str, Any]) -> str:
        shop = str(order.get("shop") or "").strip()
        shop_name = str(order.get("shop_name") or "").strip()
        if shop and shop_name and shop != shop_name:
            return f"Shop {shop} / {shop_name}"
        return f"Shop {shop or shop_name or '未填写'}"

    @staticmethod
    def _payment_info(order: dict[str, Any]) -> str:
        parts = []
        quantity = str(order.get("quantity") or "").strip()
        price = str(order.get("price") or "").strip()
        if quantity:
            parts.append(f"数量：{quantity}")
        if price:
            parts.append(f"价格：{price}")
        return "；".join(parts)

    @classmethod
    def _draw_product_information(
        cls,
        draw,
        order: dict[str, Any],
        user_message_indexes: set[int] | None,
        position,
        max_width: int,
        font,
        ink,
        accent,
    ) -> int:
        x, y = position
        for label, value, is_user_message in cls._product_information_rows(
            order,
            user_message_indexes,
        ):
            if label and is_user_message:
                y = cls._draw_untruncated_block(
                    draw,
                    f"{label}:",
                    (x, y),
                    max_width,
                    font,
                    ink,
                    40,
                )
                y = cls._draw_untruncated_block(
                    draw,
                    value,
                    (x, y),
                    max_width,
                    font,
                    accent,
                    40,
                )
                continue
            text = f"{label}: {value}" if label else value
            y = cls._draw_untruncated_block(
                draw,
                text,
                (x, y),
                max_width,
                font,
                ink,
                40,
            )
        return y

    @classmethod
    def _measure_product_information(
        cls,
        draw,
        order: dict[str, Any],
        user_message_indexes: set[int] | None,
        max_width: int,
        font,
    ) -> int:
        line_count = 0
        for label, value, is_user_message in cls._product_information_rows(
            order,
            user_message_indexes,
        ):
            if label and is_user_message:
                line_count += len(cls._wrap_text(draw, f"{label}:", font, max_width))
                line_count += len(cls._wrap_text(draw, value, font, max_width))
            else:
                text = f"{label}: {value}" if label else value
                line_count += len(cls._wrap_text(draw, text, font, max_width))
        return line_count * 40

    @staticmethod
    def _product_information_rows(
        order: dict[str, Any],
        user_message_indexes: set[int] | None,
    ) -> list[tuple[str | None, str, bool]]:
        rows: list[tuple[str | None, str, bool]] = []
        product = str(order.get("product") or "").strip()
        if product:
            rows.append((None, product, False))
        valid_index = 0
        for label, value in (order.get("product_information") or {}).items():
            label_text = str(label).strip()
            value_text = str(value).strip()
            if not label_text or not value_text:
                continue
            valid_index += 1
            rows.append(
                (
                    label_text,
                    value_text,
                    is_natural_language_user_message(value_text)
                    and (
                        is_user_message_label(label_text)
                        or valid_index in (user_message_indexes or set())
                    ),
                )
            )
        return rows or [(None, "无商品信息", False)]

    @classmethod
    def _draw_untruncated_block(
        cls,
        draw,
        text,
        position,
        max_width,
        font,
        fill,
        line_height,
    ):
        line_count = max(1, len(cls._wrap_text(draw, str(text or ""), font, max_width)))
        return cls._draw_block(
            draw,
            text,
            position,
            max_width,
            font,
            fill,
            line_height,
            max_lines=line_count,
        )

    @staticmethod
    def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
        return OrderPrintImageGenerator._wrap_text(draw, text, font, max_width)

    @classmethod
    def _font(cls, size: int, bold: bool = False):
        return OrderPrintImageGenerator._font(size, bold=bold)

    @classmethod
    def _draw_field(
        cls,
        draw,
        label,
        value,
        x,
        y,
        max_width,
        label_font,
        body_font,
        ink,
        muted,
        max_lines,
    ):
        draw.text((x, y), label, font=label_font, fill=muted)
        y += 42
        return cls._draw_block(
            draw,
            value or "未填写",
            (x, y),
            max_width,
            body_font,
            ink,
            40,
            max_lines=max_lines,
        )

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
        return OrderPrintImageGenerator._draw_block(
            draw,
            text,
            position,
            max_width,
            font,
            fill,
            line_height,
            max_lines,
        )
