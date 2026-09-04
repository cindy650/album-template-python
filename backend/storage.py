from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import mimetypes
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
WHITESPACE = re.compile(r"\s+")


def safe_filename_part(value: Any, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", str(value or "").strip())
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" ._-")
    return cleaned or fallback


def order_timestamp(order: dict[str, Any], created_at: Any = None) -> str:
    value = created_at or order.get("created_at") or order.get("创建时间")
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d%H%M%S")
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().strftime("%Y%m%d%H%M%S")
    return parsed.strftime("%Y%m%d%H%M%S")


def order_file_stem(order: dict[str, Any], created_at: Any = None) -> str:
    shop_name = (
        order.get("shop_name")
        or order.get("店铺名")
        or order.get("shop")
        or order.get("店铺")
    )
    order_name = order.get("order_number") or order.get("订单号")
    return "-".join(
        [
            safe_filename_part(shop_name, "店铺"),
            safe_filename_part(order_name, "订单"),
            order_timestamp(order, created_at=created_at),
        ]
    )


def order_resource_stem(order: dict[str, Any], created_at: Any = None) -> str:
    """Return the stable directory/file stem used for all order resources."""
    shop_name = (
        order.get("shop_name")
        or order.get("店铺名")
        or order.get("shop")
        or order.get("店铺")
    )
    order_name = order.get("order_number") or order.get("订单号")
    timestamp = order_timestamp(order, created_at=created_at)[:8]
    return "-".join(
        [
            safe_filename_part(shop_name, "店铺"),
            safe_filename_part(order_name, "订单"),
            timestamp,
        ]
    )


def order_resource_dir(
    root: Path,
    order: dict[str, Any],
    created_at: Any = None,
) -> Path:
    return Path(root) / order_resource_stem(order, created_at=created_at)


def order_resource_filename(
    order: dict[str, Any],
    extension: str,
    created_at: Any = None,
    artifact: str = "",
) -> str:
    extension = extension.lstrip(".").lower()
    stem = order_resource_stem(order, created_at=created_at)
    suffix = safe_filename_part(artifact, "") if artifact else ""
    return f"{stem}{f'-{suffix}' if suffix else ''}.{extension}"


def order_filename(
    order: dict[str, Any],
    extension: str,
    created_at: Any = None,
) -> str:
    extension = extension.lstrip(".").lower()
    return f"{order_file_stem(order, created_at=created_at)}.{extension}"


class FileStorageService:
    def __init__(
        self,
        project_root: Path,
        access_key_id: str = "",
        access_key_secret: str = "",
        bucket_name: str = "",
        endpoint: str = "",
        region: str = "",
        object_prefix: str = "",
        endpoint_is_cname: bool = False,
    ):
        self.project_root = Path(project_root)
        self.access_key_id = str(access_key_id or "").strip()
        self.access_key_secret = str(access_key_secret or "").strip()
        self.bucket_name = str(bucket_name or "").strip()
        self.endpoint = self._normalize_endpoint(endpoint)
        self.region = str(region or "").strip()
        self.object_prefix = str(object_prefix or "").strip(" /")
        self.endpoint_is_cname = bool(endpoint_is_cname)

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key_id
            and self.access_key_secret
            and self.bucket_name
            and self.endpoint
        )

    def save_bytes(
        self,
        output_dir: Path,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / filename
        try:
            path.write_bytes(content)
        except Exception as exc:
            print(f"[FILE] 本地文件保存失败：文件={filename}，错误={type(exc).__name__}: {exc}", flush=True)
            raise
        print(f"[FILE] 本地文件保存成功：文件={filename}，路径={path}，字节数={len(content)}", flush=True)
        return {
            "path": str(path),
            "filename": filename,
            "oss": self.upload_file(path),
        }

    def upload_file(self, path: Path, object_key: str | None = None) -> dict[str, Any]:
        path = Path(path)
        object_key = object_key or self.object_key_for_path(path)
        if not self.configured:
            print(f"[OSS] 跳过上传：文件={path.name}，对象键={object_key}，原因=OSS 未配置", flush=True)
            return {
                "ok": False,
                "status": "disabled",
                "bucket": self.bucket_name,
                "object_key": object_key,
            }
        try:
            import oss2
        except ImportError:
            print(f"[OSS] 上传失败：文件={path.name}，对象键={object_key}，原因=未安装 oss2", flush=True)
            return {
                "ok": False,
                "status": "failed",
                "bucket": self.bucket_name,
                "object_key": object_key,
                "error": "oss2 is not installed; run pip install oss2 to enable Aliyun OSS uploads.",
            }

        try:
            auth = oss2.Auth(self.access_key_id, self.access_key_secret)
            endpoint, is_cname = self._sdk_endpoint()
            bucket = oss2.Bucket(
                auth,
                endpoint,
                self.bucket_name,
                is_cname=is_cname,
            )
            headers = {}
            content_type, _ = mimetypes.guess_type(str(path))
            if content_type:
                headers["Content-Type"] = content_type
            result = bucket.put_object_from_file(
                object_key,
                str(path),
                headers=headers or None,
            )
            if not bucket.object_exists(object_key):
                raise RuntimeError("OSS 返回上传成功，但上传后校验未找到对象")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(
                f"[OSS] 上传失败：文件={path.name}，对象键={object_key}，错误={error}",
                flush=True,
            )
            return {
                "ok": False,
                "status": "failed",
                "bucket": self.bucket_name,
                "object_key": object_key,
                "error": error,
            }

        upload_result = {
            "ok": True,
            "status": "uploaded",
            "bucket": self.bucket_name,
            "object_key": object_key,
            "url": self.public_url(object_key),
            "etag": getattr(result, "etag", ""),
        }
        print(f"[OSS] 上传成功：文件={path.name}，对象键={object_key}，地址={upload_result['url']}", flush=True)
        return upload_result

    def object_exists_for_file(self, path: Path) -> dict[str, Any]:
        """Check whether the OSS object matching a local path exists."""
        path = Path(path)
        object_key = self.object_key_for_path(path)
        if not self.configured:
            print(
                f"[OSS] 跳过存在检查：文件={path.name}，对象键={object_key}，原因=OSS 未配置",
                flush=True,
            )
            return {
                "ok": False,
                "status": "disabled",
                "exists": False,
                "bucket": self.bucket_name,
                "object_key": object_key,
            }
        try:
            bucket = self._bucket()
            exists = bool(bucket.object_exists(object_key))
            etag = ""
            if exists:
                head = bucket.head_object(object_key)
                etag = str(getattr(head, "etag", "") or "").strip('"')
        except ImportError:
            print(
                f"[OSS] 存在检查失败：文件={path.name}，对象键={object_key}，原因=未安装 oss2",
                flush=True,
            )
            return {
                "ok": False,
                "status": "failed",
                "exists": False,
                "bucket": self.bucket_name,
                "object_key": object_key,
                "error": "oss2 is not installed; run pip install oss2 to enable Aliyun OSS uploads.",
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(
                f"[OSS] 存在检查失败：文件={path.name}，对象键={object_key}，错误={error}",
                flush=True,
            )
            return {
                "ok": False,
                "status": "failed",
                "exists": False,
                "bucket": self.bucket_name,
                "object_key": object_key,
                "error": error,
            }
        print(
            f"[OSS] 存在检查完成：文件={path.name}，对象键={object_key}，存在={exists}",
            flush=True,
        )
        return {
            "ok": True,
            "status": "exists" if exists else "missing",
            "exists": exists,
            "bucket": self.bucket_name,
            "object_key": object_key,
            "url": self.public_url(object_key) if exists else None,
            "etag": etag,
        }

    def download_file_if_exists(self, path: Path) -> dict[str, Any]:
        """Download an OSS object to its expected local path when present."""
        path = Path(path)
        checked = self.object_exists_for_file(path)
        if not checked.get("exists"):
            return checked
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._bucket().get_object_to_file(checked["object_key"], str(path))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(
                f"[OSS] 下载失败：文件={path.name}，对象键={checked['object_key']}，错误={error}",
                flush=True,
            )
            return {**checked, "ok": False, "status": "failed", "error": error}
        print(
            f"[OSS] 下载成功：文件={path.name}，对象键={checked['object_key']}，本地路径={path}",
            flush=True,
        )
        return {**checked, "ok": True, "status": "downloaded", "path": str(path)}

    def download_folder(self, object_prefix: str) -> list[dict[str, Any]]:
        """Download every OSS object below one folder prefix into memory."""
        object_prefix = str(object_prefix or "").strip(" /")
        if not object_prefix:
            raise ValueError("OSS 文件夹前缀不能为空")
        if not self.configured:
            raise RuntimeError("OSS 未配置，无法下载订单文件夹")
        prefix = object_prefix + "/"
        try:
            import oss2

            bucket = self._bucket()
            objects = []
            for item in oss2.ObjectIterator(bucket, prefix=prefix):
                object_key = str(getattr(item, "key", "") or "")
                if not object_key or object_key.endswith("/"):
                    continue
                relative_name = object_key[len(prefix):]
                if not relative_name:
                    continue
                content = bucket.get_object(object_key).read()
                objects.append(
                    {
                        "object_key": object_key,
                        "relative_name": relative_name,
                        "content": content,
                    }
                )
                print(
                    f"[OSS] 订单文件读取成功：对象键={object_key}，"
                    f"字节数={len(content)}",
                    flush=True,
                )
        except ImportError as exc:
            raise RuntimeError(
                "未安装 oss2，无法从 OSS 下载订单文件夹"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"OSS 订单文件夹下载失败：{type(exc).__name__}: {exc}"
            ) from exc
        print(
            f"[OSS] 订单文件夹读取完成：前缀={prefix}，文件数={len(objects)}",
            flush=True,
        )
        return objects

    def object_key_for_path(self, path: Path) -> str:
        path = Path(path)
        try:
            relative = path.resolve().relative_to(self.project_root.resolve())
            parts = relative.parts
        except ValueError:
            parts = (path.name,)
        key = PurePosixPath(*parts).as_posix()
        if self.object_prefix:
            key = PurePosixPath(self.object_prefix, key).as_posix()
        return key

    def public_url(self, object_key: str) -> str:
        split = urlsplit(self.endpoint)
        if self.endpoint_is_cname or self._endpoint_has_bucket_host():
            base = self.endpoint.rstrip("/")
        else:
            base = urlunsplit(
                (
                    split.scheme,
                    f"{self.bucket_name}.{split.netloc}",
                    split.path.rstrip("/"),
                    "",
                    "",
                )
            ).rstrip("/")
        return f"{base}/{quote(object_key, safe='/')}"

    def _sdk_endpoint(self) -> tuple[str, bool]:
        if self.endpoint_is_cname:
            return self.endpoint, True
        if self._endpoint_has_bucket_host():
            split = urlsplit(self.endpoint)
            bucket_prefix = f"{self.bucket_name}."
            host = split.netloc[len(bucket_prefix) :]
            return urlunsplit((split.scheme, host, split.path, "", "")), False
        return self.endpoint, False

    def _bucket(self):
        import oss2

        auth = oss2.Auth(self.access_key_id, self.access_key_secret)
        endpoint, is_cname = self._sdk_endpoint()
        return oss2.Bucket(
            auth,
            endpoint,
            self.bucket_name,
            is_cname=is_cname,
        )

    def _endpoint_has_bucket_host(self) -> bool:
        split = urlsplit(self.endpoint)
        host = split.netloc.lower()
        return bool(self.bucket_name) and host.startswith(
            f"{self.bucket_name.lower()}."
        )

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        endpoint = str(endpoint or "").strip().rstrip("/")
        if endpoint and "://" not in endpoint:
            endpoint = f"https://{endpoint}"
        return endpoint
