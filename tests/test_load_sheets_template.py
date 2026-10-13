import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


class LoadSheetsTemplateTests(unittest.TestCase):
    def test_history_shows_date_count_amount_and_download(self):
        templates = Path(__file__).resolve().parents[1] / "templates"
        html = Environment(loader=FileSystemLoader(templates)).get_template("load_sheets.html").render(
            load_sheets=[{
                "created_at": "2026-10-13T18:45:00", "shipment_count": 68,
                "amount": 245000, "tracking_numbers": ["22300000000001"],
                "link": "https://web.digidokaan.pk/load-sheet.pdf",
            }]
        )
        self.assertIn("68", html)
        self.assertIn("PKR 245,000", html)
        self.assertIn("Download / Print Load Sheet", html)
        self.assertIn("2026-10-13T18:45:00", html)


if __name__ == "__main__":
    unittest.main()
