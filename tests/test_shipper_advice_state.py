import unittest

from shipper_advice_state import advice_request_key, remove_acknowledged_advice


class ShipperAdviceStateTests(unittest.TestCase):
    def test_acknowledged_request_is_removed(self):
        row = {
            "tracking_no": "22317467960795",
            "status_date": "2026-09-25 10:30:00",
            "courier_status_reason": "Consignee Refused",
            "gateway_id": 5,
        }
        self.assertEqual([], remove_acknowledged_advice([row], {advice_request_key(row)}))

    def test_later_request_for_same_tracking_remains_visible(self):
        old = {"tracking_no": "22317467960795", "status_date": "2026-09-25", "courier_status_reason": "Consignee Refused"}
        new = {"tracking_no": "22317467960795", "status_date": "2026-10-01", "courier_status_reason": "Consignee Refused"}
        self.assertEqual([new], remove_acknowledged_advice([new], {advice_request_key(old)}))

    def test_key_is_stable_when_dictionary_order_changes(self):
        first = {"tracking_no": "123", "status_date": "2026-10-01", "gateway_id": "5"}
        second = {"gateway_id": "5", "status_date": "2026-10-01", "tracking_no": "123"}
        self.assertEqual(advice_request_key(first), advice_request_key(second))


if __name__ == "__main__":
    unittest.main()
