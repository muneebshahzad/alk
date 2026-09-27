import ast
from pathlib import Path
import re
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
wanted = {
    "normalize_scan_term", "scan_term_candidates", "build_payment_operational_metrics",
    "_digidokaan_rows", "_payment_identifier", "build_digidokaan_payment_dashboard",
}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def parse_int(value, default=0):
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return int(default)


def normalize_status_bucket(value):
    return value or "Un-Booked"


namespace = {
    "re": re,
    "parse_money": parse_money,
    "parse_int": parse_int,
    "normalize_status_bucket": normalize_status_bucket,
    "PAID_FINANCIAL_STATUSES": {"paid", "partially_paid"},
    "order_details": [],
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class MobilePortalMetricsTests(unittest.TestCase):
    def test_scan_candidates_extract_order_number_from_barcode_url(self):
        candidates = namespace["scan_term_candidates"]("https://shop.test/orders/#981587900?label=abc")
        self.assertIn("981587900", candidates)

    def test_dispatched_cod_uses_shopify_order_value(self):
        namespace["order_details"] = [
            {"total_price": "7900", "financial_status": "pending", "line_items": [{"status": "Delivery In Transit"}]},
            {"total_price": "2500", "financial_status": "paid", "line_items": [{"status": "Delivered"}]},
            {"total_price": "1200", "financial_status": "pending", "line_items": [{"status": "Un-Booked"}]},
        ]
        metrics = {item["key"]: item for item in namespace["build_payment_operational_metrics"]()}
        self.assertEqual(metrics["dispatched"]["count"], 2)
        self.assertEqual(metrics["dispatched"]["value"], 10400)
        self.assertEqual(metrics["dispatched_cod"]["count"], 1)
        self.assertEqual(metrics["dispatched_cod"]["value"], 7900)

    def test_payment_ledger_merges_entries_and_only_marks_explicit_paid_cheque(self):
        payments = {
            "balance": {"deliver_orders_payments": 1200},
            "ledger": {
                "total_cod": 5000, "total_balance": 4600,
                "total_delivery_charges": 300, "total_sales_tax": 50, "total_income_tax": 50,
                "data": [
                    {"tracking_no": "T1", "order_no": "O1", "payment_type": "COD", "amount": 3000, "sub_amount": 0, "cheque_no": "C1"},
                    {"tracking_no": "T1", "order_no": "O1", "payment_type": "DC", "amount": 0, "sub_amount": 200, "cheque_no": "C1"},
                    {"tracking_no": "T2", "order_no": "O2", "payment_type": "COD", "amount": 2000, "sub_amount": 0},
                    {"tracking_no": "T2", "order_no": "O2", "payment_type": "DC", "amount": 0, "sub_amount": 200},
                ],
            },
            "cheques": [{"cheque_no": "C1", "status": "Paid", "amount": 2800, "shipments": {"data": []}}],
        }
        dashboard = namespace["build_digidokaan_payment_dashboard"](payments)
        shipments = {row["tracking_no"]: row for row in dashboard["shipments"]}
        self.assertEqual(len(shipments), 2)
        self.assertEqual(shipments["T1"]["net"], 2800)
        self.assertEqual(shipments["T1"]["payment_status"], "Paid")
        self.assertEqual(shipments["T2"]["net"], 1800)
        self.assertEqual(shipments["T2"]["payment_status"], "Not paid")

    def test_unpaid_cheque_status_is_never_classified_as_paid(self):
        payments = {
            "balance": {},
            "ledger": {"data": [{"tracking_no": "T1", "payment_type": "COD", "amount": 1000, "cheque_no": "C1"}]},
            "cheques": [{"cheque_no": "C1", "status": "Unpaid", "shipments": {"data": []}}],
        }
        shipment = namespace["build_digidokaan_payment_dashboard"](payments)["shipments"][0]
        self.assertNotEqual(shipment["payment_status"], "Paid")

    def test_cheque_details_add_paid_shipments_and_gross_cod_uses_dispatched_metric(self):
        payments = {
            "balance": {},
            "ledger": {"total_order_price": 1000, "data": []},
            "cheques": [{
                "cheque_no": "C1", "status": "Paid", "amount": 800,
                "shipments": {"data": [
                    {"tracking_no": "T1", "order_no": "O1", "payment_type": "COD", "price": 9000, "amount": 1000, "sub_amount": 0},
                    {"tracking_no": "T1", "order_no": "O1", "payment_type": "DC", "amount": 0, "sub_amount": 200},
                ]},
            }],
        }
        dashboard = namespace["build_digidokaan_payment_dashboard"](payments)
        self.assertEqual(dashboard["cards"][0]["label"], "Gross COD")
        self.assertEqual(dashboard["cards"][0]["value"], 9000)
        self.assertEqual(dashboard["cards"][0]["count"], 1)
        self.assertEqual(dashboard["shipments"][0]["payment_status"], "Paid")
        self.assertEqual(dashboard["shipments"][0]["net"], 800)


if __name__ == "__main__":
    unittest.main()
