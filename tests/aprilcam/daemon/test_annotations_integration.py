"""Integration and acceptance tests closing out
``annotation-store-for-shared-spatial-state.md`` (ticket 024-005): the
full vertical slice -- daemon store (001), client wire (002), renderer
(003), CLI (004) -- proven together against UC-28's own scenarios
(``docs/design/use-cases/daemon.md``) and sprint 024's SUC-001..004.

Fully headless throughout, matching every prior sprint in this series
(module docstring pattern, e.g. ``test_lifecycle_integration.py``):

* :func:`test_put_by_one_client_is_visible_to_a_second_client_and_renders_on_a_real_deskewed_frame`
  reuses ``tests/aprilcam/client/test_daemon_client.py``'s own
  ``FakeBackend`` + real-``cv2.aruco``-rendered-frame + real homography
  solve pattern (``tests/aprilcam/calibration/test_calibration.py``'s
  ``render_playfield_frame``/``main_playfield_definition``) to fetch a
  *real* deskewed frame -- not a synthetic ``ImageFrame`` fixture like
  ``test_renderer.py`` uses -- and proves the whole story end to end:
  put on one ``GrpcDaemon`` connection, agree on a second independently
  -connected one (UC-28.S-1/S-3), embed into the next deskewed frame,
  and ``Renderer.render()`` draws it at the pixel the world coordinate
  predicts (ticket 003's own note: this is the one place in the
  annotation-store story that needs a real fetched frame).
* :func:`test_daemon_restart_wipes_annotations_while_the_disk_backed_playfield_and_a_re_put_survive`
  uses the real daemon assembly (``daemon.__main__.build()``,
  ``start_watch=False`` -- headless, no camera subsystem activity,
  matching ``test_main.py``) plus ticket 023-003's boot-time config-dir
  load, since that is the one place a real playfield needs to survive a
  restart (UC-28.S-4) while a fresh ``DaemonService`` instance's
  in-memory annotation store genuinely does not.

Most of the individual RPC/CLI-unit-level behaviour this ticket's own
acceptance criteria restate is already proven by tickets 001-004's own
test suites: ``test_service.py``'s ``DaemonService`` annotation section
(including ``test_two_writers_on_one_key_last_write_wins_no_merge_no_error``,
UC-28.S-6), ``test_daemon_client.py``'s client-wire section (including
its own two-independent-clients and deskewed-embed tests, and every
``UnknownPlayfield`` rejection, UC-28.S-5), ``test_renderer.py``'s
per-shape pixel tests (with synthetic ``ImageFrame`` fixtures), and
``test_playfield_cli.py``'s ``cmd_annotations`` unit tests (with a fake
``Daemon``). This module adds exactly what those do not yet cover: the
full real-frame put -> render pixel round trip, and a genuine daemon
restart. CLI *subprocess* coverage (SUC-004, real ``aprilcam`` binary)
lives alongside ``tests/aprilcam/cli/test_cli_integration.py``'s
existing daemon-subprocess fixtures instead -- see that module.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import grpc
import numpy as np
import pytest
from PIL import Image

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.renderer import Renderer
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.service import CALIBRATION_FRAME_COUNT, DaemonService
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import Annotation, Marker, WorldPoint

from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
)
from tests.aprilcam.client.test_daemon_client import _bind_client
from tests.aprilcam.daemon.test_service import _open_camera_with_frames, _service_with_camera


def _marker_annotation(id: str, layer: str, *, x: float, y: float) -> Annotation:
    return Annotation(id=id, layer=layer, shape=Marker(at=WorldPoint(x=x, y=y)))


def _mean_color_near(image: np.ndarray, x: float, y: float, *, radius: int = 3) -> np.ndarray:
    height, width = image.shape[:2]
    x0, x1 = max(0, int(x) - radius), min(width, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(height, int(y) + radius + 1)
    patch = image[y0:y1, x0:x1]
    assert patch.size > 0
    return patch.reshape(-1, patch.shape[-1]).mean(axis=0)


def _decode(png_bytes: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"))


# ---------------------------------------------------------------------------
# UC-28.S-1 / S-3: put on one client, agree on a second, render on a real
# deskewed frame.
# ---------------------------------------------------------------------------


def test_put_by_one_client_is_visible_to_a_second_client_and_renders_on_a_real_deskewed_frame(
    tmp_path,
):
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    registry = CameraRegistry(tmp_path / "state" / "cameras")
    frame_image = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame_image] * (CALIBRATION_FRAME_COUNT + 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    # Field centre -- no surveyed ArUco marker sits here (module docstring
    # of tests/aprilcam/calibration/test_calibration.py's own
    # MAIN_PLAYFIELD_ARUCO_POSITIONS), so the drawn marker cannot be
    # confused with a real ArUco square baked into the rendered frame.
    goal_world = WorldPoint(x=0.0, y=0.0)

    with _bind_client(service) as client_a, _bind_client(service) as client_b:
        client_a.put_annotation(
            "main-playfield", Annotation(id="goal", layer="planner", shape=Marker(at=goal_world))
        )

        # UC-28.S-1/S-3: a second, independently-connected client agrees
        # with no daemon-side action beyond the put itself, and reads
        # the structured set -- no image fetch required.
        seen_by_b = client_b.get_annotations("main-playfield")
        assert [a.id for a in seen_by_b.annotations] == ["goal"]
        assert seen_by_b.annotations[0].layer == "planner"
        assert seen_by_b.annotations[0].shape == Marker(at=goal_world)

        # The next deskewed frame (fetched by either client -- here B,
        # the one that never put anything) embeds the same set.
        frame = client_b.get_frame(slug, deskewed=True, encoding="png")

    assert frame.annotations is not None
    assert [a.id for a in frame.annotations.annotations] == ["goal"]
    assert frame.annotations.revision == 1

    png_bytes = Renderer().render(frame)
    image = _decode(png_bytes)

    assert frame.px_per_cm is not None and frame.world_extent is not None
    predicted = world_to_deskewed_pixel(
        goal_world, world_extent=frame.world_extent, px_per_cm=frame.px_per_cm
    )
    drawn_patch = _mean_color_near(image, predicted.x, predicted.y)

    # A control patch elsewhere in the field's clear interior -- no
    # annotation, no ArUco marker -- establishes what "undrawn" looks
    # like in *this* real, homography-warped frame, rather than
    # asserting against a hardcoded background colour.
    control_predicted = world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=20.0), world_extent=frame.world_extent, px_per_cm=frame.px_per_cm
    )
    control_patch = _mean_color_near(image, control_predicted.x, control_predicted.y)

    difference = np.linalg.norm(drawn_patch.astype(np.float64) - control_patch.astype(np.float64))
    assert difference > 30, (
        f"expected the marker drawn at {predicted} (world {goal_world}) to "
        f"differ visibly from undrawn field background at {control_predicted}; "
        f"drawn={drawn_patch} control={control_patch}"
    )


# ---------------------------------------------------------------------------
# UC-28.S-4: a daemon restart wipes the annotation slate; the disk-backed
# playfield survives (ticket 023-003), and the original client's re-put on
# reconnect is visible again.
# ---------------------------------------------------------------------------


def _write_playfield(config_dir: Path, name: str) -> None:
    (config_dir / "playfields").mkdir(parents=True, exist_ok=True)
    (config_dir / "playfields" / f"{name}.json").write_text(
        json.dumps({"name": name, "playfield": {"width_cm": 100.0, "height_cm": 80.0}}),
        encoding="utf-8",
    )


@contextmanager
def _connected_client(socket_path: Path) -> Iterator[GrpcDaemon]:
    channel = grpc.insecure_channel(f"unix:{socket_path}")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
        yield GrpcDaemon(stub, target="localhost")
    finally:
        channel.close()


@pytest.fixture
def short_tmp_dir() -> Iterator[Path]:
    # A short, top-level /tmp dir -- unix domain sockets' ~103-character
    # path limit, same workaround as every other module's identically
    # -shaped fixture (test_lifecycle_integration.py, test_main.py, ...).
    d = Path(tempfile.mkdtemp(prefix="ac-ann-restart-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_daemon_restart_wipes_annotations_while_the_disk_backed_playfield_and_a_re_put_survive(
    tmp_path, short_tmp_dir
):
    config_dir = tmp_path / "config"
    _write_playfield(config_dir, "main-playfield")
    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")
    lock_path = short_tmp_dir / "daemon.lock"
    socket_path = short_tmp_dir / "control.sock"

    lifecycle = daemon_main.build(
        directories, lock_path=lock_path, socket_path=socket_path, start_watch=False
    )
    lifecycle.start()
    try:
        with _connected_client(socket_path) as client:
            client.put_annotation(
                "main-playfield", _marker_annotation("goal", "planner", x=1.0, y=2.0)
            )
            before = client.get_annotations("main-playfield")
            assert len(before.annotations) == 1
            assert before.revision == 1
    finally:
        lifecycle.stop()

    # -- restart: a fresh process-equivalent stack, same config_dir/
    #    lock_path/socket_path -- the playfield reloads from disk
    #    (ticket 023-003's boot-time config load), but DaemonService's
    #    annotation store is a fresh, empty in-memory dict -----------------
    restarted = daemon_main.build(
        directories, lock_path=lock_path, socket_path=socket_path, start_watch=False
    )
    restarted.start()
    try:
        with _connected_client(socket_path) as client:
            after = client.get_annotations("main-playfield")
            assert after.annotations == ()
            assert after.revision == 0

            # The original client's re-put on reconnect (UC-28.S-4's own
            # main flow, mirroring UC-12's mount-parameter ritual) is
            # visible again -- same playfield, same layer/id, no error.
            client.put_annotation(
                "main-playfield", _marker_annotation("goal", "planner", x=1.0, y=2.0)
            )
            reput = client.get_annotations("main-playfield")
            assert [a.id for a in reput.annotations] == ["goal"]
    finally:
        restarted.stop()
