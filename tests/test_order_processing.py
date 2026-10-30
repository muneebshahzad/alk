import ast
import asyncio
import time
from pathlib import Path
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
function = next(
    node for node in source.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "safe_process_order"
)
process_line_item_function = next(
    node for node in source.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_line_item"
)
customer_functions = [
    node for node in source.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in {"get_order_attr", "get_resource_value", "build_shopify_customer_details", "load_shopify_customer_details"}
]


class OrderProcessingTests(unittest.TestCase):
    def test_partially_fulfilled_line_keeps_remaining_quantity_pending(self):
        async def fetch_digidokaan_tracking_status(_session, _tracking):
            return "Delivered"

        async def fetch_tracking_status(_session, _tracking):
            return "Delivered"

        namespace = {
            "fetch_digidokaan_tracking_status": fetch_digidokaan_tracking_status,
            "fetch_tracking_status": fetch_tracking_status,
            "clean_tracking_status_label": lambda value: value,
            "is_digidokaan_tracking_number": lambda value: str(value).startswith("223"),
            "tracking_url_for_number": lambda value: f"/track/{value}",
        }
        exec(compile(ast.Module(body=[process_line_item_function], type_ignores=[]), "main.py", "exec"), namespace)
        line_item = type("LineItem", (), {
            "id": 10,
            "quantity": 2,
            "fulfillment_status": "partial",
            "fulfillable_quantity": 1,
        })()
        fulfilled_item = type("FulfillmentItem", (), {"id": 10, "quantity": 1})()
        fulfillment = type("Fulfillment", (), {
            "status": "success",
            "tracking_number": "2232369665557",
            "line_items": [fulfilled_item],
        })()

        rows = asyncio.run(namespace["process_line_item"](None, line_item, [fulfillment]))

        self.assertEqual([(row["status"], row["quantity"]) for row in rows], [
            ("Delivered", 1),
            ("Un-Booked", 1),
        ])

    def test_limiter_can_be_used_by_repeated_refresh_event_loops(self):
        processed = []

        async def process_order(session, order):
            await asyncio.sleep(0)
            processed.append(order)
            return order

        namespace = {"asyncio": asyncio, "process_order": process_order}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)

        async def refresh(offset):
            return await asyncio.gather(*(
                namespace["safe_process_order"](None, offset + number)
                for number in range(12)
            ))

        first = asyncio.run(refresh(0))
        second = asyncio.run(refresh(100))

        self.assertEqual(first, list(range(12)))
        self.assertEqual(second, list(range(100, 112)))
        self.assertEqual(len(processed), 24)

    def test_customer_details_fall_back_to_customer_default_address(self):
        namespace = {}
        exec(compile(ast.Module(body=customer_functions, type_ignores=[]), "main.py", "exec"), namespace)
        order = type("Order", (), {
            "shipping_address": None,
            "billing_address": None,
            "phone": "",
            "email": "order@example.com",
            "customer": {
                "id": 12,
                "first_name": "Hina",
                "last_name": "Ali",
                "default_address": {
                    "address1": "Street 5",
                    "city": "Lahore",
                    "phone": "03001234567",
                },
            },
        })()

        details = namespace["build_shopify_customer_details"](order)

        self.assertEqual(details["name"], "Hina Ali")
        self.assertEqual(details["address"], "Street 5")
        self.assertEqual(details["city"], "Lahore")
        self.assertEqual(details["phone"], "03001234567")
        self.assertEqual(details["email"], "order@example.com")

    def test_customer_details_never_return_whitespace_placeholders(self):
        namespace = {}
        exec(compile(ast.Module(body=customer_functions, type_ignores=[]), "main.py", "exec"), namespace)
        order = type("Order", (), {
            "shipping_address": {"name": " ", "address1": "  ", "city": " ", "phone": " "},
            "billing_address": None,
            "customer": None,
        })()
        details = namespace["build_shopify_customer_details"](order)
        self.assertEqual(details["name"], "")
        self.assertEqual(details["address"], "")
        self.assertEqual(details["city"], "")
        self.assertEqual(details["phone"], "")

    def test_employee_order_note_recovers_missing_customer_fields(self):
        namespace = {}
        exec(compile(ast.Module(body=customer_functions, type_ignores=[]), "main.py", "exec"), namespace)
        order = type("Order", (), {
            "shipping_address": None,
            "billing_address": None,
            "customer": None,
            "note": "Customer: Sara Khan\nPhone: 03210000000\nCity: Karachi\nAddress: Block 2",
        })()
        details = namespace["build_shopify_customer_details"](order)
        self.assertEqual(details["name"], "Sara Khan")
        self.assertEqual(details["phone"], "03210000000")
        self.assertEqual(details["city"], "Karachi")
        self.assertEqual(details["address"], "Block 2")

    def test_order_number_is_never_used_as_customer_name(self):
        namespace = {"async_shopify_fetch": None}
        exec(compile(ast.Module(body=customer_functions, type_ignores=[]), "main.py", "exec"), namespace)
        order = type("Order", (), {
            "name": "#981596200",
            "created_at": "2099-09-29T12:00:00+00:00",
            "shipping_address": None,
            "billing_address": None,
            "customer": None,
        })()
        self.assertEqual(namespace["build_shopify_customer_details"](order)["name"], "")

    def test_missing_fields_do_not_trigger_restricted_rest_requests(self):
        namespace = {}
        exec(compile(ast.Module(body=customer_functions, type_ignores=[]), "main.py", "exec"), namespace)
        order = type("Order", (), {
            "id": 99, "name": "#981596200", "shipping_address": None,
            "billing_address": None, "customer": {"id": 12},
        })()
        details = asyncio.run(namespace["load_shopify_customer_details"](None, order))
        self.assertEqual(details["id"], "12")
        self.assertEqual(details["name"], "")


if __name__ == "__main__":
    unittest.main()
