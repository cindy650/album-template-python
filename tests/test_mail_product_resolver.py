import unittest

from backend.catalog.mail_product_resolver import MailProductResolver
from backend.schemas import ProductCreate, ProductUpdate


class FakeRepository:
    def __init__(self, *, exact=None, candidates=None):
        self.exact = exact
        self.candidates = candidates or []
        self.associations = []
        self.shop_products = []

    def find_product_name_for_shop(self, product_name, *shop_names):
        return self.exact

    def list_mail_product_candidates(self, *shop_names):
        return self.candidates

    def ensure_shop_product(self, product_name, *shop_names):
        self.shop_products.append((product_name, shop_names))
        return {
            "product_name": product_name,
            "shop_id": 3,
            "shop": shop_names[0],
            "shop_name": shop_names[1],
            "created": True,
        }

    def associate_mail_product_name(self, product_id, shop_id, product_name):
        self.associations.append((product_id, shop_id, product_name))
        return {
            "product_id": product_id,
            "shop_id": shop_id,
            "product_name": product_name,
        }

    def find_product_template_specification(
        self, product_id, shop_id, specification_value
    ):
        if specification_value == "10x8 | 100 pages":
            return {
                "size_template_id": 42,
                "matched_specification": "10x8",
            }
        return None


class FakeMatcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def match(self, product_name, candidates):
        self.calls.append((product_name, candidates))
        if self.error is not None:
            raise self.error
        return self.result


class MailProductResolverTests(unittest.TestCase):
    def test_existing_product_name_skips_deepseek_and_specification_check(self):
        existing = {"product_id": 8, "shop_id": 3}
        repository = FakeRepository(exact=existing)
        matcher = FakeMatcher(error=AssertionError("DeepSeek should not be called"))

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Known listing title",
            {},
        )

        self.assertIs(result, existing)
        self.assertEqual(matcher.calls, [])
        self.assertEqual(repository.associations, [])

    def test_specifications_match_when_order_field_contains_array_item(self):
        repository = FakeRepository(
            candidates=[
                {
                    "product_id": 11,
                    "shop_id": 3,
                    "name": "婚礼签到册",
                    "specification_field": "Book Size | Page Count",
                    "specifications": ["9x6", "9x6 | 80"],
                }
            ]
        )
        matcher = FakeMatcher(
            {"product_id": 11, "translated_title": "个性化婚礼签到册"}
        )

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Personalized Wedding Guest Book",
            {" book size | PAGE count ": "9X6 | 80 Pages"},
        )

        self.assertEqual(result["matched_specification"], "9x6 | 80")
        self.assertEqual(result["specification_value"], "9X6 | 80 Pages")
        self.assertEqual(
            repository.associations,
            [(11, 3, "Personalized Wedding Guest Book")],
        )

    def test_product_name_is_not_associated_when_no_specification_is_contained(self):
        repository = FakeRepository(
            candidates=[
                {
                    "product_id": 11,
                    "shop_id": 3,
                    "name": "婚礼签到册",
                    "specification_field": "Book Size | Page Count",
                    "specifications": ["9x6", "10x12"],
                }
            ]
        )
        matcher = FakeMatcher(
            {"product_id": 11, "translated_title": "个性化婚礼签到册"}
        )

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Personalized Wedding Guest Book",
            {"Book Size | Page Count": "8x8 | 80 Pages"},
        )

        self.assertIsNone(result["product_id"])
        self.assertFalse(result["automatic_product_match"])
        self.assertEqual(repository.associations, [])
        self.assertEqual(
            repository.shop_products,
            [("Personalized Wedding Guest Book", ("LuxeJoy", "3号店"))],
        )

    def test_empty_specifications_match_associated_template_options(self):
        repository = FakeRepository(
            candidates=[
                {
                    "product_id": 12,
                    "shop_id": 3,
                    "name": "宣誓册",
                    "specification_field": "Book Size | Page Count",
                    "specifications": [],
                }
            ]
        )
        matcher = FakeMatcher(
            {"product_id": 12, "translated_title": "婚礼宣誓册"}
        )

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Personalized Wedding Vow Book",
            {"Book Size | Page Count": "10x8 | 100 pages"},
        )

        self.assertEqual(result["product_id"], 12)
        self.assertEqual(result["size_template_id"], 42)
        self.assertEqual(result["matched_specification"], "10x8")
        self.assertEqual(
            repository.associations,
            [(12, 3, "Personalized Wedding Vow Book")],
        )

    def test_shop_without_product_candidates_still_accepts_order(self):
        repository = FakeRepository(candidates=[])
        matcher = FakeMatcher(error=AssertionError("DeepSeek should not be called"))

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Unknown listing title",
            {"Book Size | Page Count": "9x6 | 80 Pages"},
        )

        self.assertIsNone(result["product_id"])
        self.assertEqual(result["shop_id"], 3)
        self.assertFalse(result["automatic_product_match"])
        self.assertEqual(matcher.calls, [])
        self.assertEqual(repository.associations, [])
        self.assertEqual(
            repository.shop_products,
            [("Unknown listing title", ("LuxeJoy", "3号店"))],
        )

    def test_deepseek_error_does_not_block_order_intake(self):
        repository = FakeRepository(
            candidates=[
                {
                    "product_id": 11,
                    "shop_id": 3,
                    "name": "婚礼签到册",
                    "specification_field": "Book Size | Page Count",
                    "specifications": ["9x6"],
                }
            ]
        )
        matcher = FakeMatcher(error=RuntimeError("DeepSeek unavailable"))

        result = MailProductResolver(repository, matcher).resolve(
            "LuxeJoy",
            "3号店",
            "Personalized Wedding Guest Book",
            {"Book Size | Page Count": "9x6 | 80 Pages"},
        )

        self.assertIsNone(result["product_id"])
        self.assertFalse(result["automatic_product_match"])
        self.assertEqual(repository.associations, [])


class ProductSchemaTests(unittest.TestCase):
    def test_product_without_specifications_is_valid(self):
        payload = ProductCreate(
            name="宣誓册",
            product_names=["Personalized Wedding Vow Book"],
            shop_ids=[1],
            specifications=[],
            specification_field="Book Size | Page Count",
        )

        self.assertEqual(payload.specifications, [])

    def test_product_update_can_clear_specifications(self):
        payload = ProductUpdate(specifications=[])

        self.assertEqual(payload.specifications, [])

    def test_product_update_accepts_specifications_json_alias(self):
        payload = ProductUpdate(
            specifications_json=["9*6", "10*8", "12*12"],
            specification_field="Book Size | Page Count",
        )

        self.assertEqual(
            payload.model_dump(exclude_unset=True),
            {
                "specifications": ["9*6", "10*8", "12*12"],
                "specification_field": "Book Size | Page Count",
            },
        )

    def test_product_create_accepts_specifications_json_alias(self):
        payload = ProductCreate(
            name="婚礼签到册",
            product_names=[
                "Personalized Linen Wedding Guest Book, Photo Album Scrapbook"
            ],
            shop_ids=[1],
            specifications_json=["9*6", "10*8", "12*12"],
        )

        self.assertEqual(payload.specifications, ["9*6", "10*8", "12*12"])


if __name__ == "__main__":
    unittest.main()
