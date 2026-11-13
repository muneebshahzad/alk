import re
import ssl

import certifi
from aiohttp import ClientTimeout


_TCS_PUBLIC_BRIDGE = "https://www.tcsexpress.com/apibridge"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def is_tcs_tracking_number(value):
    """TCS domestic CNs returned through DigiDokaan use 12 numeric digits."""
    return bool(re.fullmatch(r"\d{12}", re.sub(r"\D", "", str(value or ""))))


async def _fetch_public_tracking_payload(session, tracking_number):
    normalized = re.sub(r"\D", "", str(tracking_number or ""))
    if not is_tcs_tracking_number(normalized):
        return None
    payload = {
        "body": {
            "url": "trackapinew",
            "type": "GET",
            "headers": {},
            "payload": {},
            "param": f"consignee={normalized}",
        }
    }
    try:
        async with session.post(
            _TCS_PUBLIC_BRIDGE,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=ClientTimeout(total=12),
            ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)
    except Exception as error:
        print(f"TCS public tracking unavailable for {normalized}: {error}")
        return None
    if response.status != 200 or not isinstance(body, dict) or not body.get("isSuccess"):
        return None
    data = body.get("responseData")
    if not isinstance(data, dict) or str(data.get("message") or "").upper() != "SUCCESS":
        return None
    return data


async def fetch_public_tracking_history(session, tracking_number):
    """Normalize TCS's structured newest-first checkpoints chronologically."""
    normalized = re.sub(r"\D", "", str(tracking_number or ""))
    data = await _fetch_public_tracking_payload(session, normalized)
    if not data:
        return []
    shipment = next((row for row in data.get("shipmentinfo") or [] if isinstance(row, dict)), {})
    history = []
    for event in reversed(data.get("checkpoints") or []):
        if not isinstance(event, dict) or not str(event.get("status") or "").strip():
            continue
        history.append({
            "ConsignmentNo": normalized,
            "TransactionDate": str(event.get("datetime") or "").strip(),
            "ProcessDescForPortal": str(event.get("status") or "").strip(),
            "ReasonDesc": "",
            "ConsigneeName": str(shipment.get("consignee") or "").strip(),
            "ConsigneeCity": str(shipment.get("destination") or "").strip(),
            "TrackingSource": "TCS Direct",
        })
    return history


async def fetch_public_tracking_status(session, tracking_number):
    """Read the latest status from TCS's structured public tracking JSON."""
    data = await _fetch_public_tracking_payload(session, tracking_number)
    if not data:
        return None
    delivery = data.get("deliveryinfo")
    if isinstance(delivery, list):
        for event in delivery:
            status = str((event or {}).get("status") or "").strip()
            if status:
                return status
    checkpoints = data.get("checkpoints")
    if isinstance(checkpoints, list):
        for event in checkpoints:
            status = str((event or {}).get("status") or "").strip()
            if status:
                return status
    return None
