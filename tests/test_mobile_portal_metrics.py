import ast
from pathlib import Path
import re
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
wanted = {"normalize_scan_term", "scan_term_candidates", "build_payment_operational_metrics"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def normalize_status_bucket(value):
    return value or "Un-Booked"


namespace = {
    "re": re,
    "parse_money": parse_money,
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


if __name__ == "__main__":
    unittest.main()
