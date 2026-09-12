import unittest
from unittest.mock import patch

import qq_idleCopy


class FakeMailClient:
    def search(self, criteria):
        return [1830]

    def fetch(self, uids, fields):
        uid = uids[0]
        if fields == ["RFC822.HEADER"]:
            return {
                uid: {
                    b"RFC822.HEADER": (
                        b"Subject: You made a sale on Etsy - Order #4160285572\r\n"
                        b"From: orders@example.com\r\n\r\n"
                    )
                }
            }
        return {
            uid: {
                b"RFC822": (
                    b"Subject: You made a sale on Etsy - Order #4160285572\r\n"
                    b"From: orders@example.com\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                    b"order body"
                )
            }
        }


class MailOrderIntakeTests(unittest.TestCase):
    def test_listing_thumbnail_is_saved_in_product_information(self):
        html = (
            '<div><img src="https://cdn.example/item-a.jpg" alt="thumbnail">'
            '<a href="https://www.etsy.com/transaction/5201101150">'
            "Personalized Wedding Guest Book"
            "</a><span>Book Size | Page Count: 10*8 | 100 sheets</span></div>"
        )

        body = qq_idleCopy.html_to_text(html)
        self.assertEqual(body.product_images, ["https://cdn.example/item-a.jpg"])
        result = qq_idleCopy.parse_order_fields(
            "You made a sale on Etsy - Order #4160334885",
            body,
            "",
        )

        self.assertEqual(
            result["商品信息"][qq_idleCopy.PRODUCT_IMAGE_FIELD],
            "https://cdn.example/item-a.jpg",
        )

    def test_listing_thumbnail_prefers_lazy_loaded_source_over_data_uri(self):
        body = qq_idleCopy.html_to_text(
            '<img src="data:image/gif;base64,placeholder" '
            'data-src="https://cdn.example/item-lazy.jpg">'
            '<a href="https://www.etsy.com/listing/123">Product</a>'
        )
        self.assertEqual(body.product_images, ["https://cdn.example/item-lazy.jpg"])

    def test_listing_thumbnail_ignores_spacer_and_backfills_real_image(self):
        body = qq_idleCopy.html_to_text(
            '<img src="https://www.etsy.com/images/email/spacer-trans.gif">'
            '<a href="https://www.etsy.com/listing/123">Product</a>'
            '<img src="https://i.etsystatic.com/54769833/r/il/dc212e/6879998294/'
            'il_75x75.6879998294_4jzc.jpg">'
        )
        self.assertEqual(
            body.product_images,
            ["https://i.etsystatic.com/54769833/r/il/dc212e/6879998294/il_75x75.6879998294_4jzc.jpg"],
        )

    def test_image_only_transaction_anchor_is_reused_by_title_anchor(self):
        product_image = (
            "https://i.etsystatic.com/54769833/r/il/1828a8/7461340003/"
            "il_75x75.7461340003_mrrk.jpg"
        )
        body = qq_idleCopy.html_to_text(
            '<a href="https://www.etsy.com/transaction/5211296760">'
            f'<img src="{product_image}">'
            "</a>"
            '<a href="https://www.etsy.com/transaction/5211296760">'
            "Luxury Leather Wedding Guest Book | Personalized Gold Foil Memory Book"
            "</a>"
            '<img src="https://i.etsystatic.com/site-assets/'
            'issue_resolution/purchase_protection/pp_2x_mail_icon.png">'
        )

        self.assertEqual(
            body.product_titles,
            ["Luxury Leather Wedding Guest Book | Personalized Gold Foil Memory Book"],
        )
        self.assertEqual(body.product_images, [product_image])

    def test_multi_item_listing_thumbnails_follow_each_transaction(self):
        html = (
            '<div><img src="https://cdn.example/item-a.jpg">'
            '<a href="https://www.etsy.com/transaction/111">Product A</a>'
            '<span>Cover Colour: Olive Green</span>'
            '<span>Transaction ID: 111</span><span>Quantity: 1</span>'
            '<span>Price: CA$10.00</span></div>'
            '<div><img data-src="https://cdn.example/item-b.jpg">'
            '<a href="https://www.etsy.com/transaction/222">Product B</a>'
            '<span>Cover Colour: Rose Red</span>'
            '<span>Transaction ID: 222</span><span>Quantity: 2</span>'
            '<span>Price: CA$20.00</span></div>'
            '<div>Shop: LuxeJoy</div>'
        )

        body = qq_idleCopy.html_to_text(html)
        parsed = qq_idleCopy.parse_mail_order_items(
            "You made a sale on Etsy - Order #4160334885",
            body,
        )

        self.assertEqual(body.product_images, [
            "https://cdn.example/item-a.jpg",
            "https://cdn.example/item-b.jpg",
        ])
        self.assertEqual(
            [item["商品信息"][qq_idleCopy.PRODUCT_IMAGE_FIELD] for item in parsed["items"]],
            ["https://cdn.example/item-a.jpg", "https://cdn.example/item-b.jpg"],
        )

    def test_html_listing_link_stays_separate_from_following_options(self):
        html = (
            '<div><a href="https://www.etsy.com/transaction/5201101150">'
            "Personalized Wedding Guest Book: Rose Red PU Leather with Gold Foil"
            "</a><span>Book Size | Page Count: 10*8 | 100 sheets</span>"
            "<span>Cover Colour: Rose Red</span></div>"
        )

        body = qq_idleCopy.html_to_text(html)
        self.assertEqual(
            body.product_title,
            "Personalized Wedding Guest Book: Rose Red PU Leather with Gold Foil",
        )
        result = qq_idleCopy.parse_order_fields(
            "You made a sale on Etsy - Order #4160334885",
            body + "\nShop: LuxeJoy",
            "",
        )

        self.assertEqual(
            result["产品"],
            "Personalized Wedding Guest Book: Rose Red PU Leather with Gold Foil",
        )
        self.assertEqual(
            result["商品信息"]["Book Size | Page Count"],
            "10*8 | 100 sheets",
        )

    def test_inline_listing_options_are_split_from_product_title(self):
        body = "\n".join(
            [
                "Sell with confidence",
                "Learn about Etsy Seller Protection.",
                'Personalized Wedding Guest Book – Linen Photo Album, Instax Polaroid Compatible Pages Quantity | Album Size: 50 printed | 10x7" Instant photo size: 2x6" Photo Booth',
                "Cover Colour: Olive Green",
                "Inner Page Layout: 10 inchx 7 inch - S3",
                "Font Style & Lettering color: #15 and can I get gold foiling?",
                "Names/date/location for the cover: Allison & Niklas October 9, 2026",
                "Phone Number for Delivery: 4034652865",
                "Shop: LuxeJoy",
                "Transaction ID: 5201101150",
                "Quantity: 1",
                "Price: CA$185.00",
            ]
        )

        result = qq_idleCopy.parse_order_fields(
            "You made a sale on Etsy - Order #4162545552",
            body,
            "",
        )

        self.assertEqual(
            result["产品"],
            "Personalized Wedding Guest Book – Linen Photo Album, Instax Polaroid Compatible",
        )
        self.assertEqual(
            result["商品信息"]["Pages Quantity | Album Size"],
            '50 printed | 10x7"',
        )
        self.assertEqual(
            result["商品信息"]["Instant photo size"],
            '2x6" Photo Booth',
        )
        self.assertEqual(
            result["商品信息"]["Inner Page Layout"],
            "10 inchx 7 inch - S3",
        )

    def test_colon_in_listing_title_is_not_parsed_as_product_option(self):
        body = "\n".join(
            [
                "Sell with confidence",
                "Learn about Etsy Seller Protection.",
                "Personalized Wedding Guest Book: Rose Red PU Leather with Gold Foil",
                "Book Size | Page Count: 10*8 | 100 sheets",
                "Inside Page Options: Blank Pages",
                "Cover Colour: Rose Red",
                "Font Style & Lettering color: #3 and Silver",
                "Names/date/location for the cover: A | S Amy & Stu 20.11.2026",
                "Phone Number for Delivery: 07444583140",
                "Shop: LuxeJoy",
                "Transaction ID: 5198924786",
                "Quantity: 1",
                "Price: CA$134.00",
            ]
        )

        result = qq_idleCopy.parse_order_fields(
            "You made a sale on Etsy - Order #4160334885",
            body,
            "",
        )

        self.assertEqual(
            result["产品"],
            "Personalized Wedding Guest Book: Rose Red PU Leather with Gold Foil",
        )
        self.assertEqual(
            result["商品信息"],
            {
                "Book Size | Page Count": "10*8 | 100 sheets",
                "Inside Page Options": "Blank Pages",
                "Cover Colour": "Rose Red",
                "Font Style & Lettering color": "#3 and Silver",
                "Names/date/location for the cover": "A | S Amy & Stu 20.11.2026",
                "Phone Number for Delivery": "07444583140",
            },
        )

    def test_missing_product_match_does_not_block_order_handler(self):
        handled = []
        order = {
            "订单号": "4160285572",
            "店铺": "Memiya",
            "店铺名": "10号店",
            "产品": "Unknown listing title",
            "商品信息": {},
            "付款方式": "",
            "邮寄地址": "",
            "交易编号": "",
            "数量": "1",
            "价格": "",
        }

        with (
            patch.object(qq_idleCopy, "extract_message_body", return_value="body"),
            patch.object(
                qq_idleCopy,
                "extract_mail_order_identity",
                return_value={"original_shop": "Memiya", "shop_name": "10号店"},
            ),
            patch.object(qq_idleCopy, "parse_order_fields", return_value=order),
            patch.object(qq_idleCopy, "save_last_uid") as save_last_uid,
        ):
            last_uid = qq_idleCopy.process_new_messages(
                FakeMailClient(),
                1829,
                order_handler=lambda payload, **kwargs: handled.append(
                    (payload, kwargs)
                ),
                shop_lookup=lambda *args: {
                    "id": 10,
                    "shop": "Memiya",
                    "shop_name": "10号店",
                },
                product_lookup=lambda *args: None,
            )

        self.assertEqual(last_uid, 1830)
        self.assertEqual(len(handled), 1)
        self.assertIs(handled[0][0], order)
        self.assertEqual(handled[0][1]["uid"], 1830)
        save_last_uid.assert_called_once_with(1830)

    def test_multiple_transaction_items_are_split_without_cross_contamination(self):
        body = "\n".join([
            "Personalized Wedding Guest Book A",
            "Book Size | Page Count: 10*8",
            "Cover Colour: Red",
            "Shop: LuxeJoy",
            "Transaction ID: 111",
            "Quantity: 1",
            "Price: US$84.00",
            "Personalized Wedding Guest Book B",
            "Book Size | Page Count: 12*12",
            "Cover Colour: Blue",
            "Shop: LuxeJoy",
            "Transaction ID: 222",
            "Quantity: 2",
            "Price: US$90.00",
        ])
        parsed = qq_idleCopy.parse_mail_order_items(
            "You made a sale on Etsy - Order #4161234567", body, ""
        )
        self.assertEqual([item["交易编号"] for item in parsed["items"]], ["111", "222"])
        self.assertEqual([item["产品"] for item in parsed["items"]], [
            "Personalized Wedding Guest Book A",
            "Personalized Wedding Guest Book B",
        ])
        self.assertEqual(parsed["items"][0]["商品信息"]["Cover Colour"], "Red")
        self.assertEqual(parsed["items"][1]["商品信息"]["Cover Colour"], "Blue")
        self.assertEqual(parsed["items"][0]["数量"], 1)
        self.assertEqual(parsed["items"][1]["数量"], 2)

    def test_listing_specific_options_stay_with_each_transaction(self):
        body = "\n".join([
            "Personalized Self-Adhesive Photo Album | Couples & Family Memory Book",
            "Album Color: Light Camel",
            "Quantity of sheets: 20 sheets",
            "Cover Personalization: Line 1: TO MAMA",
            "Line 2: LOVE, SAB",
            "Spine Personalization: MOTHER OF THE BRIDE",
            "Font: 02",
            "Shop: ObiaMoment",
            "Transaction ID: 5185167459",
            "Quantity: 1",
            "Price:",
            "US$84.00",
            "Personalized Self-Adhesive Photo Album | Couples & Family Memory Book",
            "Album Color: Light Camel",
            "Quantity of sheets: 20 sheets",
            "Cover Personalization: Line 1: TO STEPHY",
            "Line 2: LOVE, SAB",
            "Spine Personalization: MAID OF HONOR",
            "Font: 02",
            "Shop: ObiaMoment",
            "Transaction ID: 5181922006",
            "Quantity: 1",
            "Price:",
            "US$84.00",
        ])
        parsed = qq_idleCopy.parse_mail_order_items(
            "You made a sale on Etsy - Order #4147774001", body, ""
        )
        self.assertEqual(
            parsed["items"][0]["商品信息"]["Cover Personalization"],
            "Line 1: TO MAMA LOVE, SAB",
        )
        self.assertEqual(
            parsed["items"][1]["商品信息"]["Spine Personalization"],
            "MAID OF HONOR",
        )

    def test_email_sent_at_uses_only_the_message_date_header(self):
        parsed = qq_idleCopy.parse_mail_order_items(
            "You made a sale on Etsy - Order #4161234567",
            "\n".join([
                "Order date: January 1, 2025",
                "Wedding Guest Book",
                "Shop: LuxeJoy",
                "Transaction ID: 111",
                "Quantity: 1",
                "Price: US$84.00",
            ]),
            "Tue, 02 Sep 2026 19:49:00 +0800",
        )
        self.assertEqual(
            parsed["group"]["email_sent_at"], "2026-09-02 04:49:00"
        )


if __name__ == "__main__":
    unittest.main()
