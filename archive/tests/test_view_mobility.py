"""Tests for viewer mobile/stationary classification (_classify_tag_mobility).

A tag should show as mobile while moving and for 10 s after its last movement,
then revert to stationary. Pure-function test — no GUI/display required.
"""
from aprilcam.cli.view_cli import _classify_tag_mobility


def _tag(tid: int, vx: float = 0.0, vy: float = 0.0) -> dict:
    return {"id": tid, "vel_px": [vx, vy]}


def _ids(tags):
    return [t["id"] for t in tags]


def test_still_tag_is_stationary():
    mob, stat = _classify_tag_mobility([_tag(1)], {}, {}, now=100.0)
    assert _ids(mob) == [] and _ids(stat) == [1]


def test_currently_moving_shows_mobile_immediately():
    # First over-threshold frame: mobile now, but not yet stamped (debounce).
    vc, last = {}, {}
    mob, stat = _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=0.0)
    assert _ids(mob) == [1]
    assert 1 not in last  # single frame must not pin it for the whole timeout


def test_single_noise_frame_does_not_pin_mobile():
    vc, last = {}, {}
    _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=0.0)      # 1 noisy frame
    mob, stat = _classify_tag_mobility([_tag(1, vx=0)], vc, last, now=0.1)  # then still
    assert _ids(stat) == [1] and _ids(mob) == []


def test_sustained_movement_promotes_and_stamps():
    vc, last = {}, {}
    for i in range(10):  # 10 consecutive over-threshold frames
        _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=float(i))
    assert 1 in last


def test_reverts_to_stationary_after_10s_still():
    vc, last = {}, {}
    for i in range(10):  # promote; last movement stamped at now=9.0
        _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=float(i))
    t_last = last[1]

    # 9.9 s after last movement, sitting still -> still mobile (within timeout)
    mob, _ = _classify_tag_mobility([_tag(1, vx=0)], vc, last, now=t_last + 9.9)
    assert _ids(mob) == [1]

    # 10.1 s after last movement, still -> reverts to stationary
    mob, stat = _classify_tag_mobility([_tag(1, vx=0)], vc, last, now=t_last + 10.1)
    assert _ids(mob) == [] and _ids(stat) == [1]


def test_moving_again_resets_the_timer():
    vc, last = {}, {}
    for i in range(10):
        _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=float(i))
    # Long gap, then a fresh sustained move re-stamps last_moving to the new now.
    for i in range(10):
        _classify_tag_mobility([_tag(1, vx=50)], vc, last, now=100.0 + i)
    mob, _ = _classify_tag_mobility([_tag(1, vx=0)], vc, last, now=109.0 + 5.0)
    assert _ids(mob) == [1]
