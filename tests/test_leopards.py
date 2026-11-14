import unittest

from leopards import is_leopards_tracking_number, parse_tracking_history


class LeopardsTrackingTests(unittest.TestCase):
    def test_recognizes_leopards_cn(self):
        self.assertTrue(is_leopards_tracking_number("LE7548770588"))
        self.assertTrue(is_leopards_tracking_number("le7548770588"))
        self.assertFalse(is_leopards_tracking_number("223139469655969"))

    def test_history_is_normalized_oldest_first(self):
        page = """
        <div class="tracking-date">13 November, 2026 <span>(18:16)</span></div>
        <div class="tracking-content">Inter Transfer at TAMIRGARAHA</div>
        <div class="tracking-date">11 November, 2026 <span>(23:17)</span></div>
        <div class="tracking-content">Shipment Picked at ORIGIN LAHORE</div>
        """
        history = parse_tracking_history(page, "LE7548770588")
        self.assertEqual([row["ProcessDescForPortal"] for row in history], [
            "Shipment Picked at ORIGIN LAHORE", "Inter Transfer at TAMIRGARAHA",
        ])
        self.assertEqual(history[-1]["TransactionDate"], "2026-11-13 18:16:00")
        self.assertEqual(history[-1]["TrackingSource"], "Leopards Direct")


if __name__ == "__main__":
    unittest.main()
