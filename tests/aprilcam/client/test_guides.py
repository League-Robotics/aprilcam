"""Tests for ``aprilcam.client.guides`` (ticket 025-006, UC-27).

**The critical test in this file** is
``test_reimport_and_use_succeeds_when_daemon_construction_modules_are_poisoned``
-- the literal, mechanical proof that ``client.guides.Guides`` works
with **no ``Daemon``, no ``Discovery``, and no daemon process running
anywhere** (UC-27.S-1), not merely "doesn't happen to fail today." It
forces a fresh import of the module under test in an environment where
every module capable of reaching a real daemon/network connection
raises the instant anything touches it -- if ``client.guides`` needed
any of them, even indirectly through an import chain, the test would
fail on the reload itself.

``test_guides_module_imports_nothing_daemon_or_network_shaped`` is the
companion static check, in the same spirit as
``tests/test_no_cv2_in_client_modules.py``'s AST scan: it reads the
module's own source and asserts its import statements name nothing
that could construct a ``Daemon`` or open a socket.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from aprilcam.client.guides import Guides
from aprilcam.client.interfaces import Guides as GuidesBase
from aprilcam.errors import UnknownGuide

REPO_ROOT = Path(__file__).resolve().parents[3]
GUIDES_MODULE_PATH = REPO_ROOT / "src" / "aprilcam" / "client" / "guides.py"

#: Names that would indicate ``client.guides`` reaches toward a real
#: daemon connection or the network -- none of these belong in this
#: module's own import list (UC-27.S-1: it must work with no daemon
#: running at all).
_FORBIDDEN_IMPORT_NAMES = (
    "grpc",
    "aprilcam.client.discovery",
    "aprilcam.client.daemon_client",
    "aprilcam.client.lifecycle",
    "aprilcam.daemon.lifecycle",
    "aprilcam.daemon.service",
    "aprilcam.daemon.rpc",
)


# ---------------------------------------------------------------------------
# UC-27.S-1 -- no daemon/network object constructed anywhere in the path
# ---------------------------------------------------------------------------


def test_guides_module_imports_nothing_daemon_or_network_shaped() -> None:
    """Static proof: ``client/guides.py``'s own import statements name
    none of the modules that could construct a ``Daemon`` or a network
    channel."""
    tree = ast.parse(GUIDES_MODULE_PATH.read_text(), filename=str(GUIDES_MODULE_PATH))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name in _FORBIDDEN_IMPORT_NAMES]
    assert not offenders, (
        f"client/guides.py must not import daemon/network-construction "
        f"modules (UC-27.S-1), but imports: {offenders}"
    )


def test_reimport_and_use_succeeds_when_daemon_construction_modules_are_poisoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic, mechanical proof: force a fresh import of
    ``aprilcam.client.guides`` (and its dependency ``aprilcam.guides``)
    with every daemon/network-construction module replaced by a poison
    object that raises the instant any attribute is touched on it --
    including the attribute lookup ``from X import Y`` itself performs.
    ``Guides().get()``/``.available()`` still succeed, which is only
    possible if nothing in the real call path ever touched a poisoned
    module.
    """

    class _Poison:
        def __getattr__(self, name: str) -> None:
            raise AssertionError(
                f"client.guides must not touch {name!r} -- that module can "
                "reach a Daemon/network connection, which UC-27.S-1 "
                "forbids for the no-daemon guide accessor"
            )

    for poisoned in (
        "grpc",
        "aprilcam.client.discovery",
        "aprilcam.client.daemon_client",
        "aprilcam.daemon.lifecycle",
    ):
        monkeypatch.setitem(sys.modules, poisoned, _Poison())

    # Force a fresh import of the modules actually under test, so any
    # hidden import of a poisoned module above raises right here rather
    # than being masked by an already-cached, already-imported module.
    monkeypatch.delitem(sys.modules, "aprilcam.client.guides", raising=False)
    monkeypatch.delitem(sys.modules, "aprilcam.guides", raising=False)

    fresh_module = importlib.import_module("aprilcam.client.guides")

    guides = fresh_module.Guides()
    assert guides.available() == ("agent", "robot")
    text = guides.get()
    assert isinstance(text, str) and text != ""


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_guides_derives_from_client_interfaces_guides() -> None:
    assert issubclass(Guides, GuidesBase)
    assert isinstance(Guides(), GuidesBase)


# ---------------------------------------------------------------------------
# UC-27.S-1 -- default name, UC-27.S-2 -- world frame, UC-27.S-3 -- unknown name
# ---------------------------------------------------------------------------


def test_get_with_no_name_defaults_to_agent() -> None:
    guides = Guides()
    assert guides.get() == guides.get("agent")


def test_get_no_name_contains_the_world_frame_statement() -> None:
    """UC-27.S-2: the default ("agent") guide states the world frame
    explicitly -- +x east, +y north, A1-centred, heading 0 = east CCW
    positive -- so an agent never has to infer it from observed data."""
    text = Guides().get()

    assert "A1-centred" in text
    assert "+x east" in text
    assert "+y north" in text
    assert "0 = east" in text
    assert "counter-clockwise positive" in text or "CCW positive" in text


def test_unknown_guide_name_raises_naming_available_guides() -> None:
    """UC-27.S-3: an unknown name raises ``UnknownGuide`` naming the
    guides that actually exist, never empty text."""
    guides = Guides()

    with pytest.raises(UnknownGuide) as excinfo:
        guides.get("nonexistent")

    assert excinfo.value.requested == "nonexistent"
    assert excinfo.value.available == ("agent", "robot")
    assert "agent" in str(excinfo.value)
    assert "robot" in str(excinfo.value)


def test_available_returns_exactly_agent_and_robot() -> None:
    assert Guides().available() == ("agent", "robot")


# ---------------------------------------------------------------------------
# Content: mount persistence vs. per-session annotations, bring-up order
# ---------------------------------------------------------------------------


def test_agent_guide_states_mounts_persist_annotations_are_per_session() -> None:
    """Mount registrations (UC-12) persist across a daemon restart
    (ticket 036-007); annotations (UC-28) remain runtime state that
    vanishes on every daemon restart. The guide must say both
    explicitly, not leave either implied -- and must not still claim
    mounts are per-session (ticket 036-008)."""
    text = Guides().get("agent")

    assert "UC-12" in text
    assert "UC-28" in text
    assert "mount registrations (UC-12) now persist across a daemon restart" in text
    assert "annotations (UC-28) remain per-session" in text
    assert "registry.json" in text


def test_agent_guide_states_the_bring_up_order() -> None:
    """The UC-01 (start daemon) -> UC-02 (push/load playfield) -> UC-03
    (configure camera) -> UC-04 (calibrate) -> UC-05 (confirm ready) ->
    UC-06 onward (query) sequence, in that order."""
    text = Guides().get("agent")

    for step in ("UC-01", "UC-02", "UC-03", "UC-04", "UC-05", "UC-06"):
        assert step in text, f"bring-up order is missing {step}"

    # Not just present -- present in the stated order.
    positions = [text.index(step) for step in ("UC-01", "UC-02", "UC-03", "UC-04", "UC-05", "UC-06")]
    assert positions == sorted(positions), "bring-up order steps are out of sequence"


def test_robot_guide_is_available_and_distinct_from_agent_guide() -> None:
    guides = Guides()
    agent_text = guides.get("agent")
    robot_text = guides.get("robot")

    assert robot_text != agent_text
    assert "mount_x" in robot_text
    assert "register_tag" in robot_text
