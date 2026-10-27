import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import trax


class FakeResponse:
    def __init__(self, body="", status=200, json_body=None):
        self.body, self.status, self.json_body = body, status, json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return self.body

    async def json(self, content_type=None):
        return self.json_body


class FakeSession:
    def __init__(self, payload):
        self.payload, self.post_data, self.post_headers = payload, None, None

    def get(self, *_args, **_kwargs):
        return FakeResponse("<script>data: {'_token': 'public-token'}</script>")

    def post(self, *_args, **kwargs):
        self.post_data, self.post_headers = kwargs.get("data"), kwargs.get("headers")
        return FakeResponse(json_body=self.payload)


class TraxPublicTrackingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trax._history_cache.clear()
        trax._inflight.clear()

    def test_csrf_and_json_history_are_normalized_oldest_first(self):
        self.assertEqual(trax._csrf_token("'_token': 'abc123'"), "abc123")
        self.assertEqual(trax._csrf_token('<meta name="csrf-token" content="xyz">'), "xyz")
        history = trax._portal_history({"shipments": {"1": {
            "tracking_number": "22322660078220",
            "consignee": {"name": "Customer", "destination": "Lahore"},
            "tracking_history": [
                {"date_time": "2026-10-28 12:00:00", "status": "Shipment - In Transit"},
                {"date_time": "2026-10-27 10:00:00", "status": "Shipment - Booked"},
            ],
        }}}, "22322660078220")
        self.assertEqual([row["ProcessDescForPortal"] for row in history], ["Shipment - Booked", "Shipment - In Transit"])
        self.assertEqual(history[-1]["TrackingSource"], "Trax Direct")
        self.assertEqual(history[-1]["ConsigneeCity"], "Lahore")

    async def test_tracker_posts_cn_with_ajax_headers(self):
        payload = {"shipments": {"1": {
            "tracking_number": "22322660078220",
            "tracking_history": [{"date_time": "2026-10-28", "status": "Shipment - Rider Picked"}],
        }}}
        session = FakeSession(payload)
        history = await trax.fetch_public_tracking_history(session, "223 22660078220")
        self.assertEqual(session.post_data, {"tracking_numbers": "22322660078220", "_token": "public-token"})
        self.assertEqual(session.post_headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(history[-1]["ProcessDescForPortal"], "Shipment - Rider Picked")

    async def test_active_and_terminal_cache_ttls(self):
        active = [{"ProcessDescForPortal": "Shipment - In Transit"}]
        terminal = [{"ProcessDescForPortal": "Shipment - Delivered"}]
        with patch.object(trax, "_fetch_public_tracking_history", AsyncMock(side_effect=[active, terminal])):
            await trax.fetch_public_tracking_history(object(), "22322660078220")
            active_ttl = trax._history_cache["22322660078220"][1] - time.monotonic()
            await trax.fetch_public_tracking_history(object(), "22322660078221")
            terminal_ttl = trax._history_cache["22322660078221"][1] - time.monotonic()
        self.assertGreater(active_ttl, 299)
        self.assertLess(active_ttl, 301)
        self.assertGreater(terminal_ttl, 86399)

    async def test_concurrent_requests_are_deduplicated(self):
        calls = 0

        async def delayed(_session, _digits):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return [{"ProcessDescForPortal": "Shipment - In Transit"}]

        with patch.object(trax, "_fetch_public_tracking_history", delayed):
            first, second = await asyncio.gather(
                trax.fetch_public_tracking_history(object(), "22322660078220"),
                trax.fetch_public_tracking_history(object(), "22322660078220"),
            )
        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    async def test_empty_or_error_keeps_last_valid_history(self):
        cached = [{"TransactionDate": "2026-10-28", "ProcessDescForPortal": "Delivered"}]
        trax._history_cache["22322660078220"] = (cached, time.monotonic() - 1)
        with patch.object(trax, "_fetch_public_tracking_history", AsyncMock(return_value=[])):
            self.assertEqual(await trax.fetch_public_tracking_history(object(), "22322660078220"), cached)
        with patch.object(trax, "_fetch_public_tracking_history", AsyncMock(side_effect=RuntimeError("down"))):
            self.assertEqual(await trax.fetch_public_tracking_history(object(), "22322660078220"), cached)

