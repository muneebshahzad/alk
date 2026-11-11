import unittest

from tcs import fetch_public_tracking_status, is_tcs_tracking_number


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
                "deliveryinfo": [{"status": "Delivered"}],
                "checkpoints": [{"status": "Shipment Picked Up"}],
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
        self.assertFalse(is_tcs_tracking_number("22320269889521"))

    async def test_public_json_status_is_normalized(self):
        session = _Session()
        status = await fetch_public_tracking_status(session, "1730 1956 4522")
        self.assertEqual(status, "Delivered")
        self.assertEqual(session.payload["body"]["param"], "consignee=173019564522")


if __name__ == "__main__":
    unittest.main()
