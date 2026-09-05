import unittest

from backend.catalog.repository import normalize_product_spine_width_formula


class ProductSpineFormulaTests(unittest.TestCase):
    def test_normalizes_valid_formula(self):
        result = normalize_product_spine_width_formula({"unit": "cm"})
        self.assertEqual(result["unit"], "cm")
        self.assertEqual(result["page_count_coefficient"], 0.2)
        self.assertEqual(result["additional_width"], 0.9)


if __name__ == "__main__":
    unittest.main()
