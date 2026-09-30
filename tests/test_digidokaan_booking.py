import json
import unittest

import digidokaan


class DigiDokaanBookingTests(unittest.TestCase):
    def test_booking_page_metadata_keeps_only_trax_cities_and_pickups(self):
        cities = [
            {"id": 1, "city_name": "Lahore", "courier": ["trax"], "trax_shipment_type": json.dumps(["OVERNIGHT", "OVERLAND"])},
            {"id": 2, "city_name": "Other", "courier": ["another"], "trax_shipment_type": "[]"},
        ]
        pickup = {"pickup_address_id": 99, "name": "Al Karamat", "gateways": [5]}
        page = (
            '<meta name="csrf-token" content="token">'
            f'<input id="courier_cities_array" value="{json.dumps(cities).replace(chr(34), "&quot;")}">'
            '<select id="normal_pickup_location">'
            f'<option value="{json.dumps(pickup).replace(chr(34), "&quot;")}">Store</option></select>'
        )
        result = digidokaan._parse_booking_metadata(page)
        self.assertEqual(result["csrf"], "token")
        self.assertEqual(result["cities"], [{"id": "1", "name": "Lahore", "services": ["OVERNIGHT", "OVERLAND"]}])
        self.assertEqual(result["pickups"][0]["pickup_address_id"], 99)

    def test_city_matching_handles_shopify_district_suffix(self):
        cities = [{"id": "1", "name": "Islamabad", "services": ["OVERNIGHT"]}]
        self.assertEqual(digidokaan.match_trax_city("Islamabad Capital Territory", cities), cities[0])

    def test_city_matching_does_not_guess_weak_match(self):
        cities = [{"id": "1", "name": "Lahore", "services": ["OVERNIGHT"]}]
        self.assertIsNone(digidokaan.match_trax_city("Karachi", cities))


if __name__ == "__main__":
    unittest.main()
