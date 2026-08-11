from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import qq_idleCopy as core
from backend.catalog.repository import CatalogRepository


class MailShopFilterTests(unittest.TestCase):
    def test_shop_lookup_matches_resolved_shop_name(self):
        with TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "catalog.db")
            repository.create_shop(
                {
                    "shop": "LuxeJoy",
                    "shop_name": "3号店",
                }
            )

            matched = repository.find_shop_by_names(
                "LuxeJoy",
                "3号店",
            )

            self.assertIsNotNone(matched)
            self.assertEqual(matched["shop"], "LuxeJoy")
            self.assertEqual(matched["shop_name"], "3号店")
            self.assertIsNone(
                repository.find_shop_by_names(
                    "MissingShop",
                    "MissingShop",
                )
            )

    def test_unknown_shop_is_skipped_and_advances_uid(self):
        client = FakeMailClient(uid=101, shop="MissingShop")
        order_handler = Mock()
        shop_lookup = Mock(return_value=None)

        with patch.object(core, "save_last_uid") as save_last_uid:
            last_uid = core.process_new_messages(
                client,
                last_uid=100,
                order_handler=order_handler,
                shop_lookup=shop_lookup,
            )

        self.assertEqual(last_uid, 101)
        shop_lookup.assert_called_once_with("MissingShop", "")
        save_last_uid.assert_called_once_with(101)
        order_handler.assert_not_called()


class FakeMailClient:
    def __init__(self, uid: int, shop: str):
        self.uid = uid
        self.message = EmailMessage()
        self.message["Subject"] = "You made a sale"
        self.message.set_content(f"Shop: {shop}\n")

    def search(self, criteria):
        return [self.uid]

    def fetch(self, uids, fields):
        if fields == ["RFC822.HEADER"]:
            payload = self.message.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"
            return {self.uid: {b"RFC822.HEADER": payload}}
        return {self.uid: {b"RFC822": self.message.as_bytes()}}


if __name__ == "__main__":
    unittest.main()
