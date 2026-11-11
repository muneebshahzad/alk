import re

from aiohttp import ClientTimeout


_TCS_PUBLIC_BRIDGE = "https://www.tcsexpress.com/apibridge"


def is_tcs_tracking_number(value):
    """TCS domestic CNs returned through DigiDokaan use 12 numeric digits."""
    return bool(re.fullmatch(r"\d{12}", re.sub(r"\D", "", str(value or ""))))


async def fetch_public_tracking_status(session, tracking_number):
    """Read the structured JSON used by TCS's public tracking page."""
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
