import unittest

from pydantic import ValidationError

from backend.catalog.repository import CatalogRepository
from backend.schemas import InnerPageTemplateOption


class InnerPageTemplateSizeUnitTests(unittest.TestCase):
    def test_schema_accepts_and_preserves_explicit_unit(self):
        option = InnerPageTemplateOption.model_validate(
            {
                "id": "9*6",
                "label": "9*6",
                "size_unit": "in",
                "layers": {"objects": []},
            }
        )

        self.assertEqual(option.size_unit, "in")

    def test_schema_rejects_missing_unit(self):
        with self.assertRaises(ValidationError):
            InnerPageTemplateOption.model_validate(
                {
                    "id": "9*6",
                    "label": "9*6",
                    "layers": {"objects": []},
                }
            )

    def test_repository_preserves_unit_without_conversion(self):
        options = CatalogRepository._normalize_inner_page_options(
            [
                {
                    "id": "9*6",
                    "label": "9*6",
                    "size_unit": "in",
                    "layers": {"objects": []},
                }
            ]
        )

        self.assertEqual(options[0]["size_unit"], "in")


if __name__ == "__main__":
    unittest.main()
