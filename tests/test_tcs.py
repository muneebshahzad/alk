import unittest

from tcs import fetch_public_tracking_history, fetch_public_tracking_status, is_tcs_tracking_number


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return {
            "isSuccess": True,
            "responseData": {
                "message": "SUCCESS",
                "shipmentinfo": [{"consignee": "Saima Begum", "destination": "BATKHELA"}],
                "deliveryinfo": [{"status": "Delivered"}],
                "checkpoints": [
                    {"status": "Shipment Delivered", "datetime": "Tuesday Nov 10, 2026 19:00"},
                    {"status": "Shipment Picked Up", "datetime": "Friday Nov 6, 2026 20:00"},
                ],
            },
        }


class _Session:
    def __init__(self):
        self.payload = None

    def post(self, _url, **kwargs):
        self.payload = kwargs["json"]
        return _Response()


class TCSTrackingTests(unittest.IsolatedAsyncioTestCase):
    def test_tcs_cn_shape(self):
        self.assertTrue(is_tcs_tracking_number("173019564522"))
        self.assertFalse(is_tcs_tracking_number("123456789012"))
        self.assertFalse(is_tcs_tracking_number("22320269889521"))

    async def test_public_json_status_is_normalized(self):
        session = _Session()
        status = await fetch_public_tracking_status(session, "1730 1956 4522")
        self.assertEqual(status, "Delivered")
        self.assertEqual(session.payload["body"]["param"], "consignee=173019564522")

    async def test_public_history_is_chronological(self):
        history = await fetch_public_tracking_history(_Session(), "173019564522")
        self.assertEqual([row["ProcessDescForPortal"] for row in history], [
            "Shipment Picked Up", "Shipment Delivered",
        ])
        self.assertEqual(history[-1]["TrackingSource"], "TCS Direct")
        self.assertEqual(history[-1]["ConsigneeCity"], "BATKHELA")


if __name__ == "__main__":
    unittest.main()
