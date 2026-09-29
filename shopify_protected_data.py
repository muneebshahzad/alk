from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from db import get_app_setting, get_last_db_error, set_app_setting


SHOPIFY_TOKEN_SETTING_KEY = "shopify_offline_access_token"
SHOPIFY_SCOPE_SETTING_KEY = "shopify_offline_access_scopes"
SHOPIFY_INSTALLED_SHOP_KEY = "shopify_installed_shop_domain"
SHOPIFY_INSTALLED_AT_KEY = "shopify_installed_at"
SHOPIFY_REFRESH_TOKEN_SETTING_KEY = "shopify_offline_refresh_token"
SHOPIFY_TOKEN_EXPIRES_AT_KEY = "shopify_offline_access_token_expires_at"
SHOPIFY_REFRESH_EXPIRES_AT_KEY = "shopify_offline_refresh_token_expires_at"

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
_last_token_error = ""


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.upper() == "N/A" else text


def _pick(*values: Any) -> str:
    return next((_clean(value) for value in values if _clean(value)), "")


def get_shop_domain() -> str:
    explicit = _clean(os.getenv("SHOPIFY_GRAPHQL_STORE_DOMAIN"))
    if explicit:
        return explicit.replace("https://", "").replace("http://", "").strip("/")
    shop_url = _clean(os.getenv("SHOP_URL"))
    parsed = urlparse(shop_url if "://" in shop_url else f"https://{shop_url}") if shop_url else None
    return (parsed.hostname or "").strip() if parsed else ""


def get_graphql_api_version() -> str:
    return _clean(os.getenv("SHOPIFY_GRAPHQL_API_VERSION")) or "2026-04"


def get_client_id() -> str:
    return _clean(os.getenv("SHOPIFY_GRAPHQL_CLIENT_ID"))


def get_client_secret() -> str:
    return _clean(os.getenv("SHOPIFY_GRAPHQL_CLIENT_SECRET"))


def get_app_base_url() -> str:
    return (_clean(os.getenv("SHOPIFY_APP_BASE_URL")) or "https://dashboard.alkaramat.com").rstrip("/")


def get_oauth_scopes() -> list[str]:
    scopes = _clean(os.getenv("SHOPIFY_GRAPHQL_SCOPES")) or "read_orders,read_customers,read_products,write_draft_orders,write_orders"
    return [scope.strip() for scope in scopes.split(",") if scope.strip()]


def get_install_url(state: str) -> str:
    params = {
        "client_id": get_client_id(),
        "scope": ",".join(get_oauth_scopes()),
        "redirect_uri": f"{get_app_base_url()}/shopify/callback",
        "state": state,
    }
    return f"https://{get_shop_domain()}/admin/oauth/authorize?{urlencode(params)}"


def verify_oauth_hmac(query_string: bytes | str) -> bool:
    if not query_string or not get_client_secret():
        return False
    raw = query_string.decode() if isinstance(query_string, bytes) else str(query_string)
    received = ""
    parts = []
    for part in filter(None, raw.split("&")):
        if part.startswith("hmac="):
            received = part.split("=", 1)[1]
        elif not part.startswith("signature="):
            parts.append(part)
    if not received:
        return False
    digest = hmac.new(get_client_secret().encode(), "&".join(sorted(parts)).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, received)


def create_oauth_state() -> str:
    return secrets.token_urlsafe(24)


def exchange_oauth_code_for_token(shop: str, code: str) -> dict[str, Any]:
    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": get_client_id(), "client_secret": get_client_secret(), "code": code, "expiring": 1},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _refresh_offline_token(shop: str, refresh_token: str) -> dict[str, Any]:
    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": get_client_id(), "client_secret": get_client_secret(), "grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def save_offline_token(shop: str, payload: dict[str, Any]) -> None:
    token = _clean(payload.get("access_token"))
    if not token:
        raise ValueError("Shopify did not return an access token")
    now = time.time()
    expires_at = str(int(now + float(payload["expires_in"]))) if payload.get("expires_in") is not None else ""
    refresh_expires_at = str(int(now + float(payload["refresh_token_expires_in"]))) if payload.get("refresh_token_expires_in") is not None else ""
    values = {
        SHOPIFY_TOKEN_SETTING_KEY: token,
        SHOPIFY_SCOPE_SETTING_KEY: _clean(payload.get("scope") or payload.get("associated_user_scope")),
        SHOPIFY_INSTALLED_SHOP_KEY: shop,
        SHOPIFY_INSTALLED_AT_KEY: str(int(now)),
        SHOPIFY_REFRESH_TOKEN_SETTING_KEY: _clean(payload.get("refresh_token")),
        SHOPIFY_TOKEN_EXPIRES_AT_KEY: expires_at,
        SHOPIFY_REFRESH_EXPIRES_AT_KEY: refresh_expires_at,
    }
    if not all(set_app_setting(key, value) for key, value in values.items()):
        raise RuntimeError(f"Could not persist Shopify OAuth token: {get_last_db_error() or 'unknown database error'}")
    _token_cache.update(token=token, expires_at=float(expires_at or now + 86400 * 365))


def _expired(value: str) -> bool:
    try:
        return bool(value) and time.time() >= float(value)
    except (TypeError, ValueError):
        return False


def get_graphql_token() -> str:
    global _last_token_error
    static = _clean(os.getenv("SHOPIFY_GRAPHQL_ACCESS_TOKEN"))
    if static:
        return static
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expires_at"] or 0):
        return str(_token_cache["token"])
    token = _clean(get_app_setting(SHOPIFY_TOKEN_SETTING_KEY))
    shop = _clean(get_app_setting(SHOPIFY_INSTALLED_SHOP_KEY)) or get_shop_domain()
    refresh = _clean(get_app_setting(SHOPIFY_REFRESH_TOKEN_SETTING_KEY))
    expires_at = _clean(get_app_setting(SHOPIFY_TOKEN_EXPIRES_AT_KEY))
    refresh_expires_at = _clean(get_app_setting(SHOPIFY_REFRESH_EXPIRES_AT_KEY))
    if token and not _expired(expires_at):
        _token_cache.update(token=token, expires_at=float(expires_at or now + 3600))
        return token
    if refresh and shop and not _expired(refresh_expires_at):
        try:
            payload = _refresh_offline_token(shop, refresh)
            save_offline_token(shop, payload)
            _last_token_error = ""
            return _clean(payload.get("access_token"))
        except Exception as error:
            _last_token_error = str(error)
    return ""


def get_graphql_endpoint() -> str:
    return f"https://{get_shop_domain()}/admin/api/{get_graphql_api_version()}/graphql.json" if get_shop_domain() else ""


def is_graphql_configured() -> bool:
    return bool(get_shop_domain() and get_graphql_token())


def get_protected_data_config_status() -> dict[str, Any]:
    token = get_graphql_token()
    return {
        "enabled": bool(get_shop_domain() and token),
        "shop_domain": get_shop_domain(),
        "api_version": get_graphql_api_version(),
        "has_access_token": bool(token),
        "has_client_id": bool(get_client_id()),
        "has_client_secret": bool(get_client_secret()),
        "requested_oauth_scopes": ",".join(get_oauth_scopes()),
        "token_error": _last_token_error if not token else "",
        "install_url": f"{get_app_base_url()}/shopify/install",
    }


def _format_address(address: dict[str, Any] | None) -> dict[str, str]:
    address = address or {}
    return {"name": _pick(address.get("name")), "address": _pick(address.get("address1"), address.get("address2")), "city": _pick(address.get("city")), "phone": _pick(address.get("phone"))}


def _build_customer_details(node: dict[str, Any]) -> dict[str, str]:
    shipping = _format_address(node.get("shippingAddress"))
    billing = _format_address(node.get("billingAddress"))
    customer = node.get("customer") or {}
    default = _format_address(customer.get("defaultAddress"))
    customer_name = " ".join(filter(None, (_clean(customer.get("firstName")), _clean(customer.get("lastName"))))).strip()
    customer_phone = (customer.get("defaultPhoneNumber") or {}).get("phoneNumber")
    return {
        "name": _pick(shipping["name"], billing["name"], customer_name, default["name"]),
        "address": _pick(shipping["address"], billing["address"], default["address"]),
        "city": _pick(shipping["city"], billing["city"], default["city"]),
        "phone": _pick(node.get("phone"), shipping["phone"], billing["phone"], customer_phone, default["phone"]),
    }


def fetch_protected_order_details(order_ids: list[int | str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    token = get_graphql_token()
    if not token or not order_ids:
        return {}, []
    query = """query ProtectedOrderDetails($ids: [ID!]!) { nodes(ids: $ids) { ... on Order { legacyResourceId phone shippingAddress { name address1 address2 city phone } billingAddress { name address1 address2 city phone } customer { firstName lastName defaultPhoneNumber { phoneNumber } defaultAddress { name address1 address2 city phone } } } } }"""
    details: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for start in range(0, len(order_ids), 100):
        batch = order_ids[start:start + 100]
        response = requests.post(
            get_graphql_endpoint(),
            json={"query": query, "variables": {"ids": [f"gid://shopify/Order/{int(order_id)}" for order_id in batch]}},
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        errors.extend(error.get("message", "Unknown Shopify GraphQL error") for error in payload.get("errors") or [])
        for node in (payload.get("data") or {}).get("nodes") or []:
            if node and node.get("legacyResourceId"):
                details[str(node["legacyResourceId"])] = _build_customer_details(node)
    return details, errors
