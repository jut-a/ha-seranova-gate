"""Frame building and message splitting (no Home Assistant needed)."""

from __future__ import annotations

import json

from .conftest import load_pure

api = load_pure("api")


def test_split_concatenated_objects() -> None:
    text = '{"ack":{"cmd":2}}{"UID":"1","CMD":10,"OUT":"000,010"}'
    assert api.split_json_objects(text) == [
        '{"ack":{"cmd":2}}',
        '{"UID":"1","CMD":10,"OUT":"000,010"}',
    ]


def test_split_ignores_braces_and_quotes_inside_strings() -> None:
    text = '{"TXT":"a } b { \\" c"}{"x":1}'
    parts = api.split_json_objects(text)
    assert len(parts) == 2
    assert json.loads(parts[0]) == {"TXT": 'a } b { " c'}


def test_decode_payload_accepts_every_shape() -> None:
    frame = {"CMD": 1}
    assert api.decode_payload(frame) == [frame]
    assert api.decode_payload([frame, '{"CMD":2}']) == [frame, {"CMD": 2}]
    assert api.decode_payload(b'{"CMD":3}garbage{"CMD":4}') == [{"CMD": 3}, {"CMD": 4}]
    assert api.decode_payload('not json') == []
    assert api.decode_payload(None) == []


def test_build_frame_matches_the_web_app() -> None:
    prepare = api.build_frame("864000000000000", "key", 4, 2, None)
    assert prepare == {
        "IMEI": "864000000000000",
        "KEY": "key",
        "MSG": {"SEQ": 4, "CMD": 2, "VAL": None},
    }
    assert "CODE" not in prepare["MSG"]

    pulse = api.build_frame("864000000000000", "key", 5, 6, "1#2#", "1234")
    assert list(pulse["MSG"]) == ["SEQ", "CMD", "CODE", "VAL"]
    assert pulse["MSG"]["CODE"] == "1234"


def test_as_int() -> None:
    assert api.as_int("6") == 6
    assert api.as_int(3) == 3
    assert api.as_int(None) is None
    assert api.as_int("x") is None


def test_not_sent_is_a_socket_closed() -> None:
    # The command path treats NotSent as "nothing written" and any other
    # SocketClosed as "maybe written"; the hierarchy must stay this way.
    assert issubclass(api.NotSent, api.SocketClosed)
