"""Decode PROGATE CMD 14 events (Contact ID + the text the web app shows).

Example frame:
    {"CMD": 14, "EVENT": {"COPT": "…", "CID": "142200001", "TIME": 1767225600,
     "NOTE": "", "TXT": "Object Name\\nAccess Gained by            \\nJohn\\nUser 1\\n"}}

CID = qualifier (1 new / 3 restore) + 3-digit Contact ID code + 2-digit
partition + 3-digit zone or user number. TXT lines = object name, event
description, then the subject (user or zone name) and its label.
No Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def event_type_for(qualifier: str, code: int | None) -> str:
    """Map a Contact ID qualifier/code to one of const.EVENT_TYPES."""
    if qualifier in ("3", "R"):
        return "restore"
    if code in (421, 424):
        return "access_denied"
    if code in (422, 425):
        return "access_granted"
    if code is not None:
        if 100 <= code < 200:
            return "alarm"
        if 300 <= code < 400:
            return "trouble"
        if 600 <= code < 700:
            return "test"
    return "event"


def parse_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (event_type, attributes) for one EVENT object."""
    cid = str(event.get("CID") or "")
    qualifier = cid[:1]
    code = _as_int(cid[1:4]) if len(cid) >= 4 else None
    partition = _as_int(cid[4:6]) if len(cid) >= 6 else None
    number = _as_int(cid[6:9]) if len(cid) >= 9 else None

    raw_text = str(event.get("TXT") or "")
    lines = [" ".join(line.split()) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    obj = lines[0] if lines else None
    description = lines[1] if len(lines) > 1 else None
    subject = lines[2] if len(lines) > 2 else None
    detail = " ".join(lines[3:]) or None

    if description and subject:
        message = f"{description} {subject}" + (f" ({detail})" if detail else "")
    elif description:
        message = description
    else:
        message = f"Contact ID {cid}" if cid else "Event"

    device_time = None
    if (ts := _as_int(event.get("TIME"))) is not None and ts > 0:
        # A TIME outside the platform's range raises (ValueError, OSError or
        # OverflowError, depending on the value); treat it as no device time.
        try:
            device_time = datetime.fromtimestamp(ts, UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            device_time = None

    attributes = {
        "message": message,
        "object": obj,
        "description": description,
        "subject": subject,
        "subject_detail": detail,
        "user_or_zone": number,
        "partition": partition,
        "code": f"{code:03d}" if code is not None else None,
        "cid": cid or None,
        "device_time": device_time,
        "text": "\n".join(lines),
    }
    return event_type_for(qualifier, code), attributes
