import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import shopify_protected_data


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
names = {"shopify_inventory_item_costs", "find_cost_override_for_item", "product_cost_key"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]


def parse_money(value, default=0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


namespace = {
    "requests": SimpleNamespace(post=Mock()),
    "get_graphql_endpoint": lambda: "https://store.myshopify.com/admin/api/2026-04/graphql.json",
    "get_graphql_token": lambda: "token",
    "parse_money": parse_money,
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class ShopifyProductCostTests(unittest.TestCase):
    def test_inventory_costs_are_read_from_shopify_unit_cost(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"nodes": [
            {"id": "gid://shopify/InventoryItem/100", "unitCost": {"amount": "2800.00", "currencyCode": "PKR"}},
            {"id": "gid://shopify/InventoryItem/101", "unitCost": None},
        ]}}
        namespace["requests"].post.return_value = response

        result = namespace["shopify_inventory_item_costs"]([100, 101, 100])

        self.assertEqual(result, {"100": 2800.0, "101": 0.0})
        variables = namespace["requests"].post.call_args.kwargs["json"]["variables"]
        self.assertEqual(variables["ids"], ["gid://shopify/InventoryItem/100", "gid://shopify/InventoryItem/101"])

    def test_saved_portal_override_remains_explicit(self):
        overrides = {"variant:9": {"cost": 3100}}
        self.assertEqual(namespace["find_cost_override_for_item"](overrides, variant_id=9), 3100)
        self.assertIsNone(namespace["find_cost_override_for_item"]({}, variant_id=9))

    def test_inventory_scopes_are_always_requested(self):
        with patch.dict("os.environ", {"SHOPIFY_GRAPHQL_SCOPES": "read_orders,read_products"}):
            scopes = shopify_protected_data.get_oauth_scopes()
        self.assertIn("read_inventory", scopes)
        self.assertIn("write_inventory", scopes)


if __name__ == "__main__":
    unittest.main()
