import unittest

from backend.orders.service import OrderService


class FakeCatalogRepository:
    def __init__(self, webhooks):
        self.webhooks = webhooks
        self.calls = []

    def get_shop_wecom_robot_webhook(self, shop_id):
        self.calls.append(shop_id)
        return self.webhooks.get(shop_id, "")


class ShopWeComRoutingTests(unittest.TestCase):
    def test_uses_robot_configured_for_order_shop(self):
        catalog = FakeCatalogRepository(
            {
                1: "https://example.invalid/shop-1",
                2: "https://example.invalid/shop-2",
            }
        )
        service = OrderService(object(), catalog_repository=catalog)

        shop_one = service._notifier_for_order({"shop_id": 1})
        shop_two = service._notifier_for_order({"shop_id": 2})

        self.assertEqual(shop_one.webhook_url, "https://example.invalid/shop-1")
        self.assertEqual(shop_two.webhook_url, "https://example.invalid/shop-2")
        self.assertIsNot(shop_one, shop_two)

    def test_missing_shop_robot_returns_none(self):
        catalog = FakeCatalogRepository({})
        service = OrderService(object(), catalog_repository=catalog)

        self.assertIsNone(service._notifier_for_order({"shop_id": 9}))

    def test_missing_catalog_or_shop_id_returns_none(self):
        service = OrderService(object())

        self.assertIsNone(service._notifier_for_order({"shop_id": 1}))
        self.assertIsNone(service._notifier_for_order({}))

    def test_reuses_notifier_for_unchanged_webhook(self):
        catalog = FakeCatalogRepository(
            {1: "https://example.invalid/shop-1"}
        )
        service = OrderService(object(), catalog_repository=catalog)

        first = service._notifier_for_order({"shop_id": 1})
        second = service._notifier_for_order({"shop_id": 1})

        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
