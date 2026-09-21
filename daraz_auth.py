"""Deployment identity for Daraz authorization and token storage."""
import hashlib
import os
from urllib.parse import urlsplit


def callback_url():
    base = (os.getenv('DARAZ_CALLBACK_BASE_URL') or os.getenv('APP_BASE_URL')
            or os.getenv('PUBLIC_APP_BASE_URL') or 'https://dashboard.alkaramat.com').strip().rstrip('/')
    callback = (os.getenv('DARAZ_CALLBACK_URL') or base + '/daraz').strip()
    parsed = urlsplit(callback)
    if (parsed.scheme != 'https' or parsed.netloc != 'dashboard.alkaramat.com'
            or parsed.path != '/daraz' or parsed.query or parsed.fragment):
        raise RuntimeError('Set the Al Karamat Daraz callback to https://dashboard.alkaramat.com/daraz in both deployment settings and Daraz Open Platform.')
    return callback


def token_setting_key():
    identity = callback_url() + '|' + (os.getenv('DARAZ_APP_KEY') or '').strip()
    return 'daraz_tokens:' + hashlib.sha256(identity.encode()).hexdigest()
