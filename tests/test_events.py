"""CMD 14 event decoding (no Home Assistant needed)."""

from __future__ import annotations

from .conftest import load_pure

events = load_pure("events")

ACCESS = {
    "COPT": "…",
    "CID": "142200001",
    "TIME": 1767225600,
    "NOTE": "",
    "TXT": "Object Name\nAccess Gained by            \nJohn\nUser 1\n",
}


def test_access_granted_reads_like_the_web_app() -> None:
    event_type, attrs = events.parse_event(ACCESS)
    assert event_type == "access_granted"
    assert attrs["message"] == "Access Gained by John (User 1)"
    assert attrs["object"] == "Object Name"
    assert attrs["description"] == "Access Gained by"
    assert attrs["subject"] == "John"
    assert attrs["subject_detail"] == "User 1"
    assert attrs["code"] == "422"
    assert attrs["partition"] == 0
    assert attrs["user_or_zone"] == 1
    assert attrs["device_time"] == "2026-01-01T00:00:00+00:00"
    assert attrs["text"] == "Object Name\nAccess Gained by\nJohn\nUser 1"


def test_event_types_from_contact_id() -> None:
    assert events.event_type_for("1", 421) == "access_denied"
    assert events.event_type_for("1", 130) == "alarm"
    assert events.event_type_for("1", 301) == "trouble"
    assert events.event_type_for("1", 602) == "test"
    assert events.event_type_for("1", 401) == "event"
    assert events.event_type_for("3", 130) == "restore"
    assert events.event_type_for("R", 422) == "restore"
    assert events.event_type_for("", None) == "event"


def test_description_only_and_empty_events() -> None:
    _, attrs = events.parse_event({"CID": "130100003", "TXT": "Object Name\nZone alarm\n"})
    assert attrs["message"] == "Zone alarm"
    assert attrs["user_or_zone"] == 3

    event_type, attrs = events.parse_event({})
    assert event_type == "event"
    assert attrs["message"] == "Event"
    assert attrs["cid"] is None
    assert attrs["device_time"] is None


def test_cid_without_text_names_the_code() -> None:
    _, attrs = events.parse_event({"CID": "160200000"})
    assert attrs["message"] == "Contact ID 160200000"
    assert attrs["code"] == "602"


def test_out_of_range_time_is_dropped_not_raised() -> None:
    """datetime.fromtimestamp raises ValueError / OSError / OverflowError on these."""
    for stamp in (10**12, 10**18, 2**63):
        _, attrs = events.parse_event({**ACCESS, "TIME": stamp})
        assert attrs["device_time"] is None
        assert attrs["message"] == "Access Gained by John (User 1)"
