from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from backend.templates.exporter import TemplateExportService, stream_and_close


class FakeOrderRepository:
    def __init__(self):
        self.order = {
            "id": 4,
            "order_number": "4141461118",
            "shop_name": "3号店",
            "created_at": "2026-08-27T10:00:00+00:00",
            "status": 5,
        }

    def get_by_id_and_order_number(self, order_id, order_number):
        if order_id != 4 or order_number != "4141461118":
            raise LookupError("订单不存在")
        return self.order

    def list_artifacts(self, order_id, order_number):
        return [
            {
                "oss_object_key": (
                    "orders/3号店-4141461118-20260827/preview.jpg"
                )
            }
        ]


class FakeStorageService:
    configured = True

    def __init__(self):
        self.requested_prefix = None

    def object_key_for_path(self, path):
        return f"orders/{Path(path).name}"

    def download_folder(self, object_prefix):
        self.requested_prefix = object_prefix
        return [
            {
                "relative_name": "preview.jpg",
                "content": b"preview-content",
            },
            {
                "relative_name": "production/order.svg",
                "content": b"<svg/>",
            },
            {
                "relative_name": "3号店-4141461118-20260827-要求.txt",
                "content": "商品信息\n1. Book Size: 9*6\n\n翻译\n1. 书籍尺寸：9*6\n".encode("utf-8-sig"),
            },
        ]


class TemplateExportDownloadTests(unittest.TestCase):
    def test_stream_closes_memory_buffer_after_response_is_consumed(self):
        buffer = BytesIO(b"zip-content")

        self.assertEqual(b"".join(stream_and_close(buffer, chunk_size=3)), b"zip-content")
        self.assertTrue(buffer.closed)

    def test_packages_existing_oss_folder_without_generating_files(self):
        repository = FakeOrderRepository()
        storage = FakeStorageService()
        with TemporaryDirectory() as directory:
            service = TemplateExportService(
                repository,
                catalog_repository=None,
                image_generator=None,
                output_dir=Path(directory),
                storage_service=storage,
            )

            archive, filename = service.package_for_download(4, "4141461118")

        self.assertEqual(
            storage.requested_prefix,
            "orders/3号店-4141461118-20260827",
        )
        self.assertEqual(filename, "3号店-4141461118-20260827.zip")
        with archive, ZipFile(archive) as zipped:
            self.assertEqual(
                sorted(zipped.namelist()),
                [
                    "3号店-4141461118-20260827/3号店-4141461118-20260827-要求.txt",
                    "3号店-4141461118-20260827/preview.jpg",
                    "3号店-4141461118-20260827/production/order.svg",
                ],
            )
            self.assertEqual(
                zipped.read("3号店-4141461118-20260827/preview.jpg"),
                b"preview-content",
            )
            self.assertIn(
                "1. 书籍尺寸：9*6",
                zipped.read(
                    "3号店-4141461118-20260827/3号店-4141461118-20260827-要求.txt"
                ).decode("utf-8-sig"),
            )


if __name__ == "__main__":
    unittest.main()
