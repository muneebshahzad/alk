import ast
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock


source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
function_names = {
    "parse_money",
    "as_dict",
    "get_abandoned_created_at_min",
    "graphql_money_amount_and_currency",
    "graphql_checkout_address",
    "graphql_checkout_line_item",
    "graphql_abandoned_checkout_to_rest",
    "fetch_shopify_abandoned_checkouts_graphql",
}
functions = [
    node for node in source.body
    if isinstance(node, ast.FunctionDef) and node.name in function_names
]


def build_namespace(requests, token="token", endpoint="https://shop.test/graphql.json"):
    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "requests": requests,
        "get_graphql_token": lambda: token,
        "get_graphql_endpoint": lambda: endpoint,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


class AbandonedCheckoutGraphQLTests(unittest.TestCase):
    def test_maps_protected_checkout_customer_and_items(self):
        namespace = build_namespace(Mock())
        checkout = namespace["graphql_abandoned_checkout_to_rest"]({
            "id": "gid://shopify/AbandonedCheckout/1",
            "name": "#123",
            "createdAt": "2026-09-29T10:00:00Z",
            "customer": {
                "firstName": "Ayesha",
                "lastName": "Khan",
                "numberOfOrders": "2",
                "defaultEmailAddress": {"emailAddress": "ayesha@example.com"},
                "defaultPhoneNumber": {"phoneNumber": "+923001234567"},
            },
            "shippingAddress": {
                "name": "Ayesha Khan",
                "address1": "Street 1",
                "city": "Lahore",
                "country": "Pakistan",
                "countryCodeV2": "PK",
                "phone": "+923001234567",
            },
            "totalPriceSet": {"presentmentMoney": {"amount": "4648.00", "currencyCode": "PKR"}},
            "subtotalPriceSet": {"presentmentMoney": {"amount": "4499.00", "currencyCode": "PKR"}},
            "lineItems": {"nodes": [{
                "id": "line-1",
                "title": "Pashmina Shawl",
                "variantTitle": "Tan",
                "quantity": 1,
                "image": {"url": "https://cdn.example/item.jpg"},
                "discountedUnitPriceSet": {"presentmentMoney": {"amount": "4499.00", "currencyCode": "PKR"}},
                "product": {"legacyResourceId": "11"},
                "variant": {"legacyResourceId": "22"},
            }]},
        })

        self.assertEqual(checkout["customer"]["first_name"], "Ayesha")
        self.assertEqual(checkout["phone"], "+923001234567")
        self.assertEqual(checkout["shipping_address"]["city"], "Lahore")
        self.assertEqual(checkout["total_price"], 4648.0)
        self.assertEqual(checkout["line_items"][0]["image_url"], "https://cdn.example/item.jpg")

    def test_fetches_all_graphql_pages(self):
        post = Mock()
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "data": {"abandonedCheckouts": {
                "nodes": [{"id": "one", "name": "#1"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            }}
        }
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "data": {"abandonedCheckouts": {
                "nodes": [{"id": "two", "name": "#2"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}
        }
        post.side_effect = [first, second]
        requests = Mock(post=post)
        namespace = build_namespace(requests)

        rows = namespace["fetch_shopify_abandoned_checkouts_graphql"](7)

        self.assertEqual([row["token"] for row in rows], ["#1", "#2"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["variables"]["after"], "cursor-1")


if __name__ == "__main__":
    unittest.main()
