import asyncio
import html
import re
import ssl
import time

import certifi
from aiohttp import ClientTimeout


PUBLIC_TRACKING_URL = "https://sonic.pk/tracking"
PUBLIC_TRACKING_ENDPOINT = "https://sonic.pk/tracking/track"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_history_cache = {}
_inflight = {}
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_TERMINAL_MARKERS = (
    "returned to shipper", "delivered to shipper", "return delivered", "cancelled",
)


def is_trax_tracking_number(value):
    """Trax/Sonic public CNs are fourteen numeric characters."""
    return len(re.sub(r"\D", "", str(value or ""))) == 14


def _is_terminal_status(value):
    normalized = str(value or "").strip().casefold()
    if "undelivered" in normalized or "delivery unsuccessful" in normalized or "delivery failed" in normalized:
        return False
    return normalized == "delivered" or normalized.endswith(" - delivered") or any(
        marker in normalized for marker in _TERMINAL_MARKERS
    )


def _csrf_token(page):
    """Extract only the temporary token; tracking data always comes from JSON."""
    match = re.search(r"['_\"]token['\"]\s*:\s*['\"]([^'\"]+)", page or "")
    if not match:
        match = re.search(r'name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', page or "", re.I)
    return html.unescape(match.group(1)) if match else ""


def _portal_history(payload, tracking_number):
    shipments = payload.get("shipments") if isinstance(payload, dict) else None
    if not isinstance(shipments, dict):
        return []
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    shipment = next((
        row for row in shipments.values()
        if isinstance(row, dict)
        and re.sub(r"\D", "", str(row.get("tracking_number") or "")) == digits
    ), None)
    if not shipment:
        return []
    consignee = shipment.get("consignee") or {}
    history = []
    # Sonic returns newest first; Al Karamat stores courier histories oldest first.
    for event in reversed(shipment.get("tracking_history") or []):
        if not isinstance(event, dict) or not str(event.get("status") or "").strip():
            continue
        history.append({
            "ConsignmentNo": digits,
            "ProcessDescForPortal": str(event.get("status") or "").strip(),
            "TransactionDate": str(event.get("date_time") or "").strip(),
            "ReasonDesc": str(event.get("status_reason") or event.get("reason") or "").strip(),
            "ConsigneeName": str(consignee.get("name") or "").strip(),
            "ConsigneeCity": str(consignee.get("destination") or "").strip(),
            "TrackingSource": "Trax Direct",
        })
    return history


async def _fetch_public_tracking_history(session, digits):
    timeout = ClientTimeout(total=12)
    async with session.get(PUBLIC_TRACKING_URL, timeout=timeout, ssl=_SSL_CONTEXT) as response:
        if response.status != 200:
            return []
        page = await response.text()
    token = _csrf_token(page)
    if not token:
        return []
    async with session.post(
        PUBLIC_TRACKING_ENDPOINT,
        data={"tracking_numbers": digits, "_token": token},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        if response.status != 200:
            return []
        payload = await response.json(content_type=None)
    return _portal_history(payload, digits)


async def fetch_public_tracking_history(session, tracking_number):
    """Read Trax safely, retaining the last valid timeline when Sonic fails."""
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    if not is_trax_tracking_number(digits):
        return []
    cached = _history_cache.get(digits)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    task = _inflight.get(digits)
    if task is None:
        task = asyncio.create_task(_fetch_public_tracking_history(session, digits))
        _inflight[digits] = task
    try:
        history = await task
        if history:
            latest = str(history[-1].get("ProcessDescForPortal") or "").casefold()
            ttl = _TERMINAL_CACHE_SECONDS if _is_terminal_status(latest) else _ACTIVE_CACHE_SECONDS
            _history_cache[digits] = (history, time.monotonic() + ttl)
            return history
        return cached[0] if cached else []
    except Exception as error:
        print(f"Trax public tracking unavailable for {digits}: {error}")
        return cached[0] if cached else []
    finally:
        if _inflight.get(digits) is task:
            _inflight.pop(digits, None)
