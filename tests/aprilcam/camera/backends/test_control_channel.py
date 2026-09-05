"""Tests for the control-channel request/response protocol (sprint 038
ticket 003) -- ``get_property``/``set_property``/``settable_names``/
``frame_size`` round trips against a fake handler, plus protocol-error
and timeout paths. No real camera anywhere in this file.
"""

from __future__ import annotations

import json
import threading
from multiprocessing import Pipe
from typing import List, Optional, Tuple

import pytest

from aprilcam.camera.backends._control_channel import (
    CONTROL_PROTOCOL_VERSION,
    ControlChannel,
    ControlError,
    ControlHandler,
    ControlProtocolError,
    ControlTimeout,
    serve_one,
)


class _FakeHandler(ControlHandler):
    """A fake capture-worker-side handler -- no real camera, no cv2."""

    def __init__(self) -> None:
        self.properties = {"exposure": 100.0, "gain": 2.0}
        self.set_calls: List[Tuple[str, float]] = []

    def get_property(self, name: str) -> Optional[float]:
        return self.properties.get(name)

    def set_property(self, name: str, value: float) -> bool:
        if name not in self.properties:
            return False
        self.properties[name] = value
        self.set_calls.append((name, value))
        return True

    def settable_names(self) -> List[str]:
        return sorted(self.properties)

    def frame_size(self) -> Optional[Tuple[int, int]]:
        return (1920, 1080)


class _RaisingHandler(ControlHandler):
    """A handler whose every method raises -- exercises the server-side
    exception -> error-response -> client-side ControlError path."""

    def get_property(self, name: str) -> Optional[float]:
        raise ValueError(f"no such property: {name}")

    def set_property(self, name: str, value: float) -> bool:
        raise ValueError(f"no such property: {name}")

    def settable_names(self) -> List[str]:
        raise ValueError("boom")

    def frame_size(self) -> Optional[Tuple[int, int]]:
        raise ValueError("boom")


def _serve_one_in_background(conn, handler: ControlHandler, *, timeout: float = 10.0) -> threading.Thread:
    thread = threading.Thread(target=serve_one, args=(conn, handler), kwargs={"timeout": timeout})
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# round trips for the four known methods
# ---------------------------------------------------------------------------


def test_get_property_round_trip():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("get_property", {"name": "exposure"}, timeout=10.0)
        assert result == 100.0
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


def test_get_property_unknown_name_returns_none_not_error():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("get_property", {"name": "not_a_real_property"}, timeout=10.0)
        assert result is None
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


def test_set_property_round_trip():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("set_property", {"name": "gain", "value": 5.5}, timeout=10.0)
        assert result is True
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()
    assert handler.properties["gain"] == 5.5
    assert handler.set_calls == [("gain", 5.5)]


def test_set_property_unknown_name_returns_false():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("set_property", {"name": "nope", "value": 1.0}, timeout=10.0)
        assert result is False
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


def test_settable_names_round_trip():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("settable_names", timeout=10.0)
        assert result == ["exposure", "gain"]
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


def test_frame_size_round_trip():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        result = channel.request("frame_size", timeout=10.0)
        assert tuple(result) == (1920, 1080)
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_handler_exception_becomes_control_error_client_side():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _RaisingHandler()
    thread = _serve_one_in_background(child_conn, handler)
    channel = ControlChannel(parent_conn)
    try:
        with pytest.raises(ControlError):
            channel.request("get_property", {"name": "exposure"}, timeout=10.0)
    finally:
        thread.join(timeout=10.0)
        channel.close()
        child_conn.close()


def test_client_rejects_unknown_method_without_sending_anything():
    parent_conn, child_conn = Pipe(duplex=True)
    channel = ControlChannel(parent_conn)
    try:
        with pytest.raises(ControlProtocolError):
            channel.request("delete_everything")
        assert not child_conn.poll(0.2)  # nothing was ever sent
    finally:
        channel.close()
        child_conn.close()


def test_server_reports_protocol_error_for_unknown_method_on_the_wire():
    """Bypasses ControlChannel's own client-side method validation to
    exercise serve_one()/_dispatch's server-side handling directly."""
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    try:
        parent_conn.send_bytes(
            json.dumps(
                {"v": CONTROL_PROTOCOL_VERSION, "id": 1, "method": "not_a_real_method", "params": {}}
            ).encode("utf-8")
        )
        served = serve_one(child_conn, handler, timeout=10.0)
        assert served is True
        response = json.loads(parent_conn.recv_bytes().decode("utf-8"))
        assert response["ok"] is False
        assert "unknown control channel method" in response["error"]
    finally:
        parent_conn.close()
        child_conn.close()


def test_request_times_out_with_no_responder():
    parent_conn, child_conn = Pipe(duplex=True)
    channel = ControlChannel(parent_conn)
    try:
        with pytest.raises(ControlTimeout):
            channel.request("frame_size", timeout=0.2)
    finally:
        channel.close()
        child_conn.close()


def test_serve_one_returns_false_when_nothing_arrives_within_timeout():
    parent_conn, child_conn = Pipe(duplex=True)
    handler = _FakeHandler()
    try:
        assert serve_one(child_conn, handler, timeout=0.2) is False
    finally:
        parent_conn.close()
        child_conn.close()
