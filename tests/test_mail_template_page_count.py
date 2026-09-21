import unittest

from backend.catalog.mail_product_resolver import MailProductResolver
from tests.test_mail_product_resolver import FakeMatcher, FakeRepository


class TemplateRepository(FakeRepository):
    def find_product_template_marker(self, product_id, shop_id, marker):
        if marker == "04":
            return {"size_template_id": 143, "use_default_size_option": True}
        return None

    def find_template_specification(self, template_id, value):
        if template_id == 143 and value == "10x8 | 100 sheets":
            return "10x8"
        return None


class MailTemplatePageCountTests(unittest.TestCase):
    def resolve(self, path, value="40 sheets", marker="04"):
        product = {
            "product_id": 8, "shop_id": 3, "name": "自粘式相册",
            "template_marker": "Font", "specification_field": "Quantity of sheets",
            "product_identifiers": ["Quantity of sheets"] if path == "identifiers" else [],
        }
        repository = TemplateRepository(
            exact=product if path == "exact" else None,
            candidates=[product],
        )
        matcher = FakeMatcher({"product_id": 8, "translated_title": "自粘式相册"})
        information = {"Font": marker, "Quantity of sheets": value}
        result = MailProductResolver(repository, matcher).resolve(
            "ObiaMoment", "17号店", "Photo album", information,
        )
        self.assertEqual(information["Quantity of sheets"], value)
        return result

    def test_page_count_does_not_clear_uniquely_matched_template(self):
        for path in ("exact", "identifiers", "semantic"):
            with self.subTest(path=path):
                result = self.resolve(path)
                self.assertEqual(result["size_template_id"], 143)
                self.assertTrue(result["use_default_size_option"])

    def test_matching_specification_still_takes_priority(self):
        for path in ("exact", "identifiers", "semantic"):
            with self.subTest(path=path):
                result = self.resolve(path, "10x8 | 100 sheets")
                self.assertEqual(result["size_template_id"], 143)
                self.assertFalse(result["use_default_size_option"])
                self.assertEqual(result["matched_specification"], "10x8")

    def test_page_count_does_not_supply_a_missing_template(self):
        result = self.resolve("exact", marker="unknown")
        self.assertIsNone(result["size_template_id"])

    def test_missing_configured_field_is_not_assumed_to_be_page_count(self):
        result = self.resolve("exact", value="")
        self.assertIsNone(result["size_template_id"])


if __name__ == "__main__":
    unittest.main()
