import unittest
from unittest.mock import Mock

from backend.catalog.repository import CatalogRepository


class ExplicitRenderTemplateTests(unittest.TestCase):
    def setUp(self):
        self.repo = CatalogRepository.__new__(CatalogRepository)
        self.template = {"id": 103, "shop_id": 1, "shop": "LuxeJoy", "product_id": 6}
        self.repo.get_size_template = Mock(return_value=self.template)
        self.repo._flatten_render_template = Mock(return_value={"id": 103})
        self.repo.connect = Mock(side_effect=AssertionError("Explicit template lookup must not query product_names"))

    def test_new_listing_with_saved_template_does_not_require_product_name_alias(self):
        result = self.repo.find_render_template("Unregistered listing", shop_id=1, shop="LuxeJoy", template_id=103)
        self.assertEqual(result, {"id": 103})
        self.repo._flatten_render_template.assert_called_once_with(self.template)

    def test_cross_shop_template_is_rejected(self):
        self.assertIsNone(self.repo.find_render_template("Listing", shop_id=8, template_id=103))
        self.repo._flatten_render_template.assert_not_called()

    def test_explicit_different_product_is_rejected(self):
        self.assertIsNone(self.repo.find_render_template("Listing", shop_id=1, template_id=103, product_id=4))

    def test_deleted_template_does_not_fall_back_to_another(self):
        self.repo.get_size_template.side_effect = LookupError("Missing")
        self.assertIsNone(self.repo.find_render_template("Listing", shop_id=1, template_id=103))


if __name__ == "__main__":
    unittest.main()
