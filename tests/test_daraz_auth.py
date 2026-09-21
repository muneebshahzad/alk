import ast
import os
from pathlib import Path
import secrets
import time
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlencode, urlsplit, parse_qs
from flask import Flask, request, session, jsonify, redirect, url_for
from daraz_auth import callback_url, token_setting_key


class DarazAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DARAZ_APP_KEY': 'alk-key'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        app = Flask(__name__)
        app.secret_key = 'test-only'
        self.exchange = Mock()
        scope = dict(app=app, os=os, secrets=secrets, time=time, request=request,
                     session=session, jsonify=jsonify, redirect=redirect, url_for=url_for,
                     urlencode=urlencode, callback_url=callback_url,
                     daraz_configuration=lambda: {'app_key': 'alk-key', 'app_secret': 'test-secret'},
                     DARAZ_API_URL='https://api.daraz.pk/rest', lazop=self.exchange)
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text())
        names = {'get_daraz_callback_url', 'daraz_connect', 'daraz_callback'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), scope)
        self.client = app.test_client()

    def test_wrong_config_rejected(self):
        with patch.dict(os.environ, {'DARAZ_CALLBACK_URL': 'https://dashboard.thesleekspace.com/daraz'}):
            with self.assertRaises(RuntimeError):
                callback_url()

    def test_tokens_are_separate_from_legacy_and_other_apps(self):
        key = token_setting_key()
        self.assertNotEqual(key, 'daraz_tokens')
        with patch.dict(os.environ, {'DARAZ_APP_KEY': 'other-key'}):
            self.assertNotEqual(key, token_setting_key())

    def test_connect_uses_correct_callback_and_state(self):
        response = self.client.get('/daraz/connect', base_url='https://dashboard.alkaramat.com')
        params = parse_qs(urlsplit(response.location).query)
        self.assertEqual(params['redirect_uri'], ['https://dashboard.alkaramat.com/daraz'])
        self.assertTrue(params['state'][0])

    def test_unsolicited_callback_does_not_exchange_code(self):
        response = self.client.get('/daraz?code=foreign', base_url='https://dashboard.alkaramat.com')
        self.assertEqual(response.status_code, 400)
        self.exchange.LazopClient.assert_not_called()

    def test_wrong_or_expired_state_does_not_exchange_code(self):
        for expired in (False, True):
            response = self.client.get('/daraz/connect', base_url='https://dashboard.alkaramat.com')
            state = parse_qs(urlsplit(response.location).query)['state'][0]
            with patch.object(time, 'time', return_value=time.time() + (700 if expired else 0)):
                response = self.client.get('/daraz?'+urlencode({'code':'foreign', 'state':state if expired else 'wrong'}), base_url='https://dashboard.alkaramat.com')
            self.assertEqual(response.status_code, 400)
        self.exchange.LazopClient.assert_not_called()

    def test_wrong_host_cannot_start_connection(self):
        response = self.client.get('/daraz/connect', base_url='https://dashboard.thesleekspace.com')
        self.assertEqual(response.status_code, 400)

    def test_proxy_http_connect_still_generates_https_callback(self):
        response = self.client.get('/daraz/connect', base_url='http://dashboard.alkaramat.com')
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.location).query)
        self.assertEqual(params['redirect_uri'], ['https://dashboard.alkaramat.com/daraz'])

    def test_proxy_http_callback_accepts_valid_state_once(self):
        base = 'http://dashboard.alkaramat.com'
        response = self.client.get('/daraz/connect', base_url=base)
        state = parse_qs(urlsplit(response.location).query)['state'][0]
        # Stop at token exchange, avoiding any real network or persistence.
        self.exchange.LazopClient.side_effect = RuntimeError('token exchange reached')
        url = '/daraz?' + urlencode({'code': 'test-code', 'state': state})
        response = self.client.get(url, base_url=base)
        self.assertEqual(response.json['error'], 'token exchange reached')
        self.exchange.LazopClient.assert_called_once()
        self.exchange.reset_mock()
        response = self.client.get(url, base_url=base)
        self.assertEqual(response.status_code, 400)
        self.exchange.LazopClient.assert_not_called()

    def test_forwarded_header_cannot_override_wrong_host(self):
        response = self.client.get('/daraz/connect',
            base_url='http://dashboard.thesleekspace.com',
            headers={'X-Forwarded-Host': 'dashboard.alkaramat.com', 'X-Forwarded-Proto': 'https'})
        self.assertEqual(response.status_code, 400)
