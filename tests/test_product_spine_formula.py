import unittest

from backend.catalog.repository import (
    normalize_product_spine_width_formula,
    normalize_product_spine_width_mode,
    normalize_product_spine_width_page_rules,
)
from backend.templates.size_variants import (
    resolve_product_spine_width_page_table,
)


class ProductSpineFormulaTests(unittest.TestCase):
    def test_normalizes_valid_formula(self):
        result = normalize_product_spine_width_formula({"unit": "cm"})
        self.assertEqual(result["unit"], "cm")
        self.assertEqual(result["page_count_coefficient"], 0.2)
        self.assertEqual(result["additional_width"], 0.9)

    def test_normalizes_page_count_table(self):
        result = normalize_product_spine_width_page_rules(
            {
                "unit": "cm",
                "items": [
                    {"page_count": 10, "spine_width": 1.5},
                    {"page_count": 20, "spine_width": 1.8},
                ],
            }
        )
        self.assertEqual(result["match_strategy"], "ceil")
        self.assertEqual(result["items"][1]["page_count"], 20)
        self.assertEqual(
            normalize_product_spine_width_mode(
                None, page_rules=result
            ),
            "page_count_table",
        )

    def test_page_count_table_uses_next_node_and_converts_units(self):
        result = resolve_product_spine_width_page_table(
            {
                "unit": "cm",
                "match_strategy": "ceil",
                "items": [
                    {"page_count": 10, "spine_width": 1.5, "spine_bleed": 0},
                    {"page_count": 20, "spine_width": 1.8, "spine_bleed": 0},
                ],
            },
            15,
            "mm",
        )
        self.assertIsNotNone(result)
        width, metadata = result
        self.assertAlmostEqual(width, 18.0)
        self.assertEqual(metadata["matched_page_count"], 20)


if __name__ == "__main__":
    unittest.main()
