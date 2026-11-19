import ast
import asyncio
from pathlib import Path
import re
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
names = {
    "is_delivered_status",
    "normalize_status_bucket",
    "is_pending_line_item_status",
    "is_need_attention_status",
    "aggregate_order_status",
}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
build_pending_function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "build_pending_orders_mobile_data"
)
revalidate_function = next(
    node for node in tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "revalidate_pending_trax_shipments"
)
namespace = {"re": re}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class PendingTraxTrackingTests(unittest.TestCase):
    def test_only_booked_and_unbooked_shipments_remain_pending(self):
        self.assertTrue(namespace["is_pending_line_item_status"]("Shipment - Booked"))
        self.assertTrue(namespace["is_pending_line_item_status"]("Un-Booked"))
        for dispatched in (
            "Shipment - Rider Picked", "Picked From Shipper", "Arrived At Origin",
            "Shipment - In Transit", "Out For Delivery", "Delivery Failed",
            "Being Return", "Returned to Shipper", "Delivered",
            "Inter Transfer at TAMIRGARAHA",
        ):
            with self.subTest(dispatched=dispatched):
                self.assertFalse(namespace["is_pending_line_item_status"](dispatched))

    def test_shipment_prefixed_delivered_status_is_normalized(self):
        self.assertEqual(namespace["normalize_status_bucket"]("Shipment - Delivered"), "Delivered")
        self.assertEqual(namespace["aggregate_order_status"]([{"status": "Shipment Delivered"}]), "Delivered")

    def test_order_with_delivered_and_unbooked_items_has_mixed_status(self):
        items = [
            {"tracking_number": "2232369665557", "status": "Delivered"},
            {"tracking_number": "N/A", "status": "Un-Booked"},
        ]
        self.assertEqual(namespace["aggregate_order_status"](items), "Mixed Status")

    def test_same_item_status_is_preserved(self):
        items = [
            {"tracking_number": "2232369665557", "status": "Delivered"},
            {"tracking_number": "2232369665557", "status": "Delivered"},
        ]
        self.assertEqual(namespace["aggregate_order_status"](items), "Delivered")

    def test_out_for_delivery_item_drives_order_counter_status(self):
        items = [{"tracking_number": "223232369716651", "status": "Out for Delivery"}]
        self.assertEqual(namespace["aggregate_order_status"](items), "Out For Delivery")

    def test_call_courier_return_is_removed_from_pending(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Aiohttp:
            @staticmethod
            def ClientSession():
                return Client()

        async def call_courier_history(_client, _tracking_number):
            return [
                {"ProcessDescForPortal": "Booked", "TransactionDate": "2026-10-14T00:00:00"},
                {"ProcessDescForPortal": "RETURN SUBMITTED", "TransactionDate": "2026-11-03T20:26:00"},
            ]

        revalidate_namespace = {
            "asyncio": asyncio,
            "aiohttp": Aiohttp,
            "is_trax_tracking_number": lambda _value: False,
            "is_tcs_tracking_number": lambda _value: False,
            "is_leopards_tracking_number": lambda _value: False,
            "is_pending_line_item_status": namespace["is_pending_line_item_status"],
            "fetch_tracking_data": call_courier_history,
            "fetch_tcs_tracking_history": None,
            "fetch_leopards_tracking_history": None,
            "fetch_resolved_tracking_snapshot": None,
            "apply_followup_timeline_dates": lambda _item, _history: None,
            "aggregate_order_status": namespace["aggregate_order_status"],
        }
        exec(compile(ast.Module(body=[revalidate_function], type_ignores=[]), "main.py", "exec"), revalidate_namespace)
        orders = [{"status": "Booked", "line_items": [{
            "tracking_number": "10412820010066",
            "tracking_source": "Call Courier",
            "status": "Booked",
        }]}]

        updated = asyncio.run(revalidate_namespace["revalidate_pending_trax_shipments"](orders))

        self.assertEqual(updated, 1)
        self.assertEqual(orders[0]["line_items"][0]["status"], "RETURN SUBMITTED")
        self.assertEqual(orders[0]["status"], "Need Attention")
        self.assertFalse(namespace["is_pending_line_item_status"](orders[0]["line_items"][0]["status"]))

    def test_dispatched_tag_does_not_hide_remaining_unbooked_items(self):
        async def no_revalidation(_orders):
            return 0

        build_namespace = {
            "asyncio": asyncio,
            "refresh_daraz_cache_if_needed": lambda: None,
            "revalidate_pending_trax_shipments": no_revalidation,
            "order_details": [{
                "id": 981646400,
                "order_num": "981646400",
                "order_id": "981646400",
                "order_link": "https://example.com/order",
                "status": "Mixed Status",
                "tags": ["DigiDokaan", "Dispatched (2026-10-28)"],
                "customer_details": {"name": "Customer", "city": "Lahore"},
                "created_at": "2026-10-26",
                "financial_status": "Paid",
                "line_items": [{
                    "product_title": "Pending shawl",
                    "tracking_number": "N/A",
                    "status": "Un-Booked",
                    "quantity": 1,
                    "unit_price": 18049,
                }],
            }],
            "daraz_orders": [],
            "load_order_statuses": lambda: {},
            "load_product_cost_overrides": lambda: {},
            "get_active_shopify_products": lambda: [],
            "find_cost_override_for_item": lambda *args, **kwargs: None,
            "parse_money": lambda value=0, *args: float(value or 0),
            "PAID_FINANCIAL_STATUSES": {"paid", "partially_paid"},
            "normalize_status_bucket": namespace["normalize_status_bucket"],
            "is_pending_line_item_status": namespace["is_pending_line_item_status"],
            "is_lahore_city": lambda city: str(city).casefold() == "lahore",
            "parse_date_timestamp": lambda value: value,
        }
        exec(compile(ast.Module(body=[build_pending_function], type_ignores=[]), "main.py", "exec"), build_namespace)

        rows = build_namespace["build_pending_orders_mobile_data"]()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["items_list"][0]["item_title"], "Pending shawl")

    def test_tags_do_not_hide_a_genuinely_booked_duplicate(self):
        async def no_revalidation(_orders):
            return 0

        build_namespace = {
            "asyncio": asyncio,
            "refresh_daraz_cache_if_needed": lambda: None,
            "revalidate_pending_trax_shipments": no_revalidation,
            "order_details": [{
                "id": 981626600,
                "order_num": "981626600",
                "tags": ["DigiDokaan", "Return Received (2026-10-28)"],
                "line_items": [{
                    "product_title": "Returned shawl", "tracking_number": "223139469269068",
                    "status": "Booked", "quantity": 1, "unit_price": 45249,
                }],
            }],
            "daraz_orders": [],
            "load_order_statuses": lambda: {},
            "load_product_cost_overrides": lambda: {},
            "get_active_shopify_products": lambda: [],
            "find_cost_override_for_item": lambda *args, **kwargs: None,
            "parse_money": lambda value=0, *args: float(value or 0),
            "PAID_FINANCIAL_STATUSES": {"paid", "partially_paid"},
            "normalize_status_bucket": namespace["normalize_status_bucket"],
            "is_pending_line_item_status": namespace["is_pending_line_item_status"],
            "is_lahore_city": lambda _city: False,
            "parse_date_timestamp": lambda value: value,
        }
        exec(compile(ast.Module(body=[build_pending_function], type_ignores=[]), "main.py", "exec"), build_namespace)

        rows = build_namespace["build_pending_orders_mobile_data"]()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "981626600")


if __name__ == "__main__":
    unittest.main()
