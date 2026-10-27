import unittest
from unittest.mock import AsyncMock, patch

import tracking_resolver


class TrackingResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tracking_resolver._resolved_history_cache.clear()

    async def resolve(self, digi, direct):
        with patch.object(tracking_resolver, "fetch_digidokaan_tracking_history", AsyncMock(return_value=digi)), \
             patch.object(tracking_resolver, "fetch_public_tracking_history", AsyncMock(return_value=direct)):
            return await tracking_resolver.fetch_resolved_tracking_history(object(), "22322660078220")

    async def test_newer_trax_overrides_stale_digidokaan(self):
        result = await self.resolve(
            [{"TransactionDate": "2026-10-27 10:00:00", "ProcessDescForPortal": "Booked"}],
            [{"TransactionDate": "2026-10-28 12:00:00", "ProcessDescForPortal": "Rider Picked"}],
        )
        self.assertEqual(result[-1]["ProcessDescForPortal"], "Rider Picked")
        self.assertEqual(result[-1]["TrackingSource"], "Trax Direct")

    async def test_older_trax_does_not_override_digidokaan(self):
        digi = [{"TransactionDate": "2026-10-28 13:00:00", "ProcessDescForPortal": "Delivered"}]
        result = await self.resolve(digi, [{"TransactionDate": "2026-10-28 12:00:00", "ProcessDescForPortal": "In Transit"}])
        self.assertEqual(result[-1]["ProcessDescForPortal"], "Delivered")
        self.assertEqual(result[-1]["TrackingSource"], "DigiDokaan")

    async def test_digidokaan_wins_equal_timestamps(self):
        result = await self.resolve(
            [{"TransactionDate": "2026-10-28 12:00:00", "ProcessDescForPortal": "Delivered"}],
            [{"TransactionDate": "2026-10-28 12:00:00", "ProcessDescForPortal": "In Transit"}],
        )
        self.assertEqual(result[-1]["ProcessDescForPortal"], "Delivered")
        self.assertEqual(result[-1]["TrackingSource"], "DigiDokaan")

    async def test_empty_trax_preserves_digidokaan(self):
        digi = [{"TransactionDate": "2026-10-28", "ProcessDescForPortal": "Booked"}]
        self.assertEqual((await self.resolve(digi, []))[-1]["ProcessDescForPortal"], "Booked")

    async def test_cached_newer_data_survives_source_regression(self):
        tracking_resolver._resolved_history_cache["22322660078220"] = [
            {"TransactionDate": "2026-10-29", "ProcessDescForPortal": "Delivered", "TrackingSource": "Trax Direct"}
        ]
        result = await self.resolve(
            [{"TransactionDate": "2026-10-27", "ProcessDescForPortal": "Booked"}], []
        )
        self.assertEqual(result[-1]["ProcessDescForPortal"], "Delivered")

