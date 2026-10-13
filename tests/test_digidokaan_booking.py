import json
import unittest
from unittest.mock import patch

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

    def test_load_sheet_resolves_tracking_numbers_and_requests_one_pdf(self):
        class Response:
            def __init__(self, body=None, text="", status_code=200):
                self._body, self.text, self.status_code = body or {}, text, status_code

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)

        class ApiSession:
            def post(self, url, **kwargs):
                if url.endswith("/api/auth/login"):
                    return Response({"token": "api-token"})
                tracking = kwargs["json"]["search_value"]
                return Response({"data": [{"tracking_no": tracking, "order_id": "order-" + tracking[-2:], "cod_amount": "PKR 1,250"}]})

        class WebSession:
            submitted = None

            def get(self, _url, **_kwargs):
                return Response(text='<meta name="csrf-token" content="csrf-token">')

            def post(self, _url, **kwargs):
                self.submitted = kwargs["data"]
                return Response({"code": "200", "data": {"link": "https://web.digidokaan.pk/load-sheet.pdf"}})

        web_session = WebSession()
        config = {
            "base_url": "https://digidokaan.pk", "web_url": "https://web.digidokaan.pk",
            "phone": "923000000000", "password": "secret", "gateway_id": "5",
        }
        with patch.object(digidokaan, "configuration", return_value=config), \
                patch.object(digidokaan.requests, "Session", return_value=ApiSession()), \
                patch.object(digidokaan, "_web_login_session", return_value=web_session):
            result = digidokaan.generate_trax_load_sheet(["2230000000001", "2230000000002", "2230000000001"])

        self.assertEqual(result["shipment_count"], 2)
        self.assertEqual(result["amount"], 2500)
        self.assertEqual(result["link"], "https://web.digidokaan.pk/load-sheet.pdf")
        self.assertEqual(
            [value for key, value in web_session.submitted if key == "tracking_numbers[]"],
            ["2230000000001", "2230000000002"],
        )
        self.assertIn(("order_type_download", "load_sheet"), web_session.submitted)


if __name__ == "__main__":
    unittest.main()
