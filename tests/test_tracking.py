"""Exercise tracking without starting the app's database/network initialization."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

source = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text())
functions = [node for node in source.body if
             (isinstance(node, ast.AsyncFunctionDef)
              and node.name in {'fetch_tracking_data', 'process_line_item'})
             or (isinstance(node, ast.FunctionDef)
                 and node.name in {'is_digidokaan_tracking_number', 'tracking_url_for_number'})]
namespace = {
    'json': json,
    'ClientTimeout': lambda **kwargs: kwargs,
    'quote': __import__('urllib.parse', fromlist=['quote']).quote,
    'urlencode': __import__('urllib.parse', fromlist=['urlencode']).urlencode,
    'fetch_tracking_status': None,
}
exec(compile(ast.Module(body=functions, type_ignores=[]), 'main.py', 'exec'), namespace)


async def tracking_status_from_call_courier(session, tracking_number):
    data = await namespace['fetch_tracking_data'](session, tracking_number)
    if data and isinstance(data, list) and data[-1].get('ProcessDescForPortal'):
        return data[-1]['ProcessDescForPortal']
    return 'Tracking unavailable'


namespace['fetch_tracking_status'] = tracking_status_from_call_courier


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, payload, status=200):
        self.response = Response(payload, status)

    def get(self, *args, **kwargs):
        return self.response


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_never_means_delivered(self):
        line = SimpleNamespace(id=1, quantity=1, fulfillment_status='fulfilled', fulfillable_quantity=0)
        fulfillment = SimpleNamespace(status='success', tracking_number='123456789', line_items=[line])
        for payload, status in [([], 200), ({'Message': 'No details'}, 200),
                                ({'d': []}, 200), (['bad', {}], 200),
                                (ValueError('Invalid JSON'), 200), (None, 503)]:
            with self.subTest(payload=payload, status=status):
                result = await namespace['process_line_item'](Session(payload, status), line, [fulfillment])
                self.assertEqual(result[0]['status'], 'Tracking unavailable')
                self.assertEqual(result[0]['tracking_number'], '123456789')

    async def test_valid_history_and_wrapped_history(self):
        history = [{'ProcessDescForPortal': 'Booked'}, {'ProcessDescForPortal': 'DELIVERED'}]
        for payload in [history, {'d': history}, {'d': json.dumps(history)}]:
            self.assertEqual(await namespace['fetch_tracking_data'](Session(payload), '123'), history)

    async def test_missing_number(self):
        self.assertEqual(await namespace['fetch_tracking_data'](None, None), [])

    async def test_unfulfilled_remains_unbooked(self):
        line = SimpleNamespace(quantity=2, fulfillment_status=None, fulfillable_quantity=2)
        result = await namespace['process_line_item'](None, line, [])
        self.assertEqual(result[0]['status'], 'Un-Booked')

    def test_digidokaan_tracking_uses_portal(self):
        number = '22322367960798'
        self.assertTrue(namespace['is_digidokaan_tracking_number'](number))
        self.assertEqual(
            namespace['tracking_url_for_number'](number),
            '/track/22322367960798',
        )

    def test_other_tracking_numbers_stay_on_portal(self):
        self.assertFalse(namespace['is_digidokaan_tracking_number']('123456789'))
        self.assertEqual(namespace['tracking_url_for_number']('123 456'), '/track/123%20456')


if __name__ == '__main__':
    unittest.main()
