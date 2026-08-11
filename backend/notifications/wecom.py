from __future__ import annotations

import base64
from hashlib import md5
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
import requests


WECOM_IMAGE_MAX_BYTES = 2 * 1024 * 1024


class WeComRobotError(RuntimeError):
    pass


class WeComRobotNotifier:
    """Send a generated order image and its identifying text to WeCom."""

    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float = 10,
        session=None,
    ):
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.session = session or requests

    def notify_order_image(
        self,
        order: dict[str, Any],
        image_result: dict[str, Any],
        saved_order: dict[str, Any] | None = None,
    ):
        if not self.webhook_url:
            return {"ok": False, "status": "disabled"}

        image_path = Path(str(image_result.get("path") or ""))
        image_bytes, compressed = self._image_bytes(image_path)
        image_payload = {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(image_bytes).decode("ascii"),
                "md5": md5(image_bytes).hexdigest(),
            },
        }
        image_response = self._post(image_payload, message_type="image")

        order_number = str(
            (saved_order or {}).get("order_number")
            or order.get("订单号")
            or ""
        ).strip()
        product = str(
            (saved_order or {}).get("product")
            or order.get("产品")
            or ""
        ).strip()
        text_payload = {
            "msgtype": "text",
            "text": {
                "content": f"订单号：{order_number or '未知'}；产品：{product or '未知'}",
            },
        }
        text_response = self._post(text_payload, message_type="text")
        return {
            "ok": True,
            "status": "sent",
            "image_bytes": len(image_bytes),
            "image_compressed": compressed,
            "responses": {
                "image": image_response,
                "text": text_response,
            },
        }

    def _post(self, payload: dict[str, Any], message_type: str):
        try:
            response = self.session.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = (
                f"HTTP {status_code}"
                if status_code is not None
                else type(exc).__name__
            )
            raise WeComRobotError(
                f"企业微信 {message_type} 消息请求失败：{detail}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise WeComRobotError(
                f"企业微信 {message_type} 消息返回了无效 JSON"
            ) from exc

        if not isinstance(result, dict) or str(result.get("errcode")) != "0":
            raise WeComRobotError(
                f"企业微信 {message_type} 消息发送失败：{result!r}"
            )
        return result

    @staticmethod
    def _image_bytes(path: Path):
        if not path.is_file():
            raise WeComRobotError(f"企业微信待发送图片不存在：{path}")

        original = path.read_bytes()
        if path.suffix.casefold() in {".jpg", ".jpeg", ".png"} and len(
            original
        ) <= WECOM_IMAGE_MAX_BYTES:
            return original, False

        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        except (OSError, ValueError) as exc:
            raise WeComRobotError(f"企业微信待发送图片无法读取：{path}") from exc

        image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
        quality = 90
        for _ in range(12):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            encoded = output.getvalue()
            if len(encoded) <= WECOM_IMAGE_MAX_BYTES:
                return encoded, True

            if quality > 40:
                quality -= 10
            else:
                next_size = (
                    max(1, round(image.width * 0.8)),
                    max(1, round(image.height * 0.8)),
                )
                image = image.resize(next_size, Image.Resampling.LANCZOS)

        raise WeComRobotError("图片压缩后仍超过企业微信 2 MB 限制")
