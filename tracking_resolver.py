import asyncio
import re
from datetime import datetime

from digidokaan import fetch_tracking_history as fetch_digidokaan_tracking_history
from digidokaan import fetch_tracking_status as fetch_digidokaan_tracking_status
from trax import fetch_public_tracking_history, is_trax_tracking_number


_resolved_history_cache = {}
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d", "%d/%m/%Y",
)


def tracking_event_datetime(event):
    raw = str((event or {}).get("TransactionDate") or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def tracking_history_latest_datetime(history):
    dated = [tracking_event_datetime(event) for event in history or []]
    dated = [value for value in dated if value is not None]
    return max(dated) if dated else None


def _with_source(history, source):
    return [{**event, "TrackingSource": event.get("TrackingSource") or source} for event in history or []]


def choose_freshest_history(digidokaan_history, trax_history):
    """DigiDokaan wins ties; Trax wins only with a demonstrably newer scan."""
    digi = _with_source(digidokaan_history, "DigiDokaan")
    direct = _with_source(trax_history, "Trax Direct")
    if not direct:
        return digi
    if not digi:
        return direct if tracking_history_latest_datetime(direct) else []
    digi_latest = tracking_history_latest_datetime(digi)
    direct_latest = tracking_history_latest_datetime(direct)
    if direct_latest and (not digi_latest or direct_latest > digi_latest):
        return direct
    return digi


async def fetch_resolved_tracking_history(session, tracking_number):
    digits = re.sub(r"\D", "", str(tracking_number or ""))
    if not digits:
        return []
    if is_trax_tracking_number(digits):
        digi_result, direct_result = await asyncio.gather(
            fetch_digidokaan_tracking_history(session, digits),
            fetch_public_tracking_history(session, digits),
            return_exceptions=True,
        )
        digi = digi_result if isinstance(digi_result, list) else []
        direct = direct_result if isinstance(direct_result, list) else []
        selected = choose_freshest_history(digi, direct)
    else:
        result = await fetch_digidokaan_tracking_history(session, digits)
        selected = _with_source(result if isinstance(result, list) else [], "DigiDokaan")

    previous = _resolved_history_cache.get(digits) or []
    if not selected:
        return previous
    selected_latest = tracking_history_latest_datetime(selected)
    previous_latest = tracking_history_latest_datetime(previous)
    # A partial outage or stale source must never downgrade a known timeline.
    if previous and previous_latest and (not selected_latest or selected_latest < previous_latest):
        return previous
    _resolved_history_cache[digits] = selected
    return selected


async def fetch_resolved_tracking_status(session, tracking_number):
    snapshot = await fetch_resolved_tracking_snapshot(session, tracking_number)
    return snapshot["status"]


async def fetch_resolved_tracking_snapshot(session, tracking_number):
    history = await fetch_resolved_tracking_history(session, tracking_number)
    if history:
        latest = history[-1]
        return {
            "status": str(latest.get("ProcessDescForPortal") or "").strip() or None,
            "source": str(latest.get("TrackingSource") or "DigiDokaan"),
            "history": history,
        }
    status = await fetch_digidokaan_tracking_status(session, tracking_number)
    return {"status": status, "source": "DigiDokaan" if status else "", "history": []}
