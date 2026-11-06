import unittest
from unittest.mock import patch

import shopify_protected_data as protected


class ShopifyProtectedDataTests(unittest.TestCase):
    @patch.object(protected, "get_graphql_token", return_value="token")
    @patch.object(protected, "get_graphql_endpoint", return_value="https://shop.test/graphql.json")
    @patch.object(protected.requests, "post")
    def test_adds_order_tags_with_graphql_installed_app(self, post, _endpoint, _token):
        response = post.return_value
        response.json.return_value = {
            "data": {"tagsAdd": {"node": {"id": "gid://shopify/Order/99"}, "userErrors": []}}
        }

        self.assertTrue(protected.add_order_tags(99, ["Delivered in Lahore (2026-11-02)"]))

        variables = post.call_args.kwargs["json"]["variables"]
        self.assertEqual(variables["id"], "gid://shopify/Order/99")
        self.assertEqual(variables["tags"], ["Delivered in Lahore (2026-11-02)"])
        self.assertIn("tagsAdd", post.call_args.kwargs["json"]["query"])
        response.raise_for_status.assert_called_once()

    @patch.object(protected, "get_graphql_token", return_value="token")
    @patch.object(protected, "get_graphql_endpoint", return_value="https://shop.test/graphql.json")
    @patch.object(protected.requests, "post")
    def test_surfaces_graphql_tag_user_errors(self, post, _endpoint, _token):
        post.return_value.json.return_value = {
            "data": {"tagsAdd": {"node": None, "userErrors": [{"message": "Order not found"}]}}
        }

        with self.assertRaisesRegex(RuntimeError, "Order not found"):
            protected.add_order_tags(99, ["Delivered"])

    @patch.object(protected, "get_graphql_token", return_value="token")
    @patch.object(protected, "get_graphql_endpoint", return_value="https://shop.test/graphql.json")
    @patch.object(protected.requests, "post")
    def test_cancels_order_without_automatic_refund(self, post, _endpoint, _token):
        post.return_value.json.return_value = {"data": {"orderCancel": {
            "job": {"id": "gid://shopify/Job/1", "done": False},
            "orderCancelUserErrors": [], "userErrors": [],
        }}}

        self.assertTrue(protected.cancel_order(99))

        variables = post.call_args.kwargs["json"]["variables"]
        self.assertEqual(variables["orderId"], "gid://shopify/Order/99")
        self.assertEqual(variables["refundMethod"], {"originalPaymentMethodsRefund": False})
        self.assertTrue(variables["restock"])

    @patch.object(protected, "get_graphql_token", return_value="token")
    @patch.object(protected, "get_graphql_endpoint", return_value="https://shop.test/graphql.json")
    @patch.object(protected.requests, "post")
    def test_fetches_and_formats_protected_customer_data_in_one_batch(self, post, _endpoint, _token):
        response = post.return_value
        response.json.return_value = {"data": {"nodes": [{
            "legacyResourceId": "99",
            "phone": "03210000000",
            "shippingAddress": {"name": "Sara Khan", "address1": "Block 2", "city": "Karachi"},
            "billingAddress": None,
            "customer": {
                "numberOfOrders": 4,
                "orders": {"nodes": [{
                    "legacyResourceId": "98", "name": "#98", "displayFulfillmentStatus": "FULFILLED",
                    "cancelledAt": None, "tags": ["Delivered"],
                }]},
            },
        }]}}

        details, errors = protected.fetch_protected_order_details([99])

        self.assertEqual(errors, [])
        self.assertEqual(details["99"]["name"], "Sara Khan")
        self.assertEqual(details["99"]["phone"], "03210000000")
        self.assertEqual(details["99"]["address"], "Block 2")
        self.assertEqual(details["99"]["city"], "Karachi")
        self.assertEqual(details["99"]["order_count"], 4)
        self.assertEqual(details["99"]["recent_orders"][0]["fulfillment_status"], "FULFILLED")
        self.assertIn("numberOfOrders", post.call_args.kwargs["json"]["query"])
        response.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
