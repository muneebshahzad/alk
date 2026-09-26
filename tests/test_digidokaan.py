import asyncio
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import digidokaan


class AdviceResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        return {"code": 200, "msg": "Shipper advice submitted successfully."}


class AdviceSession:
    def __init__(self):
        self.payload = None

    def post(self, url, **kwargs):
        self.payload = kwargs.get("json")
        return AdviceResponse()


class DigiDokaanTrackingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        digidokaan._status_cache.clear()
        digidokaan._inflight.clear()

    def test_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(digidokaan.configuration())

    def test_delivery_failure_reason_becomes_undelivered_status(self):
        body = {
            "data": {
                "tracking_response": {
                    "courier_status": "Awaiting Shipper advice",
                    "data": [
                        {
                            "status": "Shipment - Shipper Advise Requested",
                            "status_reason": "Consignee Refused",
                        },
                        {
                            "status": "Shipment - Out for Delivery",
                            "status_reason": None,
                        },
                    ],
                }
            }
        }
        self.assertEqual(
            digidokaan.display_status_from_detail(body, "Second Attempt"),
            "Undelivered - Consignee Refused",
        )

    async def test_concurrent_requests_are_deduplicated_and_cached(self):
        async def delayed_status(*args):
            await asyncio.sleep(0)
            return "Delivered"

        environment = {
            "DIGIDOKAAN_PHONE": "923000000000",
            "DIGIDOKAAN_PASSWORD": "secret",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            digidokaan, "_fetch_status", AsyncMock(side_effect=delayed_status)
        ) as fetch:
            statuses = await asyncio.gather(
                digidokaan.fetch_tracking_status(object(), "223 22367960798"),
                digidokaan.fetch_tracking_status(object(), "22322367960798"),
            )
            cached = await digidokaan.fetch_tracking_status(object(), "22322367960798")

        self.assertEqual(statuses, ["Delivered", "Delivered"])
        self.assertEqual(cached, "Delivered")
        fetch.assert_awaited_once()

    async def test_submit_shipper_advice_uses_validated_payload(self):
        session = AdviceSession()
        config = {
            "base_url": "https://digidokaan.pk",
            "phone": "923000000000",
            "password": "secret",
            "gateway_id": "5",
        }
        with patch.object(digidokaan, "configuration", return_value=config), patch.object(
            digidokaan, "_access_token", AsyncMock(return_value="private-token")
        ):
            result = await digidokaan.submit_shipper_advice(
                session, "223 17467960795", 5, "Reattempt", "Please attempt tomorrow"
            )

        self.assertEqual(result["code"], 200)
        self.assertEqual(
            session.payload,
            {
                "phone": "923000000000",
                "gateway_id": "5",
                "tracking_no": "22317467960795",
                "shipper_advice_status": "reattempt",
                "shipper_advice_remarks": "Please attempt tomorrow",
            },
        )

    async def test_submit_shipper_advice_requires_remarks(self):
        with self.assertRaisesRegex(ValueError, "Remarks are required"):
            await digidokaan.submit_shipper_advice(object(), "22317467960795", 5, "return", "")


if __name__ == "__main__":
    import unittest
    unittest.main()
