import unittest

from backend.api.routes.template_catalog import router
from backend.schemas import InnerPageTemplateOptionUpdate


class InnerPageTemplateOptionRouteTests(unittest.TestCase):
    def test_specification_crud_routes_are_registered(self):
        routes = {
            (route.path, frozenset(route.methods or []))
            for route in router.routes
        }

        self.assertIn(
            (
                "/inner-page-templates/{template_id}/size-options",
                frozenset({"POST"}),
            ),
            routes,
        )
        option_path = (
            "/inner-page-templates/{template_id}/size-options/{size_option_id}"
        )
        self.assertIn((option_path, frozenset({"PATCH"})), routes)
        self.assertIn((option_path, frozenset({"DELETE"})), routes)

    def test_patch_payload_keeps_omitted_fields_unset(self):
        payload = InnerPageTemplateOptionUpdate(
            layers={"objects": []}
        ).model_dump(exclude_unset=True)

        self.assertEqual(payload, {"layers": {"objects": []}})


if __name__ == "__main__":
    unittest.main()
