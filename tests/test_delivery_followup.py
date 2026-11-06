from pathlib import Path
from unittest.mock import patch
import unittest

import main


def order(order_id, status, items=None, tags=None):
    return {
        "id": int(order_id), "order_id": str(order_id), "created_at": "2026-11-01",
        "status": status, "tags": tags or ["Dispatched (2026-11-01)"],
        "line_items": items if items is not None else [{"product_title": "Shawl", "status": status, "quantity": 1}],
        "customer_details": {"name": "Customer", "phone": "0300", "city": "Lahore", "address": "Street"},
        "total_price": 1000,
    }


class DeliveryFollowupTests(unittest.TestCase):
    def setUp(self):
        self.original_orders = main.order_details

    def tearDown(self):
        main.order_details = self.original_orders

    def test_failed_return_and_completed_return_classification(self):
        rows = main.build_undelivered_order_view([
            order(1, "Delivery Unsuccessful"),
            order(2, "Return - In Transit"),
            order(3, "Return - Delivered to Shipper"),
        ])
        by_id = {row["order_id"]: row for row in rows}
        self.assertEqual(by_id["1"]["followup_section"], "to_be_contacted")
        self.assertEqual(by_id["2"]["followup_section"], "to_be_returned")
        self.assertEqual(by_id["3"]["followup_section"], "return_missed")

    def test_normal_transit_and_out_for_delivery_are_excluded(self):
        rows = main.build_undelivered_order_view([order(1, "In Transit"), order(2, "Out for Delivery")])
        self.assertEqual(rows, [])

    def test_dated_return_and_claim_tags_are_resolved_but_plain_tags_are_not(self):
        self.assertEqual(main.build_undelivered_order_view([order(1, "Return To Sender", tags=["Return Received (2026-11-05)"])]), [])
        self.assertEqual(main.build_undelivered_order_view([order(2, "Return To Sender", tags=["Claim Received (2026-11-05)"])]), [])
        self.assertEqual(len(main.build_undelivered_order_view([order(3, "Return To Sender", tags=["Return Received"])])), 1)

    def test_partial_order_preserves_every_item(self):
        items = [
            {"product_title": "A", "status": "Delivery Failed", "tracking_number": "1", "quantity": 1},
            {"product_title": "B", "status": "Un-Booked", "tracking_number": "N/A", "quantity": 2},
        ]
        rows = main.build_undelivered_order_view([order(1, "Mixed Status", items=items)])
        self.assertEqual(len(rows), 1)
        self.assertEqual([item["product_title"] for item in rows[0]["line_items"]], ["A", "B"])

    def test_contact_history_changes_needs_contact_only(self):
        rows = [order(1, "Delivery Failed"), order(2, "Return - In Transit")]
        with patch.object(main, "load_delivery_followup_contacts", return_value={"1": [{"outcome": "Called"}]}):
            self.assertEqual(main.get_delivery_followup_summary(rows), {"needs_attention": 2, "needs_contact": 1})

    def test_tracking_timeline_populates_dispatch_and_return_activity_dates(self):
        item = {}
        main.apply_followup_timeline_dates(item, [
            {"ProcessDescForPortal": "Picked From Shipper", "TransactionDate": "2026-11-01 10:00:00"},
            {"ProcessDescForPortal": "Return - Confirm", "TransactionDate": "2026-11-04 11:00:00"},
            {"ProcessDescForPortal": "Return - In Transit", "TransactionDate": "2026-11-05 12:00:00"},
        ])
        self.assertEqual(item["dispatched_at"], "2026-11-01")
        self.assertEqual(item["return_marked_at"], "2026-11-04")

    def test_template_has_multi_product_modal_compact_single_layout_and_confirmations(self):
        source = (Path(__file__).parents[1] / "templates" / "undelivered.html").read_text()
        self.assertIn("{% for item in items %}", source)
        self.assertIn("products.classList.toggle('single'", source)
        self.assertIn(".order-preview-products.single", source)
        self.assertIn("/tracking-summary/", source)
        self.assertIn("window.confirm", source)
        self.assertIn("Mark Return Received", source)
        self.assertIn("Mark Claim Received", source)

    def test_undelivered_page_renders_all_products_and_return_actions(self):
        main.order_details = [order(8, "Return To Sender", items=[
            {"product_title": "Shawl A", "image_src": "/a.jpg", "status": "Return To Sender", "tracking_number": "111", "quantity": 1},
            {"product_title": "Shawl B", "image_src": "/b.jpg", "status": "Return To Sender", "tracking_number": "222", "quantity": 2},
        ])]
        with patch.object(main, "load_delivery_followup_contacts", return_value={}):
            html = main.app.test_client().get("/undelivered").get_data(as_text=True)
        self.assertIn("Shawl A", html)
        self.assertIn("Shawl B", html)
        self.assertIn("/a.jpg", html)
        self.assertIn("/b.jpg", html)
        self.assertIn("Mark Return Received", html)
        self.assertIn("Mark Claim Received", html)

    def test_return_resolutions_cancel_tag_and_remove_cache_immediately(self):
        for tag in ("Return Received", "Claim Received"):
            with self.subTest(tag=tag), \
                 patch.object(main, "cancel_order") as cancel, \
                 patch.object(main, "apply_shopify_order_tag", return_value=True) as add_tag:
                main.order_details = [order(88, "Return To Sender"), order(99, "Booked")]
                response = main.app.test_client().post("/apply_tag", json={"order_id": 88, "tag": tag})
                self.assertEqual(response.status_code, 200)
                cancel.assert_called_once_with(88)
                add_tag.assert_called_once_with(88, tag, include_date=True)
                self.assertEqual([row["id"] for row in main.order_details], [99])

    def test_contact_endpoint_validates_and_saves(self):
        with patch.object(main, "add_delivery_followup_contact", return_value={"id": 1, "outcome": "Call - No Response", "remarks": "Try later", "created_at": "now"}):
            response = main.app.test_client().post("/undelivered/contact", json={"order_id": "1", "outcome": "Call - No Response", "remarks": "Try later"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])

    def test_terminal_shipments_are_not_retracked_for_modal(self):
        main.order_details = [order(1, "Delivered", items=[{"status": "Delivered", "tracking_number": "123"}])]
        with patch.object(main, "fetch_tracking_data") as lookup:
            payload = main.build_tracking_summary_payload("123")
        lookup.assert_not_called()
        self.assertEqual(payload["status"], "Delivered")

    def test_homepage_receives_live_urgency_counts(self):
        captured = {}
        with patch.object(main, "refresh_daraz_cache_if_needed"), \
             patch.object(main, "fetch_pending_shipper_advice", return_value=[]), \
             patch.object(main, "build_employee_approval_items", return_value=[]), \
             patch.object(main, "get_abandoned_summary_safe", return_value={}), \
             patch.object(main, "get_delivery_followup_summary", return_value={"needs_attention": 4, "needs_contact": 3}), \
             patch.object(main, "render_template", side_effect=lambda template, **context: captured.update(context) or "ok"):
            response = main.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["delivery_followup_summary"], {"needs_attention": 4, "needs_contact": 3})


if __name__ == "__main__":
    unittest.main()
