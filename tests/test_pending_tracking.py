import ast
from pathlib import Path
import re
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
names = {"is_delivered_status", "normalize_status_bucket", "is_pending_line_item_status"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
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
        ):
            with self.subTest(dispatched=dispatched):
                self.assertFalse(namespace["is_pending_line_item_status"](dispatched))


if __name__ == "__main__":
    unittest.main()
