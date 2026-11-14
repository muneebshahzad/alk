import asyncio
import html
import re
import ssl
import time
from datetime import datetime

import certifi
from aiohttp import ClientTimeout


_BASE_URL = "https://pk.leopardscourier.com"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_history_cache = {}
_inflight = {}
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60


def is_leopards_tracking_number(value):
    return bool(re.fullmatch(r"LE\d{8,14}", str(value or "").strip(), re.I))


def _plain_text(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _normalize_date(value):
    raw = _plain_text(value).replace("(", "").replace(")", "")
    for fmt in ("%d %B, %Y %H:%M", "%d %B %Y, %H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return raw


def parse_tracking_history(page, tracking_number):
    """Parse Leopards' server-rendered timeline, which is returned newest-first."""
    rows = []
    pattern = re.compile(
        r'<div\s+class="tracking-date"[^>]*>(.*?)</div>\s*'
        r'<div\s+class="tracking-content"[^>]*>(.*?)</div>',
        re.I | re.S,
    )
    for date_html, status_html in reversed(pattern.findall(page or "")):
        status = _plain_text(status_html)
        if status:
            rows.append({
                "ConsignmentNo": str(tracking_number or "").strip().upper(),
                "TransactionDate": _normalize_date(date_html),
                "ProcessDescForPortal": status,
                "ReasonDesc": "",
                "TrackingSource": "Leopards Direct",
            })
    return rows


def _is_terminal(status):
    upper = str(status or "").strip().upper()
    return upper == "DELIVERED" or "RETURNED TO SHIPPER" in upper or "DELIVERED TO SHIPPER" in upper


async def _fetch_history(session, tracking_number):
    timeout = ClientTimeout(total=12)
    async with session.get(
        _BASE_URL + "/tracking",
        params={"cn_number": tracking_number},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        if response.status != 200:
            return []
        landing = await response.text()
    token_match = re.search(r"_token\s*:\s*['\"]([^'\"]+)", landing)
    if not token_match:
        return []
    async with session.get(
        _BASE_URL + "/shipment_tracking-new",
        params={"cn_number": tracking_number, "_token": token_match.group(1)},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        payload = await response.json(content_type=None)
        if response.status != 200 or not isinstance(payload, dict) or not payload.get("success"):
            return []
    async with session.get(
        _BASE_URL + "/shipment_tracking_view",
        timeout=timeout,
        ssl=_SSL_CONTEXT,
    ) as response:
        if response.status != 200:
            return []
        return parse_tracking_history(await response.text(), tracking_number)


async def fetch_public_tracking_history(session, tracking_number):
    normalized = str(tracking_number or "").strip().upper()
    if not is_leopards_tracking_number(normalized):
        return []
    cached = _history_cache.get(normalized)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    task = _inflight.get(normalized)
    if task is None:
        task = asyncio.create_task(_fetch_history(session, normalized))
        _inflight[normalized] = task
    try:
        history = await task
        if history:
            ttl = _TERMINAL_CACHE_SECONDS if _is_terminal(history[-1].get("ProcessDescForPortal")) else _ACTIVE_CACHE_SECONDS
            _history_cache[normalized] = (history, time.monotonic() + ttl)
            return history
        return cached[0] if cached else []
    except Exception as error:
        print(f"Leopards public tracking unavailable for {normalized}: {error}")
        return cached[0] if cached else []
    finally:
        if _inflight.get(normalized) is task:
            _inflight.pop(normalized, None)


async def fetch_public_tracking_status(session, tracking_number):
    history = await fetch_public_tracking_history(session, tracking_number)
    return str((history[-1] if history else {}).get("ProcessDescForPortal") or "").strip() or None
