import asyncio
import base64
import hashlib
import hmac
import os
import smtplib
import threading
import time
import json
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort, session, send_from_directory
from markupsafe import Markup
from datetime import datetime, timedelta
from urllib.parse import urlencode
import pymssql, shopify
import aiohttp
import lazop
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout, ClientSession, ClientError, BasicAuth
import pytz
from flask import send_file, make_response
import io
import functools
from db import (
    delete_order_status,
    get_app_setting,
    init_db,
    load_order_statuses,
    set_app_setting,
    upsert_order_status,
)

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')
pre_loaded = 0
order_details = []
EMPLOYEE_PORTAL_SESSION_KEY = "employee_portal_authenticated"
ADMIN_PORTAL_SESSION_KEY = "admin_portal_authenticated"
EMPLOYEE_PORTAL_PASSWORD = os.getenv("EMPLOYEE_PORTAL_PASSWORD", "@@@t")
ADMIN_PORTAL_PASSWORD = os.getenv("ADMIN_PORTAL_PASSWORD", "security")
PRODUCT_COSTS_SETTING_KEY = "product_cost_overrides_v1"
PAID_FINANCIAL_STATUSES = {"paid", "partially_paid", "partially refunded", "partially_refunded"}


def normalize_scan_term(term):
    return (term or "").strip().lower().replace("#", "")


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def format_number(value):
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value or 0)


app.jinja_env.filters["format_number"] = format_number


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


async def fetch_shopify_abandoned_checkouts(days=7):
    created_at_min = get_abandoned_created_at_min(days)
    seen = {}
    async with aiohttp.ClientSession() as session:
        for status in ("open", "closed"):
            rows = await fetch_shopify_paginated_rest(
                session,
                "checkouts.json",
                {"limit": 250, "created_at_min": created_at_min, "status": status},
                "checkouts",
            )
            for row in rows:
                seen[str(row.get("id") or row.get("token") or row.get("cart_token"))] = row
    return list(seen.values())


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
    checkouts = await fetch_shopify_abandoned_checkouts(days)
    recovery_orders = await fetch_recent_shopify_orders_for_recovery(max(days, 30))
    recovery_indexes = build_order_recovery_indexes(recovery_orders)
    fallback_counts = build_abandoned_checkout_customer_counts(recovery_orders)
    today = datetime.now().date()
    rows = []

    for checkout in checkouts:
        customer = checkout.get("customer") or {}
        shipping = checkout.get("shipping_address") or {}
        billing = checkout.get("billing_address") or {}
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
            quantity = int(line_item.get("quantity") or 0)
            unit_price = parse_money(line_item.get("price", 0))
            title = line_item.get("title") or line_item.get("name") or "Product"
            variant_title = line_item.get("variant_title") or ""
            items.append(
                {
                    "title": f"{title} - {variant_title}" if variant_title and variant_title != "Default Title" else title,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_total": round(unit_price * quantity, 2),
                    "image": line_item.get("image_url") or line_item.get("image") or "",
                }
            )

        created_at = checkout.get("created_at", "")
        rows.append(
            {
                "id": checkout.get("id"),
                "token": checkout.get("token") or checkout.get("cart_token") or checkout.get("id"),
                "created_at": created_at,
                "customer_name": customer_name,
                "customer_email": checkout.get("email") or customer.get("email") or "",
                "customer_phone": normalize_customer_phone(checkout.get("phone") or shipping.get("phone") or billing.get("phone") or customer.get("phone") or ""),
                "customer_city": shipping.get("city") or billing.get("city") or "",
                "customer_address": shipping.get("address1") or billing.get("address1") or "",
                "customer_orders_count": get_checkout_customer_total_orders(checkout, fallback_counts),
                "total_price": parse_money(checkout.get("total_price", 0)),
                "subtotal_price": parse_money(checkout.get("subtotal_price", checkout.get("total_price", 0))),
                "abandoned_checkout_url": checkout.get("abandoned_checkout_url") or "",
                "recovered": is_recovered,
                "recovered_order_name": (recovered_order or {}).get("name", ""),
                "recovered_order_link": shopify_order_admin_link((recovered_order or {}).get("id")),
                "completed_at": completed_at or (recovered_order or {}).get("created_at", ""),
                "items": items,
                "is_today": parse_date_for_sort(created_at).date() == today,
            }
        )

    rows = sorted(rows, key=lambda row: parse_date_timestamp(row.get("created_at")), reverse=True)
    summary = {
        "last_7_days": len(rows),
        "today": sum(1 for row in rows if row.get("is_today")),
        "recovered": sum(1 for row in rows if row.get("recovered")),
        "open": sum(1 for row in rows if not row.get("recovered")),
        "value": round(sum(parse_money(row.get("total_price", 0)) for row in rows), 2),
    }
    return rows, summary


async def build_abandoned_checkouts_summary(days=7):
    checkouts = await fetch_shopify_abandoned_checkouts(days)
    today = datetime.now().date()
    return {
        "last_7_days": len(checkouts),
        "today": sum(1 for checkout in checkouts if parse_date_for_sort(checkout.get("created_at")).date() == today),
        "recovered": sum(1 for checkout in checkouts if checkout.get("completed_at")),
        "open": sum(1 for checkout in checkouts if not checkout.get("completed_at")),
        "value": round(sum(parse_money(checkout.get("total_price", 0)) for checkout in checkouts), 2),
    }


def get_abandoned_summary_safe():
    try:
        return asyncio.run(build_abandoned_checkouts_summary())
    except Exception as error:
        print(f"Could not fetch abandoned checkouts: {error}")
        return {"last_7_days": 0, "today": 0, "recovered": 0, "open": 0, "value": 0.0, "error": str(error)}


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


@app.route('/book')
def book_orders():
    global order_details

    # 1. Calculate Customer History Stats
    customer_stats = {}

    for order in order_details:
        phone = order['customer_details'].get('phone', '').strip()
        if not phone:
            continue

        if phone not in customer_stats:
            customer_stats[phone] = {'total': 0, 'delivered': 0}

        customer_stats[phone]['total'] += 1

        status = str(order.get('status', '')).lower()
        if 'delivered' in status or 'completed' in status:
            customer_stats[phone]['delivered'] += 1

    # 2. Filter and Prepare List for Display
    orders_with_stats = []
    for order in order_details:
        # Filter: Only show 'Un-Booked' orders
        if order.get('status') != 'Un-Booked':
            continue

        o_copy = order.copy()

        # Prepare items_list
        items_formatted = []
        if 'line_items' in order:
            for item in order['line_items']:
                items_formatted.append({
                    'item_image': item.get('image_src', ''),
                    'item_title': item.get('product_title', ''),
                    'quantity': item.get('quantity', 0),
                    'tracking_number': item.get('tracking_number', 'N/A'),
                    'status': item.get('status', 'Un-Booked')
                })
        o_copy['items_list'] = items_formatted

        # History Stats Logic
        phone = order['customer_details'].get('phone', '').strip()
        stats = customer_stats.get(phone, {'total': 0, 'delivered': 0})

        # --- MODIFIED LOGIC: Check for First Order ---
        if stats['total'] == 1 and stats['delivered'] == 0:
            o_copy['history_str'] = "First Order"
        else:
            o_copy['history_str'] = f"{stats['delivered']} Delivered / {stats['total']} Orders"
        # ---------------------------------------------

        if stats['total'] > 0:
            o_copy['success_rate'] = (stats['delivered'] / stats['total']) * 100
        else:
            o_copy['success_rate'] = 0

        orders_with_stats.append(o_copy)

    return render_template('book.html', all_orders=orders_with_stats)

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


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
async def fetch_tracking_data(session, tracking_number):
    url = f"https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory?cn={tracking_number}"
    timeout = ClientTimeout(total=100)
    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, list) and data:
                    return data
                elif isinstance(data, dict) and data.get('d'):
                    return data['d']
                return []
            else:
                return {"error": f"HTTP {response.status}"}
    except Exception as e:
        return {"error": str(e)}


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
                    data = await fetch_tracking_data(session, tracking_number)
                    if data and isinstance(data, list) and data[-1].get('ProcessDescForPortal'):
                        # Assumes the last item in the list holds the current status
                        tracking_details = data[-1]['ProcessDescForPortal']
                    else:
                        tracking_details = "DELIVERED"  # Default or fallback
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': tracking_details,
                        'quantity': item.quantity
                    })
    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": line_item.quantity}
    ]

ORDER_PROCESS_SEM = asyncio.Semaphore(5)

async def safe_process_order(session, order):
    async with ORDER_PROCESS_SEM:
        return await process_order(session, order)


async def process_order(session, order):
    try:
        order_start_time = time.time()
        created_at_str = order.created_at
        created_at_obj = datetime.fromisoformat(created_at_str)
        formatted_date = created_at_obj.strftime('%Y-%m-%d')

        order_info = {
            'order_link': "https://admin.shopify.com/store/alkaramat/orders/" + str(order.id),
            'id': order.id,
            'shopify_id': order.id,
            'order_num': order.name.replace("#", ""),
            'order_id': order.name.replace("#", ""),
            'created_at': formatted_date,
            'total_price': order.current_subtotal_price,
            'line_items': [],
            'financial_status': order.financial_status.title(),
            'fulfillment_status': order.fulfillment_status or "Unfulfilled",
            'customer_details': {
                "id": getattr(order.customer, "id", "") if hasattr(order, 'customer') else "", # <--- NEW LINE
                "name": getattr(order.shipping_address, "name", " "),
                "address": getattr(order.shipping_address, "address1", " "),
                "city": getattr(order.shipping_address, "city", " "),
                "phone": getattr(order.shipping_address, "phone", " ")
            },
            'tags': order.tags.split(", ") if order.tags else []
        }

        tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        results = await asyncio.gather(*tasks)

        for tracking_info_list, line_item in zip(results, order.line_items):
            if tracking_info_list is None: continue

            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = line_item.variant_title or ""

            if line_item.product_id is not None:
                try:
                    product_endpoint = f"products/{line_item.product_id}.json"
                    product_data = await async_shopify_fetch(session, product_endpoint)
                    product = product_data.get('product') if product_data else None

                    if product and product.get('variants'):
                        for variant in product['variants']:
                            if variant['id'] == line_item.variant_id:
                                image_id = variant.get('image_id')
                                variant_name = line_item.variant_title or ""
                                if image_id is not None:
                                    image_endpoint = f"products/{line_item.product_id}/images/{image_id}.json"
                                    image_data = await async_shopify_fetch(session, image_endpoint)
                                    if image_data and image_data.get('image'):
                                        image_src = image_data['image']['src']
                                else:
                                    image_src = product.get('image', {}).get('src', image_src)
                                break
                except Exception as e:
                    print(f"Error fetching product details: {e}")

            for info in tracking_info_list:
                order_info['line_items'].append({
                    'fulfillment_status': line_item.fulfillment_status,
                    'image_src': image_src,
                    'product_id': line_item.product_id,
                    'variant_id': line_item.variant_id,
                    'unit_price': parse_money(getattr(line_item, 'price', 0)),
                    'product_title': line_item.title + (f" - {variant_name}" if variant_name else ""),
                    'quantity': info['quantity'],
                    'tracking_number': info['tracking_number'],
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

    return order_details


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


@app.route("/")
def tracking_home():
    global order_details
    total_order_value = sum(parse_money(order.get("total_price", 0)) for order in order_details)
    return render_template(
        "track.html",
        order_details=order_details,
        darazOrders=[],
        employee_approvals=build_employee_approval_items(),
        total_order_value=total_order_value,
        abandoned_summary=get_abandoned_summary_safe(),
    )


@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details
    try:
        order_details = asyncio.run(getShopifyOrders())
        return render_template(
            "track.html",
            order_details=order_details,
            darazOrders=[],
            employee_approvals=build_employee_approval_items(),
            abandoned_summary=get_abandoned_summary_safe(),
        )
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({'message': 'Failed to refresh data'}), 500


@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    async def async_func():
        async with aiohttp.ClientSession() as session:
            return await fetch_tracking_data(session, tracking_num)

    data = asyncio.run(async_func())
    return render_template('trackingdata_alk.html', data=data)


@app.route('/abandoned')
def abandoned_orders():
    try:
        abandoned_checkouts, summary = asyncio.run(build_abandoned_checkouts_data())
        error = None
    except Exception as fetch_error:
        print(f"Could not build abandoned checkouts page: {fetch_error}")
        abandoned_checkouts = []
        summary = {"last_7_days": 0, "today": 0, "recovered": 0, "open": 0, "value": 0.0}
        error = str(fetch_error)
    return render_template(
        "abandoned.html",
        abandoned_checkouts=abandoned_checkouts,
        summary=summary,
        error=error,
    )


@app.route('/undelivered')
def undelivered():
    global order_details
    return render_template("undelivered.html", order_details=order_details)


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


def build_employee_portal_orders():
    try:
        employee_orders = [serialize_shopify_order_for_employee(order) for order in order_details]
        return sorted(employee_orders, key=lambda order: parse_date_for_sort(order.get("created_at")), reverse=True)
    except Exception as error:
        print(f"Could not load employee portal orders: {error}")
        return []


def find_employee_portal_order(term):
    normalized = normalize_scan_term(term)
    if not normalized:
        return None
    for order in build_employee_portal_orders():
        if normalize_scan_term(order.get("order_id")) == normalized:
            return order
        for item in order.get("items", []):
            if normalize_scan_term(item.get("tracking_number")) == normalized:
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
    all_orders = []
    statuses = load_order_statuses()
    overrides = load_product_cost_overrides()
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
                "subtotal_price": parse_money(shopify_order.get("total_price", 0)),
                "current_subtotal_price": parse_money(shopify_order.get("total_price", 0)),
                "shipping_charges": 0,
                "total_discounts": 0,
                "total_price": parse_money(shopify_order.get("total_price", 0)),
                "current_total_price": parse_money(shopify_order.get("total_price", 0)),
                "display_total_price": parse_money(shopify_order.get("total_price", 0)),
                "pending_total_price": round(sum(parse_money(item.get("line_total", 0)) for item in items), 2),
                "pending_total_cost": round(sum(parse_money(item.get("line_cost_total", 0)) for item in items), 2),
            }
        )
    return sorted(all_orders, key=lambda order: parse_date_for_sort(order.get("date")), reverse=True)


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
    return sorted(approvals, key=lambda item: parse_date_for_sort(item.get("date")), reverse=True)


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
        {"id": "dashboard", "label": "Dashboard", "icon": "Home", "src": "/?embedded=1"},
        {"id": "scanner", "label": "Scanner", "icon": "Scan", "src": "/employee_portal"},
        {"id": "employee-orders", "label": "Orders", "icon": "List", "src": "/employee_portal/orders"},
        {"id": "pending", "label": "Pending", "icon": "Board", "src": "/pending?embedded=1"},
        {"id": "abandoned", "label": "Abandoned", "icon": "Cart", "src": "/abandoned?embedded=1"},
        {"id": "undelivered", "label": "Undelivered", "icon": "Truck", "src": "/undelivered?embedded=1"},
        {"id": "product-costs", "label": "Product Costs", "icon": "Cost", "src": "/product-costs?embedded=1"},
    ]


def split_customer_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", "Customer"
    if len(parts) == 1:
        return parts[0], "Customer"
    return parts[0], " ".join(parts[1:])


def build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, delivery_method, catalog_items, custom_items, discount_amount, delivery_charges, advance_amount):
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
    balance_due = round(max(total - advance_amount, 0), 2)
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
            "advance_paid": round(advance_amount, 2),
            "balance_due": round(balance_due, 2),
        },
    }


def create_shopify_employee_order(payload):
    customer_name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    city = (payload.get("city") or "").strip()
    address = (payload.get("address") or "").strip()
    payment_method = (payload.get("payment_method") or "").strip()
    delivery_method = (payload.get("delivery_method") or "").strip()
    discount_amount = parse_money(payload.get("discount_amount"))
    delivery_charges = parse_money(payload.get("delivery_charges"))
    advance_amount = parse_money(payload.get("advance_amount"))
    catalog_items = payload.get("catalog_items") or []
    custom_items = payload.get("custom_items") or []
    extra_notes = (payload.get("notes") or "").strip()
    if not customer_name:
        raise ValueError("Customer name is required.")
    if not phone:
        raise ValueError("Phone number is required.")
    if payment_method.lower() == "partial" and advance_amount <= 0:
        raise ValueError("Enter the advance paid amount for partial payment.")

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
        f"Payment method: {payment_method or 'Not specified'}",
        f"Delivery method: {delivery_method or 'Not specified'}",
        f"Phone: {phone or 'Not provided'}",
    ]
    if extra_notes:
        note_lines.append(f"Notes: {extra_notes}")
    draft_order = shopify.DraftOrder()
    draft_order.line_items = line_items
    draft_order.note = "\n".join(note_lines)
    draft_order.tags = "Employee Portal"
    draft_order.shipping_address = {"first_name": first_name, "last_name": last_name or "Customer", "phone": phone, "address1": address, "city": city, "country": "Pakistan"}
    draft_order.billing_address = draft_order.shipping_address
    draft_order.customer = {"first_name": first_name, "last_name": last_name or "Customer", "phone": phone}
    if discount_amount > 0:
        draft_order.applied_discount = {"description": "Employee portal discount", "value_type": "fixed_amount", "value": discount_amount, "amount": discount_amount, "title": "Employee portal discount"}
    if delivery_charges > 0:
        draft_order.shipping_line = {"title": "Delivery Charges", "price": delivery_charges, "custom": True}
    if not draft_order.save():
        raise RuntimeError(json.dumps(getattr(draft_order, "errors", {}) or {"error": "Could not save draft order"}))
    draft_order.complete({"payment_pending": True})
    refreshed = shopify.DraftOrder.find(draft_order.id)
    order_id = getattr(refreshed, "order_id", None) or getattr(draft_order, "order_id", None)
    order_name = getattr(refreshed, "name", "") or getattr(draft_order, "name", "") or ""
    if not order_id:
        raise RuntimeError("Shopify created the draft, but the completed order ID did not come back.")
    return {
        "draft_order_id": getattr(draft_order, "id", None),
        "order_id": order_id,
        "order_name": order_name,
        "invoice": build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, delivery_method, catalog_items, normalized_custom_items, discount_amount, delivery_charges, advance_amount),
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
            return redirect(next_url)
        return render_template("employee_portal.html", view="login", login_error="Wrong password. Try again.", next_url=next_url), 401
    if not employee_portal_is_authenticated():
        return render_template("employee_portal.html", view="login", login_error="", next_url=next_url)
    return render_template("employee_portal.html", view="portal", employee_orders=build_employee_portal_orders())


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
    summaries.sort(key=lambda item: parse_date_for_sort(item.get("created_at")), reverse=True)
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
            return redirect(url_for("admin_portal", section=selected))
        return render_template("admin_portal.html", view="login", login_error="Wrong password. Try again.", sections=sections, selected_section=selected), 401
    if not admin_portal_is_authenticated():
        return render_template("admin_portal.html", view="login", login_error="", sections=sections, selected_section=selected)
    return render_template("admin_portal.html", view="portal", sections=sections, selected_section=selected, employee_approvals=build_employee_approval_items())


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
    scanned_value = request.json.get('scan_input')
    if not scanned_value: return jsonify({'error': 'No input'}), 400
    scan_term = str(scanned_value).strip().replace("#", "")

    found_order = next((o for o in order_details if o.get('order_num') == scan_term), None)
    if not found_order:
        for order in order_details:
            if order.get('line_items'):
                for item in order['line_items']:
                    if item.get('tracking_number') == scan_term:
                        found_order = order;
                        break
                if found_order: break

    if found_order:
        items_list = [{'title': item['product_title'], 'quantity': item['quantity'], 'image_src': item['image_src']} for
                      item in found_order['line_items']]
        return jsonify({'success': True, 'order_num': found_order['order_num'], 'items': items_list}), 200
    else:
        return jsonify({'success': False, 'error': 'Not found'}), 404


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)
init_db()

try:
    print("Starting initial fetch...")
    order_details = asyncio.run(getShopifyOrders())
    print(f"Loaded {len(order_details)} orders.")
except Exception as e:
    print(f"Init load failed: {e}")
    order_details = []

if __name__ == "__main__":
    app.run(port=5001)


