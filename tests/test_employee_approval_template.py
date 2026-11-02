from pathlib import Path
import unittest


class EmployeeApprovalTemplateTests(unittest.TestCase):
    def test_approval_posts_shopify_resource_id_separately(self):
        template = (Path(__file__).parents[1] / "templates" / "track.html").read_text()

        self.assertIn("approval.shopify_id", template)
        self.assertIn("function approveEmployeeRequest(orderId, shopifyId, trackingNumber, requestedStatus)", template)
        self.assertIn("shopify_id: shopifyId", template)


if __name__ == "__main__":
    unittest.main()
