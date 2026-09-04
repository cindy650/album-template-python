from pathlib import Path
import tempfile
import unittest

from PIL import Image

from backend.orders.service import OrderService
from backend.storage import order_resource_dir, order_resource_filename
from backend.templates.exporter import TemplateExportService


class _StatusRepository:
    def __init__(self):
        self.status = 1
        self.advance_calls = []

    def get_by_id_and_order_number(self, order_id, order_number):
        return {"id": order_id, "order_number": order_number, "status": self.status}

    def advance_status(self, order_id, order_number, expected_status=None):
        self.advance_calls.append((order_id, order_number, expected_status))
        if expected_status != self.status:
            raise ValueError("unexpected status")
        self.status += 1
        return {"id": order_id, "order_number": order_number, "status": self.status}


class _ConfirmationArtifactService:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def regenerate_customer_confirmation_artifacts(self, order_id, order_number):
        self.calls.append((order_id, order_number))
        if self.error:
            raise self.error
        return {"files": ["order.svg", "order-生产单.jpg"], "artifacts": []}


class _TemplateResolver:
    @staticmethod
    def restore_order_snapshot(_order):
        return {"id": 1, "objects": []}


class _ImageGenerator:
    dpi = 96
    template_resolver = _TemplateResolver()

    @staticmethod
    def render_for_order(_payload, **_kwargs):
        return {
            "image": Image.new("RGB", (96, 48), "white"),
            "template": {
                "_fabric_render_svg": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'width="96" height="48"><text font-family="Arial">NEW</text></svg>'
                ),
                "_fabric_render_text_to_svg": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'width="96" height="48"><path d="M0 0"/></svg>'
                )
            },
        }


class _CatalogRepository:
    @staticmethod
    def find_render_template(**_kwargs):
        return {"id": 1}


class _ArtifactRepository:
    def __init__(self, order):
        self.order = order
        self.saved_artifacts = []

    def get_by_id_and_order_number(self, _order_id, _order_number):
        return dict(self.order)

    def order_payload(self, _order_id):
        return {"订单号": self.order["order_number"]}

    def save_artifact(self, _order_id, artifact):
        self.saved_artifacts.append(artifact)


class _A4Generator:
    def __init__(self, output_dir, order):
        self.output_dir = output_dir
        self.order = order
        self.preview_paths = []

    def generate(
        self,
        _order_id,
        _order_number,
        upload_to_oss=True,
        preview_path=None,
    ):
        self.assertions(upload_to_oss, preview_path)
        target_dir = order_resource_dir(self.output_dir, self.order)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / order_resource_filename(
            self.order,
            "jpg",
            artifact="生产单",
        )
        path.write_bytes(b"new-production-sheet")
        return {"path": str(path)}

    def assertions(self, upload_to_oss, preview_path):
        if upload_to_oss is not False:
            raise AssertionError("confirmation A4 must not upload itself")
        path = Path(preview_path)
        if not path.is_file():
            raise AssertionError("confirmation A4 must use the freshly rendered preview")
        self.preview_paths.append(path)


class _WecomGenerator:
    def __init__(self, output_dir, order):
        self.output_dir = output_dir
        self.order = order

    def generate(self, _order_id, _order_number, preview_result=None, upload_to_oss=True):
        if upload_to_oss is not False:
            raise AssertionError("confirmation WeCom image must not upload itself")
        if not Path(preview_result["path"]).is_file():
            raise AssertionError("confirmation WeCom image must use the fresh preview")
        target_dir = order_resource_dir(self.output_dir, self.order)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / order_resource_filename(self.order, "jpg", artifact="企业微信")
        path.write_bytes(b"new-wecom")
        return {"path": str(path)}


class _StorageService:
    configured = True

    def __init__(self):
        self.uploaded = []

    def upload_file(self, path):
        path = Path(path)
        self.uploaded.append(path)
        return {
            "ok": True,
            "status": "uploaded",
            "url": f"https://oss.invalid/{path.name}",
            "object_key": f"generated_orders/{path.parent.name}/{path.name}",
        }


class CustomerConfirmationStatusTests(unittest.TestCase):
    def test_status_one_regenerates_files_before_advancing_to_two(self):
        repository = _StatusRepository()
        artifacts = _ConfirmationArtifactService()
        service = OrderService(repository, production_artifact_service=artifacts)

        result = service.advance_status(78, "4156669962")

        self.assertEqual(artifacts.calls, [(78, "4156669962")])
        self.assertEqual(repository.advance_calls, [(78, "4156669962", 1)])
        self.assertEqual(result["status"], 2)
        self.assertIn("customer_confirmation_artifacts", result)

    def test_generation_failure_keeps_status_one(self):
        repository = _StatusRepository()
        artifacts = _ConfirmationArtifactService(RuntimeError("OSS failed"))
        service = OrderService(repository, production_artifact_service=artifacts)

        with self.assertRaisesRegex(RuntimeError, "OSS failed"):
            service.advance_status(78, "4156669962")

        self.assertEqual(repository.status, 1)
        self.assertEqual(repository.advance_calls, [])


class CustomerConfirmationArtifactTests(unittest.TestCase):
    def test_all_five_artifacts_are_replaced_and_uploaded(self):
        order = {
            "id": 78,
            "order_number": "4156669962",
            "shop": "LuxeJoy",
            "shop_name": "3号店",
            "product": "Guest Book",
            "shop_id": 1,
            "size_template_id": 1,
            "created_at": "2026-08-27T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory(prefix="customer-confirm-test-") as directory:
            output_dir = Path(directory) / "generated_orders"
            order_dir = order_resource_dir(output_dir, order)
            order_dir.mkdir(parents=True)
            preview = order_dir / order_resource_filename(order, "jpg", artifact="预览图")
            wecom = order_dir / order_resource_filename(order, "jpg", artifact="企业微信")
            preview.write_bytes(b"old-preview")
            wecom.write_bytes(b"old-wecom")

            repository = _ArtifactRepository(order)
            storage = _StorageService()
            a4_generator = _A4Generator(output_dir, order)
            service = TemplateExportService(
                repository,
                _CatalogRepository(),
                _ImageGenerator(),
                output_dir=output_dir,
                storage_service=storage,
                order_print_image_generator=a4_generator,
                wecom_order_info_image_generator=_WecomGenerator(output_dir, order),
            )

            result = service.regenerate_customer_confirmation_artifacts(
                78,
                "4156669962",
            )

            self.assertEqual(len(result["artifacts"]), 5)
            self.assertEqual(
                {item["artifact_type"] for item in result["artifacts"]},
                {"preview", "wecom", "svg", "converted_svg", "production_sheet"},
            )
            self.assertEqual(len(storage.uploaded), 5)
            self.assertEqual(len(repository.saved_artifacts), 5)
            self.assertNotEqual(preview.read_bytes(), b"old-preview")
            self.assertNotEqual(wecom.read_bytes(), b"old-wecom")
            svg_path = next(path for path in storage.uploaded if path.name.endswith(".svg"))
            a4_path = next(path for path in storage.uploaded if path.name.endswith("生产单.jpg"))
            self.assertIn("font-family", svg_path.read_text(encoding="utf-8"))
            self.assertEqual(a4_path.read_bytes(), b"new-production-sheet")


if __name__ == "__main__":
    unittest.main()
