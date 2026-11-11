from pathlib import Path
import unittest


class DashboardOrderPreviewTemplateTests(unittest.TestCase):
    def test_shopify_cards_include_shared_style_order_preview(self):
        template = (Path(__file__).parents[1] / "templates" / "track.html").read_text()

        self.assertIn("openDashboardOrderPreview('{{ order.id }}')", template)
        self.assertIn('id="dashboardOrderPreview"', template)
        self.assertIn("items.map(item=>", template)
        self.assertIn("object-fit:contain!important", template)
        self.assertIn("/tracking-summary/", template)
        self.assertIn("Promise.all(numbers.map", template)
        self.assertIn("No courier tracking is attached to this order.", template)


if __name__ == "__main__":
    unittest.main()
