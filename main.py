import asyncio
import secrets
from daraz_auth import callback_url
import base64
import hashlib
import hmac
import os
import smtplib
import threading
import time
import json
import random
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort, session, send_from_directory, has_request_context
from markupsafe import Markup
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlparse
import pymssql, shopify
import aiohttp
import lazop
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout, ClientSession, ClientError, BasicAuth
import pytz
import requests
from flask import send_file, make_response
import io
import functools
from db import (
    delete_order_status,
    get_app_setting,
    init_db,
    load_admin_passkeys,
    load_employee_passkeys,
    load_order_statuses,
    save_admin_passkey,
    save_employee_passkey,
    set_app_setting,
    update_admin_passkey_usage,
    update_employee_passkey_usage,
    upsert_order_status,
)
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from shopify_protected_data import (
    create_oauth_state,
    exchange_oauth_code_for_token,
    fetch_protected_order_details,
    get_graphql_endpoint,
    get_graphql_token,
    get_install_url,
    get_protected_data_config_status,
    get_shop_domain,
    save_offline_token,
    verify_oauth_hmac,
)
from token_manager import get_access_token, load_tokens, save_tokens
from digidokaan import (
    create_trax_booking,
    fetch_booking_metadata,
    fetch_pending_shipper_advice,
    fetch_payments as fetch_digidokaan_payments,
    fetch_tracking_history as fetch_digidokaan_tracking_history,
    fetch_tracking_status as fetch_digidokaan_tracking_status,
    fetch_trax_label,
    match_trax_city,
    submit_shipper_advice,
)
from finance import build_entry, finance_dashboard, post_journal, reverse_journal

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')
pre_loaded = 0
order_details = []
daraz_orders = []
daraz_refresh_lock = threading.Lock()
daraz_refresh_attempted = False
daraz_last_error = ""
product_display_cache = {}
tracking_refresh_lock = threading.Lock()
tracking_refresh_state = {"running": False, "error": "", "shopify_count": 0, "daraz_count": 0, "updated_at": 0}
TRACKING_AUTO_REFRESH_SECONDS = max(5 * 60, int(os.getenv("TRACKING_AUTO_REFRESH_SECONDS", "3600")))
abandoned_checkout_cache = {"rows": None, "expires_at": 0.0}
EMPLOYEE_PORTAL_SESSION_KEY = "employee_portal_authenticated"
ADMIN_PORTAL_SESSION_KEY = "admin_portal_authenticated"
EMPLOYEE_PORTAL_PASSWORD = os.getenv("EMPLOYEE_PORTAL_PASSWORD", "@@@t")
ADMIN_PORTAL_PASSWORD = os.getenv("ADMIN_PORTAL_PASSWORD", "security")
ADMIN_PORTAL_RP_ID = os.getenv("ADMIN_PORTAL_RP_ID", "dashboard.alkaramat.com").strip()
ADMIN_PORTAL_ORIGIN = os.getenv("ADMIN_PORTAL_ORIGIN", "https://dashboard.alkaramat.com").rstrip("/")
ADMIN_PASSKEY_CHALLENGE_KEY = "admin_passkey_challenge"
EMPLOYEE_PASSKEY_CHALLENGE_KEY = "employee_passkey_challenge"
SHOPIFY_OAUTH_STATE_SESSION_KEY = "shopify_oauth_state"
PRODUCT_COSTS_SETTING_KEY = "product_cost_overrides_v1"
ABANDONED_VIEWED_SETTING_KEY = "abandoned_checkout_viewed_v1"
TRAX_BOOKING_LOG_SETTING_KEY = "trax_booking_log_v1"
PAID_FINANCIAL_STATUSES = {"paid", "partially_paid", "partially refunded", "partially_refunded"}
DARAZ_API_URL = os.getenv("DARAZ_API_URL", "https://api.daraz.pk/rest").rstrip("/")
DARAZ_ORDER_STATUSES = tuple(
    status.strip()
    for status in os.getenv("DARAZ_ORDER_STATUSES", "shipped,pending,ready_to_ship,packed").split(",")
    if status.strip()
)
DARAZ_ORDER_LOOKBACK_DAYS = max(int(os.getenv("DARAZ_ORDER_LOOKBACK_DAYS", "365")), 1)
DARAZ_PAGE_LIMIT = min(max(int(os.getenv("DARAZ_PAGE_LIMIT", "50")), 1), 100)
DARAZ_MAX_PAGES_PER_STATUS = max(int(os.getenv("DARAZ_MAX_PAGES_PER_STATUS", "1")), 1)


def normalize_scan_term(term):
    return re.sub(r"[^a-z0-9]", "", str(term or "").strip().lower().replace("#", ""))


def scan_term_candidates(term):
    raw = str(term or "").strip()
    candidates = []
    for value in [raw, *re.findall(r"\d{4,}", raw)]:
        normalized = normalize_scan_term(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def parse_int(value, default=0):
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return int(default)


def extract_shopify_money(value, default=0.0):
    if isinstance(value, dict):
        for key in ("amount", "current_total_amount"):
            if key in value:
                return parse_money(value.get(key), default)
        for nested_key in ("shop_money", "presentment_money"):
            nested = value.get(nested_key)
            if isinstance(nested, dict) and "amount" in nested:
                return parse_money(nested.get("amount"), default)
            if nested is not None and hasattr(nested, "amount"):
                return parse_money(getattr(nested, "amount", None), default)
    if value is not None:
        for key in ("amount", "current_total_amount"):
            if hasattr(value, key):
                return parse_money(getattr(value, key, None), default)
        for nested_key in ("shop_money", "presentment_money"):
            nested = getattr(value, nested_key, None)
            if isinstance(nested, dict) and "amount" in nested:
                return parse_money(nested.get("amount"), default)
            if nested is not None and hasattr(nested, "amount"):
                return parse_money(getattr(nested, "amount", None), default)
    return parse_money(value, default)


def get_order_attr(order, name, default=None):
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def get_resource_value(resource, name, default=""):
    if resource is None:
        return default
    if isinstance(resource, dict):
        return resource.get(name, default)
    return getattr(resource, name, default)


def build_shopify_customer_details(order, customer_override=None):
    customer = customer_override or get_order_attr(order, "customer")
    shipping = get_order_attr(order, "shipping_address")
    billing = get_order_attr(order, "billing_address")
    default_address = get_resource_value(customer, "default_address", None)
    sources = (shipping, billing, default_address, customer)

    def first_value(*names):
        for source in sources:
            for name in names:
                value = str(get_resource_value(source, name, "") or "").strip()
                if value:
                    return value
        return ""

    name = first_value("name")
    if not name:
        first_name = first_value("first_name")
        last_name = first_value("last_name")
        name = " ".join(part for part in (first_name, last_name) if part)
    address = " ".join(part for part in (
        first_value("address1"), first_value("address2")
    ) if part)
    note_fields = {}
    for line in str(get_order_attr(order, "note", "") or "").splitlines():
        key, separator, value = line.partition(":")
        if separator and value.strip():
            note_fields[key.strip().casefold()] = value.strip()
    order_phone = str(get_order_attr(order, "phone", "") or "").strip()
    order_email = str(get_order_attr(order, "email", "") or "").strip()
    return {
        "id": str(get_resource_value(customer, "id", "") or "").strip(),
        "name": name or note_fields.get("customer", ""),
        "address": address or note_fields.get("address", ""),
        "city": first_value("city") or note_fields.get("city", ""),
        "phone": first_value("phone") or order_phone or note_fields.get("phone", ""),
        "email": first_value("email") or order_email,
    }


async def load_shopify_customer_details(session, order):
    return build_shopify_customer_details(order)


def enrich_orders_with_protected_customer_data(orders):
    if not orders:
        return orders
    try:
        protected, errors = fetch_protected_order_details([order.get("id") for order in orders if order.get("id")])
    except Exception as error:
        print(f"Could not load Shopify protected customer data: {error}")
        return orders
    for error in errors:
        print(f"Shopify protected data warning: {error}")
    for order in orders:
        details = protected.get(str(order.get("id") or ""))
        if not details:
            continue
        customer = order.setdefault("customer_details", {})
        for field in ("name", "phone", "address", "city"):
            customer[field] = details.get(field) or customer.get(field, "")
        customer["order_count"] = int(details.get("order_count") or customer.get("order_count") or 0)
        customer["recent_orders"] = details.get("recent_orders") or customer.get("recent_orders") or []
    return orders


def get_shopify_order_shipping_total(order):
    shipping_set = get_order_attr(order, "total_shipping_price_set")
    shipping_total = extract_shopify_money(shipping_set, 0)
    if shipping_total:
        return shipping_total

    shipping_lines = get_order_attr(order, "shipping_lines", []) or []
    try:
        return round(sum(extract_shopify_money(getattr(line, "price", None), 0) for line in shipping_lines), 2)
    except TypeError:
        return round(sum(extract_shopify_money((line or {}).get("price"), 0) for line in shipping_lines), 2)


def format_number(value):
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value or 0)


def format_currency(value):
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


app.jinja_env.filters["format_number"] = format_number
app.jinja_env.filters["format_currency"] = format_currency


_TAG_STYLES = {
    "Call Courier": "background:#ede7f6;color:#4527a0",
    "Leopards": "background:#e6f6f8;color:#0a5c6e",
    "Order Confirmed": "background:#e8f5e9;color:#1b5e20",
    "Fulfilment Not Set": "background:#fff8e1;color:#e65100",
    "No Throw": "background:#fce4ec;color:#880e4f",
    "Lahore": "background:#fff3cd;color:#8b5a00",
}


def tag_style(label):
    return _TAG_STYLES.get(label, "background:#e8eaf6;color:#283593")


def status_badge(label):
    normalized = normalize_status_bucket(label)
    class_name = "sb-mixed"
    if normalized == "Booked":
        class_name = "sb-booked"
    elif normalized == "Un-Booked":
        class_name = "sb-unbooked"
    elif normalized == "Delivered":
        class_name = "sb-delivered"
    elif normalized == "Out For Delivery":
        class_name = "sb-ofd"
    elif "Return" in normalized:
        class_name = "sb-return"
    elif normalized in {"Undelivered", "Being Return"}:
        class_name = "sb-attention"
    return Markup(f'<span class="sbadge {class_name}">{normalized}</span>')


app.jinja_env.globals["tag_style"] = tag_style
app.jinja_env.globals["status_badge"] = status_badge


def parse_date_for_sort(value):
    if not value:
        return datetime.min
    raw = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.min


def parse_date_timestamp(value):
    parsed = parse_date_for_sort(value)
    if parsed == datetime.min:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def format_daraz_date(value):
    if not value:
        return "N/A"
    parsed = parse_date_for_sort(value)
    if parsed == datetime.min:
        return str(value)
    return parsed.strftime("%Y-%m-%d")


def daraz_configuration():
    values = {
        "app_key": (os.getenv("DARAZ_APP_KEY") or "").strip(),
        "app_secret": (os.getenv("DARAZ_APP_SECRET") or "").strip(),
    }
    missing = ["DARAZ_" + name.upper() for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Daraz is not configured. Set {', '.join(missing)}.")
    return values


def execute_daraz_request(client, path, access_token, **parameters):
    api_request = lazop.LazopRequest(path, "GET")
    for key, value in parameters.items():
        if value is not None:
            api_request.add_api_param(key, str(value))
    response = client.execute(api_request, access_token)
    body = response.body if isinstance(response.body, dict) else {}
    code = str(body.get("code", "0"))
    if code != "0":
        message = body.get("message") or body.get("detail") or "Unknown Daraz API error"
        raise RuntimeError(f"Daraz API {path} failed ({code}): {message}")
    return body


def daraz_tracking_statuses(trace_body):
    statuses = {}
    result = trace_body.get("result") or {}
    groups = result.get("data") or []
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups:
        if not isinstance(group, dict):
            continue
        for package in group.get("package_detail_info_list") or []:
            tracking_number = str(package.get("tracking_number") or "").strip()
            events = package.get("logistic_detail_info_list") or []
            latest = events[-1] if events and isinstance(events[-1], dict) else {}
            if tracking_number:
                statuses[tracking_number] = latest.get("title") or "N/A"
    return statuses


def normalize_daraz_order(order, status, items, tracking_statuses):
    shipping = order.get("address_shipping") or {}
    if not isinstance(shipping, dict):
        shipping = {"address": str(shipping)}
    address = shipping.get("address") or " ".join(
        str(shipping.get(key) or "").strip()
        for key in ("address1", "address2", "address3", "address4", "address5")
        if shipping.get(key)
    )
    item_rows = []
    for item in items:
        tracking_number = str(item.get("tracking_code") or "").strip()
        title = str(item.get("name") or "Unknown item").strip()
        variation = str(item.get("variation") or "").strip()
        if variation:
            title = f"{title} - {variation.replace('Color family:', '').strip()}"
        item_rows.append(
            {
                "item_image": item.get("product_main_image") or "",
                "item_title": title,
                "quantity": 1,
                "tracking_number": tracking_number or "N/A",
                "status": tracking_statuses.get(tracking_number, "N/A"),
            }
        )
    customer_name = " ".join(
        part
        for part in (
            str(order.get("customer_first_name") or "").strip(),
            str(order.get("customer_last_name") or "").strip(),
        )
        if part
    ) or "Unknown"
    return {
        "order_id": str(order.get("order_id") or "Unknown"),
        "customer": {
            "name": customer_name,
            "address": address or "N/A",
            "phone": shipping.get("phone") or order.get("customer_phone") or "N/A",
        },
        "status": str(status).replace("_", " ").title(),
        "date": format_daraz_date(order.get("created_at")),
        "created_at": order.get("created_at") or "",
        "total_price": order.get("price") or "0.00",
        "items_list": item_rows,
    }


def get_daraz_orders(statuses=None):
    config = daraz_configuration()
    access_token = get_access_token()
    client = lazop.LazopClient(DARAZ_API_URL, config["app_key"], config["app_secret"], timeout=30)
    created_after = (datetime.now().astimezone() - timedelta(days=DARAZ_ORDER_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    rows = []
    for status in tuple(statuses or DARAZ_ORDER_STATUSES):
        offset = 0
        for _ in range(DARAZ_MAX_PAGES_PER_STATUS):
            body = execute_daraz_request(
                client,
                "/orders/get",
                access_token,
                sort_direction="DESC",
                sort_by="updated_at",
                created_after=created_after,
                update_after=created_after,
                offset=offset,
                limit=DARAZ_PAGE_LIMIT,
                status=status,
            )
            orders = (body.get("data") or {}).get("orders") or []
            for order in orders:
                order_id = order.get("order_id")
                item_body = execute_daraz_request(
                    client, "/order/items/get", access_token, order_id=order_id
                )
                trace_body = execute_daraz_request(
                    client, "/logistic/order/trace", access_token, order_id=order_id
                )
                rows.append(
                    normalize_daraz_order(
                        order,
                        status,
                        item_body.get("data") or [],
                        daraz_tracking_statuses(trace_body),
                    )
                )
            if len(orders) < DARAZ_PAGE_LIMIT:
                break
            offset += DARAZ_PAGE_LIMIT
    rows.sort(key=lambda order: parse_date_timestamp(order.get("created_at")), reverse=True)
    return rows


def get_app_base_url():
    explicit = (
        os.getenv("DARAZ_CALLBACK_BASE_URL")
        or os.getenv("APP_BASE_URL")
        or os.getenv("PUBLIC_APP_BASE_URL")
        or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")
    if has_request_context():
        return request.url_root.rstrip("/")
    return ""


def get_daraz_callback_url():
    return callback_url()


def get_daraz_authorize_url():
    if not (os.getenv("DARAZ_APP_KEY") or "").strip():
        return ""
    return url_for('daraz_connect')


@app.route('/daraz/connect')
def daraz_connect():
    try:
        callback = get_daraz_callback_url()
        config = daraz_configuration()
    except RuntimeError as error:
        return jsonify({'success': False, 'error': str(error)}), 400
    # TLS can terminate at the hosting proxy; validate the host independently.
    if request.host.lower() != 'dashboard.alkaramat.com':
        return jsonify({'error': 'Start Connect Daraz at https://dashboard.alkaramat.com.'}), 400
    state = secrets.token_urlsafe(32)
    session['daraz_oauth'] = {'state': state, 'created_at': time.time(), 'callback': callback}
    return redirect('https://api.daraz.pk/oauth/authorize?' + urlencode({
        'response_type': 'code', 'redirect_uri': callback,
        'client_id': config['app_key'], 'state': state, 'force_auth': 'true',
    }))


def refresh_daraz_cache_if_needed(force=False):
    global daraz_orders, daraz_refresh_attempted, daraz_last_error
    if daraz_orders and not force:
        return daraz_orders
    if daraz_refresh_attempted and not force:
        return daraz_orders
    if not daraz_refresh_lock.acquire(blocking=False):
        return daraz_orders
    try:
        daraz_refresh_attempted = True
        refreshed = get_daraz_orders()
        daraz_last_error = ""
        if refreshed or force:
            daraz_orders = refreshed
        return daraz_orders
    except Exception as error:
        daraz_last_error = str(error)
        print(f"Could not refresh Daraz cache: {error}")
        return daraz_orders
    finally:
        daraz_refresh_lock.release()


@app.context_processor
def inject_daraz_context():
    return {
        "daraz_oauth_url": get_daraz_authorize_url(),
        "embedded_mode": request.args.get("embedded") == "1",
        "skip_base_password_prompt": bool(session.get(ADMIN_PORTAL_SESSION_KEY)),
    }


def normalize_customer_lookup_value(value):
    return str(value or "").strip().lower()


def normalize_customer_phone(value):
    if isinstance(value, dict):
        value = value.get("phone") or value.get("number") or ""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("92") and len(digits) > 10:
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_country_code(value):
    return str(value or "").strip().upper()


def infer_country_code(country):
    normalized = str(country or "").strip().lower()
    aliases = {
        "pakistan": "PK",
        "united states": "US",
        "usa": "US",
        "united kingdom": "GB",
        "uk": "GB",
        "united arab emirates": "AE",
        "uae": "AE",
        "saudi arabia": "SA",
    }
    return aliases.get(normalized, "")


def get_phone_country_prefix(country_code):
    prefixes = {
        "PK": "92",
        "US": "1",
        "CA": "1",
        "GB": "44",
        "AE": "971",
        "SA": "966",
    }
    return prefixes.get(normalize_country_code(country_code), "")


def format_customer_phone(phone, country_code=""):
    raw = str(phone or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if raw.startswith("+"):
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    prefix = get_phone_country_prefix(country_code)
    local = normalize_customer_phone(raw)
    if prefix and local:
        return f"+{prefix}{local}"
    return raw


def get_customer_phone_candidates(checkout, shipping, billing, customer):
    default_address = as_dict(checkout.get("default_address"))
    return [
        checkout.get("phone"),
        shipping.get("phone"),
        billing.get("phone"),
        customer.get("phone"),
        default_address.get("phone"),
    ]


def first_present(values):
    for value in values:
        if value:
            return value
    return ""


def format_currency_amount(value, currency="PKR"):
    amount = parse_money(value, 0)
    currency = str(currency or "PKR").upper()
    amount_text = f"{int(amount):,}" if amount == int(amount) else f"{amount:,.2f}"
    symbols = {
        "USD": "$",
        "GBP": "£",
        "EUR": "€",
    }
    if currency in symbols:
        return f"{symbols[currency]}{amount_text} ({currency})"
    if currency == "PKR":
        return f"PKR {amount_text}"
    return f"{amount_text} {currency}"


def shopify_api_base_url():
    shop_url = (os.getenv("SHOP_URL") or "").strip()
    if not shop_url:
        raise RuntimeError("SHOP_URL is not configured.")
    base_url_clean = shop_url.split("/admin")[0].rstrip("/")
    return f"{base_url_clean}/admin/api/2024-04"


async def fetch_shopify_rest_resource(session, resource_path, params=None):
    query = f"?{urlencode(params)}" if params else ""
    return await async_shopify_fetch(session, f"{resource_path}{query}")


async def fetch_shopify_paginated_rest(session, resource_path, params=None, root_key=None, max_pages=10):
    collected = []
    since_id = None
    params = dict(params or {})
    for _ in range(max_pages):
        page_params = dict(params)
        if since_id:
            page_params["since_id"] = since_id
        payload = await fetch_shopify_rest_resource(session, resource_path, page_params)
        if not payload:
            break
        rows = payload.get(root_key) if root_key else None
        if rows is None:
            rows = payload.get(resource_path.split(".", 1)[0], [])
        rows = rows or []
        collected.extend(rows)
        if len(rows) < int(page_params.get("limit", 250)):
            break
        since_id = rows[-1].get("id")
        if not since_id:
            break
    return collected


def get_abandoned_created_at_min(days=7):
    return (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat()


def as_dict(value):
    return value if isinstance(value, dict) else {}


def graphql_money_amount_and_currency(money_bag):
    money_bag = as_dict(money_bag)
    money = as_dict(money_bag.get("presentmentMoney")) or as_dict(money_bag.get("shopMoney"))
    return parse_money(money.get("amount"), 0), (money.get("currencyCode") or "PKR")


def graphql_checkout_address(address):
    address = as_dict(address)
    name = address.get("name") or " ".join(
        str(value).strip() for value in (address.get("firstName"), address.get("lastName")) if str(value or "").strip()
    )
    return {
        "name": name,
        "address1": address.get("address1") or "",
        "address2": address.get("address2") or "",
        "city": address.get("city") or "",
        "country": address.get("country") or "",
        "country_code": address.get("countryCodeV2") or address.get("countryCode") or "",
        "phone": address.get("phone") or "",
    }


def graphql_checkout_line_item(line_item):
    line_item = as_dict(line_item)
    unit_price, _ = graphql_money_amount_and_currency(
        line_item.get("discountedUnitPriceSet") or line_item.get("originalUnitPriceSet")
    )
    product = as_dict(line_item.get("product"))
    variant = as_dict(line_item.get("variant"))
    image = as_dict(line_item.get("image"))
    return {
        "id": line_item.get("id"),
        "title": line_item.get("title") or product.get("title") or "Product",
        "variant_title": line_item.get("variantTitle") or variant.get("title") or "",
        "quantity": line_item.get("quantity") or 0,
        "price": unit_price,
        "image_url": image.get("url") or image.get("src") or "",
        "product_id": product.get("legacyResourceId"),
        "variant_id": variant.get("legacyResourceId"),
    }


def graphql_abandoned_checkout_to_rest(node):
    node = as_dict(node)
    customer = as_dict(node.get("customer"))
    email = as_dict(customer.get("defaultEmailAddress")).get("emailAddress") or ""
    phone = as_dict(customer.get("defaultPhoneNumber")).get("phoneNumber") or ""
    default_address = graphql_checkout_address(customer.get("defaultAddress"))
    total_price, currency = graphql_money_amount_and_currency(node.get("totalPriceSet"))
    subtotal_price, subtotal_currency = graphql_money_amount_and_currency(
        node.get("subtotalPriceSet") or node.get("totalLineItemsPriceSet")
    )
    return {
        "id": node.get("id"),
        "token": node.get("name") or node.get("id"),
        "cart_token": node.get("defaultCursor") or "",
        "created_at": node.get("createdAt") or "",
        "updated_at": node.get("updatedAt") or "",
        "completed_at": node.get("completedAt") or "",
        "abandoned_checkout_url": node.get("abandonedCheckoutUrl") or "",
        "email": email,
        "phone": phone,
        "customer": {
            "first_name": customer.get("firstName") or "",
            "last_name": customer.get("lastName") or "",
            "email": email,
            "phone": phone,
            "number_of_orders": customer.get("numberOfOrders"),
            "default_address": default_address,
        },
        "shipping_address": graphql_checkout_address(node.get("shippingAddress")),
        "billing_address": graphql_checkout_address(node.get("billingAddress")),
        "line_items": [
            graphql_checkout_line_item(item)
            for item in (as_dict(node.get("lineItems")).get("nodes") or [])
        ],
        "total_price": total_price,
        "subtotal_price": subtotal_price,
        "total_line_items_price": subtotal_price,
        "currency": currency or subtotal_currency or "PKR",
        "presentment_currency": currency or subtotal_currency or "PKR",
    }


def fetch_shopify_abandoned_checkouts_graphql(days=7):
    token = get_graphql_token()
    endpoint = get_graphql_endpoint()
    if not token or not endpoint:
        return []
    query_text = f"created_at:>={get_abandoned_created_at_min(days)}"
    query = """
    query AbandonedCheckouts($first: Int!, $after: String, $query: String) {
      abandonedCheckouts(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id name defaultCursor abandonedCheckoutUrl completedAt createdAt updatedAt
          customer {
            firstName lastName numberOfOrders
            defaultEmailAddress { emailAddress }
            defaultPhoneNumber { phoneNumber }
            defaultAddress { name address1 address2 city country countryCodeV2 phone }
          }
          shippingAddress { name address1 address2 city country countryCodeV2 phone }
          billingAddress { name address1 address2 city country countryCodeV2 phone }
          subtotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          totalLineItemsPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          totalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          lineItems(first: 50) {
            nodes {
              id title variantTitle quantity image { url }
              discountedUnitPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
              originalUnitPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
              product { legacyResourceId title }
              variant { legacyResourceId title }
            }
          }
        }
      }
    }
    """
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": token}
    rows, after = [], None
    for _ in range(10):
        response = requests.post(
            endpoint,
            json={"query": query, "variables": {"first": 100, "after": after, "query": query_text}},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            raise RuntimeError("; ".join(error.get("message", "Unknown Shopify GraphQL error") for error in errors))
        connection = as_dict(as_dict(payload.get("data")).get("abandonedCheckouts"))
        rows.extend(graphql_abandoned_checkout_to_rest(node) for node in (connection.get("nodes") or []))
        page_info = as_dict(connection.get("pageInfo"))
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return rows


def get_checkout_image_url(line_item):
    image = line_item.get("image_url") or line_item.get("image") or ""
    if isinstance(image, dict):
        return image.get("src") or image.get("url") or ""
    return image or ""


def get_checkout_shipping_total(checkout):
    shipping_lines = checkout.get("shipping_lines") or []
    if isinstance(shipping_lines, dict):
        shipping_lines = [shipping_lines]
    shipping_total = sum(parse_money(as_dict(line).get("price"), 0) for line in shipping_lines)
    if shipping_total:
        return round(shipping_total, 2)

    for key in ("shipping_price", "shipping_rate"):
        value = checkout.get(key)
        if isinstance(value, dict):
            candidate = parse_money(value.get("price"), 0)
        else:
            candidate = parse_money(value, 0)
        if candidate:
            return candidate

    total = parse_money(checkout.get("total_price"), 0)
    subtotal = parse_money(checkout.get("subtotal_price"), 0)
    if total > subtotal:
        return round(total - subtotal, 2)
    return 0.0


async def get_checkout_line_item_image(session, line_item, product_cache):
    image_src = get_checkout_image_url(line_item)
    if image_src:
        return image_src

    product_id = line_item.get("product_id")
    variant_id = line_item.get("variant_id")
    if not product_id:
        return ""

    product_id_key = str(product_id)
    if product_id_key not in product_cache:
        product_cache[product_id_key] = None
        try:
            product_data = await async_shopify_fetch(session, f"products/{product_id}.json")
            product_cache[product_id_key] = as_dict(product_data.get("product") if product_data else {})
        except Exception as error:
            print(f"Could not fetch product image for abandoned checkout item {product_id}: {error}")

    product = as_dict(product_cache.get(product_id_key))
    if not product:
        return ""

    if variant_id:
        for variant in product.get("variants", []) or []:
            variant = as_dict(variant)
            if str(variant.get("id")) == str(variant_id) and variant.get("image_id"):
                for image in product.get("images", []) or []:
                    image = as_dict(image)
                    if str(image.get("id")) == str(variant.get("image_id")):
                        return image.get("src") or ""

    return as_dict(product.get("image")).get("src") or ""


async def preload_checkout_products(session, checkouts, product_cache):
    product_ids = {
        str(item.get("product_id"))
        for checkout in checkouts
        for item in ((checkout.get("line_items") or []) if isinstance(checkout.get("line_items") or [], list) else [])
        if item.get("product_id") and not get_checkout_image_url(item)
    }

    async def load(product_id):
        try:
            product_data = await async_shopify_fetch(session, f"products/{product_id}.json")
            product_cache[product_id] = as_dict(product_data.get("product") if product_data else {})
        except Exception as error:
            product_cache[product_id] = {}
            print(f"Could not preload abandoned checkout product {product_id}: {error}")

    await asyncio.gather(*(load(product_id) for product_id in product_ids))


def build_abandoned_whatsapp_url(phone, customer_name):
    phone = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not phone:
        return ""
    text = f"Hello {customer_name or ''}, you left items in your cart. Would you like help completing your order?"
    return f"https://wa.me/{phone}?{urlencode({'text': text})}"


def load_abandoned_viewed_tokens():
    try:
        return set(json.loads(get_app_setting(ABANDONED_VIEWED_SETTING_KEY, "[]")) or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def save_abandoned_viewed_tokens(tokens):
    cleaned = sorted({str(token) for token in tokens if token})
    return set_app_setting(ABANDONED_VIEWED_SETTING_KEY, json.dumps(cleaned))


def relative_time_label(value):
    created = parse_date_for_sort(value)
    now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    seconds = max(int((now - created).total_seconds()), 0)
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


async def fetch_shopify_abandoned_checkouts(days=7):
    if days == 7 and abandoned_checkout_cache["rows"] is not None and abandoned_checkout_cache["expires_at"] > time.monotonic():
        return abandoned_checkout_cache["rows"]
    try:
        rows = fetch_shopify_abandoned_checkouts_graphql(days)
        if rows:
            if days == 7:
                abandoned_checkout_cache["rows"] = rows
                abandoned_checkout_cache["expires_at"] = time.monotonic() + 5 * 60
            return rows
    except Exception as error:
        print(f"Could not fetch abandoned checkouts through Shopify GraphQL: {error}")
    created_at_min = get_abandoned_created_at_min(days)
    seen = {}
    async with aiohttp.ClientSession() as session:
        for status in ("open", "closed"):
            rows = await fetch_shopify_paginated_rest(
                session,
                "checkouts.json",
                {
                    "limit": 250,
                    "created_at_min": created_at_min,
                    "status": status,
                },
                "checkouts",
            )
            for row in rows:
                row = as_dict(row)
                seen[str(row.get("id") or row.get("token") or row.get("cart_token"))] = row
    rows = list(seen.values())
    if days == 7:
        abandoned_checkout_cache["rows"] = rows
        abandoned_checkout_cache["expires_at"] = time.monotonic() + 5 * 60
    return rows


async def fetch_recent_shopify_orders_for_recovery(days=30):
    async with aiohttp.ClientSession() as session:
        return await fetch_shopify_paginated_rest(
            session,
            "orders.json",
            {
                "limit": 250,
                "status": "any",
                "created_at_min": get_abandoned_created_at_min(days),
                "fields": "id,name,created_at,email,phone,total_price,customer,checkout_token,cart_token",
            },
            "orders",
        )


def build_order_recovery_indexes(orders):
    by_checkout_token = {}
    by_cart_token = {}
    by_email = {}
    by_phone = {}

    for order in orders or []:
        customer = order.get("customer") or {}
        checkout_token = normalize_customer_lookup_value(order.get("checkout_token"))
        cart_token = normalize_customer_lookup_value(order.get("cart_token"))
        email = normalize_customer_lookup_value(order.get("email") or customer.get("email"))
        phone = normalize_customer_phone(order.get("phone") or customer.get("phone"))
        if checkout_token:
            by_checkout_token.setdefault(checkout_token, []).append(order)
        if cart_token:
            by_cart_token.setdefault(cart_token, []).append(order)
        if email:
            by_email.setdefault(email, []).append(order)
        if phone:
            by_phone.setdefault(phone, []).append(order)

    return {
        "checkout_token": by_checkout_token,
        "cart_token": by_cart_token,
        "email": by_email,
        "phone": by_phone,
    }


def find_recovered_order(checkout, indexes):
    checkout_created_at = parse_date_timestamp(checkout.get("created_at"))
    customer = checkout.get("customer") or {}
    token = normalize_customer_lookup_value(checkout.get("token"))
    cart_token = normalize_customer_lookup_value(checkout.get("cart_token"))
    email = normalize_customer_lookup_value(checkout.get("email") or customer.get("email"))
    phone = normalize_customer_phone(checkout.get("phone") or customer.get("phone"))
    candidates = []

    for key, index in (
        (token, indexes.get("checkout_token", {})),
        (cart_token, indexes.get("cart_token", {})),
        (email, indexes.get("email", {})),
        (phone, indexes.get("phone", {})),
    ):
        if key:
            candidates.extend(index.get(key, []))

    unique_candidates = {str(order.get("id")): order for order in candidates if order.get("id")}.values()
    dated_candidates = [
        order for order in unique_candidates
        if parse_date_timestamp(order.get("created_at")) >= checkout_created_at
    ]
    if not dated_candidates:
        return None
    return sorted(dated_candidates, key=lambda order: parse_date_timestamp(order.get("created_at")))[0]


def build_abandoned_checkout_customer_counts(recovery_orders):
    counts = {}
    for order in recovery_orders or []:
        customer = order.get("customer") or {}
        customer_id = str(customer.get("id") or "").strip()
        email = normalize_customer_lookup_value(order.get("email") or customer.get("email"))
        phone = normalize_customer_phone(order.get("phone") or customer.get("phone"))
        for key in (f"id:{customer_id}" if customer_id else "", f"email:{email}" if email else "", f"phone:{phone}" if phone else ""):
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def get_checkout_customer_total_orders(checkout, fallback_counts):
    customer = checkout.get("customer") or {}
    for field in ("orders_count", "order_count", "number_of_orders"):
        if customer.get(field) is not None:
            try:
                return int(customer.get(field) or 0)
            except (TypeError, ValueError):
                pass

    customer_id = str(customer.get("id") or "").strip()
    email = normalize_customer_lookup_value(checkout.get("email") or customer.get("email"))
    phone = normalize_customer_phone(checkout.get("phone") or customer.get("phone"))
    for key in (f"id:{customer_id}" if customer_id else "", f"email:{email}" if email else "", f"phone:{phone}" if phone else ""):
        if key and key in fallback_counts:
            return fallback_counts[key]
    return 0


def shopify_order_admin_link(order_id):
    if not order_id:
        return ""
    return f"https://admin.shopify.com/store/alkaramat/orders/{order_id}"


async def build_abandoned_checkouts_data(days=7):
    checkouts, recovery_orders = await asyncio.gather(
        fetch_shopify_abandoned_checkouts(days),
        fetch_recent_shopify_orders_for_recovery(max(days, 30)),
    )
    recovery_indexes = build_order_recovery_indexes(recovery_orders)
    fallback_counts = build_abandoned_checkout_customer_counts(recovery_orders)
    viewed_tokens = load_abandoned_viewed_tokens()
    today = datetime.now().date()
    rows = []
    product_cache = {}

    async with aiohttp.ClientSession() as session:
        await preload_checkout_products(session, checkouts, product_cache)
        for checkout in checkouts:
            checkout = as_dict(checkout)
            customer = as_dict(checkout.get("customer"))
            shipping = as_dict(checkout.get("shipping_address"))
            billing = as_dict(checkout.get("billing_address"))
            recovered_order = find_recovered_order(checkout, recovery_indexes)
            completed_at = checkout.get("completed_at")
            is_recovered = bool(completed_at or recovered_order)
            customer_name = (
                shipping.get("name")
                or billing.get("name")
                or " ".join(part for part in [customer.get("first_name"), customer.get("last_name")] if part)
                or checkout.get("email")
                or checkout.get("phone")
                or "No customer"
            )
            checkout_line_items = checkout.get("line_items", []) or []
            if isinstance(checkout_line_items, dict):
                checkout_line_items = [checkout_line_items]
            items = []
            for line_item in checkout_line_items:
                line_item = as_dict(line_item)
                quantity = parse_int(line_item.get("quantity"), 0)
                unit_price = parse_money(line_item.get("price", 0))
                title = line_item.get("title") or line_item.get("name") or "Product"
                variant_title = line_item.get("variant_title") or ""
                items.append(
                    {
                        "title": f"{title} - {variant_title}" if variant_title and variant_title != "Default Title" else title,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "line_total": round(unit_price * quantity, 2),
                        "image": await get_checkout_line_item_image(session, line_item, product_cache),
                    }
                )

            created_at = checkout.get("created_at", "")
            country = shipping.get("country") or billing.get("country") or ""
            country_code = normalize_country_code(shipping.get("country_code") or billing.get("country_code") or checkout.get("buyer_accepts_sms_marketing_country") or infer_country_code(country))
            raw_phone = first_present(get_customer_phone_candidates(checkout, shipping, billing, customer))
            customer_phone = format_customer_phone(raw_phone, country_code)
            currency = checkout.get("presentment_currency") or checkout.get("currency") or "PKR"
            total_price = parse_money(checkout.get("total_price", 0))
            subtotal_price = parse_money(checkout.get("subtotal_price", checkout.get("total_line_items_price", total_price)))
            shipping_total = get_checkout_shipping_total(checkout)
            for item in items:
                item["display_line_total"] = format_currency_amount(item.get("line_total"), currency)
                item["display_unit_price"] = format_currency_amount(item.get("unit_price"), currency)
            rows.append(
                {
                    "id": checkout.get("id"),
                    "token": checkout.get("token") or checkout.get("cart_token") or checkout.get("id"),
                    "created_at": created_at,
                    "customer_name": customer_name,
                    "customer_email": checkout.get("email") or customer.get("email") or "",
                    "customer_phone": customer_phone,
                    "customer_city": shipping.get("city") or billing.get("city") or "",
                    "customer_country": country,
                    "customer_country_code": country_code,
                    "customer_address": " ".join(part for part in [shipping.get("address1") or billing.get("address1") or "", shipping.get("address2") or billing.get("address2") or ""] if part),
                    "customer_orders_count": get_checkout_customer_total_orders(checkout, fallback_counts),
                    "total_price": total_price,
                    "subtotal_price": subtotal_price,
                    "shipping_charges": shipping_total,
                    "currency": currency,
                    "display_total": format_currency_amount(total_price, currency),
                    "display_subtotal": format_currency_amount(subtotal_price, currency),
                    "display_shipping": format_currency_amount(shipping_total, currency),
                    "viewed": str(checkout.get("token") or checkout.get("cart_token") or checkout.get("id")) in viewed_tokens,
                    "abandoned_checkout_url": checkout.get("abandoned_checkout_url") or "",
                    "recovered": is_recovered,
                    "recovered_order_name": (recovered_order or {}).get("name", ""),
                    "recovered_order_link": shopify_order_admin_link((recovered_order or {}).get("id")),
                    "completed_at": completed_at or (recovered_order or {}).get("created_at", ""),
                    "items": items,
                    "created_date": str(created_at or "")[:10],
                    "relative_age": relative_time_label(checkout.get("updated_at") or created_at),
                    "whatsapp_url": build_abandoned_whatsapp_url(customer_phone, customer_name),
                    "is_today": parse_date_for_sort(created_at).date() == today,
                }
            )

    rows = sorted(rows, key=lambda row: parse_date_timestamp(row.get("created_at")), reverse=True)
    summary = {
        "last_7_days": len(rows),
        "today": sum(1 for row in rows if row.get("is_today")),
        "recovered": sum(1 for row in rows if row.get("recovered")),
        "open": sum(1 for row in rows if not row.get("recovered")),
        "viewed": sum(1 for row in rows if row.get("viewed")),
        "not_viewed": sum(1 for row in rows if not row.get("viewed")),
        "value": round(sum(parse_money(row.get("total_price", 0)) for row in rows), 2),
    }
    return rows, summary


async def build_abandoned_checkouts_summary(days=7):
    checkouts = await fetch_shopify_abandoned_checkouts(days)
    viewed_tokens = load_abandoned_viewed_tokens()
    today = datetime.now().date()
    unviewed = [
        checkout for checkout in checkouts
        if str(checkout.get("token") or checkout.get("cart_token") or checkout.get("id")) not in viewed_tokens
    ]
    newest_unviewed = max(unviewed, key=lambda row: parse_date_timestamp(row.get("updated_at") or row.get("created_at")), default=None)
    return {
        "last_7_days": len(checkouts),
        "today": sum(1 for checkout in checkouts if parse_date_for_sort(checkout.get("created_at")).date() == today),
        "recovered": sum(1 for checkout in checkouts if checkout.get("completed_at")),
        "open": sum(1 for checkout in checkouts if not checkout.get("completed_at")),
        "viewed": len(checkouts) - len(unviewed),
        "not_viewed": len(unviewed),
        "newest_unviewed_age": relative_time_label(newest_unviewed.get("updated_at") or newest_unviewed.get("created_at")) if newest_unviewed else "",
        "value": round(sum(parse_money(checkout.get("total_price", 0)) for checkout in checkouts), 2),
    }


def get_abandoned_summary_safe():
    try:
        return asyncio.run(build_abandoned_checkouts_summary())
    except Exception as error:
        print(f"Could not fetch abandoned checkouts: {error}")
        return {"last_7_days": 0, "today": 0, "recovered": 0, "open": 0, "viewed": 0, "not_viewed": 0, "value": 0.0, "error": str(error)}


def is_lahore_city(city):
    normalized = (city or "").strip().lower()
    return "lahore" in normalized or "lhr" in normalized


def is_delivered_status(status):
    normalized = (status or "").strip().upper()
    return normalized == "DELIVERED" or normalized.startswith("DELIVERED ")


def normalize_status_bucket(status):
    raw = (status or "Un-Booked").strip()
    upper = raw.upper()
    if "PARTIALLY DELIVERED" in upper:
        return "Partially Delivered"
    if "RETURNED TO SHIPPER" in upper:
        return "RETURNED TO SHIPPER"
    if "BEING RETURN" in upper or "OUT FOR RETURN" in upper or "RETURN SUBMISSION" in upper:
        return "Being Return"
    if "UNDELIVERED" in upper:
        return "Undelivered"
    if "OUT FOR DELIVERY" in upper:
        return "Out For Delivery"
    if is_delivered_status(raw):
        return "Delivered"
    if "PICKED FROM SHIPPER" in upper:
        return "Picked From Shipper"
    if upper == "BOOKED" or "CONSIGNMENT BOOKED" in upper:
        return "Booked"
    if upper in {"UN-BOOKED", "UNBOOKED"}:
        return "Un-Booked"
    return raw


def is_pending_line_item_status(status):
    normalized = normalize_status_bucket(status)
    return normalized in {"Booked", "Un-Booked"}


def employee_portal_is_authenticated():
    return bool(session.get(EMPLOYEE_PORTAL_SESSION_KEY) or session.get(ADMIN_PORTAL_SESSION_KEY))


def admin_portal_is_authenticated():
    return bool(session.get(ADMIN_PORTAL_SESSION_KEY))


def _admin_passkey_descriptors(passkeys):
    return [
        PublicKeyCredentialDescriptor(id=bytes(passkey["credential_id"]))
        for passkey in passkeys
    ]


def employee_portal_safe_next_url(candidate):
    if candidate and str(candidate).startswith("/employee_portal"):
        return candidate
    return url_for("employee_portal")


def product_cost_key(product_id=None, variant_id=None, title=""):
    if variant_id:
        return f"variant:{variant_id}"
    if product_id:
        return f"product:{product_id}"
    return f"title:{str(title or '').strip().lower()}"


def load_product_cost_overrides():
    raw = get_app_setting(PRODUCT_COSTS_SETTING_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def save_product_cost_overrides(overrides):
    return set_app_setting(PRODUCT_COSTS_SETTING_KEY, json.dumps(overrides or {}))


def get_cost_override_for_item(overrides, product_id=None, variant_id=None, title=""):
    for key in (
        product_cost_key(product_id=product_id, variant_id=variant_id, title=title),
        product_cost_key(product_id=product_id, title=title),
        product_cost_key(title=title),
    ):
        entry = overrides.get(key)
        if isinstance(entry, dict):
            return parse_money(entry.get("cost", 0))
    return 0.0


def set_cost_override(overrides, product_id=None, variant_id=None, title="", price=0, cost=0):
    key = product_cost_key(product_id=product_id, variant_id=variant_id, title=title)
    overrides[key] = {
        "product_id": str(product_id or ""),
        "variant_id": str(variant_id or ""),
        "title": title,
        "price": parse_money(price),
        "cost": parse_money(cost),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return overrides

# NOTE: Global semaphore removed to fix "different event loop" error.
# It is now handled dynamically inside 'limited_request'.

# PostEx Token
POSTEX_TOKEN = "M2E4Y2QyZTJiMjM0NGNjNGI4Y2E1YWYzNDY3MjE1ODY6MjFiOTFkNjVmZTNlNDMyNWI3MzNkYTU4NTM1OTQ3NmU="
POSTEX_BASE_URL = "https://api.postex.pk/services/integration/api"

POSTEX_ADDRESS_CODE = None


# --- RATE LIMIT DEFENDER (Sync) ---
def shopify_api_retry(func):
    """
    Decorator to handle Shopify 429 Too Many Requests errors
    for SYNCHRONOUS library calls.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        retries = 3
        while retries > 0:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_msg = str(e).lower()
                # Check for rate limit indicators in the error message
                if "429" in error_msg or "too many requests" in error_msg:
                    print(f"⚠️ Shopify Rate Limit Hit (Sync). Sleeping 2s... (Retries left: {retries})")
                    time.sleep(2 + random.uniform(0, 1))  # Add jitter
                    retries -= 1
                else:
                    raise e
        return func(*args, **kwargs)

    return wrapper


# --- Shopify Fulfillment Helper (Fixed for API 2025-01 & Rate Limits) ---
@shopify_api_retry
def fulfill_order_sync(order_id, tracking_number):
    try:
        # 1. Find Fulfillment Order
        fulfillment_orders = shopify.FulfillmentOrders.find(order_id=order_id)
        target_fo = next((fo for fo in fulfillment_orders if fo.status == 'open'), None)

        if not target_fo:
            print(f"Skipping fulfillment for {order_id}: No open fulfillment order.")
            return False

        # 2. Construct Payload
        payload = {
            "fulfillment": {
                "message": "Fulfilled via PostEx Integration",
                "notify_customer": True,
                "tracking_info": {
                    "number": tracking_number,
                    "url": f"https://postex.pk/tracking?cn={tracking_number}",
                    "company": "PostEx"
                },
                "line_items_by_fulfillment_order": [
                    {
                        "fulfillment_order_id": target_fo.id
                    }
                ]
            }
        }

        # 3. Request Settings
        url = "/admin/api/2025-01/fulfillments.json"
        headers = {"Content-Type": "application/json"}

        # 4. SEND REQUEST
        response = shopify.ShopifyResource.connection.post(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers
        )

        # 5. Check Success
        if response.code == 201:
            print(f"SUCCESS: Order {order_id} fulfilled. Tracking: {tracking_number}")
            return True
        else:
            print(f"FAILURE: Shopify returned code {response.code} for {order_id}.")
            return False

    except Exception as e:
        print(f"Shopify Fulfillment Error for {order_id}: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response Body: {e.response.body}")
        if "429" in str(e):
            raise e
        return False


@shopify_api_retry
def fulfill_selected_items_with_trax(order_id, tracking_number, selected_items):
    """Fulfil only the booked Shopify quantities and attach the public Al Karamat tracker."""
    requested = {
        str(item.get("line_item_id")): int(item.get("quantity") or 0)
        for item in selected_items or [] if item.get("line_item_id") and int(item.get("quantity") or 0) > 0
    }
    if not requested:
        return False, "No Shopify line items were selected"
    groups = []
    allocated = {key: 0 for key in requested}
    try:
        for fulfillment_order in shopify.FulfillmentOrders.find(order_id=order_id):
            if str(getattr(fulfillment_order, "status", "")).lower() not in {"open", "in_progress", "scheduled"}:
                continue
            rows = []
            for fulfillment_line in getattr(fulfillment_order, "line_items", []) or []:
                line_item_id = str(getattr(fulfillment_line, "line_item_id", ""))
                remaining = int(getattr(fulfillment_line, "remaining_quantity", None) or getattr(fulfillment_line, "quantity", 0) or 0)
                needed = requested.get(line_item_id, 0) - allocated.get(line_item_id, 0)
                quantity = min(remaining, max(needed, 0))
                if quantity:
                    rows.append({"id": fulfillment_line.id, "quantity": quantity})
                    allocated[line_item_id] += quantity
            if rows:
                groups.append({"fulfillment_order_id": fulfillment_order.id, "fulfillment_order_line_items": rows})
        missing = [key for key, quantity in requested.items() if allocated.get(key, 0) != quantity]
        if missing:
            return False, "Selected quantities are no longer available for fulfillment"
        payload = {"fulfillment": {
            "message": "Booked with Trax through DigiDokaan",
            "notify_customer": True,
            "tracking_info": {
                "number": tracking_number,
                "url": f"https://track.alkaramat.com/{quote(str(tracking_number), safe='')}",
                "company": "Trax",
            },
            "line_items_by_fulfillment_order": groups,
        }}
        response = shopify.ShopifyResource.connection.post(
            "/admin/api/2025-01/fulfillments.json",
            data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"},
        )
        if response.code == 201:
            return True, ""
        return False, f"Shopify returned {response.code}"
    except Exception as error:
        if "429" in str(error):
            raise
        return False, str(error)


async def get_pickup_address_code():
    global POSTEX_ADDRESS_CODE
    if POSTEX_ADDRESS_CODE:
        return POSTEX_ADDRESS_CODE

    url = f"{POSTEX_BASE_URL}/order/v1/get-merchant-address"
    headers = {'token': POSTEX_TOKEN}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('dist') and len(data['dist']) > 0:
                        POSTEX_ADDRESS_CODE = data['dist'][0].get('addressCode')
                        return POSTEX_ADDRESS_CODE
        except Exception as e:
            print(f"Error fetching PostEx address code: {e}")
    return None


async def fetch_postex_cities():
    url = f"{POSTEX_BASE_URL}/order/v2/get-operational-city"
    headers = {'token': POSTEX_TOKEN}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    cities = [c['operationalCityName'] for c in data.get('dist', [])]
                    return sorted(cities)
                return []
        except Exception as e:
            print(f"Error fetching PostEx cities: {e}")
            return []


@app.route('/prepare_postex_booking', methods=['POST'])
def prepare_booking():
    raw_ids = request.form.get('order_ids')
    if not raw_ids:
        return "No orders selected", 400

    selected_ids = json.loads(raw_ids)
    postex_cities = asyncio.run(fetch_postex_cities())
    orders_to_book = []

    for order_id_str in selected_ids:
        try:
            target_id = int(order_id_str)
        except:
            continue

        order_data = next((o for o in order_details if int(o.get('id') or o.get('shopify_id') or 0) == target_id), None)

        if order_data:
            is_paid = order_data.get('financial_status', '').lower() in ['paid', 'partially_refunded']
            cod_amount = 0.0 if is_paid else float(order_data.get('total_price', 0))

            shopify_city = order_data['customer_details'].get('city', '').strip()
            matched_city = ""

            for pc in postex_cities:
                if pc.lower() == shopify_city.lower():
                    matched_city = pc
                    break

            if not matched_city:
                for pc in postex_cities:
                    if shopify_city.lower() in pc.lower():
                        matched_city = pc
                        break

            orders_to_book.append({
                'order_id': order_data.get('id') or order_data.get('shopify_id'),
                'order_num': order_data['order_num'],
                'customer_name': order_data['customer_details']['name'],
                'customer_phone': order_data['customer_details']['phone'],
                'address': order_data['customer_details']['address'],
                'shopify_city': shopify_city,
                'matched_city': matched_city,
                'cod_amount': int(cod_amount)
            })

    return render_template('booking.html', orders=orders_to_book, postex_cities=postex_cities)


@app.route('/submit_postex_booking', methods=['POST'])
def submit_booking():
    data = request.get_json()
    bookings = data.get('bookings', [])

    global POSTEX_ADDRESS_CODE
    if not POSTEX_ADDRESS_CODE:
        POSTEX_ADDRESS_CODE = asyncio.run(get_pickup_address_code())

    if not POSTEX_ADDRESS_CODE:
        return jsonify({'results': [], 'error': 'Could not fetch Pickup Address Code.'}), 500

    results = []

    async def book_order_async(booking_item):
        order_id = int(booking_item['order_id'])
        postex_city = booking_item['postex_city']
        cod_amount = booking_item['cod_amount']

        original_order = next((o for o in order_details if int(o.get('id') or o.get('shopify_id') or 0) == order_id), None)
        if not original_order:
            return {'order_id': order_id, 'success': False, 'message': 'Original data not found'}

        # --- NEW: Generate Item Details String ---
        # Format: "Item 1 Name x Quantity , Item 2 Name x Quantity"
        item_strings = []
        if original_order.get('line_items'):
            for item in original_order['line_items']:
                title = item.get('product_title', 'Unknown Item')
                qty = item.get('quantity', 1)
                item_strings.append(f"{title} x {qty}")

        # Join with comma
        order_detail_str = " , ".join(item_strings)
        # -----------------------------------------

        payload = {
            "orderRefNumber": str(original_order['order_num']),
            "invoicePayment": str(cod_amount),
            "customerName": original_order['customer_details']['name'],
            "customerPhone": original_order['customer_details']['phone'] or "03000000000",
            "deliveryAddress": original_order['customer_details']['address'],
            "cityName": postex_city,
            "invoiceDivision": 1,
            "items": 1,  # Pieces kept as 1 per your requirement
            "orderType": "Normal",
            "transactionNotes": "Urgent Delivery",
            "pickupAddressCode": POSTEX_ADDRESS_CODE,
            "orderDetail": order_detail_str  # <--- ADDED THIS FIELD
        }

        url = f"{POSTEX_BASE_URL}/order/v3/create-order"
        headers = {
            'token': POSTEX_TOKEN,
            'Content-Type': 'application/json'
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    resp_data = await response.json()

                    if response.status == 200 and resp_data.get('statusCode') == '200':
                        tracking = resp_data.get('dist', {}).get('trackingNumber', 'N/A')

                        original_order['status'] = 'CONSIGNMENT BOOKED'
                        original_order['tracking_number'] = tracking

                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, fulfill_order_sync, order_id, tracking)

                        return {'order_id': order_id, 'success': True, 'tracking': tracking}
                    else:
                        msg = resp_data.get('statusMessage', 'Unknown Error')
                        return {'order_id': order_id, 'success': False, 'message': msg}

            except Exception as e:
                return {'order_id': order_id, 'success': False, 'message': str(e)}

    async def process_all():
        tasks = [book_order_async(item) for item in bookings]
        return await asyncio.gather(*tasks)

    results = asyncio.run(process_all())

    return jsonify({'results': results})

@app.route('/print_labels')
def print_labels():
    tracking_numbers = request.args.get('tracking_numbers')
    if not tracking_numbers:
        return "No tracking numbers provided", 400

    url = f"{POSTEX_BASE_URL}/order/v1/get-invoice?trackingNumbers={tracking_numbers}"
    headers = {'token': POSTEX_TOKEN}

    async def fetch_pdf():
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
                return None

    pdf_content = asyncio.run(fetch_pdf())

    if pdf_content:
        response = make_response(pdf_content)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = 'inline; filename=airway_bills.pdf'
        return response
    else:
        return "Failed to fetch PDF from PostEx", 500


def load_trax_booking_logs():
    try:
        rows = json.loads(get_app_setting(TRAX_BOOKING_LOG_SETTING_KEY, "[]") or "[]")
        return rows if isinstance(rows, list) else []
    except (TypeError, ValueError):
        return []


def save_trax_booking_log(entry):
    rows = load_trax_booking_logs()
    identity = str(entry.get("tracking_no") or entry.get("booking_key") or "")
    rows = [row for row in rows if str(row.get("tracking_no") or row.get("booking_key") or "") != identity]
    rows.insert(0, entry)
    set_app_setting(TRAX_BOOKING_LOG_SETTING_KEY, json.dumps(rows[:1000]))


def trax_booking_key(order_id, selected_items):
    parts = sorted(f"{item.get('line_item_id')}:{int(item.get('quantity') or 0)}" for item in selected_items)
    return f"{order_id}|{'|'.join(parts)}"


def find_cached_order(order_id):
    return next((row for row in order_details if str(row.get("id") or row.get("shopify_id")) == str(order_id)), None)


def validate_booking_selection(order, requested_items):
    available = {
        str(item.get("line_item_id")): item for item in order.get("line_items", [])
        if item.get("line_item_id") and int(item.get("fulfillable_quantity") or item.get("quantity") or 0) > 0
        and str(item.get("tracking_number") or "N/A") in {"", "N/A"}
    }
    selected = []
    for requested in requested_items or []:
        item = available.get(str(requested.get("line_item_id") or ""))
        quantity = int(requested.get("quantity") or 0)
        maximum = int((item or {}).get("fulfillable_quantity") or (item or {}).get("quantity") or 0)
        if not item or quantity < 1 or quantity > maximum:
            raise ValueError("One or more selected item quantities are no longer available")
        selected.append({
            "line_item_id": str(item["line_item_id"]), "quantity": quantity,
            "title": item.get("product_title", ""), "sku": item.get("sku", ""),
            "image": item.get("image_src", ""),
        })
    if not selected:
        raise ValueError("Select at least one item")
    return selected


def build_trax_booking_orders(cities):
    rows = []
    for order in order_details:
        items = [item for item in order.get("line_items", [])
                 if item.get("line_item_id") and int(item.get("fulfillable_quantity") or item.get("quantity") or 0) > 0
                 and str(item.get("tracking_number") or "N/A") in {"", "N/A"}]
        if not items:
            continue
        customer = order.get("customer_details") or {}
        city = match_trax_city(customer.get("city"), cities)
        current_id = str(order.get("id") or order.get("shopify_id") or "")
        previous = next((recent for recent in customer.get("recent_orders") or [] if str(recent.get("id")) != current_id), None)
        customer_order_count = int(customer.get("order_count") or 0)
        previous_status = "First order" if customer_order_count <= 1 else "History unavailable"
        if previous:
            status_text = " ".join([str(previous.get("fulfillment_status") or ""), " ".join(previous.get("tags") or [])]).casefold()
            if previous.get("cancelled") or "return" in status_text or "restock" in status_text:
                previous_status = "Returned"
            elif "fulfilled" in status_text and "unfulfilled" not in status_text:
                previous_status = "Delivered"
            else:
                previous_status = "Pending"
        identity_phone = re.sub(r"\D", "", str(customer.get("phone") or ""))[-10:]
        identity_address = re.sub(r"[^a-z0-9]", "", str(customer.get("address") or "").casefold())
        identity = f"{identity_phone}|{identity_address}" if identity_phone and identity_address else ""
        total = parse_money(order.get("current_total_price") or order.get("total_price"))
        replacement_text = " ".join([str(order.get("note") or ""), " ".join(order.get("tags") or [])]).casefold()
        replacement_reasons = []
        if total == 0:
            replacement_reasons.append("zero-value order")
        if "replacement" in replacement_text:
            replacement_reasons.append("marked Replacement")
        rows.append({
            "id": str(order.get("id") or order.get("shopify_id") or ""),
            "number": str(order.get("order_num") or order.get("order_id") or ""),
            "date": order.get("created_at", ""), "customer": customer,
            "city_match": city, "items": items,
            "total": total,
            "cod": 0 if str(order.get("financial_status", "")).casefold() in PAID_FINANCIAL_STATUSES else total,
            "financial_status": order.get("financial_status", ""),
            "customer_order_count": customer_order_count,
            "last_order_status": previous_status,
            "last_order_name": (previous or {}).get("name", ""),
            "customer_identity": identity,
            "is_replacement": bool(replacement_reasons),
            "replacement_reason": " and ".join(replacement_reasons),
        })
    grouped = {}
    for row in rows:
        if row["customer_identity"]:
            grouped.setdefault((row["is_replacement"], row["customer_identity"]), []).append(row)
    for members in grouped.values():
        if len(members) < 2:
            continue
        group_id = hashlib.sha256(members[0]["customer_identity"].encode()).hexdigest()[:12]
        for row in members:
            row["duplicate_group"] = group_id
            row["duplicate_count"] = len(members)
    return [row for row in rows if not row["is_replacement"]] + [row for row in rows if row["is_replacement"]]


@app.route('/book')
def book_orders():
    return redirect(url_for("bookings_page"))


@app.route('/bookings')
def bookings_page():
    try:
        metadata = fetch_booking_metadata()
        cities, booking_error = metadata.get("cities", []), ""
    except Exception as error:
        cities, booking_error = [], str(error)
    return render_template(
        "bookings.html", booking_orders=build_trax_booking_orders(cities), cities=cities,
        booking_logs=load_trax_booking_logs(), booking_error=booking_error,
    )


@app.route('/api/bookings/trax', methods=['POST'])
def create_trax_bookings_api():
    requested = (request.get_json(silent=True) or {}).get("bookings") or []
    if not requested or len(requested) > 30:
        return jsonify({"success": False, "error": "Select between 1 and 30 orders"}), 400
    try:
        metadata = fetch_booking_metadata()
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 502
    city_by_id = {str(city.get("id")): city for city in metadata.get("cities", [])}
    results = []
    for request_row in requested:
        try:
            order_requests = request_row.get("orders") or [{"order_id": request_row.get("order_id"), "items": request_row.get("items")}]
            order_selections = []
            for order_request in order_requests:
                order = find_cached_order(order_request.get("order_id"))
                if not order:
                    raise ValueError("One of the orders is no longer available")
                selected_for_order = validate_booking_selection(order, order_request.get("items"))
                order_selections.append({"order": order, "items": selected_for_order})
            replacement_flags = {
                parse_money(selection["order"].get("current_total_price") or selection["order"].get("total_price")) == 0
                or "replacement" in " ".join([
                    str(selection["order"].get("note") or ""),
                    " ".join(selection["order"].get("tags") or []),
                ]).casefold()
                for selection in order_selections
            }
            if True in replacement_flags and not request_row.get("replacement_confirmed"):
                raise ValueError("Confirm the replacement order before booking")
            if len(replacement_flags) > 1:
                raise ValueError("Replacement and regular orders cannot be merged together")
            if len(order_selections) > 1:
                identities = set()
                for selection in order_selections:
                    customer = selection["order"].get("customer_details") or {}
                    phone = re.sub(r"\D", "", str(customer.get("phone") or ""))[-10:]
                    address = re.sub(r"[^a-z0-9]", "", str(customer.get("address") or "").casefold())
                    identities.add(f"{phone}|{address}")
                if len(identities) != 1 or identities == {"|"}:
                    raise ValueError("Only orders with the same customer phone and address can be merged")
            order = order_selections[0]["order"]
            selected = [item for selection in order_selections for item in selection["items"]]
            city = city_by_id.get(str(request_row.get("destination_city_id") or ""))
            if not city:
                raise ValueError("Choose a valid Trax destination city")
            service = str(request_row.get("service_type") or "OVERNIGHT").upper()
            if service not in (city.get("services") or []):
                raise ValueError(f"{service.title()} is unavailable for {city.get('name')}")
            customer_name = str(request_row.get("customer_name") or "").strip()
            customer_phone = re.sub(r"[^0-9+]", "", str(request_row.get("customer_phone") or ""))
            customer_address = str(request_row.get("customer_address") or "").strip()
            if not customer_name or len(re.sub(r"\D", "", customer_phone)) < 10 or not customer_address:
                raise ValueError("Customer name, valid phone and address are required")
            cod_amount = round(parse_money(request_row.get("cod_amount")), 2)
            weight = float(request_row.get("weight") or 0)
            if cod_amount < 0 or weight <= 0:
                raise ValueError("COD and weight values are invalid")
            booking_key = "merge|" + "|".join(sorted(
                trax_booking_key(selection["order"]["id"], selection["items"])
                for selection in order_selections
            ))
            existing = next((row for row in load_trax_booking_logs() if row.get("booking_key") == booking_key), None)
            if existing:
                raise ValueError(f"These items are already booked as {existing.get('tracking_no')}")
            payload = {
                "destination_city_id": city["id"], "service_type": service,
                "customer_name": customer_name, "customer_phone": customer_phone,
                "customer_address": customer_address, "pieces": sum(item["quantity"] for item in selected),
                "quantity": sum(item["quantity"] for item in selected), "weight": weight,
                "cod_amount": cod_amount, "parcel_value": round(parse_money(request_row.get("parcel_value") or order.get("total_price")), 2),
                "product_name": str(request_row.get("product_name") or ", ".join(item["title"] for item in selected))[:250],
                "reference_number": "+".join(str(selection["order"].get("order_num") or "") for selection in order_selections)[:20],
                "special_instruction": str(request_row.get("special_instruction") or "")[:500],
            }
            booked = create_trax_booking(payload)
            if not booked.get("tracking_no"):
                raise RuntimeError("DigiDokaan created no tracking number")
            log = {
                "booking_key": booking_key, "shopify_order_id": str(order["id"]),
                "shopify_orders": [
                    {"order_id": str(selection["order"]["id"]), "order_number": str(selection["order"].get("order_num") or ""), "items": selection["items"]}
                    for selection in order_selections
                ],
                "order_number": ", ".join(str(selection["order"].get("order_num") or "") for selection in order_selections), "order_no": booked.get("order_no"),
                "tracking_no": booked["tracking_no"], "tracking_url": f"https://track.alkaramat.com/{booked['tracking_no']}",
                "customer_name": customer_name, "customer_phone": customer_phone, "customer_address": customer_address,
                "city": city.get("name"), "service_type": service, "cod_amount": cod_amount, "weight": weight,
                "items": selected, "booked_at": datetime.now().isoformat(timespec="seconds"),
                "shopify_fulfilled": False, "shopify_error": "Pending",
            }
            save_trax_booking_log(log)
            fulfillment_results = [
                fulfill_selected_items_with_trax(selection["order"]["id"], booked["tracking_no"], selection["items"])
                for selection in order_selections
            ]
            fulfilled = all(result[0] for result in fulfillment_results)
            fulfillment_error = "; ".join(result[1] for result in fulfillment_results if result[1])
            log.update(shopify_fulfilled=fulfilled, shopify_error=fulfillment_error)
            save_trax_booking_log(log)
            results.append({"success": True, **log})
        except Exception as error:
            results.append({"success": False, "order_id": str(request_row.get("order_id") or ""), "error": str(error)})
    return jsonify({"success": all(row["success"] for row in results), "results": results})


@app.route('/api/bookings/<tracking_number>/retry-fulfillment', methods=['POST'])
def retry_trax_fulfillment(tracking_number):
    log = next((row for row in load_trax_booking_logs() if str(row.get("tracking_no")) == str(tracking_number)), None)
    if not log:
        return jsonify({"success": False, "error": "Booking log not found"}), 404
    selections = log.get("shopify_orders") or [{"order_id": log["shopify_order_id"], "items": log.get("items") or []}]
    results = [fulfill_selected_items_with_trax(selection["order_id"], tracking_number, selection.get("items") or []) for selection in selections]
    fulfilled = all(result[0] for result in results)
    error = "; ".join(result[1] for result in results if result[1])
    log.update(shopify_fulfilled=fulfilled, shopify_error=error)
    save_trax_booking_log(log)
    return jsonify({"success": fulfilled, "error": error}), (200 if fulfilled else 502)


@app.route('/bookings/label/<tracking_number>')
def trax_booking_label(tracking_number):
    log = next((row for row in load_trax_booking_logs() if str(row.get("tracking_no")) == str(tracking_number)), None)
    if not log:
        abort(404)
    try:
        label = fetch_trax_label(log.get("order_no"), tracking_number)
        if str(label).startswith(("https://", "http://")):
            host = (urlparse(label).hostname or "").casefold()
            if host == "digidokaan.pk" or host.endswith(".digidokaan.pk"):
                return redirect(label)
            abort(502)
        return make_response(str(label), 200, {"Content-Type": "text/html; charset=utf-8"})
    except Exception as error:
        return f"Could not generate label: {error}", 502

@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
async def fetch_with_retry(session, url, method="GET", **kwargs):
    async with session.request(method, url, **kwargs) as response:
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            print(f"Async Rate limit hit. Retrying after {retry_after} seconds...")
            await asyncio.sleep(retry_after)
            response.raise_for_status()

        if response.status == 404:
            print(f"Warning: Resource not found (404) at URL: {url}")
            return None

        response.raise_for_status()
        return await response.json()


# --- FIX FOR SEMAPHORE ERROR: DYNAMIC BINDING ---
async def limited_request(coroutine):
    """
    Ensure requests adhere to rate limits using a loop-bound semaphore.
    This fixes the 'bound to a different event loop' error.
    """
    loop = asyncio.get_running_loop()

    # Check if the current loop already has a semaphore attached to it
    if not hasattr(loop, 'shopify_sem'):
        # If not, create one specifically for this loop
        loop.shopify_sem = asyncio.Semaphore(2)

    async with loop.shopify_sem:
        await asyncio.sleep(0.5)
        return await coroutine


async def async_shopify_fetch(session, resource_path):
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    API_VERSION = '2024-04'

    base_url_clean = shop_url.split('/admin')[0].rstrip('/')
    shopify_url = f"{base_url_clean}/admin/api/{API_VERSION}/{resource_path.lstrip('/')}"
    auth = BasicAuth(api_key, password)

    return await limited_request(
        fetch_with_retry(session, shopify_url, auth=auth, headers={'Content-Type': 'application/json'})
    )


@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    to_emails = data.get('to', [])
    cc_emails = data.get('cc', [])
    subject = data.get('subject', '')
    body = data.get('body', '')

    try:
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')
        smtp_password = os.getenv('SMTP_PASSWORD')

        msg = MIMEText(body)
        msg['From'] = smtp_user
        msg['To'] = ', '.join(to_emails)
        msg['Cc'] = ', '.join(cc_emails)
        msg['Subject'] = subject

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()
        return jsonify({'message': 'Email sent successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


async def fetch_tracking_data(session, tracking_number):
    if not tracking_number or str(tracking_number).strip() == "N/A":
        return []
    url = "https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory"
    timeout = ClientTimeout(total=20)
    try:
        async with session.get(url, params={"cn": tracking_number}, timeout=timeout) as response:
            if response.status != 200:
                return {"error": "Courier tracking is temporarily unavailable. Please try again later."}
            data = await response.json()
            if isinstance(data, dict) and "d" in data:
                data = data["d"]
                if isinstance(data, str):
                    data = json.loads(data)
            if isinstance(data, list):
                return [event for event in data if isinstance(event, dict)
                        and str(event.get("ProcessDescForPortal") or "").strip()]
            return []
    except Exception:
        return {"error": "Courier tracking is temporarily unavailable. Please try again later."}


async def fetch_tracking_status(session, tracking_number):
    if is_digidokaan_tracking_number(tracking_number):
        status = await fetch_digidokaan_tracking_status(session, tracking_number)
        return status or "Tracking unavailable"
    data = await fetch_tracking_data(session, tracking_number)
    if data and isinstance(data, list) and data[-1].get("ProcessDescForPortal"):
        return data[-1]["ProcessDescForPortal"]
    return "Tracking unavailable"


def is_digidokaan_tracking_number(tracking_number):
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    return len(normalized) in {14, 15} and normalized.startswith("223")


def tracking_url_for_number(tracking_number):
    normalized = str(tracking_number or "").strip()
    return f"/track/{quote(normalized, safe='')}"

async def process_line_item(session, line_item, fulfillments):
    if line_item.fulfillment_status is None and line_item.fulfillable_quantity == 0:
        return []

    tracking_info = []
    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if fulfillment.status == "cancelled":
                continue
            for item in fulfillment.line_items:
                if item.id == line_item.id:
                    tracking_number = fulfillment.tracking_number
                    tracking_details = await fetch_tracking_status(session, tracking_number)
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'tracking_url': tracking_url_for_number(tracking_number),
                        'status': tracking_details,
                        'quantity': item.quantity
                    })
    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": int(getattr(line_item, "fulfillable_quantity", 0) or 0)}
    ]


async def fetch_product_display(session, line_item):
    image_src = "https://static.thenounproject.com/png/1578832-200.png"
    variant_name = line_item.variant_title or ""
    if line_item.product_id is None:
        return image_src, variant_name
    cache_key = (str(line_item.product_id), str(line_item.variant_id or ""))
    cached = product_display_cache.get(cache_key)
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    inflight = getattr(loop, "product_display_inflight", None)
    if inflight is None:
        inflight = {}
        loop.product_display_inflight = inflight

    task = inflight.get(cache_key)
    if task is None:
        async def load_product():
            current_image = image_src
            current_variant = variant_name
            try:
                product_data = await async_shopify_fetch(session, f"products/{line_item.product_id}.json")
                product = product_data.get("product") if product_data else None
                if product and product.get("variants"):
                    for variant in product["variants"]:
                        if str(variant.get("id")) != str(line_item.variant_id):
                            continue
                        image_id = variant.get("image_id")
                        if image_id is not None:
                            image_data = await async_shopify_fetch(
                                session, f"products/{line_item.product_id}/images/{image_id}.json"
                            )
                            if image_data and image_data.get("image"):
                                current_image = image_data["image"].get("src") or current_image
                        else:
                            current_image = (product.get("image") or {}).get("src") or current_image
                        break
            except Exception as error:
                print(f"Error fetching product details: {error}")
            return current_image, current_variant

        task = asyncio.create_task(load_product())
        inflight[cache_key] = task
    try:
        result = await task
        product_display_cache[cache_key] = result
        return result
    finally:
        if inflight.get(cache_key) is task:
            inflight.pop(cache_key, None)

async def safe_process_order(session, order):
    loop = asyncio.get_running_loop()
    semaphore = getattr(loop, "order_process_sem", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(25)
        loop.order_process_sem = semaphore
    async with semaphore:
        return await process_order(session, order)


async def process_order(session, order):
    try:
        order_start_time = time.time()
        created_at_str = order.created_at
        created_at_obj = datetime.fromisoformat(created_at_str)
        formatted_date = created_at_obj.strftime('%Y-%m-%d')
        subtotal_price = parse_money(getattr(order, "current_subtotal_price", None) or getattr(order, "subtotal_price", 0))
        total_price = parse_money(getattr(order, "current_total_price", None) or getattr(order, "total_price", subtotal_price))
        shipping_charges = get_shopify_order_shipping_total(order)
        customer_details = await load_shopify_customer_details(session, order)

        order_info = {
            'order_link': "https://admin.shopify.com/store/alkaramat/orders/" + str(order.id),
            'id': order.id,
            'shopify_id': order.id,
            'order_num': order.name.replace("#", ""),
            'order_id': order.name.replace("#", ""),
            'created_at': formatted_date,
            'subtotal_price': subtotal_price,
            'current_subtotal_price': subtotal_price,
            'shipping_charges': shipping_charges,
            'total_discounts': parse_money(getattr(order, "total_discounts", 0)),
            'total_price': total_price,
            'current_total_price': total_price,
            'display_total_price': total_price,
            'line_items': [],
            'financial_status': order.financial_status.title(),
            'fulfillment_status': order.fulfillment_status or "Unfulfilled",
            'customer_details': customer_details,
            'tags': order.tags.split(", ") if order.tags else [],
            'note': str(getattr(order, 'note', '') or '')
        }

        tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        results = await asyncio.gather(*tasks)

        for tracking_info_list, line_item in zip(results, order.line_items):
            if tracking_info_list is None: continue

            image_src, variant_name = await fetch_product_display(session, line_item)

            for info in tracking_info_list:
                order_info['line_items'].append({
                    'line_item_id': line_item.id,
                    'fulfillment_status': line_item.fulfillment_status,
                    'image_src': image_src,
                    'product_id': line_item.product_id,
                    'variant_id': line_item.variant_id,
                    'sku': getattr(line_item, 'sku', '') or '',
                    'fulfillable_quantity': int(getattr(line_item, 'fulfillable_quantity', 0) or 0),
                    'unit_price': parse_money(getattr(line_item, 'price', 0)),
                    'product_title': line_item.title + (f" - {variant_name}" if variant_name else ""),
                    'quantity': info['quantity'],
                    'tracking_number': info['tracking_number'],
                    'tracking_url': info.get('tracking_url') or tracking_url_for_number(info['tracking_number']),
                    'status': info['status']
                })
                order_info['status'] = info['status']

        order_end_time = time.time()
        return order_info
    except Exception as e:
        print(f"Error processing order {order.order_number}: {e}")
        return None



@app.route('/pending')
def pending_orders():
    all_orders, pending_items, summary = build_pending_items_table_data()
    return render_template('pending.html', all_orders=all_orders, pending_items=pending_items, summary=summary)


@app.route('/payments')
def payments_page():
    try:
        async def load_payments():
            async with aiohttp.ClientSession() as client:
                return await fetch_digidokaan_payments(client)

        payments = asyncio.run(load_payments())
        error = ""
    except Exception as fetch_error:
        print(f"Could not load DigiDokaan payments: {fetch_error}")
        payments = {"balance": {}, "ready": {}, "ledger": {}}
        error = str(fetch_error)
    dashboard = build_digidokaan_payment_dashboard(payments)
    return render_template("payments.html", payments=payments, payment_dashboard=dashboard, payments_error=error)


def _digidokaan_rows(payload):
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def _payment_identifier(row, keys):
    for key in keys:
        value = str((row or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def build_digidokaan_payment_dashboard(payments, operational_metrics=None):
    balance = payments.get("balance") or {}
    ledger = payments.get("ledger") or {}
    ledger_rows = _digidokaan_rows(ledger)
    cheque_rows = payments.get("cheques") if isinstance(payments.get("cheques"), list) else []
    shipment_keys = ("tracking_no", "tracking_number", "consignment_no", "order_no", "external_reference_no", "reference_no")
    cheque_keys = ("cheque_no", "cheque_number", "settlement_id", "payment_id", "batch_id")

    cheque_by_id = {}
    cheque_by_shipment = {}
    for cheque in cheque_rows:
        cheque_id = _payment_identifier(cheque, cheque_keys)
        if cheque_id:
            cheque_by_id[cheque_id.casefold()] = cheque
        for key in shipment_keys:
            value = str(cheque.get(key) or "").strip()
            if value:
                cheque_by_shipment[value.casefold()] = cheque

    # Cheque details contain the paid shipment ledger lines. Merge them into
    # the open ledger and annotate duplicates, instead of adding their COD and
    # charges twice.
    merged_rows = {}
    def row_signature(row):
        return (
            str(row.get("tracking_no") or "").strip(), str(row.get("order_no") or "").strip(),
            str(row.get("payment_type") or row.get("payment_mode") or "").strip().casefold(),
            str(row.get("amount") or "0"), str(row.get("sub_amount") or "0"),
        )
    for row in ledger_rows:
        merged_rows[row_signature(row)] = dict(row)
    for cheque in cheque_rows:
        for row in _digidokaan_rows(cheque.get("shipments") or {}):
            annotated = dict(row)
            annotated["cheque_no"] = cheque.get("cheque_no")
            annotated["cheque_status"] = cheque.get("status")
            merged_rows[row_signature(row)] = annotated
    ledger_rows = list(merged_rows.values())

    grouped = {}
    for index, row in enumerate(ledger_rows):
        tracking = _payment_identifier(row, ("tracking_no", "tracking_number", "consignment_no"))
        order_no = _payment_identifier(row, ("order_no", "external_reference_no", "reference_no"))
        key = tracking or order_no or f"ledger-{index}"
        group = grouped.setdefault(key, {
            "order_no": order_no, "reference": str(row.get("external_reference_no") or "").strip(),
            "tracking_no": tracking, "order_date": str(row.get("order_date") or "").strip(),
            "order_status": str(row.get("order_status") or "").strip(), "price": 0.0, "rows": [],
        })
        group["rows"].append(row)
        group["price"] = max(group["price"], parse_money(row.get("price"), 0))
        for field, value in (("tracking_no", tracking), ("order_no", order_no), ("reference", str(row.get("external_reference_no") or "").strip()), ("order_date", str(row.get("order_date") or "").strip()), ("order_status", str(row.get("order_status") or "").strip())):
            if value:
                group[field] = value

    paid_words = ("cleared", "completed", "disbursed", "transferred", "success")
    pending_words = ("pending", "ready", "processing", "generated", "issued", "scheduled")
    shipments = []
    for group in grouped.values():
        cod = 0.0
        deductions = 0.0
        linked_cheque = None
        cheque_id = ""
        ledger_cheque_status = ""
        entry_types = []
        for row in group.pop("rows"):
            entry_type = str(row.get("payment_type") or row.get("payment_mode") or "").strip().upper()
            if entry_type and entry_type not in entry_types:
                entry_types.append(entry_type)
            amount = parse_money(row.get("amount"), 0)
            charge = parse_money(row.get("sub_amount"), 0)
            if "COD" in entry_type:
                cod += amount
            elif amount < 0 and not charge:
                deductions += abs(amount)
            deductions += abs(charge)
            row_cheque_id = _payment_identifier(row, cheque_keys)
            if row_cheque_id:
                cheque_id = row_cheque_id
                linked_cheque = cheque_by_id.get(row_cheque_id.casefold()) or linked_cheque
                ledger_cheque_status = str(row.get("cheque_status") or row.get("settlement_status") or row.get("payment_status") or "").strip() or ledger_cheque_status
        if not linked_cheque:
            for value in (group.get("tracking_no"), group.get("order_no"), group.get("reference")):
                if value and value.casefold() in cheque_by_shipment:
                    linked_cheque = cheque_by_shipment[value.casefold()]
                    cheque_id = _payment_identifier(linked_cheque, cheque_keys)
                    break
        settlement_status = str((linked_cheque or {}).get("status") or (linked_cheque or {}).get("settlement_status") or ledger_cheque_status).strip()
        normalized_status = settlement_status.casefold()
        explicitly_paid = bool(re.search(r"\bpaid\b", normalized_status)) or any(word in normalized_status for word in paid_words)
        explicitly_unpaid = "unpaid" in normalized_status or "not paid" in normalized_status
        if explicitly_unpaid:
            payment_status = "Not paid"
        elif (linked_cheque or cheque_id) and explicitly_paid:
            payment_status = "Paid"
        elif linked_cheque or cheque_id:
            payment_status = "Pending cheque" if not normalized_status or any(word in normalized_status for word in pending_words) else "Unconfirmed"
        else:
            payment_status = "Not paid"
        group.update({
            "cod": round(cod, 2), "deductions": round(deductions, 2),
            "net": round(cod - deductions, 2), "entry_types": ", ".join(entry_types),
            "cheque_no": cheque_id, "cheque_status": settlement_status,
            "payment_status": payment_status,
        })
        shipments.append(group)

    total_cod = parse_money(ledger.get("total_order_price"), parse_money(ledger.get("total_cod"), sum(row["cod"] for row in shipments)))
    deduction_keys = ("total_delivery_charges", "total_sales_tax", "total_income_tax")
    total_deductions = round(
        sum(parse_money(ledger.get(key), 0) for key in deduction_keys)
        if any(ledger.get(key) is not None for key in deduction_keys)
        else sum(row["deductions"] for row in shipments),
        2,
    )
    paid_shipments = [row for row in shipments if row["payment_status"] == "Paid"]
    unpaid_shipments = [row for row in shipments if row["payment_status"] != "Paid"]
    delivered_shipments = [row for row in shipments if "deliver" in row.get("order_status", "").casefold() and "undeliver" not in row.get("order_status", "").casefold()]
    deducted_shipments = [row for row in shipments if row["deductions"] > 0]
    cheque_total = round(sum(parse_money(row.get("amount") or row.get("cheque_amount"), 0) for row in cheque_rows), 2)
    if cheque_rows:
        received = cheque_total
    elif ledger.get("total_paid") is not None:
        received = parse_money(ledger.get("total_paid"), 0)
    elif balance.get("payment_received") is not None:
        received = parse_money(balance.get("payment_received"), 0)
    else:
        received = round(sum(max(row["net"], 0) for row in paid_shipments), 2)
    outstanding = parse_money(ledger.get("total_balance"), sum(max(row["net"], 0) for row in unpaid_shipments))
    combined_cheques = [dict(row) for row in cheque_rows]
    known_cheques = {_payment_identifier(row, cheque_keys).casefold() for row in combined_cheques if _payment_identifier(row, cheque_keys)}
    for row in ledger_rows:
        cheque_id = _payment_identifier(row, cheque_keys)
        if cheque_id and cheque_id.casefold() not in known_cheques:
            combined_cheques.append({
                "cheque_no": cheque_id,
                "cheque_date": row.get("cheque_date") or row.get("settlement_date") or row.get("payment_date"),
                "cheque_amount": row.get("cheque_amount") or row.get("settlement_amount"),
                "status": row.get("cheque_status") or row.get("settlement_status") or row.get("payment_status") or "Unconfirmed",
            })
            known_cheques.add(cheque_id.casefold())

    gross_shipments = [row for row in shipments if row.get("price", 0) > 0 or row.get("cod", 0) > 0]
    gross_cod = round(sum(row.get("price", 0) or row.get("cod", 0) for row in gross_shipments), 2)
    dispatched_count = len(shipments)
    gross_count = len(gross_shipments)
    ready_shipments = [row for row in delivered_shipments if row["payment_status"] != "Paid"]
    cards = [
        {"label": "Gross COD", "value": gross_cod, "count": gross_count, "note": "dispatched shipments"},
        {"label": "Total shipments", "value": dispatched_count, "count": dispatched_count, "note": f"PKR {gross_cod:,.2f} combined gross COD", "count_primary": True},
        {"label": "Payment received", "value": received, "count": len(paid_shipments), "note": "shipments explicitly linked to paid cheques"},
        {"label": "Outstanding payment", "value": outstanding, "count": len(unpaid_shipments), "note": "shipments not confirmed paid"},
        {"label": "Ready for payout", "value": parse_money(balance.get("deliver_orders_payments"), 0), "count": len(ready_shipments), "note": "delivered shipments not yet paid"},
        {"label": "Courier deductions", "value": total_deductions, "count": len(deducted_shipments), "note": "shipments with recorded deductions"},
        {"label": "Effective deduction", "value": (total_deductions / gross_cod * 100) if gross_cod else 0, "count": len(deducted_shipments), "note": "percent of gross COD", "percent": True},
        {"label": "Delivered COD", "value": round(sum(row["cod"] for row in delivered_shipments), 2), "count": len(delivered_shipments), "note": "delivered shipments"},
        {"label": "Ledger balance", "value": parse_money(ledger.get("total_balance"), outstanding), "count": len(unpaid_shipments), "note": "DigiDokaan current net balance"},
    ]
    return {"cards": cards, "shipments": shipments, "cheques": combined_cheques}


def build_payment_operational_metrics():
    buckets = {
        "all": [], "dispatched": [], "in_transit": [], "delivered": [],
        "attention": [], "unbooked": [], "paid": [], "unpaid": [],
    }
    for order in order_details:
        statuses = {normalize_status_bucket(item.get("status")) for item in order.get("line_items", [])}
        financial_status = str(order.get("financial_status") or "").strip().lower()
        buckets["all"].append(order)
        if statuses and statuses != {"Un-Booked"}:
            buckets["dispatched"].append(order)
        if statuses & {"Booked", "Picked From Shipper", "Out For Delivery"}:
            buckets["in_transit"].append(order)
        if "Delivered" in statuses:
            buckets["delivered"].append(order)
        if statuses & {"Undelivered", "Being Return", "Partially Delivered", "RETURNED TO SHIPPER"}:
            buckets["attention"].append(order)
        if not statuses or statuses == {"Un-Booked"}:
            buckets["unbooked"].append(order)
        buckets["paid" if financial_status in PAID_FINANCIAL_STATUSES else "unpaid"].append(order)

    def metric(key, label, description):
        orders = buckets[key]
        return {
            "key": key,
            "label": label,
            "count": len(orders),
            "value": round(sum(parse_money(order.get("total_price"), 0) for order in orders), 2),
            "description": description,
        }

    dispatched_unpaid = [
        order for order in buckets["dispatched"]
        if str(order.get("financial_status") or "").strip().lower() not in PAID_FINANCIAL_STATUSES
    ]
    return [
        metric("all", "Loaded Shopify orders", "All Shopify orders currently loaded in the dashboard."),
        metric("dispatched", "Dispatched orders", "Orders with a courier booking or later tracking status."),
        {"key": "dispatched_cod", "label": "Dispatched COD", "count": len(dispatched_unpaid), "value": round(sum(parse_money(order.get("total_price"), 0) for order in dispatched_unpaid), 2), "description": "Unpaid dispatched orders; this is the expected COD price."},
        metric("in_transit", "In transit", "Booked, picked-up, or out-for-delivery orders."),
        metric("delivered", "Delivered", "Orders with at least one delivered shipment."),
        metric("attention", "Needs attention", "Undelivered, returning, returned, or partially delivered orders."),
        metric("unbooked", "Not dispatched", "Orders without an active courier booking."),
        metric("paid", "Shopify paid", "Orders Shopify currently reports as paid or partially paid."),
        metric("unpaid", "Shopify unpaid", "Orders Shopify currently reports as unpaid."),
    ]


@app.route('/api/shipper-advice', methods=['POST'])
def post_shipper_advice():
    data = request.get_json(silent=True) or {}
    tracking_number = "".join(ch for ch in str(data.get("tracking_number") or "") if ch.isdigit())
    advice_status = str(data.get("advice_status") or "").strip().casefold()
    remarks = str(data.get("remarks") or "").strip()
    if not tracking_number or advice_status not in {"reattempt", "return"} or not remarks:
        return jsonify({"success": False, "error": "Tracking number, action and remarks are required."}), 400

    async def submit():
        async with aiohttp.ClientSession() as client:
            pending = await fetch_pending_shipper_advice(client)
            shipment = next(
                (row for row in pending if str(row.get("tracking_no") or "").strip() == tracking_number),
                None,
            )
            if not shipment:
                raise ValueError("This shipment is no longer awaiting shipper advice.")
            return await submit_shipper_advice(
                client,
                tracking_number,
                shipment.get("gateway_id"),
                advice_status,
                remarks,
            )

    try:
        result = asyncio.run(submit())
        return jsonify({
            "success": True,
            "message": result.get("msg") or result.get("message") or "Shipper advice submitted successfully.",
        })
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 409
    except Exception as error:
        print(f"Could not submit DigiDokaan shipper advice: {error}")
        return jsonify({"success": False, "error": str(error)}), 502


async def getShopifyOrders():
    start_date = datetime(2024, 9, 1).isoformat()
    order_details = []
    total_start_time = time.time()

    try:
        orders = shopify.Order.find(limit=250, order="created_at DESC", created_at_min=start_date)
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return []

    async with aiohttp.ClientSession() as session:
        while True:
            tasks = [safe_process_order(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing an order: {result}")
                elif result is not None:
                    order_details.append(result)

            try:
                if not orders.has_next_page():
                    break

                time.sleep(1.0)
                orders = orders.next_page()

            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    order_details = enrich_orders_with_protected_customer_data(order_details)
    total_end_time = time.time()
    print(f"Processed {len(order_details)} orders in {total_end_time - total_start_time:.2f} seconds")
    return order_details


def adjust_to_shopify_timezone(from_date, to_date):
    from_date = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
    to_date = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    from_date_gmt_plus_5 = from_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    to_date_gmt_plus_5 = to_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    return from_date_gmt_plus_5, to_date_gmt_plus_5


async def getShopifyOrderswithDates(start_date: str, end_date: str):
    order_details = []

    try:
        orders = shopify.Order.find(
            limit=50,
            order="created_at DESC",
            created_at_min=start_date,
            created_at_max=end_date,
            status='any'
        )
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return []

    async with aiohttp.ClientSession() as session:
        while True:
            tasks = [process_order(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing: {result}")
                elif result is not None:
                    order_details.append(result)

            try:
                if not orders.has_next_page():
                    break

                time.sleep(1.0)
                orders = orders.next_page()
            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    return enrich_orders_with_protected_customer_data(order_details)


@app.route('/fetch-orders', methods=['POST'])
def fetch_orders():
    data = request.get_json()
    from_date = data.get('fromDate')
    to_date = data.get('toDate')
    from_date_utc, to_date_utc = adjust_to_shopify_timezone(from_date, to_date)

    orders = asyncio.run(getShopifyOrderswithDates(from_date_utc, to_date_utc))
    total_sales = 0.0
    for order in orders:
        try:
            total_sales += float(order['total_price'])
        except:
            pass

    return jsonify({
        'orders': orders,
        'total_sales': total_sales,
        'total_cost': 0.0
    })


@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    tag = data.get('tag')

    # Get today's date in YYYY-MM-DD format
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"

    try:
        # Fetch the order
        order = shopify.Order.find(order_id)

        # If the tag is "Returned", cancel the order
        if tag.strip().lower() == "returned":
            # Attempt to cancel the order
            if order.cancel():
                print("Order Cancelled")
            else:
                print("Order Cancellation Failed")
        if tag.strip().lower() == "delivered":
            if order.close():
                print("Order Cloed")
            else:
                print("Order Closing Failed")

        # Process existing tags
        if order.tags:
            tags = [t.strip() for t in order.tags.split(", ")]  # Remove excess spaces
        else:
            tags = []

        # Remove a specific tag if needed (e.g., "Leopards Courier")
        if "Leopards Courier" in tags:
            tags.remove("Leopards Courier")

        # Add new tag if it doesn't already exist
        if tag_with_date not in tags:
            tags.append(tag_with_date)

        # Update the order with the new tags
        order.tags = ", ".join(tags)

        # Save the order
        if order.save():
            return jsonify({"success": True, "message": "Tag applied successfully."})
        else:
            return jsonify({"success": False, "error": "Failed to save order changes."})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/apply_bulk_tag', methods=['POST'])
def apply_bulk_tag():
    data = request.json
    order_ids_to_tag = data.get('order_ids', [])
    # Add 'DELIVERED' to the allowed list
    tag_type = data.get('tag_type')

    if not order_ids_to_tag or tag_type not in ['RETURNED', 'DISPATCHED', 'DELIVERED']:
        return jsonify({"success": False, "error": "Invalid input."}), 400

    today_date = datetime.now().strftime('%Y-%m-%d')
    results = []

    for order_shopify_id in order_ids_to_tag:
        try:
            # 1. FIX FOR BUG #2: Sleep to prevent 429 (Shopify Limit)
            time.sleep(1.0) # Increased to 1.0s to be safe

            base_tag = ""
            order = shopify.Order.find(order_shopify_id)
            
            if not order:
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Order not found.'})
                continue

            # Handle Tag Logic
            if tag_type == 'RETURNED':
                base_tag = "Return Received"
                # Optional: Cancel order if returned
                # order.cancel() 
            elif tag_type == 'DISPATCHED':
                base_tag = "DISPATCHED"
            elif tag_type == 'DELIVERED':
                base_tag = "Delivered"
                # Archive/Close the order in Shopify
                try:
                    order.close()
                except:
                    pass

            final_tag = f"{base_tag} ({today_date})"

            tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []
            
            # Remove conflicting tags if necessary
            if "Leopards Courier" in tags: tags.remove("Leopards Courier")
            
            if final_tag not in tags:
                tags.append(final_tag)

            order.tags = ", ".join(tags)

            if order.save():
                results.append({'id': order_shopify_id, 'status': 'success', 'message': f'Tag "{final_tag}" applied.'})
            else:
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Failed to save.'})

        except Exception as e:
            # Retry logic or error logging
            if "429" in str(e):
                time.sleep(2)
                results.append({'id': order_shopify_id, 'status': 'error', 'message': 'Rate Limit Hit'})
            else:
                results.append({'id': order_shopify_id, 'status': 'error', 'message': str(e)})

    return jsonify({
        'success': True,
        'tag_applied': tag_type,
        'total_orders': len(order_ids_to_tag),
        'results': results
    }), 200


def _digits(value):
    return "".join(character for character in str(value or "") if character.isdigit())


def enrich_shipper_advice_orders(advice_rows, shopify_orders):
    """Attach the matching Shopify order summary without trusting a single identifier."""
    by_reference = {}
    by_tracking = {}
    for order in shopify_orders or []:
        reference = _digits(order.get("order_num") or order.get("order_id"))
        if reference:
            by_reference[reference] = order
        for item in order.get("line_items") or []:
            tracking = _digits(item.get("tracking_number"))
            if tracking and tracking != "0":
                by_tracking[tracking] = order

    enriched = []
    for raw_advice in advice_rows or []:
        advice = dict(raw_advice or {})
        reference = _digits(advice.get("external_reference_no") or advice.get("order_id"))
        tracking = _digits(advice.get("tracking_no"))
        order = by_reference.get(reference) or by_tracking.get(tracking)
        if order:
            matching_items = [
                item for item in (order.get("line_items") or [])
                if not tracking or _digits(item.get("tracking_number")) == tracking
            ] or list(order.get("line_items") or [])
            advice["shopify_order"] = {
                "order_num": order.get("order_num") or order.get("order_id") or reference,
                "total": parse_money(order.get("total_price"), 0),
                "display_total": format_currency_amount(order.get("total_price"), "PKR"),
                "items": [{
                    "title": item.get("product_title") or item.get("item_title") or "Product",
                    "image": item.get("image_src") or item.get("item_image") or "",
                    "quantity": parse_int(item.get("quantity"), 1),
                } for item in matching_items],
            }
        else:
            advice["shopify_order"] = None
        enriched.append(advice)
    return enriched


@app.route("/")
def tracking_home():
    global order_details
    refresh_daraz_cache_if_needed()
    try:
        async def load_shipper_advice():
            async with aiohttp.ClientSession() as client:
                return await fetch_pending_shipper_advice(client)

        shipper_advice_orders = enrich_shipper_advice_orders(asyncio.run(load_shipper_advice()), order_details)
    except Exception as advice_error:
        print(f"Could not load DigiDokaan shipper advice: {advice_error}")
        shipper_advice_orders = []
    total_order_value = sum(parse_money(order.get("total_price", 0)) for order in order_details)
    return render_template(
        "track.html",
        order_details=order_details,
        darazOrders=daraz_orders,
        shipper_advice_orders=shipper_advice_orders,
        employee_approvals=build_employee_approval_items(),
        total_order_value=total_order_value,
        abandoned_summary=get_abandoned_summary_safe(),
    )


def refresh_tracking_in_background():
    global order_details
    try:
        refreshed_orders = asyncio.run(getShopifyOrders())
        if refreshed_orders:
            refreshed_ids = {
                str(order.get("id") or order.get("shopify_id") or "")
                for order in refreshed_orders
            }
            preserved_orders = [
                order for order in order_details
                if str(order.get("id") or order.get("shopify_id") or "") not in refreshed_ids
            ]
            order_details = refreshed_orders + preserved_orders
        elif order_details:
            print("Shopify refresh returned no orders; preserving the existing dashboard cache.")
        daraz_rows = refresh_daraz_cache_if_needed(force=True)
        with tracking_refresh_lock:
            tracking_refresh_state.update(
                running=False,
                error="",
                shopify_count=len(order_details),
                daraz_count=len(daraz_rows),
                updated_at=int(time.time()),
            )
    except Exception as error:
        print(f"Error refreshing data: {error}")
        with tracking_refresh_lock:
            tracking_refresh_state.update(running=False, error=str(error))


def automatic_tracking_refresh_loop():
    """Refresh courier statuses periodically; the lock prevents overlapping runs."""
    while True:
        time.sleep(TRACKING_AUTO_REFRESH_SECONDS)
        with tracking_refresh_lock:
            if tracking_refresh_state["running"]:
                continue
            tracking_refresh_state.update(running=True, error="")
        refresh_tracking_in_background()


@app.route('/refresh', methods=['POST'])
def refresh_data():
    with tracking_refresh_lock:
        if tracking_refresh_state["running"]:
            return jsonify({"message": "Tracking refresh is already running", "status": "running"}), 202
        tracking_refresh_state.update(running=True, error="")
    worker = threading.Thread(target=refresh_tracking_in_background, daemon=True)
    worker.start()
    return jsonify({"message": "Tracking refresh started", "status": "running"}), 202


@app.route('/refresh/status')
def refresh_data_status():
    with tracking_refresh_lock:
        state = dict(tracking_refresh_state)
    state["auto_refresh_seconds"] = TRACKING_AUTO_REFRESH_SECONDS
    state["status"] = "running" if state["running"] else ("failed" if state["error"] else "complete")
    state["message"] = (
        "Refreshing tracking data"
        if state["running"]
        else ("Tracking refresh failed" if state["error"] else "Data refreshed successfully")
    )
    return jsonify(state)


@app.route("/shopify/protected-data/status")
def shopify_protected_data_status():
    return jsonify(get_protected_data_config_status())


@app.route("/shopify/install")
def shopify_install():
    state = create_oauth_state()
    session[SHOPIFY_OAUTH_STATE_SESSION_KEY] = state
    return redirect(get_install_url(state))


@app.route("/shopify/callback")
def shopify_callback():
    shop = (request.args.get("shop") or "").strip()
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    saved_state = session.get(SHOPIFY_OAUTH_STATE_SESSION_KEY, "")
    if not state or not saved_state or not secrets.compare_digest(state, saved_state):
        return jsonify({"success": False, "error": "Invalid Shopify callback state"}), 400
    if not verify_oauth_hmac(request.query_string):
        return jsonify({"success": False, "error": "Invalid Shopify callback signature"}), 400
    if shop != get_shop_domain() or not code:
        return jsonify({"success": False, "error": "Invalid Shopify store or missing authorization code"}), 400
    try:
        save_offline_token(shop, exchange_oauth_code_for_token(shop, code))
        session.pop(SHOPIFY_OAUTH_STATE_SESSION_KEY, None)
        with tracking_refresh_lock:
            if not tracking_refresh_state["running"]:
                tracking_refresh_state.update(running=True, error="")
                threading.Thread(target=refresh_tracking_in_background, daemon=True).start()
        return redirect("/shopify/protected-data/status?connected=1")
    except Exception as error:
        return jsonify({"success": False, "error": f"Shopify token exchange failed: {error}"}), 500


@app.route('/daraz')
def daraz_callback():
    pending = session.pop('daraz_oauth', None)
    state = request.args.get('state', '')
    try:
        callback = get_daraz_callback_url()
    except RuntimeError as error:
        return jsonify({'success': False, 'error': str(error)}), 400
    if (not pending or not state
            or not secrets.compare_digest(state, pending.get('state', ''))
            or not 0 <= time.time() - pending.get('created_at', 0) <= 600
            or pending.get('callback') != callback
            or request.host.lower() != 'dashboard.alkaramat.com'
            or request.path != '/daraz'):
        return jsonify({'success': False, 'error': 'Invalid or expired Daraz connection. Start Connect Daraz again from Al Karamat; do not edit the callback URL.'}), 400
    code = (request.args.get('code') or '').strip()
    if code:
        try:
            config = daraz_configuration()
            client = lazop.LazopClient(DARAZ_API_URL, config['app_key'], config['app_secret'])
            token_request = lazop.LazopRequest('/auth/token/create')
            token_request.add_api_param('code', code)
            body = client.execute(token_request).body or {}
            if not body.get('access_token'):
                raise RuntimeError(body.get('message') or body.get('code') or 'Daraz authorization failed')
            save_tokens(
                body['access_token'],
                body.get('refresh_token'),
                body.get('expires_in') or 604800,
            )
            refresh_daraz_cache_if_needed(force=True)
            return redirect(url_for('refresh_daraz_orders_page'))
        except Exception as error:
            return jsonify({'success': False, 'error': str(error)}), 400
    return redirect(url_for('tracking_home'))


@app.route('/daraz/orders')
def refresh_daraz_orders_page():
    rows = refresh_daraz_cache_if_needed(force=True)
    return render_template('daraz.html', darazOrders=rows, error_message=daraz_last_error)


@app.route('/daraz/token-status')
def daraz_token_status():
    tokens = load_tokens()
    if not tokens:
        return jsonify({'status': 'missing', 'error': daraz_last_error})
    expires_at_raw = str(tokens.get('expires_at') or '')
    try:
        days_left = (datetime.fromisoformat(expires_at_raw) - datetime.now()).days
    except ValueError:
        days_left = None
    return jsonify(
        {
            'status': 'ok',
            'expires_at': expires_at_raw,
            'days_left': days_left,
            'error': daraz_last_error,
        }
    )


@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    async def async_func():
        async with aiohttp.ClientSession() as session:
            if is_digidokaan_tracking_number(tracking_num):
                return await fetch_digidokaan_tracking_history(session, tracking_num)
            return await fetch_tracking_data(session, tracking_num)

    data = asyncio.run(async_func())
    return render_template(
        'trackingdata_alk.html',
        data=data if isinstance(data, list) else [],
        tracking_number=tracking_num,
        tracking_error=data.get("error") if isinstance(data, dict) else None,
    )


@app.route('/abandoned')
def abandoned_orders():
    try:
        abandoned_checkouts, summary = asyncio.run(build_abandoned_checkouts_data())
        error = None
    except Exception as fetch_error:
        print(f"Could not build abandoned checkouts page: {fetch_error}")
        abandoned_checkouts = []
        summary = {"last_7_days": 0, "today": 0, "recovered": 0, "open": 0, "viewed": 0, "not_viewed": 0, "value": 0.0}
        error = str(fetch_error)
    return render_template(
        "abandoned.html",
        abandoned_checkouts=abandoned_checkouts,
        summary=summary,
        error=error,
    )


@app.route('/abandoned/mark-viewed', methods=['POST'])
def mark_abandoned_viewed():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "Missing checkout token"}), 400
    viewed_tokens = load_abandoned_viewed_tokens()
    viewed_tokens.add(token)
    saved = save_abandoned_viewed_tokens(viewed_tokens)
    return jsonify({"success": saved, "token": token, "viewed_count": len(viewed_tokens)})


@app.route('/api/admin/notifications')
def admin_notifications():
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    async def load_notifications():
        async with aiohttp.ClientSession() as client:
            abandoned, advice = await asyncio.gather(
                fetch_shopify_abandoned_checkouts(7),
                fetch_pending_shipper_advice(client),
            )
        return abandoned, advice

    try:
        abandoned, advice = asyncio.run(load_notifications())
        viewed_tokens = load_abandoned_viewed_tokens()
        abandoned_items = []
        for checkout in sorted(abandoned, key=lambda row: parse_date_timestamp(row.get("updated_at") or row.get("created_at")), reverse=True):
            token = str(checkout.get("token") or checkout.get("cart_token") or checkout.get("id") or "")
            if not token or token in viewed_tokens:
                continue
            customer = as_dict(checkout.get("customer"))
            shipping = as_dict(checkout.get("shipping_address"))
            name = shipping.get("name") or " ".join(
                part for part in (customer.get("first_name"), customer.get("last_name")) if part
            ) or checkout.get("email") or "Customer"
            abandoned_items.append({
                "id": f"abandoned:{token}",
                "token": token,
                "title": name,
                "age": relative_time_label(checkout.get("updated_at") or checkout.get("created_at")),
                "amount": format_currency_amount(checkout.get("total_price"), checkout.get("presentment_currency") or checkout.get("currency") or "PKR"),
                "url": "/admin_portal?section=abandoned",
            })
        advice_items = [{
            "id": f"advice:{str(item.get('tracking_no') or '')}:{str(item.get('status_date') or item.get('advice_date') or '')}",
            "tracking": str(item.get("tracking_no") or ""),
            "title": item.get("customer_name") or item.get("consignee_name") or "Shipment",
            "reason": item.get("courier_status_reason") or "Shipper advice required",
            "url": "/admin_portal?section=dashboard",
        } for item in advice]
        return jsonify({
            "success": True,
            "count": len(abandoned_items) + len(advice_items),
            "abandoned": abandoned_items[:20],
            "shipper_advice": advice_items[:20],
        })
    except Exception as error:
        print(f"Could not load admin notifications: {error}")
        return jsonify({"success": False, "error": "Notifications are temporarily unavailable."}), 503


@app.route('/undelivered')
def undelivered():
    global order_details
    refresh_daraz_cache_if_needed()
    active_daraz_orders = [
        order
        for order in daraz_orders
        if order.get('status') not in {'Ready To Ship', 'Pending', 'Packed', 'Packed by seller / warehouse'}
    ]
    return render_template("undelivered.html", order_details=order_details, darazOrders=active_daraz_orders)


@app.route('/report')
def report():
    global order_details
    return render_template("report.html", order_details=order_details)


def verify_shopify_webhook(request):
    shopify_hmac = request.headers.get('X-Shopify-Hmac-Sha256')
    data = request.get_data()
    secret = os.getenv('SHOPIFY_WEBHOOK_SECRET')
    if secret is None: return False
    digest = hmac.new(secret.encode('utf-8'), data, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed_hmac, shopify_hmac)


# ----------------------------------------------------------------------
# === FIX: BACKGROUND WEBHOOK PROCESSING ===
# Prevents Gunicorn Timeouts by processing data in a separate thread
# ----------------------------------------------------------------------

def background_webhook_processor(order_shopify_id):
    """
    Runs in a background thread to process the updated order
    without blocking the webhook response.
    """
    global order_details
    print(f"🔄 Webhook: Starting background update for order {order_shopify_id}")

    try:
        # 1. Fetch the fresh order object inside the thread
        # Note: We use the synchronous shopify library here which is fine in a thread
        order = shopify.Order.find(order_shopify_id)
        if not order:
            print(f"❌ Webhook: Order {order_shopify_id} not found in Shopify.")
            return

        # 2. Set up a new Async Event Loop for this thread
        # (asyncio.run works, but explicit loop handling is safer in threads)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run_update():
            async with aiohttp.ClientSession() as session:
                return await process_order(session, order)

        # Run the heavy processing
        updated_order_info = loop.run_until_complete(run_update())
        loop.close()

        if not updated_order_info:
            print(f"❌ Webhook: Failed to process data for {order_shopify_id}")
            return

        # 3. Update the Global List safely
        order_num_to_match = updated_order_info.get('order_num')
        updated = False

        # Find and replace
        for idx, existing_order in enumerate(order_details):
            if existing_order.get('order_num') == order_num_to_match:
                order_details[idx] = updated_order_info
                updated = True
                break

        # If not found (new order), append it
        if not updated:
            order_details.insert(0, updated_order_info)  # Add to top

        print(f"✅ Webhook: Successfully updated order {order_num_to_match}")

    except Exception as e:
        print(f"❌ Webhook Background Error: {e}")


@app.route('/shopify/webhook/order_updated', methods=['POST'])
def shopify_order_updated():
    global order_details
    time.sleep(1)
    try:
        # 1. Verify HMAC (Fast, keep synchronous)
        if not verify_shopify_webhook(request):
            print("Webhook verification failed.")
            return jsonify({'error': 'Invalid webhook signature'}), 401

        order_data = request.get_json()
        order_shopify_id = order_data.get('id')

        if not order_shopify_id:
            return jsonify({'error': 'No order id found'}), 400

        print(f"Received webhook trigger for order ID: {order_shopify_id}")

        # 2. Handle closed/archived orders (Fast)
        if order_data.get('closed_at'):
            print(f"Order {order_shopify_id} is closed. Removing from list.")
            order_details[:] = [o for o in order_details if str(o.get('id') or o.get('shopify_id') or '') != str(order_shopify_id)]
            return jsonify({'success': True, 'message': 'Order removed'}), 200

        # 3. Offload Processing to Background Thread
        # This returns 200 OK to Shopify immediately, preventing the timeout.
        thread = threading.Thread(target=background_webhook_processor, args=(order_shopify_id,))
        thread.daemon = True  # Ensures thread cleans up if app restarts
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Webhook received. Processing in background.'
        }), 200

    except Exception as e:
        print(f"Webhook processing error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def serialize_shopify_order_for_employee(order):
    customer = order.get("customer_details") or {}
    return {
        "source": "shopify",
        "source_label": "Alkaramat",
        "shopify_id": order.get("id") or order.get("shopify_id"),
        "order_id": str(order.get("order_id") or order.get("order_num") or ""),
        "status": order.get("status", ""),
        "customer_name": customer.get("name", ""),
        "customer_phone": customer.get("phone", ""),
        "customer_city": customer.get("city", ""),
        "total_price": order.get("total_price", 0),
        "created_at": order.get("created_at", ""),
        "items": [
            {
                "title": item.get("product_title", ""),
                "quantity": item.get("quantity", 0),
                "image": item.get("image_src", ""),
                "tracking_number": item.get("tracking_number", "N/A"),
                "status": item.get("status", ""),
            }
            for item in order.get("line_items", [])
        ],
    }


def serialize_daraz_order_for_employee(order):
    customer = order.get('customer') or {}
    return {
        'source': 'daraz',
        'source_label': 'Daraz',
        'shopify_id': None,
        'order_id': str(order.get('order_id') or ''),
        'status': order.get('status', ''),
        'customer_name': customer.get('name', ''),
        'customer_phone': customer.get('phone', ''),
        'customer_city': '',
        'total_price': order.get('total_price', 0),
        'created_at': order.get('created_at') or order.get('date', ''),
        'items': [
            {
                'title': item.get('item_title', ''),
                'quantity': item.get('quantity', 0),
                'image': item.get('item_image', ''),
                'tracking_number': item.get('tracking_number', 'N/A'),
                'status': item.get('status', ''),
            }
            for item in order.get('items_list', [])
        ],
    }


def build_employee_portal_orders():
    try:
        refresh_daraz_cache_if_needed()
        employee_orders = [serialize_shopify_order_for_employee(order) for order in order_details]
        employee_orders.extend(serialize_daraz_order_for_employee(order) for order in daraz_orders)
        return sorted(employee_orders, key=lambda order: parse_date_timestamp(order.get("created_at")), reverse=True)
    except Exception as error:
        print(f"Could not load employee portal orders: {error}")
        return []


def find_employee_portal_order(term):
    candidates = set(scan_term_candidates(term))
    if not candidates:
        return None
    for order in build_employee_portal_orders():
        order_id = normalize_scan_term(order.get("order_id"))
        if order_id in candidates or any(order_id.endswith(candidate) for candidate in candidates):
            return order
        for item in order.get("items", []):
            tracking = normalize_scan_term(item.get("tracking_number"))
            if tracking in candidates:
                return order
    return None


def apply_shopify_order_tag(order_id, tag, include_date=False):
    order = shopify.Order.find(order_id)
    tags = [item.strip() for item in str(getattr(order, "tags", "") or "").split(",") if item.strip()]
    clean_tag = tag.strip()
    if include_date:
        clean_tag = f"{clean_tag} ({datetime.now().strftime('%Y-%m-%d')})"
    if clean_tag not in tags:
        tags.append(clean_tag)
    order.tags = ", ".join(tags)
    return order.save()


def build_pending_orders_mobile_data():
    refresh_daraz_cache_if_needed()
    all_orders = []
    statuses = load_order_statuses()
    overrides = load_product_cost_overrides()
    for daraz_order in daraz_orders:
        if daraz_order.get('status') not in {'Ready To Ship', 'Pending', 'Packed', 'Packed by seller / warehouse'}:
            continue
        items = []
        for item in daraz_order.get('items_list', []):
            tracking_number = item.get('tracking_number', 'N/A')
            normalized_item = dict(item)
            normalized_item.update(
                {
                    'product_id': None,
                    'variant_id': None,
                    'unit_price': 0,
                    'unit_cost': 0,
                    'line_total': 0,
                    'line_cost_total': 0,
                    'applied_status': statuses.get(f"{daraz_order.get('order_id')}:{tracking_number}", ''),
                }
            )
            items.append(normalized_item)
        if not items:
            continue
        customer = daraz_order.get('customer') or {}
        total_price = parse_money(daraz_order.get('total_price', 0))
        all_orders.append(
            {
                'order_via': 'Daraz',
                'shopify_id': None,
                'order_link': None,
                'order_id': daraz_order.get('order_id'),
                'status': daraz_order.get('status', ''),
                'tags': [],
                'customer_name': customer.get('name', ''),
                'customer_phone': customer.get('phone', ''),
                'customer_address': customer.get('address', ''),
                'customer_city': '',
                'is_lahore': False,
                'date': daraz_order.get('created_at') or daraz_order.get('date', ''),
                'items_list': items,
                'financial_status': 'pending',
                'payment_status_label': 'Pending',
                'payment_status_class': 'pending',
                'subtotal_price': total_price,
                'current_subtotal_price': total_price,
                'shipping_charges': 0,
                'total_discounts': 0,
                'total_price': total_price,
                'current_total_price': total_price,
                'display_total_price': total_price,
                'pending_total_price': total_price,
                'pending_total_cost': 0,
            }
        )
    for shopify_order in order_details:
        if any(str(tag).startswith("Dispatched") for tag in shopify_order.get("tags", [])):
            continue
        customer = shopify_order.get("customer_details") or {}
        customer_city = (customer.get("city") or "").strip()
        items = []
        for item in shopify_order.get("line_items", []):
            item_status = normalize_status_bucket(item.get("status", ""))
            if not is_pending_line_item_status(item_status):
                continue
            tracking_number = item.get("tracking_number", "N/A")
            key = f"{shopify_order.get('order_num')}:{tracking_number}"
            quantity = int(item.get("quantity") or 0)
            unit_price = parse_money(item.get("unit_price", 0))
            unit_cost = get_cost_override_for_item(overrides, title=item.get("product_title", ""))
            items.append(
                {
                    "item_image": item.get("image_src", ""),
                    "item_title": item.get("product_title", ""),
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "unit_cost": unit_cost,
                    "line_total": round(unit_price * quantity, 2),
                    "line_cost_total": round(unit_cost * quantity, 2),
                    "tracking_number": tracking_number,
                    "status": item_status,
                    "applied_status": statuses.get(key, ""),
                }
            )
        if not items:
            continue
        financial_status = str(shopify_order.get("financial_status", "") or "").strip().lower()
        payment_label = "Pending"
        payment_class = "pending"
        if financial_status in PAID_FINANCIAL_STATUSES:
            payment_label = "Partially Paid" if "partially" in financial_status else "Paid"
            payment_class = "partial" if "partially" in financial_status else "paid"
        subtotal_price = parse_money(shopify_order.get("current_subtotal_price", shopify_order.get("subtotal_price", 0)))
        total_price = parse_money(shopify_order.get("current_total_price", shopify_order.get("total_price", subtotal_price)))
        shipping_charges = parse_money(shopify_order.get("shipping_charges", 0))
        total_discounts = parse_money(shopify_order.get("total_discounts", 0))
        all_orders.append(
            {
                "order_via": "Shopify",
                "shopify_id": shopify_order.get("id") or shopify_order.get("shopify_id"),
                "order_link": shopify_order.get("order_link"),
                "order_id": shopify_order.get("order_id") or shopify_order.get("order_num"),
                "status": normalize_status_bucket(shopify_order.get("status", "")),
                "tags": [tag for tag in shopify_order.get("tags", []) if tag != "Leopards Courier"],
                "customer_name": customer.get("name", ""),
                "customer_phone": customer.get("phone", ""),
                "customer_address": customer.get("address", ""),
                "customer_city": customer_city,
                "is_lahore": is_lahore_city(customer_city),
                "date": shopify_order.get("created_at", ""),
                "items_list": items,
                "financial_status": shopify_order.get("financial_status", ""),
                "payment_status_label": payment_label,
                "payment_status_class": payment_class,
                "subtotal_price": subtotal_price,
                "current_subtotal_price": subtotal_price,
                "shipping_charges": shipping_charges,
                "total_discounts": total_discounts,
                "total_price": total_price,
                "current_total_price": total_price,
                "display_total_price": total_price,
                "pending_total_price": round(sum(parse_money(item.get("line_total", 0)) for item in items), 2),
                "pending_total_cost": round(sum(parse_money(item.get("line_cost_total", 0)) for item in items), 2),
            }
        )
    # Shopify display dates can be timezone-naive while Daraz returns ISO dates
    # with an offset. Normalize both to numeric timestamps before comparing them.
    return sorted(all_orders, key=lambda order: parse_date_timestamp(order.get("date")), reverse=True)


def build_pending_items_table_data():
    pending_items = {}
    all_orders = build_pending_orders_mobile_data()
    paid_pending_value = 0.0
    unpaid_pending_value = 0.0
    total_items_cost = 0.0
    for order in all_orders:
        financial_status = str(order.get("financial_status", "") or "").strip().lower()
        pending_value = parse_money(order.get("pending_total_price", 0))
        pending_cost = parse_money(order.get("pending_total_cost", 0))
        total_items_cost += pending_cost
        if financial_status in PAID_FINANCIAL_STATUSES:
            paid_pending_value += pending_value
        else:
            unpaid_pending_value += pending_value
        for item in order.get("items_list", []):
            product_title = item["item_title"]
            quantity = int(item.get("quantity") or 0)
            if product_title not in pending_items:
                pending_items[product_title] = {
                    "item_image": item.get("item_image", ""),
                    "item_title": product_title,
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "unit_price": parse_money(item.get("unit_price", 0)),
                    "unit_cost": parse_money(item.get("unit_cost", 0)),
                    "quantity": 0,
                    "total_price": 0.0,
                    "total_cost": 0.0,
                    "statuses": {},
                }
            pending_items[product_title]["quantity"] += quantity
            pending_items[product_title]["total_price"] += parse_money(item.get("line_total", 0))
            pending_items[product_title]["total_cost"] += parse_money(item.get("line_cost_total", 0))
            status = item.get("status", "")
            pending_items[product_title]["statuses"][status] = pending_items[product_title]["statuses"].get(status, 0) + quantity
    pending_items_sorted = sorted(
        pending_items.values(),
        key=lambda item: str(item.get("item_title", "")).casefold(),
    )
    summary = {
        "paid_pending_value": round(paid_pending_value, 2),
        "unpaid_pending_value": round(unpaid_pending_value, 2),
        "total_items_cost": round(total_items_cost, 2),
    }
    return all_orders, pending_items_sorted, summary


def build_employee_approval_items():
    approvals = []
    statuses = load_order_statuses()
    approval_statuses = {"Delivered in Lahore", "Cancelled by Employee"}
    for shopify_order in order_details:
        customer = shopify_order.get("customer_details") or {}
        for item in shopify_order.get("line_items", []):
            tracking_number = item.get("tracking_number", "N/A")
            key = f"{shopify_order.get('order_num')}:{tracking_number}"
            applied_status = statuses.get(key, "")
            if applied_status not in approval_statuses:
                continue
            approvals.append(
                {
                    "shopify_id": shopify_order.get("id") or shopify_order.get("shopify_id"),
                    "order_id": shopify_order.get("order_id") or shopify_order.get("order_num"),
                    "tracking_number": tracking_number,
                    "requested_status": applied_status,
                    "item_title": item.get("product_title", ""),
                    "item_image": item.get("image_src", ""),
                    "quantity": item.get("quantity", 0),
                    "customer_name": customer.get("name") or "",
                    "customer_city": customer.get("city") or "",
                    "customer_phone": customer.get("phone") or "",
                    "total_price": shopify_order.get("total_price", 0),
                    "date": shopify_order.get("created_at", ""),
                    "tags": shopify_order.get("tags", []),
                }
            )
    return sorted(approvals, key=lambda item: parse_date_timestamp(item.get("date")), reverse=True)


def get_active_shopify_products(limit=250):
    overrides = load_product_cost_overrides()
    try:
        products = shopify.Product.find(limit=limit, published_status="published")
    except Exception as error:
        print(f"Could not fetch Shopify products: {error}")
        return []

    results = []
    while True:
        for product in products:
            if getattr(product, "status", "active") != "active":
                continue
            base_image = product.image.src if getattr(product, "image", None) else ""
            for variant in getattr(product, "variants", []) or []:
                variant_title = getattr(variant, "title", "") or ""
                display_title = product.title if variant_title in {"Default Title", ""} else f"{product.title} - {variant_title}"
                results.append(
                    {
                        "product_id": getattr(product, "id", None),
                        "variant_id": getattr(variant, "id", None),
                        "inventory_item_id": getattr(variant, "inventory_item_id", None),
                        "title": display_title,
                        "product_title": getattr(product, "title", ""),
                        "variant_title": variant_title,
                        "price": parse_money(getattr(variant, "price", 0)),
                        "cost": get_cost_override_for_item(overrides, product_id=getattr(product, "id", None), variant_id=getattr(variant, "id", None), title=display_title),
                        "image": base_image,
                        "sku": getattr(variant, "sku", "") or "",
                    }
                )
        try:
            if not products.has_next_page():
                break
            products = products.next_page()
        except Exception as error:
            print(f"Could not load next Shopify product page: {error}")
            break
    return results


def build_product_cost_rows(limit=250):
    return sorted(get_active_shopify_products(limit=limit), key=lambda row: str(row.get("title", "")).lower())


def build_admin_mobile_sections():
    return [
        {"id": "dashboard", "label": "Dashboard", "icon": "home", "src": "/?embedded=1"},
        {"id": "scanner", "label": "Scanner", "icon": "scan", "src": "/employee_portal"},
        {"id": "employee-orders", "label": "Orders", "icon": "orders", "src": "/employee_portal/orders"},
        {"id": "pending", "label": "Pending", "icon": "pending", "src": "/pending?embedded=1"},
        {"id": "abandoned", "label": "Abandoned", "icon": "abandoned", "src": "/abandoned?embedded=1"},
        {"id": "payments", "label": "Payments", "icon": "payments", "src": "/payments?embedded=1"},
        {"id": "finance", "label": "Finance", "icon": "finance", "src": "/finance?embedded=1"},
        {"id": "product-costs", "label": "Product Costs", "icon": "cost", "src": "/product-costs?embedded=1"},
    ]


def split_customer_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", "Customer"
    if len(parts) == 1:
        return parts[0], "Customer"
    return parts[0], " ".join(parts[1:])


def build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, payment_status, catalog_items, custom_items, discount_amount, delivery_charges):
    items = []
    subtotal = 0.0
    for item in catalog_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append({"title": item.get("title") or "Product", "quantity": quantity, "image": item.get("image") or "", "unit_price": unit_price, "line_total": line_total})
    for item in custom_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append({"title": item.get("title") or "Custom product", "quantity": quantity, "image": item.get("image") or "", "unit_price": unit_price, "line_total": line_total})
    total = round(subtotal - discount_amount + delivery_charges, 2)
    amount_paid = total if payment_status == "Paid" else 0.0
    balance_due = round(max(total - amount_paid, 0), 2)
    return {
        "order_id": order_name,
        "customer_name": customer_name,
        "customer_phone": phone,
        "customer_city": city,
        "customer_address": address,
        "status": "Created",
        "items": items,
        "totals": {
            "subtotal": round(subtotal, 2),
            "discount": round(discount_amount, 2),
            "delivery_charges": round(delivery_charges, 2),
            "total": round(total, 2),
            "advance_paid": round(amount_paid, 2),
            "balance_due": round(balance_due, 2),
        },
    }


def create_shopify_employee_order(payload):
    customer_name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    city = (payload.get("city") or "").strip()
    address = (payload.get("address") or "").strip()
    payment_method = (payload.get("payment_method") or "").strip()
    payment_status = (payload.get("payment_status") or "Unpaid").strip().title()
    discount_amount = parse_money(payload.get("discount_amount"))
    delivery_charges = parse_money(payload.get("delivery_charges"))
    catalog_items = payload.get("catalog_items") or []
    custom_items = payload.get("custom_items") or []
    extra_notes = (payload.get("notes") or "").strip()
    if not customer_name:
        raise ValueError("Customer name is required.")
    if not phone:
        raise ValueError("Phone number is required.")
    if payment_method not in {"Cash on Delivery", "Bank Deposit"}:
        raise ValueError("Choose Cash on Delivery or Bank Deposit.")
    if payment_status not in {"Paid", "Unpaid"}:
        raise ValueError("Choose Paid or Unpaid.")

    line_items = []
    normalized_custom_items = []
    for item in catalog_items:
        variant_id = item.get("variant_id")
        quantity = int(item.get("quantity") or 1)
        if not variant_id or quantity < 1:
            continue
        line_item = {"variant_id": int(variant_id), "quantity": quantity}
        override_price = parse_money(item.get("price"))
        if override_price > 0:
            line_item["original_unit_price"] = override_price
        line_items.append(line_item)
    for item in custom_items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        custom_item = {"title": title, "price": parse_money(item.get("price")), "quantity": int(item.get("quantity") or 1), "image": (item.get("image") or "").strip()}
        normalized_custom_items.append(custom_item)
        line_items.append({"title": title, "original_unit_price": custom_item["price"], "quantity": custom_item["quantity"]})
    if not line_items:
        raise ValueError("At least one product is required.")

    first_name, last_name = split_customer_name(customer_name)
    note_lines = [
        "Created from Alkaramat employee portal.",
        f"Customer: {customer_name}",
        f"City: {city or 'Not provided'}",
        f"Address: {address or 'Not provided'}",
        f"Payment method: {payment_method or 'Not specified'}",
        f"Payment status: {payment_status}",
        f"Phone: {phone or 'Not provided'}",
    ]
    if extra_notes:
        note_lines.append(f"Notes: {extra_notes}")
    draft_order = shopify.DraftOrder()
    draft_order.line_items = line_items
    draft_order.note = "\n".join(note_lines)
    draft_order.tags = f"Employee Portal, {payment_method}, {payment_status}"
    draft_order.shipping_address = {"first_name": first_name, "last_name": last_name or "Customer", "phone": phone, "address1": address, "city": city, "country": "Pakistan"}
    draft_order.billing_address = draft_order.shipping_address
    draft_order.customer = {"first_name": first_name, "last_name": last_name or "Customer", "phone": phone}
    if discount_amount > 0:
        draft_order.applied_discount = {"description": "Employee portal discount", "value_type": "fixed_amount", "value": discount_amount, "amount": discount_amount, "title": "Employee portal discount"}
    if delivery_charges > 0:
        draft_order.shipping_line = {"title": "Delivery Charges", "price": delivery_charges, "custom": True}
    if not draft_order.save():
        raise RuntimeError(json.dumps(getattr(draft_order, "errors", {}) or {"error": "Could not save draft order"}))
    # Shopify treats payment_pending=False as paid. Only an explicit Paid
    # selection may take that path; COD and bank-deposit orders default Unpaid.
    draft_order.complete({"payment_pending": payment_status != "Paid"})
    refreshed = shopify.DraftOrder.find(draft_order.id)
    order_id = getattr(refreshed, "order_id", None) or getattr(draft_order, "order_id", None)
    order_name = getattr(refreshed, "name", "") or getattr(draft_order, "name", "") or ""
    if not order_id:
        raise RuntimeError("Shopify created the draft, but the completed order ID did not come back.")
    return {
        "draft_order_id": getattr(draft_order, "id", None),
        "order_id": order_id,
        "order_name": order_name,
        "invoice": build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, payment_status, catalog_items, normalized_custom_items, discount_amount, delivery_charges),
    }


@app.route("/orders")
def mobile_orders():
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=False)


@app.route("/employee_portal", methods=["GET", "POST"])
def employee_portal():
    next_url = employee_portal_safe_next_url(request.values.get("next"))
    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == EMPLOYEE_PORTAL_PASSWORD:
            session[EMPLOYEE_PORTAL_SESSION_KEY] = True
            session.permanent = True
            return redirect(next_url)
        return render_template("employee_portal.html", view="login", login_error="Wrong password. Try again.", next_url=next_url, passkey_available=bool(load_employee_passkeys())), 401
    if not employee_portal_is_authenticated():
        return render_template("employee_portal.html", view="login", login_error="", next_url=next_url, passkey_available=bool(load_employee_passkeys()))
    return render_template("employee_portal.html", view="portal", employee_orders=build_employee_portal_orders(), passkey_available=bool(load_employee_passkeys()))


@app.route("/employee_portal/passkeys/register/options", methods=["POST"])
def employee_passkey_registration_options():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Password login required."}), 401
    options = generate_registration_options(
        rp_id=ADMIN_PORTAL_RP_ID, rp_name="Alkaramat Employee",
        user_id=b"alkaramat-employee", user_name="employee@alkaramat", user_display_name="Alkaramat Employee",
        exclude_credentials=_admin_passkey_descriptors(load_employee_passkeys()),
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED),
    )
    session[EMPLOYEE_PASSKEY_CHALLENGE_KEY] = base64.urlsafe_b64encode(options.challenge).decode().rstrip("=")
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/employee_portal/passkeys/register/verify", methods=["POST"])
def employee_passkey_registration_verify():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Password login required."}), 401
    data = request.get_json(silent=True) or {}
    challenge = session.pop(EMPLOYEE_PASSKEY_CHALLENGE_KEY, "")
    try:
        verified = verify_registration_response(credential=data.get("credential"), expected_challenge=base64url_to_bytes(challenge), expected_rp_id=ADMIN_PORTAL_RP_ID, expected_origin=ADMIN_PORTAL_ORIGIN, require_user_verification=True)
        if not save_employee_passkey(verified.credential_id, verified.credential_public_key, verified.sign_count, str(data.get("device_name") or "Mobile device")[:80]):
            raise RuntimeError("Could not save passkey")
        return jsonify({"success": True, "message": "Fingerprint login is ready."})
    except Exception as error:
        print(f"Employee passkey registration failed: {error}")
        return jsonify({"success": False, "error": "Passkey setup could not be verified."}), 400


@app.route("/employee_portal/passkeys/login/options", methods=["POST"])
def employee_passkey_login_options():
    passkeys = load_employee_passkeys()
    if not passkeys:
        return jsonify({"success": False, "error": "Log in with the password once to enable fingerprint login."}), 404
    options = generate_authentication_options(rp_id=ADMIN_PORTAL_RP_ID, allow_credentials=_admin_passkey_descriptors(passkeys), user_verification=UserVerificationRequirement.REQUIRED)
    session[EMPLOYEE_PASSKEY_CHALLENGE_KEY] = base64.urlsafe_b64encode(options.challenge).decode().rstrip("=")
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/employee_portal/passkeys/login/verify", methods=["POST"])
def employee_passkey_login_verify():
    data = request.get_json(silent=True) or {}; credential = data.get("credential") or {}
    challenge = session.pop(EMPLOYEE_PASSKEY_CHALLENGE_KEY, "")
    try:
        credential_id = base64url_to_bytes(credential.get("id") or "")
        passkey = next((row for row in load_employee_passkeys() if bytes(row["credential_id"]) == credential_id), None)
        if not passkey: raise ValueError("Unknown passkey")
        verified = verify_authentication_response(credential=credential, expected_challenge=base64url_to_bytes(challenge), expected_rp_id=ADMIN_PORTAL_RP_ID, expected_origin=ADMIN_PORTAL_ORIGIN, credential_public_key=bytes(passkey["public_key"]), credential_current_sign_count=int(passkey["sign_count"] or 0), require_user_verification=True)
        update_employee_passkey_usage(credential_id, verified.new_sign_count)
        session[EMPLOYEE_PORTAL_SESSION_KEY] = True; session.permanent = True
        return jsonify({"success": True, "redirect": url_for("employee_portal")})
    except Exception as error:
        print(f"Employee passkey login failed: {error}")
        return jsonify({"success": False, "error": "Fingerprint login was not verified."}), 401


@app.route("/employee_portal/orders")
def employee_portal_orders():
    if not employee_portal_is_authenticated():
        return redirect(url_for("employee_portal", next="/employee_portal/orders"))
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=True)


@app.route("/employee_portal/products")
def employee_portal_products():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return jsonify({"success": True, "products": get_active_shopify_products()})


@app.route("/employee_portal/create-order", methods=["POST"])
def employee_portal_create_order():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    try:
        result = create_shopify_employee_order(data)
        try:
            order_details[:] = asyncio.run(getShopifyOrders())
        except Exception as refresh_error:
            print(f"Employee order created, but refresh failed: {refresh_error}")
        return jsonify(
            {
                "success": True,
                "draft_order_id": result.get("draft_order_id"),
                "order_id": result.get("order_id"),
                "order_name": result.get("order_name"),
                "invoice": result.get("invoice"),
            }
        )
    except Exception as error:
        print(f"Employee order create failed: {error}")
        return jsonify({"success": False, "error": str(error)}), 400


@app.route("/employee_portal/logout", methods=["POST"])
def employee_portal_logout():
    session.pop(EMPLOYEE_PORTAL_SESSION_KEY, None)
    return redirect(url_for("employee_portal"))


@app.route("/employee_portal/updates")
def employee_portal_updates():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    orders = build_employee_portal_orders()
    summaries = [
        {
            "id": f"{order.get('source')}:{order.get('order_id')}",
            "order_id": order.get("order_id"),
            "source": order.get("source"),
            "created_at": order.get("created_at"),
        }
        for order in orders
    ]
    summaries.sort(key=lambda item: parse_date_timestamp(item.get("created_at")), reverse=True)
    return jsonify(
        {
            "success": True,
            "count": len(summaries),
            "order_ids": [item["id"] for item in summaries],
            "latest": summaries[:6],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


@app.route("/employee_portal/report", methods=["POST"])
def employee_portal_report():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    mode = (data.get("mode") or "").strip().lower()
    scanned_orders = data.get("orders") or []
    if mode not in {"dispatch", "return"}:
        return jsonify({"success": False, "error": "Invalid report mode."}), 400
    if not scanned_orders:
        return jsonify({"success": False, "error": "No scanned orders provided."}), 400
    tag_name = "Dispatched" if mode == "dispatch" else "Return Received"
    tagged_count = 0
    skipped_count = 0
    failed = []
    seen_order_ids = set()
    for entry in scanned_orders:
        shopify_id = str(entry.get("shopify_id") or "").strip()
        if not shopify_id or shopify_id in seen_order_ids:
            skipped_count += 1
            continue
        seen_order_ids.add(shopify_id)
        try:
            if apply_shopify_order_tag(shopify_id, tag_name, include_date=True):
                tagged_count += 1
            else:
                skipped_count += 1
        except Exception as error:
            failed.append({"order_id": entry.get("order_id") or shopify_id, "error": str(error)})
    return jsonify(
        {
            "success": not failed,
            "tagged_count": tagged_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
            "failed": failed[:5],
            "tagged_by_source": {"Alkaramat": tagged_count} if tagged_count else {},
            "tag_name": tag_name,
        }
    ), 207 if failed else 200


@app.route("/dispatch", methods=["GET"])
def dispatch_orders():
    return jsonify(build_employee_portal_orders())


@app.route("/return", methods=["GET"])
def return_orders():
    return jsonify(build_employee_portal_orders())


@app.route("/scan", methods=["GET", "POST"])
def employee_scan_lookup():
    search_term = (request.args.get("term") or request.form.get("search_term") or "").split(",")[0].strip()
    if not search_term:
        return render_template("scan.html")
    order_found = find_employee_portal_order(search_term)
    if request.method == "POST":
        if order_found:
            order_found = {
                "line_items": [
                    {"product_title": item.get("title"), "quantity": item.get("quantity"), "image_src": item.get("image")}
                    for item in order_found.get("items", [])
                ]
            }
        return render_template("scan.html", search_term=search_term, order_found=order_found)
    return jsonify(order_found if order_found else {"error": "Order not found"}), 200 if order_found else 404


@app.route("/update_status", methods=["POST"])
def update_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "")
    tracking_number = str(data.get("tracking_number") or "N/A")
    status = str(data.get("status") or "")
    key = f"{order_id}:{tracking_number}"
    upsert_order_status(key, status)
    response_message = f"Status updated to {status} for {order_id} ({tracking_number})"

    if status == "Delivered in Lahore":
        matching_order = next((order for order in order_details if normalize_scan_term(order.get("order_num")) == normalize_scan_term(order_id)), None)
        if matching_order and matching_order.get("order_id"):
            try:
                if apply_shopify_order_tag(matching_order["order_id"], "Delivered in Lahore"):
                    response_message = f"{response_message}. Shopify tag applied: Delivered in Lahore."
            except Exception as error:
                print(f"Could not apply Lahore tag: {error}")
    return jsonify({"message": response_message})


@app.route("/employee_status/approve", methods=["POST"])
def approve_employee_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "")
    tracking_number = str(data.get("tracking_number") or "N/A")
    requested_status = str(data.get("requested_status") or "").strip()
    key = f"{order_id}:{tracking_number}"
    if requested_status not in {"Delivered in Lahore", "Cancelled by Employee"}:
        return jsonify({"success": False, "error": "Unsupported employee approval status."}), 400
    matching_order = next((order for order in order_details if normalize_scan_term(order.get("order_num")) == normalize_scan_term(order_id)), None)
    if not matching_order or not matching_order.get("order_id"):
        return jsonify({"success": False, "error": "Shopify order not found."}), 404
    try:
        tag_name = "Delivered in Lahore" if requested_status == "Delivered in Lahore" else "Cancelled by Employee"
        apply_shopify_order_tag(matching_order["order_id"], tag_name, include_date=True)
        delete_order_status(key)
        return jsonify({"success": True, "message": f"Approved {requested_status} for {order_id}.", "warnings": []})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/product-costs")
def product_costs():
    return render_template("product_costs.html", products=build_product_cost_rows())


def _finance_csrf_token():
    token = session.get("finance_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["finance_csrf_token"] = token
    return token


def _require_finance_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not supplied or not secrets.compare_digest(supplied, session.get("finance_csrf_token", "")):
        abort(400, "This finance form expired. Reload the page and try again.")


@app.route("/finance")
def finance_page():
    if not admin_portal_is_authenticated():
        return redirect(url_for("admin_portal", section="finance"))
    try:
        period = (request.args.get("period") or datetime.now().strftime("%Y-%m")).strip()
        period_start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
        dashboard = finance_dashboard(period_start)
        error = ""
    except Exception as finance_error:
        print(f"Finance dashboard error: {finance_error}")
        dashboard = {"accounts": [], "journals": [], "by_key": {}, "income": 0, "expenses": 0, "profit": 0}
        error = str(finance_error)
    return render_template(
        "finance.html",
        finance=dashboard,
        finance_error=error,
        csrf_token=_finance_csrf_token(),
        today=datetime.now().date().isoformat(),
        period=(request.args.get("period") or datetime.now().strftime("%Y-%m")),
    )


@app.route("/finance/transactions", methods=["POST"])
def finance_create_transaction():
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    _require_finance_csrf()
    try:
        kind = (request.form.get("kind") or "").strip()
        lines = build_entry(
            kind,
            request.form.get("amount"),
            cash_account=(request.form.get("cash_account") or "bank").strip(),
            category=(request.form.get("category") or "").strip() or None,
            destination=(request.form.get("destination") or "").strip() or None,
            deduction=request.form.get("deduction") or 0,
        )
        public_id = post_journal(
            request.form.get("transaction_date") or datetime.now().date(),
            request.form.get("description"),
            request.form.get("reference"),
            lines,
        )
        flash(f"Transaction posted: {public_id[:8]}", "success")
    except Exception as error:
        flash(str(error), "error")
    return redirect(url_for("finance_page"))


@app.route("/finance/transactions/<public_id>/reverse", methods=["POST"])
def finance_reverse_transaction(public_id):
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    _require_finance_csrf()
    try:
        reverse_journal(public_id)
        flash("Transaction reversed. The original entry remains in the audit trail.", "success")
    except Exception as error:
        flash(str(error), "error")
    return redirect(url_for("finance_page"))


@app.route("/product-costs/update", methods=["POST"])
def update_product_costs():
    data = request.get_json() or {}
    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    title = (data.get("title") or "").strip()
    submitted_price = parse_money(data.get("price", 0))
    submitted_cost = parse_money(data.get("cost", 0))
    if not variant_id and not product_id and not title:
        return jsonify({"success": False, "error": "Product identity is required."}), 400
    try:
        if variant_id:
            variant = shopify.Variant.find(int(variant_id))
            variant.price = submitted_price
            if not variant.save():
                raise RuntimeError("Shopify price update failed.")
        overrides = load_product_cost_overrides()
        set_cost_override(overrides, product_id=product_id, variant_id=variant_id, title=title, price=submitted_price, cost=submitted_cost)
        if not save_product_cost_overrides(overrides):
            raise RuntimeError("Could not save cost override.")
        return jsonify({"success": True, "price": submitted_price, "cost": submitted_cost})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/admin_portal", methods=["GET", "POST"])
def admin_portal():
    selected = (request.values.get("section") or "dashboard").strip().lower()
    sections = build_admin_mobile_sections()
    if selected not in {section["id"] for section in sections}:
        selected = "dashboard"
    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == ADMIN_PORTAL_PASSWORD:
            session[ADMIN_PORTAL_SESSION_KEY] = True
            session.permanent = True
            return redirect(url_for("admin_portal", section=selected))
        return render_template("admin_portal.html", view="login", login_error="Wrong password. Try again.", sections=sections, selected_section=selected, passkey_available=bool(load_admin_passkeys())), 401
    if not admin_portal_is_authenticated():
        return render_template("admin_portal.html", view="login", login_error="", sections=sections, selected_section=selected, passkey_available=bool(load_admin_passkeys()))
    return render_template("admin_portal.html", view="portal", sections=sections, selected_section=selected, employee_approvals=build_employee_approval_items(), passkey_available=bool(load_admin_passkeys()))


@app.route("/admin_portal/passkeys/register/options", methods=["POST"])
def admin_passkey_registration_options():
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Password login required."}), 401
    passkeys = load_admin_passkeys()
    options = generate_registration_options(
        rp_id=ADMIN_PORTAL_RP_ID,
        rp_name="Alkaramat Admin",
        user_id=b"alkaramat-admin",
        user_name="admin@alkaramat",
        user_display_name="Alkaramat Admin",
        exclude_credentials=_admin_passkey_descriptors(passkeys),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    session[ADMIN_PASSKEY_CHALLENGE_KEY] = base64.urlsafe_b64encode(options.challenge).decode().rstrip("=")
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/admin_portal/passkeys/register/verify", methods=["POST"])
def admin_passkey_registration_verify():
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Password login required."}), 401
    data = request.get_json(silent=True) or {}
    challenge = session.pop(ADMIN_PASSKEY_CHALLENGE_KEY, "")
    if not challenge or not data.get("credential"):
        return jsonify({"success": False, "error": "Passkey setup expired. Please try again."}), 400
    try:
        verified = verify_registration_response(
            credential=data["credential"],
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=ADMIN_PORTAL_RP_ID,
            expected_origin=ADMIN_PORTAL_ORIGIN,
            require_user_verification=True,
        )
        device_name = str(data.get("device_name") or "Mobile device").strip()[:80]
        if not save_admin_passkey(
            verified.credential_id,
            verified.credential_public_key,
            verified.sign_count,
            device_name,
        ):
            raise RuntimeError("Could not save this passkey.")
        return jsonify({"success": True, "message": "Face ID / fingerprint login is ready."})
    except Exception as error:
        print(f"Admin passkey registration failed: {error}")
        return jsonify({"success": False, "error": "Passkey setup could not be verified."}), 400


@app.route("/admin_portal/passkeys/login/options", methods=["POST"])
def admin_passkey_login_options():
    passkeys = load_admin_passkeys()
    if not passkeys:
        return jsonify({"success": False, "error": "Log in with the password once to enable Face ID or fingerprint."}), 404
    options = generate_authentication_options(
        rp_id=ADMIN_PORTAL_RP_ID,
        allow_credentials=_admin_passkey_descriptors(passkeys),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session[ADMIN_PASSKEY_CHALLENGE_KEY] = base64.urlsafe_b64encode(options.challenge).decode().rstrip("=")
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.route("/admin_portal/passkeys/login/verify", methods=["POST"])
def admin_passkey_login_verify():
    data = request.get_json(silent=True) or {}
    credential = data.get("credential") or {}
    challenge = session.pop(ADMIN_PASSKEY_CHALLENGE_KEY, "")
    if not challenge or not credential.get("id"):
        return jsonify({"success": False, "error": "Login expired. Please try again."}), 400
    try:
        credential_id = base64url_to_bytes(credential["id"])
        passkey = next(
            (row for row in load_admin_passkeys() if bytes(row["credential_id"]) == credential_id),
            None,
        )
        if not passkey:
            raise ValueError("Unknown passkey")
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=ADMIN_PORTAL_RP_ID,
            expected_origin=ADMIN_PORTAL_ORIGIN,
            credential_public_key=bytes(passkey["public_key"]),
            credential_current_sign_count=int(passkey["sign_count"] or 0),
            require_user_verification=True,
        )
        update_admin_passkey_usage(credential_id, verified.new_sign_count)
        session[ADMIN_PORTAL_SESSION_KEY] = True
        session.permanent = True
        return jsonify({"success": True, "redirect": url_for("admin_portal")})
    except Exception as error:
        print(f"Admin passkey login failed: {error}")
        return jsonify({"success": False, "error": "Face ID / fingerprint login was not verified."}), 401


@app.route("/admin_portal/logout", methods=["POST"])
def admin_portal_logout():
    session.pop(ADMIN_PORTAL_SESSION_KEY, None)
    return redirect(url_for("admin_portal"))


@app.route("/employee_portal-manifest.webmanifest")
def employee_portal_manifest():
    return send_from_directory("static", "employee-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/employee_portal-sw.js")
def employee_portal_service_worker():
    return send_from_directory("static", "employee-portal-sw.js", mimetype="application/javascript")


@app.route("/admin_portal-manifest.webmanifest")
def admin_portal_manifest():
    return send_from_directory("static", "admin-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/admin_portal-sw.js")
def admin_portal_service_worker():
    return send_from_directory("static", "admin-portal-sw.js", mimetype="application/javascript")


@app.route('/scanner')
def scanner_page():
    return render_template('scanner.html')


@app.route('/api/scan/order', methods=['POST'])
def scan_single_order():
    scanned_value = (request.get_json(silent=True) or {}).get('scan_input')
    if not scanned_value: return jsonify({'error': 'No input'}), 400
    found_order = find_employee_portal_order(scanned_value)

    if found_order:
        items_list = [
            {
                'title': item.get('title', 'Unknown item'),
                'quantity': item.get('quantity', 0),
                'image_src': item.get('image', ''),
            }
            for item in found_order.get('items', [])
        ]
        return jsonify({
            'success': True,
            'order': found_order,
            'order_num': found_order.get('order_id'),
            'source': found_order.get('source'),
            'items': items_list,
        }), 200
    else:
        return jsonify({'success': False, 'error': 'Not found'}), 404


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)
init_db()

order_details = []
print("Starting initial order fetch in background...")
with tracking_refresh_lock:
    tracking_refresh_state.update(running=True, error="")
initial_refresh_worker = threading.Thread(target=refresh_tracking_in_background, daemon=True)
initial_refresh_worker.start()
automatic_refresh_worker = threading.Thread(target=automatic_tracking_refresh_loop, daemon=True)
automatic_refresh_worker.start()

if __name__ == "__main__":
    app.run(port=5001)


