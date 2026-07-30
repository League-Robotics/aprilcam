"""Tests for the EnumerateCameras RPC added in ticket 014-001.

Verifies that:
- The proto-generated types exist and are importable.
- The CameraDevice Pydantic model exists and converts from proto correctly.
- DaemonControl.enumerate_cameras() method exists and has the right signature.
- No regressions in existing proto imports.
"""

from __future__ import annotations

import inspect


# ---------------------------------------------------------------------------
# Proto message existence
# ---------------------------------------------------------------------------


def test_enumerate_cameras_response_in_pb2() -> None:
    """EnumerateCamerasResponse is importable from aprilcam_pb2."""
    from aprilcam.proto import aprilcam_pb2

    assert hasattr(aprilcam_pb2, "EnumerateCamerasResponse"), (
        "aprilcam_pb2 missing EnumerateCamerasResponse"
    )


def test_camera_device_in_pb2() -> None:
    """CameraDevice message is importable from aprilcam_pb2."""
    from aprilcam.proto import aprilcam_pb2

    assert hasattr(aprilcam_pb2, "CameraDevice"), (
        "aprilcam_pb2 missing CameraDevice"
    )


def test_camera_device_fields() -> None:
    """CameraDevice proto message has index, name, and slug fields."""
    from aprilcam.proto import aprilcam_pb2

    d = aprilcam_pb2.CameraDevice(index=2, name="OV9782 1", slug="ov9782-1")
    assert d.index == 2
    assert d.name == "OV9782 1"
    assert d.slug == "ov9782-1"


def test_enumerate_cameras_response_holds_camera_devices() -> None:
    """EnumerateCamerasResponse accepts repeated CameraDevice."""
    from aprilcam.proto import aprilcam_pb2

    devices = [
        aprilcam_pb2.CameraDevice(index=0, name="FaceTime HD", slug="facetime-hd"),
        aprilcam_pb2.CameraDevice(index=1, name="OV9782 1",   slug="ov9782-1"),
    ]
    resp = aprilcam_pb2.EnumerateCamerasResponse(cameras=devices)
    assert len(resp.cameras) == 2
    assert resp.cameras[0].index == 0
    assert resp.cameras[1].name == "OV9782 1"


# ---------------------------------------------------------------------------
# gRPC stub method existence
# ---------------------------------------------------------------------------


def test_enumerate_cameras_stub_method_exists() -> None:
    """AprilCamStub has an EnumerateCameras attribute after channel init."""
    # We can't easily create a real channel in unit tests, but we can verify
    # that the stub class wires the method in __init__ by inspecting the source.
    from aprilcam.proto import aprilcam_pb2_grpc

    src = inspect.getsource(aprilcam_pb2_grpc.AprilCamStub.__init__)
    assert "EnumerateCameras" in src, (
        "AprilCamStub.__init__ does not wire EnumerateCameras"
    )


def test_enumerate_cameras_servicer_method_exists() -> None:
    """AprilCamServicer has an EnumerateCameras method."""
    from aprilcam.proto import aprilcam_pb2_grpc

    assert hasattr(aprilcam_pb2_grpc.AprilCamServicer, "EnumerateCameras"), (
        "AprilCamServicer missing EnumerateCameras"
    )


# ---------------------------------------------------------------------------
# Pydantic CameraDevice model
# ---------------------------------------------------------------------------


def test_camera_device_pydantic_model_exists() -> None:
    """CameraDevice Pydantic model is importable from client.models."""
    from aprilcam.client.models import CameraDevice

    assert CameraDevice is not None


def test_camera_device_pydantic_roundtrip() -> None:
    """CameraDevice.from_proto() converts a proto message to Pydantic correctly."""
    from aprilcam.proto import aprilcam_pb2
    from aprilcam.client.models import CameraDevice

    proto_msg = aprilcam_pb2.CameraDevice(index=3, name="My Camera", slug="my-camera")
    model = CameraDevice.from_proto(proto_msg)

    assert model.index == 3
    assert model.name == "My Camera"
    assert model.slug == "my-camera"


def test_camera_device_pydantic_fields() -> None:
    """CameraDevice Pydantic model has index, name, and slug fields."""
    from aprilcam.client.models import CameraDevice

    device = CameraDevice(index=0, name="Test Cam", slug="test-cam")
    assert device.index == 0
    assert device.name == "Test Cam"
    assert device.slug == "test-cam"


# ---------------------------------------------------------------------------
# DaemonControl.enumerate_cameras() method
# ---------------------------------------------------------------------------


def test_enumerate_cameras_method_on_daemon_control() -> None:
    """DaemonControl has an enumerate_cameras() method."""
    from aprilcam.client.control import DaemonControl

    assert hasattr(DaemonControl, "enumerate_cameras"), (
        "DaemonControl missing enumerate_cameras method"
    )
    assert callable(DaemonControl.enumerate_cameras)


def test_enumerate_cameras_return_annotation() -> None:
    """DaemonControl.enumerate_cameras() is annotated to return list[CameraDevice]."""
    import typing
    from aprilcam.client.control import DaemonControl

    # Use get_type_hints to resolve forward references / PEP 563 string annotations.
    hints = typing.get_type_hints(DaemonControl.enumerate_cameras)
    assert "return" in hints, "enumerate_cameras has no return annotation"
    ret = hints["return"]
    origin = getattr(ret, "__origin__", None)
    assert origin is list, f"Expected list return, got {ret}"


# ---------------------------------------------------------------------------
# Regression: existing proto messages still import
# ---------------------------------------------------------------------------


def test_existing_proto_messages_unaffected() -> None:
    """ListCamerasResponse and other pre-existing messages still import."""
    from aprilcam.proto import aprilcam_pb2

    assert hasattr(aprilcam_pb2, "ListCamerasResponse")
    assert hasattr(aprilcam_pb2, "OpenCameraRequest")
    assert hasattr(aprilcam_pb2, "TagMsg")
    assert hasattr(aprilcam_pb2, "TagFrame")
    assert hasattr(aprilcam_pb2, "Empty")
    assert hasattr(aprilcam_pb2, "StatusReply")
