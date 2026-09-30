from pathlib import Path
import unittest

from flask import Flask, render_template


class BookingsTemplateTests(unittest.TestCase):
    def test_populated_order_and_log_render_without_dict_items_collision(self):
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        order = {
            "id": "1", "number": "100", "date": "2026-09-30", "total": 5000, "cod": 5000,
            "customer": {"name": "Customer", "phone": "03000000000", "address": "Address", "city": "Lahore"},
            "city_match": {"id": "1", "name": "Lahore", "services": ["OVERNIGHT"]},
            "items": [{"line_item_id": "10", "product_title": "Shawl", "image_src": "", "sku": "S1", "quantity": 1, "fulfillable_quantity": 1}],
        }
        log = {
            "booked_at": "2026-09-30T12:00:00", "order_number": "100", "order_no": "20",
            "customer_name": "Customer", "items": [{"title": "Shawl", "quantity": 1}],
            "tracking_url": "https://track.alkaramat.com/123", "tracking_no": "123", "city": "Lahore",
            "service_type": "OVERNIGHT", "cod_amount": 5000, "shopify_fulfilled": True,
        }
        with app.test_request_context("/bookings"):
            result = render_template(
                "bookings.html", booking_orders=[order], booking_logs=[log], cities=[order["city_match"]],
                booking_error="", embedded_mode=False, skip_base_password_prompt=True,
            )
        self.assertIn("Shawl", result)
        self.assertIn("123", result)


if __name__ == "__main__":
    unittest.main()
