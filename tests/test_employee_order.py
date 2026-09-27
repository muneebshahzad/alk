import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
names = {"split_customer_name", "build_employee_invoice_payload", "create_shopify_employee_order"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


class FakeDraftOrder:
    instances = []

    def __init__(self):
        self.id = 44
        self.order_id = 99
        self.name = "#1001"
        self.complete_payload = None
        self.__class__.instances.append(self)

    def save(self):
        return True

    def complete(self, payload):
        self.complete_payload = payload
        return True

    @classmethod
    def find(cls, _draft_id):
        return cls.instances[-1]


namespace = {
    "json": json,
    "parse_money": parse_money,
    "shopify": SimpleNamespace(DraftOrder=FakeDraftOrder),
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class EmployeeOrderTests(unittest.TestCase):
    def setUp(self):
        FakeDraftOrder.instances.clear()

    def payload(self, payment_status):
        return {
            "customer_name": "Test Customer",
            "phone": "03000000000",
            "city": "Lahore",
            "address": "Test address",
            "payment_method": "Bank Deposit",
            "payment_status": payment_status,
            "custom_items": [{"title": "Test item", "price": 1000, "quantity": 1}],
        }

    def test_unpaid_order_is_completed_as_payment_pending(self):
        namespace["create_shopify_employee_order"](self.payload("Unpaid"))

        self.assertEqual(FakeDraftOrder.instances[-1].complete_payload, {"payment_pending": True})

    def test_only_explicit_paid_order_is_completed_as_paid(self):
        namespace["create_shopify_employee_order"](self.payload("Paid"))

        self.assertEqual(FakeDraftOrder.instances[-1].complete_payload, {"payment_pending": False})


if __name__ == "__main__":
    unittest.main()
