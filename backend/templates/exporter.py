from __future__ import annotations

from base64 import b64encode
from copy import deepcopy
from hashlib import md5
from io import BytesIO
import json
import math
from pathlib import Path
import struct
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from PIL import Image, ImageColor, ImageFont

from backend.orders.statuses import PRODUCTION_ORDER_STATUS
from backend.storage import (
    order_resource_dir,
    order_resource_filename,
    order_resource_stem,
)
from backend.templates.size_variants import apply_selected_size_variant, resolve_spine_width
from backend.templates.rendering_rules import fabric_layer_geometry


EXPORT_FORMATS = ("psd", "eps", "svg", "jpg", "png")
PSD_ARCHIVE_VERSION = b"psd-layout-v9-fabric-scene"


class TemplateExportService:
    """Export the same order template as raster, SVG, EPS, or editable PSD."""

    def __init__(
        self,
        order_repository,
        catalog_repository,
        image_generator,
        output_dir: Path | None = None,
        storage_service=None,
        order_print_image_generator=None,
        wecom_order_info_image_generator=None,
    ):
        self.order_repository = order_repository
        self.catalog_repository = catalog_repository
        self.image_generator = image_generator
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.storage_service = storage_service
        self.order_print_image_generator = order_print_image_generator
        self.wecom_order_info_image_generator = wecom_order_info_image_generator
        self._psd_font_name_cache: dict[str, str] = {}
        self._psd_font_content_cache: dict[str, bytes | None] = {}

    def export(
        self,
        order_id: int,
        order_number: str,
        file_format: str | None,
        template_json: dict[str, Any] | None = None,
    ):
        """Confirm production and generate/upload individual order files.

        ZIP creation is intentionally excluded from this production path. The
        download endpoint calls :meth:`package_for_download` when the frontend
        actually requests a bundle.
        """
        file_format = str(file_format or "").strip().lower().lstrip(".")
        if file_format and file_format not in EXPORT_FORMATS:
            raise ValueError(
                f"不支持的导出格式：{file_format or '空'}，可选："
                f"{', '.join(EXPORT_FORMATS)}"
            )
        if file_format:
            print(
                f"[EXPORT] [COMPAT] 已忽略源文件格式参数：{file_format}；"
                "订单目录固定生成四个生产文件",
                flush=True,
            )
        return self.generate_order_artifacts(
            order_id,
            order_number,
            template_json=template_json,
            update_status=True,
        )

    def generate_order_artifacts(
        self,
        order_id: int,
        order_number: str,
        source_format: str | None = None,
        template_json: dict[str, Any] | None = None,
        preview_result: dict[str, Any] | None = None,
        update_status: bool = False,
    ):
        """Generate the four fixed order artifacts, then upload them together."""
        if source_format:
            print(
                f"[EXPORT] [COMPAT] 已忽略源文件格式参数：{source_format}",
                flush=True,
            )
        order = self.order_repository.get_by_id_and_order_number(order_id, order_number)
        payload = self.order_repository.order_payload(order_id)
        database_template = self.catalog_repository.find_render_template(
            product_name=order.get("product", ""),
            shop_id=order.get("shop_id"),
            shop=order.get("shop", ""),
            template_id=order.get("size_template_id"),
        )
        if database_template is None:
            raise LookupError("未找到订单关联的商品尺寸模板")
        template_resolved = False
        if template_json:
            template = self._merge_template(database_template, template_json)
        else:
            snapshot = self._restore_order_template_snapshot(order)
            if snapshot is not None:
                template = snapshot
                template_resolved = True
                print(
                    f"[EXPORT] [TEMPLATE] 复用订单当前 Fabric JSON：订单号={order_number}",
                    flush=True,
                )
            else:
                template = database_template
        output_dir = order_resource_dir(self.output_dir, order)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._remove_obsolete_source_artifacts(output_dir, order_id)
        rendered = self._render_image(
            payload,
            order,
            template,
            template_resolved=template_resolved,
        )
        image = rendered["image"].convert("RGB")
        artifacts: list[dict[str, Any]] = []

        def persist(artifact_type: str, file_format: str, filename: str, content: bytes):
            path = self._save_file(output_dir, filename, content)
            record = {
                "order_id": order_id,
                "artifact_type": artifact_type,
                "file_format": file_format,
                "filename": filename,
                "local_path": str(path),
                "oss": {"status": "pending", "url": None, "object_key": None},
                "file_size": len(content),
            }
            artifacts.append(record)
            print(f"[EXPORT] [FILE] {artifact_type} 生成成功：文件={filename}", flush=True)
            return path

        preview_path = Path(str((preview_result or {}).get("path") or ""))
        target_preview = output_dir / order_resource_filename(order, "jpg", artifact="预览图")
        if not preview_path.is_file() or preview_path.resolve() != target_preview.resolve():
            preview_path = target_preview
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=95, dpi=(self.image_generator.dpi, self.image_generator.dpi))
            persist("preview", "jpg", preview_path.name, buffer.getvalue())
        else:
            # Register/upload the preview already created by the order flow.
            persist("preview", "jpg", preview_path.name, preview_path.read_bytes())

        native_svg = str((rendered.get("template") or {}).get("_fabric_render_svg") or "")
        svg_name = order_resource_filename(order, "svg")
        persist(
            "svg",
            "svg",
            svg_name,
            self._svg_bytes(image, order, native_svg=native_svg),
        )

        if self.order_print_image_generator is None:
            raise RuntimeError("未配置 A4 订单打印图生成器")
        try:
            a4_result = self.order_print_image_generator.generate(
                order_id,
                order_number,
                upload_to_oss=False,
                preview_path=preview_path,
            )
        except TypeError as exc:
            if "preview_path" not in str(exc):
                raise
            a4_result = self.order_print_image_generator.generate(
                order_id,
                order_number,
                upload_to_oss=False,
            )
        a4_path = Path(str(a4_result.get("path") or ""))
        if not a4_path.is_file():
            raise RuntimeError("A4 订单打印图生成失败")
        persist("production_sheet", "jpg", a4_path.name, a4_path.read_bytes())

        # The WeCom auxiliary image is generated before this coordinator is
        # called. Register it with the same order folder, then upload the
        # complete folder contents in one final phase.
        wecom_path = output_dir / order_resource_filename(
            order,
            "jpg",
            artifact="企业微信",
        )
        if not wecom_path.is_file() and self.wecom_order_info_image_generator is not None:
            wecom_result = self.wecom_order_info_image_generator.generate(
                order_id,
                order_number,
                preview_result={"path": str(preview_path)},
                upload_to_oss=False,
            )
            wecom_path = Path(str(wecom_result.get("path") or ""))
        if not wecom_path.is_file():
            raise RuntimeError("企业微信辅助图生成失败")
        persist("wecom", "jpg", wecom_path.name, wecom_path.read_bytes())

        required_types = {"preview", "svg", "wecom", "production_sheet"}
        artifact_types = [item["artifact_type"] for item in artifacts]
        missing_types = sorted(required_types.difference(artifact_types))
        duplicate_types = sorted(
            artifact_type
            for artifact_type in required_types
            if artifact_types.count(artifact_type) != 1
        )
        missing_files = [
            item["filename"]
            for item in artifacts
            if not Path(item["local_path"]).is_file()
        ]
        if missing_types or duplicate_types or missing_files or len(artifacts) != 4:
            raise RuntimeError(
                "订单四个生产文件未完整生成："
                f"缺少类型={missing_types or '无'}，"
                f"重复类型={duplicate_types or '无'}，"
                f"缺少文件={missing_files or '无'}，实际数量={len(artifacts)}"
            )
        print(
            "[EXPORT] [FILE] 四个订单文件已全部生成，开始统一上传 OSS："
            + "、".join(item["filename"] for item in artifacts),
            flush=True,
        )

        for artifact in artifacts:
            path = Path(artifact["local_path"])
            oss = self.storage_service.upload_file(path) if self.storage_service is not None else {
                "status": "disabled", "url": None, "object_key": None,
            }
            if (
                self.storage_service is not None
                and self.storage_service.configured
                and (not oss.get("ok") or oss.get("status") != "uploaded")
            ):
                raise RuntimeError(
                    f"订单文件上传 OSS 失败：文件={path.name}，"
                    f"原因={oss.get('error') or oss.get('status') or '未知错误'}"
                )
            artifact["oss"] = oss
            artifact["oss_url"] = oss.get("url")

        print(
            "[EXPORT] [OSS] 四个订单文件统一上传阶段完成，开始写入产物记录",
            flush=True,
        )
        for artifact in artifacts:
            path = Path(artifact["local_path"])
            oss = artifact["oss"]
            saver = getattr(self.order_repository, "save_artifact", None)
            if callable(saver):
                saver(order_id, artifact)
            print(
                f"[EXPORT] [OSS] 订单文件记录完成：文件={path.name}，状态={oss.get('status')}，地址={oss.get('url') or '无'}",
                flush=True,
            )

        updated_order = self.order_repository.update_status(order_id, PRODUCTION_ORDER_STATUS) if update_status else order
        return {
            "artifacts": artifacts,
            "files": [item["filename"] for item in artifacts],
            "oss_folder": self.storage_service.public_url(
                self.storage_service.object_key_for_path(output_dir).rstrip("/") + "/"
            ) if self.storage_service is not None else None,
            "template_id": template["id"],
            "order_status": updated_order.get("status"),
            "order_status_text": updated_order.get("status_text"),
        }

    def _remove_obsolete_source_artifacts(self, output_dir: Path, order_id: int) -> None:
        for path in output_dir.glob("*-源文件.*"):
            if path.is_file():
                path.unlink()
                print(
                    f"[EXPORT] [FILE] 已删除旧源文件：{path}",
                    flush=True,
                )
        delete_records = getattr(
            self.order_repository,
            "delete_artifacts_by_type",
            None,
        )
        if callable(delete_records):
            delete_records(order_id, "source")

    def package_for_download(self, order_id: int, order_number: str) -> tuple[bytes, str]:
        """Package existing order files only when the frontend downloads."""
        order = self.order_repository.get_by_id_and_order_number(order_id, order_number)
        records = self.order_repository.list_artifacts(order_id, order_number)
        paths = [Path(item["local_path"]) for item in records if Path(item["local_path"]).is_file()]
        if not paths:
            raise LookupError("订单尚未生成可下载文件")
        order_dir = order_resource_dir(self.output_dir, order)
        return self._zip_order_files(order_dir, paths), f"{order_resource_stem(order)}.zip"

    def _generate_export(
        self,
        order_id: int,
        order_number: str,
        file_format: str,
        template_json: dict[str, Any] | None = None,
    ):
        file_format = str(file_format or "").strip().lower().lstrip(".")
        print(
            f"[EXPORT] 执行文件生成：订单ID={order_id}，订单号={order_number}，格式={file_format}",
            flush=True,
        )
        if file_format not in EXPORT_FORMATS:
            raise ValueError(
                f"不支持的导出格式：{file_format or '空'}，可选："
                f"{', '.join(EXPORT_FORMATS)}"
            )
        order = self.order_repository.get_by_id_and_order_number(
            order_id,
            order_number,
        )
        payload = self.order_repository.order_payload(order_id)
        database_template = self.catalog_repository.find_render_template(
            product_name=order.get("product", ""),
            shop_id=order.get("shop_id"),
            shop=order.get("shop", ""),
            template_id=order.get("size_template_id"),
        )
        if database_template is None:
            raise LookupError("未找到订单关联的商品尺寸模板")

        template_resolved = False
        if template_json is None:
            snapshot = self._restore_order_template_snapshot(order)
            if snapshot is not None:
                template = snapshot
                template_resolved = True
                print(
                    f"[EXPORT] 复用订单模板解析快照：订单号={order_number}",
                    flush=True,
                )
            else:
                template = database_template
        else:
            # A frontend template is an explicit override and must be resolved
            # against the current order, never replaced by an older snapshot.
            template = self._merge_template(database_template, template_json)
        rendered = self._render_image(
            payload,
            order,
            template,
            template_resolved=template_resolved,
        )
        image = rendered["image"].convert("RGB")
        if file_format in {"jpg", "png"}:
            output = BytesIO()
            image.save(
                output,
                format="JPEG" if file_format == "jpg" else "PNG",
                dpi=(self.image_generator.dpi, self.image_generator.dpi),
            )
        elif file_format == "svg":
            output = BytesIO(
                self._svg_bytes(
                    image,
                    order,
                    native_svg=str(
                        (rendered.get("template") or {}).get("_fabric_render_svg")
                        or ""
                    ),
                )
            )
        elif file_format == "eps":
            output = BytesIO(self._eps_bytes(template, image))
        else:
            output = BytesIO(
                self._psd_bytes(
                    rendered.get("template") or template,
                    order,
                    payload,
                    image,
                )
            )

        export_filename = self._filename(order, file_format)
        content = output.getvalue()
        print(
            f"[EXPORT] 源文件生成成功：订单号={order_number}，格式={file_format}，文件={export_filename}，字节数={len(content)}",
            flush=True,
        )
        order_dir = order_resource_dir(self.output_dir, order)
        order_dir.mkdir(parents=True, exist_ok=True)
        export_path = self._save_file(order_dir, export_filename, content)
        preview_path, svg_path, a4_path = self._prepare_production_artifacts(
            order,
            image,
            force=bool(template_json),
            native_svg=str(
                (rendered.get("template") or {}).get("_fabric_render_svg") or ""
            ),
        )
        archive_filename = f"{order_resource_stem(order)}.zip"
        archive_content = self._zip_order_files(
            order_dir,
            (preview_path, svg_path, a4_path, export_path),
            comment=self._psd_archive_comment(database_template, order)
            if file_format == "psd"
            else PSD_ARCHIVE_VERSION,
        )
        archive_path = Path(self.output_dir) / archive_filename
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive_content)
        print(
            f"[FILE] 最终 ZIP 本地保存成功：文件={archive_filename}，路径={archive_path}",
            flush=True,
        )
        oss_url = self._ensure_zip_uploaded(
            archive_path,
            force=bool(template_json),
        )
        updated_order = self.order_repository.update_status(
            order_id,
            PRODUCTION_ORDER_STATUS,
        )
        print(
            f"[EXPORT] 生产确认完成：订单ID={order_id}，订单号={order_number}，状态=生产中",
            flush=True,
        )
        return {
            "oss_url": oss_url,
            "filename": archive_filename,
            "format": "zip",
            "export_format": file_format,
            "mime_type": "application/zip",
            "encoding": "base64",
            "file_base64": b64encode(archive_content).decode("ascii"),
            "file_size": len(archive_content),
            "template_id": template["id"],
            "files": [
                preview_path.name,
                svg_path.name,
                a4_path.name,
                export_path.name,
            ],
            "order_status": updated_order["status"],
            "order_status_text": updated_order.get("status_text", "生产中"),
        }

    def _artifact_paths(self, order, file_format):
        order_dir = order_resource_dir(self.output_dir, order)
        paths = (
            order_dir
            / order_resource_filename(order, "jpg", artifact="预览图"),
            order_dir / order_resource_filename(order, "svg"),
            order_dir
            / order_resource_filename(order, "jpg", artifact="生产单"),
            order_dir
            / order_resource_filename(order, file_format, artifact="源文件"),
        )
        archive_path = Path(self.output_dir) / f"{order_resource_stem(order)}.zip"
        return order_dir, paths, archive_path

    def _find_cached_artifacts(
        self,
        order_dir,
        paths,
        archive_path,
        file_format=None,
        template=None,
        order=None,
    ):
        is_psd = str(file_format or "").lower() == "psd"
        if archive_path.is_file():
            archive_content = archive_path.read_bytes()
            if (
                self._archive_matches(order_dir, paths, archive_content)
                and self._cached_source_dimensions_match(
                    archive_content,
                    order_dir,
                    paths,
                    file_format,
                    template,
                    order,
                )
            ):
                print(
                    f"[FILE] 找到可复用的本地 ZIP：路径={archive_path}",
                    flush=True,
                )
                return archive_content, "本地 ZIP"
            print(
                f"[FILE] 本地 ZIP 内容与本次格式或新命名不匹配，将重新检查文件：路径={archive_path}",
                flush=True,
            )

        if self.storage_service is not None:
            downloaded = self.storage_service.download_file_if_exists(archive_path)
            if downloaded.get("status") == "downloaded":
                archive_content = archive_path.read_bytes()
                if (
                    self._archive_matches(order_dir, paths, archive_content)
                    and self._cached_source_dimensions_match(
                        archive_content,
                        order_dir,
                        paths,
                        file_format,
                        template,
                        order,
                    )
                ):
                    return archive_content, "OSS ZIP"
                print(
                    f"[OSS] OSS ZIP 内容与本次格式或新命名不匹配：对象键={downloaded.get('object_key')}",
                    flush=True,
                )

        missing = []
        for path in paths:
            invalid_psd = (
                is_psd
                and Path(path) == Path(paths[-1])
                and path.is_file()
            )
            if path.is_file() and not invalid_psd:
                print(f"[FILE] 找到可复用的本地文件：路径={path}", flush=True)
                continue
            if invalid_psd:
                print(
                    f"[FILE] 本地 PSD 未通过当前 ZIP 版本校验，将重新生成：路径={path}",
                    flush=True,
                )
            missing.append(path)
            print(f"[FILE] 缺少生产文件：预期路径={path}", flush=True)

        if missing:
            return None

        archive_content = self._zip_order_files(
            order_dir,
            paths,
            comment=self._psd_archive_comment(template, order)
            if is_psd
            else PSD_ARCHIVE_VERSION,
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive_content)
        print(f"[FILE] 使用已有四个本地文件重新打包 ZIP：路径={archive_path}", flush=True)
        return archive_content, "已有生产文件"

    def _cached_source_dimensions_match(
        self,
        archive_content,
        order_dir,
        paths,
        file_format,
        template,
        order,
    ):
        if str(file_format or "").lower() != "psd":
            return True
        source_name = f"{order_dir.name}/{Path(paths[-1]).name}"
        try:
            with ZipFile(BytesIO(archive_content)) as archive:
                expected_comment = self._psd_archive_comment(template, order)
                if archive.comment != expected_comment:
                    print(
                        "[FILE] 缓存 PSD 的模板内容或图层版本已变化，将重新生成",
                        flush=True,
                    )
                    return False
                content = archive.read(source_name)
        except (BadZipFile, KeyError):
            return False
        expected = self._expected_psd_size(template, order)
        matched = self._psd_dimensions_match(content, expected)
        if not matched:
            print(
                "[FILE] 缓存 PSD 尺寸不匹配，将重新生成："
                f"实际={self._psd_header_size(content)}，预期={expected}",
                flush=True,
            )
        return matched

    def _expected_psd_size(self, template, order):
        resolved = deepcopy(template or {})
        information = (order or {}).get("product_information") or {}
        matcher = getattr(
            getattr(self.image_generator, "template_resolver", None),
            "match_size_option",
            None,
        )
        if not callable(matcher):
            matcher = getattr(self.image_generator, "_match_size_option", None)
        option = matcher(resolved, information) if callable(matcher) else None
        if isinstance(option, dict):
            fields = dict(resolved.get("fields") or {})
            spec = dict(fields.get("size_spec") or resolved.get("size_spec") or {})
            spec["selected"] = str(option.get("id"))
            spec["options"] = [
                option
                if isinstance(item, dict)
                and str(item.get("id")) == str(option.get("id"))
                else item
                for item in (spec.get("options") or [])
            ]
            fields["size_spec"] = spec
            resolved["fields"] = fields
            resolved["size_spec"] = spec
        apply_selected_size_variant(resolved)
        dimensions = self._dimensions(resolved)
        unit = self._required_size_unit(resolved)
        unit_scale = 300 * {
            "in": 1,
            "cm": 1 / 2.54,
            "mm": 1 / 25.4,
        }[unit]
        return (
            max(1, round(dimensions["total_width"] * unit_scale)),
            max(1, round(dimensions["total_height"] * unit_scale)),
        )

    @staticmethod
    def _psd_header_size(content: bytes):
        if len(content) < 22 or content[:4] != b"8BPS":
            return None
        height = int.from_bytes(content[14:18], "big")
        width = int.from_bytes(content[18:22], "big")
        return width, height

    @staticmethod
    def _psd_resolution(content: bytes):
        """Read horizontal/vertical DPI from the PSD ResolutionInfo resource."""
        if len(content) < 34 or content[:4] != b"8BPS":
            return None
        try:
            offset = 26
            color_data_size = int.from_bytes(content[offset : offset + 4], "big")
            offset += 4 + color_data_size
            resources_size = int.from_bytes(content[offset : offset + 4], "big")
            offset += 4
            resources_end = offset + resources_size
            if resources_end > len(content):
                return None

            while offset + 12 <= resources_end:
                if content[offset : offset + 4] not in {b"8BIM", b"MeSa"}:
                    return None
                resource_id = int.from_bytes(content[offset + 4 : offset + 6], "big")
                offset += 6

                name_size = content[offset]
                name_field_size = 1 + name_size
                offset += name_field_size + (name_field_size % 2)
                if offset + 4 > resources_end:
                    return None

                data_size = int.from_bytes(content[offset : offset + 4], "big")
                offset += 4
                data_end = offset + data_size
                if data_end > resources_end:
                    return None
                if resource_id == 1005 and data_size >= 16:
                    horizontal = int.from_bytes(content[offset : offset + 4], "big") / 65536
                    vertical = int.from_bytes(content[offset + 8 : offset + 12], "big") / 65536
                    return horizontal, vertical
                offset = data_end + (data_size % 2)
        except (IndexError, OverflowError):
            return None
        return None

    @classmethod
    def _psd_dimensions_match(cls, content: bytes, expected):
        resolution = cls._psd_resolution(content)
        return (
            cls._psd_header_size(content) == expected
            and resolution is not None
            and abs(resolution[0] - 300.0) < 0.01
            and abs(resolution[1] - 300.0) < 0.01
        )

    def _cached_result(
        self,
        order_id,
        order_number,
        file_format,
        template_id,
        paths,
        archive_filename,
        archive_content,
    ):
        archive_path = Path(self.output_dir) / archive_filename
        oss_url = self._ensure_zip_uploaded(archive_path)
        updated_order = self.order_repository.update_status(
            order_id,
            PRODUCTION_ORDER_STATUS,
        )
        print(
            f"[EXPORT] 生产确认完成：订单ID={order_id}，订单号={order_number}，状态=生产中",
            flush=True,
        )
        return {
            "oss_url": oss_url,
            "filename": archive_filename,
            "format": "zip",
            "export_format": file_format,
            "mime_type": "application/zip",
            "encoding": "base64",
            "file_base64": b64encode(archive_content).decode("ascii"),
            "file_size": len(archive_content),
            "template_id": template_id,
            "files": [Path(path).name for path in paths],
            "order_status": updated_order["status"],
            "order_status_text": updated_order.get("status_text", "生产中"),
        }

    def _ensure_zip_uploaded(self, archive_path: Path, force: bool = False) -> str:
        if self.storage_service is None or not self.storage_service.configured:
            raise RuntimeError("OSS 未配置，无法上传生产文件 ZIP")
        local_etag = md5(archive_path.read_bytes()).hexdigest()
        checked = self.storage_service.object_exists_for_file(archive_path)
        if not force and checked.get("exists") and checked.get("url"):
            remote_etag = str(checked.get("etag") or "").strip('"').lower()
            if not remote_etag or remote_etag == local_etag:
                versioned_url = self._versioned_oss_url(
                    checked["url"],
                    local_etag,
                )
                print(
                    f"[OSS] 最终 ZIP 已存在且内容一致，直接返回：文件={archive_path.name}，地址={versioned_url}",
                    flush=True,
                )
                return versioned_url
            print(
                "[OSS] 同名 ZIP 内容已过期，将用本地新版覆盖："
                f"文件={archive_path.name}，本地ETag={local_etag}，远端ETag={remote_etag}",
                flush=True,
            )
        uploaded = self.storage_service.upload_file(archive_path)
        if not uploaded.get("ok") or not uploaded.get("url"):
            raise RuntimeError(
                "生产文件 ZIP 上传 OSS 失败："
                f"{uploaded.get('error') or uploaded.get('status') or '未知错误'}"
            )
        return self._versioned_oss_url(uploaded["url"], local_etag)

    @staticmethod
    def _versioned_oss_url(url: str, content_etag: str) -> str:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}v={content_etag[:12]}"

    @staticmethod
    def _archive_matches(order_dir, paths, archive_content):
        expected = {
            f"{order_dir.name}/{Path(path).name}"
            for path in paths
        }
        try:
            with ZipFile(BytesIO(archive_content)) as archive:
                return set(archive.namelist()) == expected
        except (BadZipFile, OSError):
            return False

    def _prepare_production_artifacts(
        self,
        order,
        image,
        force=False,
        native_svg: str = "",
    ):
        output_dir = order_resource_dir(self.output_dir, order)
        output_dir.mkdir(parents=True, exist_ok=True)
        preview_path = output_dir / order_resource_filename(
            order,
            "jpg",
            artifact="预览图",
        )
        preview_matches = False
        if preview_path.is_file():
            try:
                with Image.open(preview_path) as existing_preview:
                    preview_matches = existing_preview.size == image.size
            except OSError:
                preview_matches = False
        artifacts_stale = force or not preview_matches
        if artifacts_stale:
            image.convert("RGB").save(preview_path, format="JPEG", quality=95)
            print(
                f"[FILE] 订单预览图保存成功：文件={preview_path.name}，路径={preview_path}",
                flush=True,
            )
        else:
            print(f"[FILE] 复用本地订单预览图：路径={preview_path}", flush=True)
        svg_path = output_dir / order_resource_filename(order, "svg")
        if artifacts_stale or not svg_path.is_file():
            svg_path.write_bytes(
                self._svg_bytes(
                    image.convert("RGB"),
                    order,
                    native_svg=native_svg,
                )
            )
            print(
                f"[FILE] SVG 文件保存成功：文件={svg_path.name}，路径={svg_path}",
                flush=True,
            )
        else:
            print(f"[FILE] 复用本地 SVG 文件：路径={svg_path}", flush=True)
        if self.order_print_image_generator is None:
            raise RuntimeError("未配置 A4 订单打印图生成器，无法确认生产")
        a4_path = output_dir / order_resource_filename(
            order,
            "jpg",
            artifact="生产单",
        )
        if artifacts_stale or not a4_path.is_file():
            try:
                a4_result = self.order_print_image_generator.generate(
                    order["id"],
                    order["order_number"],
                    upload_to_oss=False,
                    preview_path=preview_path,
                )
            except TypeError as exc:
                # Keep adapters written against the old print-image interface
                # usable while the built-in generator accepts preview_path.
                if "preview_path" not in str(exc):
                    raise
                a4_result = self.order_print_image_generator.generate(
                    order["id"],
                    order["order_number"],
                    upload_to_oss=False,
                )
            a4_path = Path(str(a4_result.get("path") or ""))
        else:
            print(f"[FILE] 复用本地 A4 生产单：路径={a4_path}", flush=True)
        if not a4_path.is_file():
            raise RuntimeError("A4 订单打印图生成失败，未找到输出文件")
        return preview_path, svg_path, a4_path

    def _save_file(self, output_dir: Path, filename: str, content: bytes) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / filename
        path.write_bytes(content)
        print(
            f"[FILE] 本地文件保存成功：文件={filename}，路径={path}，字节数={len(content)}",
            flush=True,
        )
        return path

    @staticmethod
    def _psd_archive_comment(template, order) -> bytes:
        order_fields = {
            key: (order or {}).get(key)
            for key in (
                "product_information",
                "product",
                "shop_id",
                "shop",
                "size_template_id",
            )
        }
        canonical = json.dumps(
            {"template": template or {}, "order": order_fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        fingerprint = md5(canonical).hexdigest().encode("ascii")
        return PSD_ARCHIVE_VERSION + b":" + fingerprint

    @staticmethod
    def _zip_order_files(
        order_dir: Path,
        paths,
        comment: bytes = PSD_ARCHIVE_VERSION,
    ) -> bytes:
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.comment = comment
            for path in paths:
                path = Path(path)
                archive.write(path, arcname=f"{order_dir.name}/{path.name}")
        return output.getvalue()

    def _render_image(
        self,
        payload,
        order,
        template,
        template_resolved: bool = False,
    ):
        try:
            kwargs = {
                "saved_order": order,
                "template": template,
            }
            if template_resolved:
                kwargs["template_resolved"] = True
            return self.image_generator.render_for_order(
                payload,
                **kwargs,
            )
        except Exception as exc:
            print(
                f"[EXPORT] 模板渲染失败：订单号={order.get('order_number')}，错误={type(exc).__name__}: {exc}",
                flush=True,
            )
            raise

    def _restore_order_template_snapshot(self, order):
        resolver = getattr(self.image_generator, "template_resolver", None)
        restore = getattr(resolver, "restore_order_snapshot", None)
        if not callable(restore):
            return None
        return restore(order)

    @staticmethod
    def _merge_template(
        database_template: dict[str, Any],
        override: dict[str, Any] | None,
    ):
        effective = dict(database_template)
        if not override:
            return effective
        if not isinstance(override, dict):
            raise ValueError("template_json must be an object")
        protected = {
            "id",
            "shop_id",
            "shop",
            "shop_name",
            "product_names",
        }
        for key, value in override.items():
            if key not in protected:
                effective[key] = value
        if isinstance(override.get("fields"), dict):
            fields = dict(database_template.get("fields") or {})
            fields.update(override["fields"])
            effective["fields"] = fields
            effective.update(fields)
        if isinstance(override.get("size_template"), dict):
            effective["size_template"] = override["size_template"]
        apply_selected_size_variant(effective)
        return effective

    @staticmethod
    def _filename(order: dict[str, Any], file_format: str):
        return order_resource_filename(order, file_format, artifact="源文件")

    @staticmethod
    def _svg_bytes(
        image: Image.Image,
        order: dict[str, Any],
        native_svg: str = "",
    ):
        if native_svg.lstrip().startswith(("<?xml", "<svg")):
            return native_svg.encode("utf-8")
        png = BytesIO()
        image.save(png, format="PNG")
        encoded = b64encode(png.getvalue()).decode("ascii")
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{image.width}" height="{image.height}" '
            f'viewBox="0 0 {image.width} {image.height}">'
            f'<title>{order.get("order_number", "")}</title>'
            f'<image width="100%" height="100%" '
            f'xlink:href="data:image/png;base64,{encoded}"/>'
            "</svg>"
        ).encode("utf-8")

    @staticmethod
    def _eps_bytes(template: dict[str, Any], image: Image.Image):
        try:
            import pyx
        except ImportError as exc:
            raise RuntimeError(
                "EPS 导出需要安装 PyX：pip install PyX"
            ) from exc
        dimensions = TemplateExportService._dimensions(template)
        factor = {"in": 2.54, "cm": 1, "mm": 0.1}[
            TemplateExportService._required_size_unit(template)
        ]
        width = dimensions["total_width"] * factor
        height = dimensions["total_height"] * factor
        bleed = dimensions["bleed"] * factor
        side_width = dimensions["side_width"] * factor
        spine_width = dimensions["spine_width"] * factor
        canvas = pyx.canvas.canvas()
        canvas.insert(pyx.bitmap.bitmap(0, 0, image, width=width, height=height))
        output = BytesIO()
        # PyX writes to a filename, so use a temporary file and return its bytes.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.eps"
            canvas.writeEPSfile(str(path.with_suffix("")))
            output.write(path.read_bytes())
        return output.getvalue()

    @staticmethod
    def _postscript_name_from_font_bytes(content: bytes) -> str | None:
        """Read name ID 6 from a TTF, OTF, or TTC without extra packages."""
        if len(content) < 12:
            return None
        font_offset = 0
        if content[:4] == b"ttcf":
            if len(content) < 16:
                return None
            try:
                font_count = struct.unpack_from(">I", content, 8)[0]
                if font_count < 1:
                    return None
                font_offset = struct.unpack_from(">I", content, 12)[0]
            except struct.error:
                return None
        try:
            table_count = struct.unpack_from(">H", content, font_offset + 4)[0]
        except struct.error:
            return None
        name_offset = name_length = None
        table_start = font_offset + 12
        for index in range(table_count):
            record_offset = table_start + index * 16
            try:
                tag, _checksum, offset, length = struct.unpack_from(
                    ">4sIII", content, record_offset
                )
            except struct.error:
                return None
            if tag == b"name":
                name_offset, name_length = offset, length
                break
        if name_offset is None or name_length is None:
            return None
        if name_offset < 0 or name_offset + name_length > len(content):
            return None
        try:
            _version, record_count, storage_offset = struct.unpack_from(
                ">HHH", content, name_offset
            )
        except struct.error:
            return None
        storage_start = name_offset + storage_offset
        names: list[tuple[int, str]] = []
        for index in range(record_count):
            record_offset = name_offset + 6 + index * 12
            try:
                platform_id, _encoding_id, language_id, name_id, length, offset = (
                    struct.unpack_from(">HHHHHH", content, record_offset)
                )
            except struct.error:
                break
            if name_id != 6:
                continue
            start = storage_start + offset
            end = start + length
            if start < storage_start or end > name_offset + name_length:
                continue
            raw_name = content[start:end]
            try:
                if platform_id in {0, 3}:
                    decoded = raw_name.decode("utf-16-be")
                elif platform_id == 1:
                    decoded = raw_name.decode("mac_roman")
                else:
                    decoded = raw_name.decode("latin-1")
            except UnicodeDecodeError:
                continue
            decoded = decoded.replace("\x00", "").strip()
            if not decoded:
                continue
            score = (4 if platform_id == 3 else 3 if platform_id == 0 else 0)
            if language_id in {0, 0x0409}:
                score += 2
            names.append((score, decoded))
        if not names:
            return None
        return max(names, key=lambda item: item[0])[1]

    @staticmethod
    def _read_font_source(source: str) -> bytes | None:
        normalized = str(source or "").strip()
        if not normalized:
            return None
        parsed = urlparse(normalized)
        try:
            if parsed.scheme in {"http", "https"}:
                request = Request(
                    normalized,
                    headers={"User-Agent": "CatalogTemplate-PSD/1.0"},
                )
                with urlopen(request, timeout=15) as response:
                    content = response.read(20 * 1024 * 1024 + 1)
                return content if len(content) <= 20 * 1024 * 1024 else None
            if parsed.scheme == "file":
                path = Path(unquote(parsed.path.lstrip("/")))
            else:
                path = Path(normalized)
            return path.read_bytes() if path.is_file() else None
        except (OSError, ValueError):
            return None

    def _cached_font_source(self, source: str) -> bytes | None:
        key = str(source or "").strip()
        if key not in self._psd_font_content_cache:
            self._psd_font_content_cache[key] = self._read_font_source(key)
        return self._psd_font_content_cache[key]

    @staticmethod
    def _psd_font_sources(font_data: dict[str, Any]):
        identifiers: list[str] = []
        for key in (
            "postscript_name",
            "font_name",
            "font_family",
            "fontFamily",
            "family",
        ):
            value = str(font_data.get(key) or "").strip()
            if value and value.casefold() not in {
                item.casefold() for item in identifiers
            }:
                identifiers.append(value)

        sources: list[str] = []
        windows_fonts = Path("C:/Windows/Fonts")
        for identifier in identifiers:
            for extension in (".otf", ".ttf", ".ttc"):
                candidate = str(windows_fonts / f"{identifier}{extension}")
                if candidate not in sources:
                    sources.append(candidate)
        for key in (
            "file_path",
            "font_path",
            "path",
            "family_url",
            "font_url",
            "fontUrl",
            "url",
        ):
            source = str(font_data.get(key) or "").strip()
            if source and source not in sources:
                sources.append(source)
        return identifiers, sources

    def _psd_measure_font(self, size: float, font_data: dict[str, Any]):
        _identifiers, sources = self._psd_font_sources(font_data)
        pixel_size = max(1, round(float(size)))
        for source in sources:
            content = self._cached_font_source(source)
            if not content:
                continue
            try:
                return ImageFont.truetype(BytesIO(content), pixel_size)
            except OSError:
                continue
        return self.image_generator._font(pixel_size, font_data)

    def _psd_font_name(self, font_data: dict[str, Any]) -> str:
        identifiers, sources = self._psd_font_sources(font_data)

        cache_key = "|".join(sources + identifiers).casefold()
        cached = self._psd_font_name_cache.get(cache_key)
        if cached:
            return cached
        for source in sources:
            content = self._cached_font_source(source)
            if not content:
                continue
            postscript_name = self._postscript_name_from_font_bytes(content)
            if not postscript_name:
                continue
            self._psd_font_name_cache[cache_key] = postscript_name
            print(
                f"[EXPORT] [PSD] 字体名称解析成功：模板字体={identifiers[0] if identifiers else '未命名'}，"
                f"PostScript={postscript_name}",
                flush=True,
            )
            return postscript_name

        known_names = {
            "arial": "ArialMT",
            "arialmt": "ArialMT",
            "times new roman": "TimesNewRomanPSMT",
            "timesnewromanpsmt": "TimesNewRomanPSMT",
        }
        for identifier in identifiers:
            known = known_names.get(identifier.casefold())
            if known:
                self._psd_font_name_cache[cache_key] = known
                return known
        requested = identifiers[0] if identifiers else "未命名"
        if sources:
            raise RuntimeError(
                f"PSD 字体加载失败：模板字体={requested}；"
                "请检查 fontUrl/font_path 指向的字体文件是否可访问"
            )
        raise RuntimeError(
            f"PSD 字体缺少可加载的字体文件和已知 PostScript 名称：{requested}"
        )

    @staticmethod
    def _photoshop_tracking(text: str, char_spacing: int, word_spacing: int) -> int:
        """Map Fabric spacing to Photoshop's single effective tracking value."""
        if not word_spacing or len(text) <= 1:
            return int(char_spacing)
        whitespace_count = sum(character.isspace() for character in text)
        return round(
            char_spacing
            + word_spacing * whitespace_count / (len(text) - 1)
        )

    @staticmethod
    def _fabric_psd_layout(layout: dict[str, Any], width: int, height: int):
        """Resolve current Fabric scene coordinates directly into PSD pixels."""
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        objects = layout.get("objects")
        if not isinstance(objects, list):
            objects = layers.get("objects")
        if not isinstance(objects, list):
            return None
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
            return None
        try:
            reference_width = float(
                workarea.get("workareaWidth") or workarea["width"]
            )
            reference_height = float(
                workarea.get("workareaHeight") or workarea["height"]
            )
            workarea_scale_x = abs(float(workarea.get("scaleX", 1) or 1))
            workarea_scale_y = abs(float(workarea.get("scaleY", 1) or 1))
            origin_x = float(workarea["left"])
            origin_y = float(workarea["top"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("当前 Fabric JSON 的 workarea 坐标无效") from exc
        if reference_width <= 0 or reference_height <= 0:
            raise RuntimeError("当前 Fabric JSON 的 workarea 尺寸无效")
        if str(workarea.get("originX") or "center").lower() in {"center", "middle"}:
            origin_x -= reference_width * workarea_scale_x / 2
        if str(workarea.get("originY") or "center").lower() in {"center", "middle"}:
            origin_y -= reference_height * workarea_scale_y / 2
        scale_x = width / reference_width
        scale_y = height / reference_height
        if abs(scale_x - scale_y) > max(scale_x, scale_y) * 0.0001:
            raise RuntimeError(
                f"PSD 画布与 Fabric workarea 比例不一致：{scale_x:.6f}x{scale_y:.6f}"
            )
        return {
            "objects": [
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().lower() != "workarea"
                and "frame" not in item
                and item.get("visible", True) is not False
            ],
            "origin_x": origin_x,
            "origin_y": origin_y,
            "scale": scale_x,
        }

    def _add_fabric_psd_layers(
        self,
        document,
        psapi,
        np,
        layout: dict[str, Any],
        width: int,
        height: int,
        render_geometry: list[dict[str, Any]] | None = None,
    ) -> int:
        """Add editable PSD layers from the exact JSON used by Fabric preview."""
        resolved = self._fabric_psd_layout(layout, width, height)
        if resolved is None:
            return 0
        output_scale = float(resolved["scale"])
        origin_x = float(resolved["origin_x"])
        origin_y = float(resolved["origin_y"])
        added = 0
        checked = 0

        geometry_by_index = {
            int(item["source_index"]): item
            for item in (render_geometry or [])
            if isinstance(item, dict)
            and isinstance(item.get("source_index"), int)
        }

        # Resolve every requested font before constructing the first editable
        # text layer. Missing template fonts must fail the export instead of
        # silently producing fallback-font text.
        psd_fonts: dict[int, str] = {}
        for source_index, element in enumerate(resolved["objects"]):
            element_type = str(element.get("type") or "").replace("-", "").lower()
            if element_type not in {"text", "fabrictext", "textbox", "itext"}:
                continue
            if not str(element.get("text") or ""):
                continue
            font_data = {
                **element,
                "font_family": element.get("fontFamily"),
                "font_url": element.get("fontUrl"),
            }
            psd_fonts[source_index] = self._psd_font_name(font_data)
        if psd_fonts:
            print(
                f"[EXPORT] [PSD] 字体预加载完成：文字图层={len(psd_fonts)}，"
                f"字体={','.join(sorted(set(psd_fonts.values())))}",
                flush=True,
            )

        # Fabric paints later objects above earlier objects. PhotoshopAPI keeps
        # the first document layer at the top, hence the reverse iteration.
        indexed_objects = list(enumerate(resolved["objects"]))
        for source_index, element in reversed(indexed_objects):
            try:
                geometry = fabric_layer_geometry(element)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"PSD 图层坐标无效：id={element.get('id') or element.get('name') or '未命名'}"
                ) from exc
            center_x = (float(geometry["center_x"]) - origin_x) * output_scale
            center_y = (float(geometry["center_y"]) - origin_y) * output_scale
            visual_width = max(1.0, float(geometry["width"]) * output_scale)
            visual_height = max(1.0, float(geometry["height"]) * output_scale)
            angle = float(geometry["angle"])
            browser_geometry = geometry_by_index.get(source_index)
            if browser_geometry:
                try:
                    center_x = float(browser_geometry["center_x"]) * output_scale
                    center_y = float(browser_geometry["center_y"]) * output_scale
                    visual_width = max(
                        1.0,
                        abs(
                            float(browser_geometry["width"])
                            * float(browser_geometry.get("scale_x", 1) or 1)
                        ) * output_scale,
                    )
                    visual_height = max(
                        1.0,
                        abs(
                            float(browser_geometry["height"])
                            * float(browser_geometry.get("scale_y", 1) or 1)
                        ) * output_scale,
                    )
                    angle = float(browser_geometry.get("angle", angle) or 0)
                except (KeyError, TypeError, ValueError):
                    browser_geometry = None
            name = str(
                element.get("name") or element.get("id") or "未命名图层"
            ).strip()
            element_type = str(element.get("type") or "").replace("-", "").lower()

            if element_type in {"text", "fabrictext", "textbox", "itext"}:
                text = str(element.get("text") or "")
                if not text:
                    continue
                font_data = {
                    **element,
                    "font_family": element.get("fontFamily"),
                    "font_url": element.get("fontUrl"),
                }
                font_name = psd_fonts[source_index]
                try:
                    css_font_size = float(element.get("fontSize") or 12)
                except (TypeError, ValueError):
                    css_font_size = 12.0
                # PhotoshopAPI's constructor accepts document-pixel text
                # size, then Photoshop exposes it as points using 72 / DPI.
                # Fabric fontSize is CSS px, so map it through the exact same
                # reference-to-output scale as the preview. Converting to pt
                # here would apply 72 / DPI twice and shrink 300-DPI text by
                # 4.1667x when Photoshop opens the file.
                psd_font_pixels = max(
                    1.0,
                    css_font_size
                    * abs(float(element.get("scaleY", 1) or 1))
                    * output_scale,
                )
                try:
                    char_spacing = round(float(element.get("charSpacing") or 0))
                except (TypeError, ValueError):
                    char_spacing = 0
                try:
                    word_spacing = round(float(element.get("wordSpacing") or 0))
                except (TypeError, ValueError):
                    word_spacing = 0
                # Photoshop ignores tracking ranges placed on whitespace.
                # Preserve Fabric's total line width by distributing the word
                # spacing contribution over all inter-character gaps.
                effective_tracking = self._photoshop_tracking(
                    text,
                    char_spacing,
                    word_spacing,
                )

                # Keep the authored Fabric box as the visual anchor while
                # giving Photoshop extra room to avoid clipping after edits.
                text_box_width = max(
                    visual_width * 2.0,
                    visual_width + psd_font_pixels * 4,
                )
                text_box_height = max(
                    visual_height * 2.0,
                    visual_height + psd_font_pixels * 2,
                )
                alignment = str(element.get("textAlign") or "left").lower()
                if alignment in {"right", "end"}:
                    anchor_x = text_box_width - visual_width / 2
                elif alignment in {"center", "justify", "justify-center"}:
                    anchor_x = text_box_width / 2
                else:
                    anchor_x = visual_width / 2
                anchor_y = visual_height / 2

                color = self.image_generator._font_layout_color(
                    element.get("fill"),
                    "#000000",
                )
                red, green, blue = ImageColor.getrgb(color)
                layer = psapi.TextLayer_8bit(
                    layer_name=name,
                    text=text,
                    font=font_name,
                    font_size=float(psd_font_pixels),
                    fill_color=[1.0, red / 255, green / 255, blue / 255],
                    position_x=0.0,
                    position_y=0.0,
                    box_width=float(text_box_width),
                    box_height=float(text_box_height),
                )
                justification = {
                    "left": psapi.enum.Justification.Left,
                    "start": psapi.enum.Justification.Left,
                    "right": psapi.enum.Justification.Right,
                    "end": psapi.enum.Justification.Right,
                }.get(alignment, psapi.enum.Justification.Center)
                layer.set_paragraph_normal_justification(justification)
                for run_index in range(layer.paragraph_run_count):
                    layer.set_paragraph_run_justification(run_index, justification)
                layer.set_style_normal_tracking(effective_tracking)
                layer.set_style_normal_no_break(True)
                for run_index in range(layer.style_run_count):
                    layer.set_style_run_tracking(run_index, effective_tracking)
                    layer.set_style_run_no_break(run_index, True)
                weight = str(element.get("fontWeight") or "").lower()
                if weight in {"bold", "600", "700", "800", "900"}:
                    layer.set_style_normal_faux_bold(True)
                    for run_index in range(layer.style_run_count):
                        layer.set_style_run_faux_bold(run_index, True)
                if str(element.get("fontStyle") or "").lower() == "italic":
                    layer.set_style_normal_faux_italic(True)
                    for run_index in range(layer.style_run_count):
                        layer.set_style_run_faux_italic(run_index, True)
                try:
                    line_height = float(element.get("lineHeight") or 0)
                except (TypeError, ValueError):
                    line_height = 0
                if line_height > 0:
                    leading = psd_font_pixels * line_height
                    layer.set_style_normal_leading(leading)
                    for run_index in range(layer.style_run_count):
                        layer.set_style_run_leading(run_index, leading)

                radians = math.radians(angle)
                flip_x = -1.0 if geometry["flip_x"] else 1.0
                flip_y = -1.0 if geometry["flip_y"] else 1.0
                xx = math.cos(radians) * flip_x
                xy = math.sin(radians) * flip_x
                yx = -math.sin(radians) * flip_y
                yy = math.cos(radians) * flip_y
                translate_x = center_x - xx * anchor_x - yx * anchor_y
                translate_y = center_y - xy * anchor_x - yy * anchor_y
                ink_offset_x = 0.0
                ink_offset_y = 0.0
                if browser_geometry:
                    pixel_bbox = browser_geometry.get("pixel_bbox")
                    object_bbox = browser_geometry.get("bounding_rect")
                    if isinstance(pixel_bbox, dict) and isinstance(object_bbox, dict):
                        normalized_angle = angle % 360
                        # Photoshop paragraph text starts at the top edge of
                        # its box. Fabric places glyph ink below that edge using
                        # the loaded font's ascender. Correct that cross-axis
                        # offset with the browser's measured ink bounds.
                        if min(normalized_angle, 360 - normalized_angle) < 0.01:
                            ink_offset_y = (
                                float(pixel_bbox["top"])
                                - float(object_bbox["top"])
                            ) * output_scale
                        elif abs(normalized_angle - 90) < 0.01:
                            ink_offset_x = (
                                float(pixel_bbox["right"])
                                - float(object_bbox["right"])
                            ) * output_scale
                        elif abs(normalized_angle - 180) < 0.01:
                            ink_offset_y = (
                                float(pixel_bbox["bottom"])
                                - float(object_bbox["bottom"])
                            ) * output_scale
                        elif abs(normalized_angle - 270) < 0.01:
                            ink_offset_x = (
                                float(pixel_bbox["left"])
                                - float(object_bbox["left"])
                            ) * output_scale
                translate_x += ink_offset_x
                translate_y += ink_offset_y
                layer.set_transform([xx, xy, yx, yy, translate_x, translate_y])
                layer.fill = 1.0
                document.add_layer(layer)

                # Verify the transform maps the authored Fabric anchor back to
                # the same preview pixel center before writing the PSD.
                actual_x = xx * anchor_x + yx * anchor_y + layer.transform_tx
                actual_y = xy * anchor_x + yy * anchor_y + layer.transform_ty
                expected_x = center_x + ink_offset_x
                expected_y = center_y + ink_offset_y
                if abs(actual_x - expected_x) > 0.5 or abs(actual_y - expected_y) > 0.5:
                    raise RuntimeError(f"PSD 文字图层位置校验失败：{name}")
                checked += 1
                added += 1
                continue

            layer_width = max(1, round(visual_width))
            layer_height = max(1, round(visual_height))
            if element_type in {"rect", "rectangle"}:
                color = self.image_generator._font_layout_color(
                    element.get("fill"),
                    "#000000",
                )
                try:
                    opacity_value = float(element.get("opacity", 1))
                except (TypeError, ValueError):
                    opacity_value = 1.0
                opacity = round(max(0.0, min(1.0, opacity_value)) * 255)
                element_image = Image.new(
                    "RGBA",
                    (layer_width, layer_height),
                    (*ImageColor.getrgb(color), opacity),
                )
            else:
                source = element.get("src") or element.get("url")
                source_image = self.image_generator._load_layout_image(source)
                if source_image is None:
                    print(
                        f"[EXPORT] [PSD] 跳过无可用像素内容的 Fabric 图层：{name}",
                        flush=True,
                    )
                    continue
                element_image = source_image.convert("RGBA").resize(
                    (layer_width, layer_height),
                    Image.Resampling.LANCZOS,
                )
            element_image = self.image_generator._transform_layout_layer(
                element_image,
                angle,
                bool(geometry["flip_x"]),
                bool(geometry["flip_y"]),
            )
            rgba = np.asarray(element_image.convert("RGBA"), dtype=np.uint8)
            channels = np.moveaxis(rgba, 2, 0).copy()
            layer = psapi.ImageLayer_8bit(
                channels,
                name,
                width=element_image.width,
                height=element_image.height,
                pos_x=round(center_x),
                pos_y=round(center_y),
            )
            layer.fill = 1.0
            document.add_layer(layer)
            if abs(layer.center_x - center_x) > 0.51 or abs(layer.center_y - center_y) > 0.51:
                raise RuntimeError(f"PSD 图像图层位置校验失败：{name}")
            checked += 1
            added += 1

        print(
            f"[EXPORT] [PSD] 当前 Fabric JSON 图层已转换：图层={added}，"
            f"与预览图坐标校验={checked} 个通过，比例={output_scale:.6f}",
            flush=True,
        )
        return added

    def _psd_bytes(
        self,
        template: dict[str, Any],
        order: dict[str, Any],
        payload,
        preview: Image.Image | None = None,
    ):
        try:
            import numpy as np
            import photoshopapi as psapi
        except ImportError as exc:
            raise RuntimeError(
                "PSD 导出需要安装 PhotoshopAPI：pip install PhotoshopAPI"
            ) from exc
        dimensions = TemplateExportService._dimensions(template)
        scale = 300 * {"in": 1, "cm": 1 / 2.54, "mm": 1 / 25.4}[
            TemplateExportService._required_size_unit(template)
        ]
        width = max(1, round(dimensions["total_width"] * scale))
        height = max(1, round(dimensions["total_height"] * scale))
        document = psapi.LayeredFile_8bit(psapi.enum.ColorMode.rgb, width, height)
        document.dpi = 300.0

        # PSD layer geometry must always target the PSD document itself.
        # A cached or legacy preview may have different dimensions.
        layer_canvas_width = width
        layer_canvas_height = height
        raw_background = (
            template.get("background_color")
            or (template.get("fields") or {}).get("background_color")
            or "#ffffff"
        )
        try:
            background_color = ImageColor.getrgb(str(raw_background))
        except (TypeError, ValueError):
            background_color = (255, 255, 255)
        background = np.empty((3, height, width), dtype=np.uint8)
        for channel_index, channel_value in enumerate(background_color):
            background[channel_index].fill(channel_value)
        background_layer = psapi.ImageLayer_8bit(
            background,
            "背景",
            width=width,
            height=height,
            pos_x=width // 2,
            pos_y=height // 2,
            is_locked=True,
        )
        background_layer.fill = 1.0
        del background

        guides = np.zeros((4, height, width), dtype=np.uint8)
        line_color = (119, 201, 223)
        line_width = max(2, round(scale / 150))
        bleed = round(dimensions["bleed"] * scale)
        side_width = round(dimensions["side_width"] * scale)
        spine_width = round(dimensions["spine_width"] * scale)
        spine_bleed = round(dimensions["spine_bleed"] * scale)
        back_right = bleed + side_width
        spine_left = back_right + spine_bleed
        spine_right = spine_left + spine_width
        cover_left = spine_right + spine_bleed
        for x in (
            bleed,
            back_right,
            spine_left,
            spine_right,
            cover_left,
            width - bleed - 1,
        ):
            left = max(0, x - line_width // 2)
            right = min(width, left + line_width)
            guides[0, :, left:right] = line_color[0]
            guides[1, :, left:right] = line_color[1]
            guides[2, :, left:right] = line_color[2]
            guides[3, :, left:right] = 255
        for y in (bleed, height - bleed - 1):
            top = max(0, y - line_width // 2)
            bottom = min(height, top + line_width)
            guides[0, top:bottom, :] = line_color[0]
            guides[1, top:bottom, :] = line_color[1]
            guides[2, top:bottom, :] = line_color[2]
            guides[3, top:bottom, :] = 255
        guides_layer = psapi.ImageLayer_8bit(
            guides,
            "辅助线",
            width=width,
            height=height,
            pos_x=width // 2,
            pos_y=height // 2,
            is_locked=True,
        )
        guides_layer.fill = 1.0
        del guides
        layout = template.get("_selected_font_layout") or {}
        layers = layout.get("layers") if isinstance(layout.get("layers"), dict) else {}
        elements = layout.get("objects")
        if not isinstance(elements, list):
            elements = layers.get("objects")
        if not isinstance(elements, list):
            elements = layout.get("elements") or []
        current_fabric_layout = self._fabric_psd_layout(
            layout,
            layer_canvas_width,
            layer_canvas_height,
        )
        if current_fabric_layout is not None:
            self._add_fabric_psd_layers(
                document,
                psapi,
                np,
                layout,
                layer_canvas_width,
                layer_canvas_height,
                render_geometry=template.get("_fabric_render_geometry") or [],
            )
            # Current JSON is consumed directly above. Never reinterpret the
            # same objects through the retired frame/reference_px path.
            elements = []
        reference_canvas = (
            layout.get("canvas")
            or layers.get("canvas")
            or (layout.get("options") or {}).get("reference_canvas")
            or {}
        )
        try:
            reference_width = float(
                reference_canvas.get("width")
                or reference_canvas.get("width_px")
            )
            reference_height = float(
                reference_canvas.get("height")
                or reference_canvas.get("height_px")
            )
        except (TypeError, ValueError):
            reference_width = reference_height = 0

        if elements and reference_width > 0 and reference_height > 0:
            offset_x, offset_y, layout_scale = (
                self.image_generator._layout_output_transform(
                    template,
                    reference_width,
                    reference_height,
                    layer_canvas_width,
                    layer_canvas_height,
                )
            )
            padding_x = 0.0
            padding_y = 0.0
            rule_context = {
                **(template.get("fields") or {}),
                **template,
                **(payload if isinstance(payload, dict) else {}),
                **order,
                "product_information": order.get("product_information") or {},
            }

            for element in sorted(
                (item for item in elements if isinstance(item, dict)),
                key=lambda item: item.get("z_index", 0),
                reverse=True,
            ):
                frame = self.image_generator._final_layout_frame(
                    element,
                    reference_width,
                    reference_height,
                )
                if frame is None:
                    continue
                x = padding_x + (float(frame["x"]) - offset_x) * layout_scale
                y = padding_y + (float(frame["y"]) - offset_y) * layout_scale
                box_width = max(1.0, float(frame["width"]) * layout_scale)
                box_height = max(1.0, float(frame["height"]) * layout_scale)
                center_x = x + box_width / 2
                center_y = y + box_height / 2
                name = str(
                    element.get("name")
                    or element.get("id")
                    or "未命名图层"
                ).strip()
                transform = element.get("transform") or {}
                try:
                    angle = float(
                        transform.get(
                            "rotation_deg",
                            transform.get("rotation", 0),
                        )
                        or 0
                    )
                except (TypeError, ValueError):
                    angle = 0.0
                element_type = str(element.get("type") or "text").lower()

                if element_type == "text":
                    text = self.image_generator._font_layout_text(
                        element,
                        rule_context,
                    )
                    if not text:
                        continue
                    font_data = element.get("font") or {}
                    font_name = self._psd_font_name(font_data)
                    try:
                        reference_font_size = float(font_data.get("size_px"))
                    except (TypeError, ValueError):
                        reference_font_size = max(1.0, float(frame["height"]) * 0.7)
                    style = element.get("style") or {}
                    tracking = font_data.get(
                        "tracking",
                        font_data.get(
                            "char_spacing",
                            style.get("char_spacing", style.get("letter_spacing", 0)),
                        ),
                    )
                    raw_leading = font_data.get(
                        "leading_px",
                        style.get("leading_px"),
                    )
                    try:
                        leading = float(raw_leading or 0)
                    except (TypeError, ValueError):
                        leading = 0
                    base_font_px = max(1.0, reference_font_size * layout_scale)
                    measure_font = self._psd_measure_font(base_font_px, font_data)
                    tracking_px = self.image_generator._tracking_pixels(
                        tracking,
                        measure_font,
                    )
                    leading_px = self.image_generator._leading_pixels(
                        raw_leading,
                        measure_font,
                    )
                    natural_width, natural_height, _bbox = (
                        self.image_generator._spaced_text_metrics(
                            text,
                            measure_font,
                            tracking_px,
                            leading_px,
                        )
                    )
                    fit_scale = min(
                        box_width / max(natural_width, 1),
                        box_height / max(natural_height, 1),
                    )
                    effective_font_px = max(1.0, base_font_px * fit_scale)
                    measure_font = self._psd_measure_font(
                        effective_font_px,
                        font_data,
                    )
                    tracking_px = self.image_generator._tracking_pixels(
                        tracking,
                        measure_font,
                    )
                    leading_px = self.image_generator._leading_pixels(
                        leading * layout_scale * fit_scale if leading > 0 else None,
                        measure_font,
                    )
                    measured_width, measured_height, _bbox = (
                        self.image_generator._spaced_text_metrics(
                            text,
                            measure_font,
                            tracking_px,
                            leading_px,
                        )
                    )
                    text_box_width = max(
                        box_width * 1.2,
                        measured_width + max(12.0, effective_font_px * 0.75),
                    )
                    text_box_height = max(
                        box_height * 1.2,
                        measured_height + max(12.0, effective_font_px * 0.35),
                    )
                    point_size = max(
                        1.0,
                        effective_font_px,
                    )
                    color = self.image_generator._font_layout_color(
                        style.get("fill_color"),
                        "#172033",
                    )
                    red, green, blue = ImageColor.getrgb(color)
                    layer = psapi.TextLayer_8bit(
                        layer_name=name,
                        text=text,
                        font=font_name,
                        font_size=float(point_size),
                        fill_color=[
                            1.0,
                            red / 255,
                            green / 255,
                            blue / 255,
                        ],
                        position_x=0.0,
                        position_y=0.0,
                        box_width=float(text_box_width),
                        box_height=float(text_box_height),
                    )
                    try:
                        tracking_value = round(float(tracking or 0))
                        layer.set_style_normal_tracking(tracking_value)
                        for run_index in range(layer.style_run_count):
                            layer.set_style_run_tracking(
                                run_index,
                                tracking_value,
                            )
                    except (TypeError, ValueError):
                        pass
                    if font_data.get("bold"):
                        layer.set_style_normal_faux_bold(True)
                        for run_index in range(layer.style_run_count):
                            layer.set_style_run_faux_bold(run_index, True)
                    if font_data.get("italic"):
                        layer.set_style_normal_faux_italic(True)
                        for run_index in range(layer.style_run_count):
                            layer.set_style_run_faux_italic(run_index, True)
                    if leading > 0:
                        leading_value = (
                            leading
                            * layout_scale
                            * fit_scale
                        )
                        layer.set_style_normal_leading(leading_value)
                        for run_index in range(layer.style_run_count):
                            layer.set_style_run_leading(
                                run_index,
                                leading_value,
                            )
                    alignment = str(style.get("alignment") or "center").lower()
                    justification = {
                        "left": psapi.enum.Justification.Left,
                        "start": psapi.enum.Justification.Left,
                        "right": psapi.enum.Justification.Right,
                        "end": psapi.enum.Justification.Right,
                    }.get(alignment, psapi.enum.Justification.Center)
                    layer.set_paragraph_normal_justification(justification)
                    for run_index in range(layer.paragraph_run_count):
                        layer.set_paragraph_run_justification(
                            run_index,
                            justification,
                        )

                    if alignment in {"left", "start"}:
                        target_center_x = x + text_box_width / 2
                    elif alignment in {"right", "end"}:
                        target_center_x = x + box_width - text_box_width / 2
                    else:
                        target_center_x = center_x
                    target_center_y = center_y
                    radians = math.radians(angle)
                    cos_angle = math.cos(radians)
                    sin_angle = math.sin(radians)
                    scale_x = -1.0 if transform.get("flip_horizontal", False) else 1.0
                    scale_y = -1.0 if transform.get("flip_vertical", False) else 1.0
                    xx = cos_angle * scale_x
                    xy = sin_angle * scale_x
                    yx = -sin_angle * scale_y
                    yy = cos_angle * scale_y
                    translate_x = (
                        target_center_x
                        - xx * text_box_width / 2
                        - yx * text_box_height / 2
                    )
                    translate_y = (
                        target_center_y
                        - xy * text_box_width / 2
                        - yy * text_box_height / 2
                    )
                    layer.set_transform(
                        [xx, xy, yx, yy, translate_x, translate_y]
                    )
                    layer.fill = 1.0
                    document.add_layer(layer)
                    continue

                appearance = element.get("appearance") or {}
                asset = element.get("asset") or {}
                shape = str(
                    appearance.get("shape")
                    or asset.get("kind")
                    or element.get("shape")
                    or ""
                ).lower()
                layer_width = max(1, round(box_width))
                layer_height = max(1, round(box_height))
                if shape == "rectangle" or element_type in {"rectangle", "shape"}:
                    color = self.image_generator._font_layout_color(
                        appearance.get("fill_color"),
                        "#000000",
                    )
                    try:
                        opacity = max(
                            0,
                            min(
                                255,
                                round(float(appearance.get("opacity", 100)) * 2.55),
                            ),
                        )
                    except (TypeError, ValueError):
                        opacity = 255
                    element_image = Image.new(
                        "RGBA",
                        (layer_width, layer_height),
                        (*ImageColor.getrgb(color), opacity),
                    )
                else:
                    source = (
                        asset.get("data_url")
                        or asset.get("dataUrl")
                        or asset.get("path")
                        or asset.get("url")
                    )
                    source_image = self.image_generator._load_layout_image(source)
                    if source_image is None:
                        continue
                    element_image = source_image.convert("RGBA").resize(
                        (layer_width, layer_height),
                        Image.Resampling.LANCZOS,
                    )
                element_image = self.image_generator._transform_layout_layer(
                    element_image,
                    angle,
                    bool(transform.get("flip_horizontal", False)),
                    bool(transform.get("flip_vertical", False)),
                )
                rgba = np.asarray(element_image.convert("RGBA"), dtype=np.uint8)
                channels = np.moveaxis(rgba, 2, 0).copy()
                layer = psapi.ImageLayer_8bit(
                    channels,
                    name,
                    width=element_image.width,
                    height=element_image.height,
                    pos_x=round(center_x),
                    pos_y=round(center_y),
                )
                layer.fill = 1.0
                document.add_layer(layer)
        document.add_layer(guides_layer)
        document.add_layer(background_layer)
        output = BytesIO()
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.psd"
            document.write(str(path))
            output.write(path.read_bytes())
        return output.getvalue()

    @staticmethod
    def _required_size_unit(template: dict[str, Any]) -> str:
        unit = str(template.get("size_unit") or "").strip().lower()
        if not unit:
            raise RuntimeError("命中的尺寸规格缺少 size_unit，拒绝使用默认单位")
        if unit not in {"in", "cm", "mm"}:
            raise RuntimeError(f"模板单位不受支持：{unit!r}")
        return unit

    @staticmethod
    def _dimensions(template: dict[str, Any]):
        def number(key):
            try:
                value = float(template.get(key))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"模板字段 {key} 不是有效数字") from exc
            if value < 0:
                raise RuntimeError(f"模板字段 {key} 不能小于 0")
            return value

        side_width = number("single_side_width")
        side_height = number("single_side_height")
        bleed = number("bleed")
        spine_width = resolve_spine_width(template)
        spine_bleed = number("spine_bleed")
        return {
            "side_width": side_width,
            "side_height": side_height,
            "bleed": bleed,
            "spine_width": spine_width,
            "spine_bleed": spine_bleed,
            "total_width": (
                side_width * 2
                + spine_width
                + spine_bleed * 2
                + bleed * 2
            ),
            "total_height": side_height + bleed * 2,
        }
