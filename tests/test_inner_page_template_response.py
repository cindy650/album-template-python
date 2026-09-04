import unittest

from backend.api.routes.template_catalog import public_inner_page_template


PREVIEW_URL = "https://example.invalid/font_layout_image/preview.png"


class InnerPageTemplateResponseTests(unittest.TestCase):
    def test_preserves_repository_preview_image(self):
        result = public_inner_page_template(
            {"id": 1, "preview_image": PREVIEW_URL}
        )

        self.assertEqual(result["preview_image"], PREVIEW_URL)
        self.assertNotIn("preview_image_path", result)

    def test_preserves_preview_image_in_paginated_items(self):
        result = public_inner_page_template(
            {
                "items": [{"id": 1, "preview_image": PREVIEW_URL}],
                "total": 1,
            }
        )

        self.assertEqual(result["items"][0]["preview_image"], PREVIEW_URL)


if __name__ == "__main__":
    unittest.main()
