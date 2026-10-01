import asyncio
import html
import json
import os
import re
import ssl
import threading
import time
from difflib import SequenceMatcher
from html.parser import HTMLParser

import certifi
from aiohttp import ClientTimeout
import requests


_token = ""
_token_created_at = 0.0
_status_cache = {}
_inflight = {}
_payments_cache = None
_payments_cache_expires_at = 0.0
_shipper_advice_cache = None
_shipper_advice_cache_expires_at = 0.0
_booking_metadata_cache = None
_booking_metadata_expires_at = 0.0
_booking_lock = threading.Lock()
_TOKEN_TTL_SECONDS = 6 * 60 * 60
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_OPERATIONS_REFRESH_SECONDS = 6 * 60 * 60
_TERMINAL_STATUSES = {"delivered", "returned", "return delivered", "cancelled"}
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class _DigiDokaanPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf = ""
        self.inputs = {}
        self.options = {}
        self._select_id = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.csrf = html.unescape(values.get("content") or "")
        elif tag == "input" and values.get("id"):
            self.inputs[values["id"]] = html.unescape(values.get("value") or "")
        elif tag == "select":
            self._select_id = values.get("id") or values.get("name") or ""
        elif tag == "option" and self._select_id:
            self.options.setdefault(self._select_id, []).append(html.unescape(values.get("value") or ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select_id = ""


def _web_login_session(config):
    session = requests.Session()
    login_page = session.get(config["web_url"] + "/", timeout=30)
    login_page.raise_for_status()
    parser = _DigiDokaanPageParser()
    parser.feed(login_page.text)
    if not parser.csrf:
        raise RuntimeError("DigiDokaan login token was not found")
    digits = re.sub(r"\D", "", config["phone"])
    number = digits[2:] if digits.startswith("92") else digits.lstrip("0")
    headers = {"Accept": "application/json", "X-CSRF-TOKEN": parser.csrf, "X-Requested-With": "XMLHttpRequest"}
    send = session.post(
        config["web_url"] + "/user/send-otp",
        data={"country_code": "+92", "number": number}, headers=headers, timeout=30,
    )
    send.raise_for_status()
    login = session.post(
        config["web_url"] + "/user/login-new-password",
        data={"password": config["password"], "number": "+92" + number}, headers=headers, timeout=30,
    )
    result = login.json()
    if login.status_code != 200 or str(result.get("code")) != "200":
        raise RuntimeError("DigiDokaan web login failed")
    session.get(config["web_url"] + "/", timeout=30).raise_for_status()
    return session


def _parse_booking_metadata(page_html):
    parser = _DigiDokaanPageParser()
    parser.feed(page_html)
    try:
        cities = json.loads(parser.inputs.get("courier_cities_array") or "[]")
    except (TypeError, ValueError):
        cities = []
    pickups = []
    for raw in parser.options.get("normal_pickup_location", []):
        try:
            pickup = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if "5" in [str(value) for value in pickup.get("gateways") or []]:
            pickups.append(pickup)
    trax_cities = []
    for city in cities:
        if "trax" not in [str(value).casefold() for value in city.get("courier") or []]:
            continue
        raw_services = city.get("trax_shipment_type")
        try:
            services = json.loads(raw_services) if isinstance(raw_services, str) else list(raw_services or [])
        except (TypeError, ValueError):
            services = []
        trax_cities.append({
            "id": str(city.get("id") or ""),
            "name": str(city.get("city_name") or "").strip(),
            "services": [str(value).upper() for value in services],
        })
    business = {}
    try:
        business = json.loads(parser.inputs.get("business_detail") or "{}")
    except (TypeError, ValueError):
        pass
    return {"csrf": parser.csrf, "cities": trax_cities, "pickups": pickups, "business": business}


def fetch_booking_metadata(force=False):
    global _booking_metadata_cache, _booking_metadata_expires_at
    if not force and _booking_metadata_cache and _booking_metadata_expires_at > time.monotonic():
        return _booking_metadata_cache
    config = configuration()
    if not config:
        raise RuntimeError("DigiDokaan booking credentials are not configured")
    session = _web_login_session(config)
    page = session.get(config["web_url"] + "/manage/book-packet-show", timeout=45)
    page.raise_for_status()
    metadata = _parse_booking_metadata(page.text)
    if not metadata["cities"] or not metadata["pickups"]:
        raise RuntimeError("DigiDokaan Trax cities or pickup address are unavailable")
    _booking_metadata_cache = metadata
    _booking_metadata_expires_at = time.monotonic() + _OPERATIONS_REFRESH_SECONDS
    return metadata


def match_trax_city(city_name, cities):
    wanted = re.sub(r"[^a-z0-9]", "", str(city_name or "").casefold())
    if not wanted:
        return None
    normalized = [(re.sub(r"[^a-z0-9]", "", city["name"].casefold()), city) for city in cities]
    exact = next((city for key, city in normalized if key == wanted), None)
    if exact:
        return exact
    contained = [city for key, city in normalized if wanted in key or key in wanted]
    if len(contained) == 1:
        return contained[0]
    scored = sorted(((SequenceMatcher(None, wanted, key).ratio(), city) for key, city in normalized), reverse=True, key=lambda row: row[0])
    return scored[0][1] if scored and scored[0][0] >= 0.82 else None


def normalize_booking_phone(phone):
    """Return the 03XXXXXXXXX format required by DigiDokaan's booking form."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 12 and digits.startswith("92"):
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "0" + digits
    if not re.fullmatch(r"03\d{9}", digits):
        raise ValueError("Customer phone must be a valid Pakistani mobile number")
    return digits


def create_trax_booking(payload):
    """Create one Trax shipment through DigiDokaan's authenticated merchant workflow."""
    config = configuration()
    if not config:
        raise RuntimeError("DigiDokaan booking credentials are not configured")
    with _booking_lock:
        session = _web_login_session(config)
        page = session.get(config["web_url"] + "/manage/book-packet-show", timeout=45)
        page.raise_for_status()
        metadata = _parse_booking_metadata(page.text)
        pickup = next((row for row in metadata["pickups"] if str(row.get("pickup_address_id")) == str(payload.get("pickup_address_id"))), None)
        pickup = pickup or (metadata["pickups"][0] if metadata["pickups"] else None)
        if not pickup:
            raise RuntimeError("No Trax pickup address is approved in DigiDokaan")
        api_session = requests.Session()
        token_response = api_session.post(
            config["base_url"] + "/api/auth/login",
            json={"phone": config["phone"], "password": config["password"]},
            headers={"Accept": "application/json"}, timeout=30,
        )
        token_response.raise_for_status()
        token = token_response.json().get("token")
        shipper_response = api_session.post(
            config["base_url"] + "/api/courier/get_courier_shipper",
            json={"phone": config["phone"], "gateway_id": "5", "pickup_address_id": pickup["pickup_address_id"], "business_name": pickup.get("name") or "Al Karamat"},
            headers={"Accept": "application/json", "Authorization": "Bearer " + str(token or "")}, timeout=30,
        )
        shipper_body = shipper_response.json()
        shippers = shipper_body.get("data") if isinstance(shipper_body, dict) else None
        if shipper_response.status_code != 200 or str(shipper_body.get("code")) != "200" or not shippers:
            raise RuntimeError(shipper_body.get("error") or "Trax pickup is not approved")
        shipper = shippers[0]
        service_codes = {"OVERNIGHT": "1", "DETAIN": "2", "OVERLAND": "3"}
        service = str(payload.get("service_type") or "OVERNIGHT").upper()
        customer_phone = normalize_booking_phone(payload.get("customer_phone"))
        shipper_phone = normalize_booking_phone(shipper.get("phone") or config["phone"])
        form = {
            "origin": pickup.get("city") or "Lahore",
            "destination_city": str(payload.get("destination_city_id") or ""),
            "consignee_phone": customer_phone,
            "consignee_phone_two": "",
            "consignee_name": str(payload.get("customer_name") or ""),
            "piece": str(payload.get("pieces") or 1),
            "quantity": str(payload.get("quantity") or payload.get("pieces") or 1),
            "consignee_address": str(payload.get("customer_address") or ""),
            "net_weight": str(payload.get("weight") or "0.5"),
            "cod_amount": str(payload.get("cod_amount") or 0),
            "parcel_value": str(payload.get("parcel_value") or payload.get("cod_amount") or 0),
            "other_product": "true",
            "product_name": str(payload.get("product_name") or "Clothing"),
            "reference_number": str(payload.get("reference_number") or "")[:20],
            "special_instruction": str(payload.get("special_instruction") or ""),
            "normal_pickup_location": json.dumps(pickup, separators=(",", ":")),
            "pickup_location": str(shipper.get("shipper_id") or ""),
            "shipper_phone": shipper_phone,
            "shipper_name": str(shipper.get("shipment_name") or pickup.get("name") or "Al Karamat"),
            "gateway_id": "5",
            "shipment_type": service_codes.get(service, "1"),
        }
        response = session.post(
            config["web_url"] + "/manage/save-book-packet", data=form,
            headers={"Accept": "application/json", "X-CSRF-TOKEN": metadata["csrf"], "X-Requested-With": "XMLHttpRequest"}, timeout=60,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or str(body.get("code")) != "200":
            message = body.get("error") or body.get("msg") or body.get("message")
            if isinstance(message, dict):
                message = "; ".join(
                    f"{field}: {', '.join(map(str, errors if isinstance(errors, list) else [errors]))}"
                    for field, errors in message.items()
                )
            raise RuntimeError(str(message or "DigiDokaan rejected the Trax booking"))
        result = body.get("data") if isinstance(body.get("data"), dict) else body
        order_no = result.get("order_no") or result.get("order_id") or result.get("id")
        tracking_no = result.get("tracking_no") or result.get("tracking_number") or result.get("tracking")
        return {"order_no": str(order_no or ""), "tracking_no": str(tracking_no or ""), "raw": body}


def fetch_trax_label(order_no, tracking_no):
    config = configuration()
    if not config:
        raise RuntimeError("DigiDokaan booking credentials are not configured")
    session = _web_login_session(config)
    page = session.get(config["web_url"] + f"/orders/order-detail/{order_no}", timeout=30)
    page.raise_for_status()
    parser = _DigiDokaanPageParser()
    parser.feed(page.text)
    response = session.post(
        config["web_url"] + "/orders/generate-load-sheet",
        data={"orders[]": order_no, "tracking_numbers[]": tracking_no, "gateway_id": "5", "order_type_download": "label", "order_type": "2", "phone": config["phone"]},
        headers={"Accept": "application/json", "X-CSRF-TOKEN": parser.csrf, "X-Requested-With": "XMLHttpRequest"}, timeout=60,
    )
    body = response.json()
    if response.status_code != 200 or str(body.get("code")) != "200":
        raise RuntimeError(body.get("error") or "DigiDokaan could not generate the label")
    result = body.get("data") if isinstance(body.get("data"), dict) else body
    return result.get("link") or result.get("pdf_link") or ""


def configuration():
    values = {
        "base_url": (os.getenv("DIGIDOKAAN_API_URL") or "https://digidokaan.pk").rstrip("/"),
        "phone": (os.getenv("DIGIDOKAAN_PHONE") or "").strip(),
        "password": os.getenv("DIGIDOKAAN_PASSWORD") or "",
        "gateway_id": str(os.getenv("DIGIDOKAAN_GATEWAY_ID") or "5").strip(),
        "web_url": (os.getenv("DIGIDOKAAN_WEB_URL") or "https://web.digidokaan.pk").rstrip("/"),
    }
    if not values["phone"] or not values["password"]:
        return None
    return values


async def _access_token(session, config):
    global _token, _token_created_at
    if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
        return _token

    loop = asyncio.get_running_loop()
    lock = getattr(loop, "digidokaan_auth_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        loop.digidokaan_auth_lock = lock

    async with lock:
        if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
            return _token
        timeout = ClientTimeout(total=20)
        async with session.post(
            config["base_url"] + "/api/auth/login",
            json={"phone": config["phone"], "password": config["password"]},
            headers={"Accept": "application/json"},
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            payload = await response.json(content_type=None)
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if response.status != 200 or not token:
            raise RuntimeError("DigiDokaan authentication failed")
        _token = token
        _token_created_at = time.monotonic()
        return token


def _cached_status(tracking_number):
    cached = _status_cache.get(tracking_number)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    if cached:
        _status_cache.pop(tracking_number, None)
    return None


def display_status_from_detail(body, fallback=None):
    data = body.get("data") if isinstance(body, dict) else None
    tracking = data.get("tracking_response") if isinstance(data, dict) else None
    events = tracking.get("data") if isinstance(tracking, dict) else None
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_status = str(event.get("status") or "").strip()
            reason = str(event.get("status_reason") or "").strip()
            failure_event = any(
                marker in event_status.casefold()
                for marker in (
                    "delivery unsuccessful",
                    "reason validation",
                    "shipper advise",
                    "undelivered",
                )
            )
            if reason and failure_event:
                return f"Undelivered - {reason}"
    current = str(tracking.get("courier_status") or "").strip() if isinstance(tracking, dict) else ""
    return current or fallback


async def _fetch_status(session, tracking_number, config):
    global _token, _token_created_at
    token = await _access_token(session, config)
    timeout = ClientTimeout(total=20)
    payload = {
        "phone": config["phone"],
        "search_value": tracking_number,
        "gateway_id": config["gateway_id"],
    }
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    async with session.post(
        config["base_url"] + "/api/orders/search-courier-order",
        json=payload,
        headers=headers,
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        body = await response.json(content_type=None)

    if response.status == 401 or (isinstance(body, dict) and body.get("code") == 401):
        _token = ""
        _token_created_at = 0.0
        token = await _access_token(session, config)
        headers["Authorization"] = "Bearer " + token
        async with session.post(
            config["base_url"] + "/api/orders/search-courier-order",
            json=payload,
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)

    rows = body.get("data") if isinstance(body, dict) else None
    if response.status != 200 or not isinstance(rows, list) or not rows:
        return None
    exact = next(
        (row for row in rows if str(row.get("tracking_no") or "").strip() == tracking_number),
        rows[0],
    )
    fallback = str(exact.get("courier_status") or exact.get("status") or "").strip()
    order_id = str(exact.get("order_id") or "").strip()
    if not order_id:
        return fallback or None
    async with session.post(
        config["base_url"] + "/api/seller/order/get_single_order_detail",
        json={"phone": config["phone"], "order_no": order_id},
        headers=headers,
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        detail_body = await response.json(content_type=None)
    if response.status != 200:
        return fallback or None
    return display_status_from_detail(detail_body, fallback) or None


async def fetch_tracking_status(session, tracking_number):
    """Return a DigiDokaan courier status, deduplicating concurrent refresh requests."""
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    if not normalized:
        return None
    cached = _cached_status(normalized)
    if cached:
        return cached
    config = configuration()
    if not config:
        return None

    task = _inflight.get(normalized)
    if task is None:
        task = asyncio.create_task(_fetch_status(session, normalized, config))
        _inflight[normalized] = task
    try:
        status = await task
        if status:
            ttl = _TERMINAL_CACHE_SECONDS if status.casefold() in _TERMINAL_STATUSES else _ACTIVE_CACHE_SECONDS
            _status_cache[normalized] = (status, time.monotonic() + ttl)
        return status
    except Exception as error:
        print(f"DigiDokaan tracking unavailable for {normalized}: {error}")
        return None
    finally:
        if _inflight.get(normalized) is task:
            _inflight.pop(normalized, None)


async def fetch_tracking_history(session, tracking_number):
    """Return DigiDokaan events in the portal's existing tracking-history shape."""
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    config = configuration()
    if not normalized or not config:
        return []
    try:
        token = await _access_token(session, config)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
        timeout = ClientTimeout(total=20)
        async with session.post(
            config["base_url"] + "/api/orders/search-courier-order",
            json={
                "phone": config["phone"],
                "search_value": normalized,
                "gateway_id": config["gateway_id"],
            },
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            search_body = await response.json(content_type=None)
        rows = search_body.get("data") if isinstance(search_body, dict) else None
        if response.status != 200 or not isinstance(rows, list) or not rows:
            return []
        order = next(
            (row for row in rows if str(row.get("tracking_no") or "").strip() == normalized),
            rows[0],
        )
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return []
        async with session.post(
            config["base_url"] + "/api/seller/order/get_single_order_detail",
            json={"phone": config["phone"], "order_no": order_id},
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            detail_body = await response.json(content_type=None)
        data = detail_body.get("data") if isinstance(detail_body, dict) else None
        detail = data.get("order_detail") if isinstance(data, dict) else None
        tracking = data.get("tracking_response") if isinstance(data, dict) else None
        events = tracking.get("data") if isinstance(tracking, dict) else None
        if response.status != 200 or not isinstance(detail, dict) or not isinstance(events, list):
            return []
        history = []
        for event in reversed(events):
            if not isinstance(event, dict) or not str(event.get("status") or "").strip():
                continue
            history.append(
                {
                    "ConsignmentNo": normalized,
                    "TransactionDate": event.get("date_time") or "",
                    "ProcessDescForPortal": event.get("status") or "",
                    "ReasonDesc": event.get("status_reason") or "",
                    "ConsigneeName": detail.get("customer_name") or "",
                    "ConsigneeCity": detail.get("customer_city") or "",
                }
            )
        return history
    except Exception as error:
        print(f"DigiDokaan tracking history unavailable for {normalized}: {error}")
        return []


async def fetch_payments(session):
    """Return the merchant's read-only DigiDokaan settlement summary and ledger."""
    global _payments_cache, _payments_cache_expires_at
    if _payments_cache is not None and _payments_cache_expires_at > time.monotonic():
        return _payments_cache
    config = configuration()
    if not config:
        raise RuntimeError("DigiDokaan payment credentials are not configured")
    token = await _access_token(session, config)
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    payload = {"phone": config["phone"]}
    timeout = ClientTimeout(total=30)

    async def post(path):
        async with session.post(
            config["base_url"] + "/api/" + path,
            json=payload,
            headers=headers,
            timeout=timeout,
            ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)
        if response.status != 200 or not isinstance(body, dict) or body.get("code") != 200:
            raise RuntimeError("DigiDokaan payments are temporarily unavailable")
        return body

    balance, ready, ledger = await asyncio.gather(
        post("settlements/ledger_ready_for_payment_balance"),
        post("settlements/ledger_ready_for_payments"),
        post("settlements/ledger_single_cheque_detail"),
    )
    try:
        cheques = await _fetch_cheque_history(session, config, headers)
    except Exception as error:
        print(f"DigiDokaan cheque history unavailable: {error}")
        cheques = []
    result = {"balance": balance, "ready": ready, "ledger": ledger, "cheques": cheques}
    _payments_cache = result
    _payments_cache_expires_at = time.monotonic() + _OPERATIONS_REFRESH_SECONDS
    return result


def _money_from_text(value):
    matches = re.findall(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    cleaned = matches[-1].replace(",", "") if matches else ""
    try:
        return round(float(cleaned or 0), 2)
    except ValueError:
        return 0.0


def _plain_html(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


async def _fetch_cheque_history(session, config, api_headers):
    """Fetch the merchant cheque list and each cheque's shipment detail.

    DigiDokaan's settlement API exposes cheque detail by cheque number but does
    not expose the cheque-number list. The merchant ledger page is therefore
    read once per six-hour payment refresh to discover those identifiers.
    """
    timeout = ClientTimeout(total=30)
    async with session.get(config["web_url"] + "/", timeout=timeout, ssl=_SSL_CONTEXT) as response:
        login_html = await response.text()
    csrf_match = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', login_html, re.I)
    if not csrf_match:
        csrf_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', login_html, re.I)
    if not csrf_match:
        raise RuntimeError("DigiDokaan web login token was not found")
    csrf = html.unescape(csrf_match.group(1))
    digits = re.sub(r"\D", "", config["phone"])
    number = digits[2:] if digits.startswith("92") else digits.lstrip("0")
    web_headers = {"Accept": "application/json", "X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"}
    async with session.post(
        config["web_url"] + "/user/send-otp",
        data={"country_code": "+92", "number": number}, headers=web_headers,
        timeout=timeout, ssl=_SSL_CONTEXT,
    ) as response:
        login_step = await response.json(content_type=None)
    if login_step.get("code") != 201:
        raise RuntimeError("DigiDokaan web account requires an interactive login")
    async with session.post(
        config["web_url"] + "/user/login-new-password",
        data={"password": config["password"], "number": "+92" + number}, headers=web_headers,
        timeout=timeout, ssl=_SSL_CONTEXT,
    ) as response:
        login_result = await response.json(content_type=None)
    if login_result.get("code") != 200:
        raise RuntimeError("DigiDokaan web payment login failed")
    # The web app finalizes the authenticated merchant session when it follows
    # the successful login redirect through the root dashboard route.
    async with session.get(config["web_url"] + "/", timeout=timeout, ssl=_SSL_CONTEXT) as response:
        await response.read()
    async with session.get(
        config["web_url"] + "/manage/merchant-payment-ledger",
        timeout=timeout, ssl=_SSL_CONTEXT,
    ) as response:
        ledger_html = await response.text()

    cheques = []
    row_pattern = re.compile(r"<tr[^>]*class=[\"'][^\"']*\bcheque\b[^\"']*[\"'][^>]*>(.*?)</tr>", re.I | re.S)
    for row_html in row_pattern.findall(ledger_html):
        id_match = re.search(r"merchant-ledger/([0-9a-f-]{20,})", row_html, re.I)
        if not id_match:
            continue
        cells = [_plain_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.I | re.S)]
        cheque_no = id_match.group(1)
        cheque = {
            "cheque_no": cheque_no,
            "cheque_date": cells[0] if cells else "",
            "bank": cells[2] if len(cells) > 2 else "",
            "amount": _money_from_text(cells[3] if len(cells) > 3 else ""),
            "balance": _money_from_text(cells[4] if len(cells) > 4 else ""),
            "status": "Paid",
        }
        async with session.post(
            config["base_url"] + "/api/settlements/ledger_single_cheque_detail",
            json={"phone": config["phone"], "cheque_no": cheque_no},
            headers=api_headers, timeout=timeout, ssl=_SSL_CONTEXT,
        ) as response:
            detail = await response.json(content_type=None)
        cheque["shipments"] = detail if response.status == 200 and detail.get("code") == 200 else {"data": []}
        cheques.append(cheque)
    return cheques


async def fetch_pending_shipper_advice(session):
    """Return the merchant's pending shipper-advice queue."""
    global _shipper_advice_cache, _shipper_advice_cache_expires_at
    if _shipper_advice_cache is not None and _shipper_advice_cache_expires_at > time.monotonic():
        return _shipper_advice_cache
    config = configuration()
    if not config:
        return []
    token = await _access_token(session, config)
    timeout = ClientTimeout(total=20)
    async with session.post(
        config["base_url"] + "/api/courier/get_shipper_advice_order",
        json={"phone": config["phone"]},
        headers={"Accept": "application/json", "Authorization": "Bearer " + token},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        body = await response.json(content_type=None)
    rows = body.get("data") if isinstance(body, dict) else None
    if response.status != 200 or not isinstance(rows, list):
        raise RuntimeError("DigiDokaan shipper advice is temporarily unavailable")
    _shipper_advice_cache = rows
    _shipper_advice_cache_expires_at = time.monotonic() + _OPERATIONS_REFRESH_SECONDS
    return rows


async def submit_shipper_advice(session, tracking_number, gateway_id, advice_status, remarks):
    """Submit a reattempt or return instruction for one pending shipment."""
    global _shipper_advice_cache, _shipper_advice_cache_expires_at
    normalized_tracking = "".join(ch for ch in str(tracking_number or "") if ch.isdigit())
    normalized_status = str(advice_status or "").strip().casefold()
    status_values = {"reattempt": "reattempt", "return": "return"}
    if not normalized_tracking or normalized_status not in status_values:
        raise ValueError("Choose Reattempt or Return")
    clean_remarks = str(remarks or "").strip()
    if not clean_remarks:
        raise ValueError("Remarks are required")
    config = configuration()
    if not config:
        raise RuntimeError("DigiDokaan is not configured")

    token = await _access_token(session, config)
    timeout = ClientTimeout(total=25)
    async with session.post(
        config["base_url"] + "/api/courier/shipper_advice_action",
        json={
            "phone": config["phone"],
            "gateway_id": str(gateway_id or config["gateway_id"]),
            "tracking_no": normalized_tracking,
            "shipper_advice_status": status_values[normalized_status],
            "shipper_advice_remarks": clean_remarks,
        },
        headers={"Accept": "application/json", "Authorization": "Bearer " + token},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        body = await response.json(content_type=None)
    if response.status != 200 or not isinstance(body, dict) or body.get("code") != 200:
        message = (body.get("error") or body.get("message")) if isinstance(body, dict) else ""
        raise RuntimeError(message or "DigiDokaan did not accept the shipper advice")
    _shipper_advice_cache = None
    _shipper_advice_cache_expires_at = 0.0
    return body
