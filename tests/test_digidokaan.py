import asyncio
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import digidokaan


class DigiDokaanTrackingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        digidokaan._status_cache.clear()
        digidokaan._inflight.clear()

    def test_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(digidokaan.configuration())

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


if __name__ == "__main__":
    import unittest
    unittest.main()
