import ast
import hashlib
from pathlib import Path
import re
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_trax_booking_orders"]
namespace = {
    "order_details": [], "hashlib": hashlib, "re": re,
    "parse_money": lambda value: float(value or 0),
    "match_trax_city": lambda name, cities: cities[0] if cities else None,
    "PAID_FINANCIAL_STATUSES": {"paid"},
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class BookingOrderTests(unittest.TestCase):
    def test_same_phone_and_address_are_grouped_and_history_excludes_current_order(self):
        customer = {
            "name": "Customer", "phone": "+923001234567", "address": "House 1, Main Road", "city": "Lahore",
            "order_count": 5,
            "recent_orders": [
                {"id": "1", "name": "#1", "fulfillment_status": "UNFULFILLED", "tags": []},
                {"id": "9", "name": "#9", "fulfillment_status": "FULFILLED", "tags": []},
            ],
        }
        item = {"line_item_id": "10", "fulfillable_quantity": 1, "quantity": 1, "tracking_number": "N/A", "product_title": "Shawl"}
        namespace["order_details"] = [
            {"id": str(order_id), "order_num": str(order_id), "created_at": "2026-09-30", "customer_details": dict(customer), "line_items": [dict(item, line_item_id=str(order_id))], "total_price": 1000, "financial_status": "Pending"}
            for order_id in (1, 2)
        ]
        rows = namespace["build_trax_booking_orders"]([{"id": "1", "name": "Lahore", "services": ["OVERNIGHT"]}])
        self.assertEqual(rows[0]["duplicate_group"], rows[1]["duplicate_group"])
        self.assertEqual(rows[0]["duplicate_count"], 2)
        self.assertEqual(rows[0]["customer_order_count"], 5)
        self.assertEqual(rows[0]["last_order_status"], "Delivered")
        self.assertEqual(rows[0]["last_order_name"], "#9")


if __name__ == "__main__":
    unittest.main()
