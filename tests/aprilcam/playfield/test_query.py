"""Tests for ``aprilcam.playfield.query`` (ticket 025-001).

Headless: pure logic against constructed :class:`Feature` fixtures and
against the real ``main-playfield`` definition loaded from disk (no
daemon, no camera, no calibration). Exercises UC-08.S-1/S-2/S-3 plus the
bare-"tag" tie-break and the exact-slug-always-matches guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aprilcam.config.interfaces import Feature
from aprilcam.config.loader import ConfigLoader
from aprilcam.playfield.interfaces import FeatureQuery as FeatureQueryBase
from aprilcam.playfield.query import FeatureQuery
from aprilcam.types import WorldPoint

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PLAYFIELD_PATH = REPO_ROOT / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"


# ---------------------------------------------------------------------------
# A hand-built fixture matching main-playfield's worked example
# (sprint.md Test Strategy): AprilTag 1 at the centre, ArUco 1 at a
# distinct corner, dot-west-blue, and the four orange dots.
# ---------------------------------------------------------------------------


def _main_playfield_fixture() -> tuple[Feature, ...]:
    return (
        Feature(slug="apriltag-center-a1", kind="april_tag", position=WorldPoint(x=0, y=0)),
        Feature(
            slug="aruco-northwest-u1",
            kind="aruco_tag",
            position=WorldPoint(x=-67, y=44.65),
        ),
        Feature(
            slug="rect-southwest-blue",
            kind="rectangle",
            position=WorldPoint(x=-35, y=-24),
            colour="blue",
        ),
        Feature(
            slug="dot-northwest-orange",
            kind="dot",
            position=WorldPoint(x=-50, y=30),
            colour="orange",
        ),
        Feature(
            slug="dot-northeast-orange",
            kind="dot",
            position=WorldPoint(x=50, y=30),
            colour="orange",
        ),
        Feature(
            slug="dot-southeast-orange",
            kind="dot",
            position=WorldPoint(x=50, y=-30),
            colour="orange",
        ),
        Feature(
            slug="dot-southwest-orange",
            kind="dot",
            position=WorldPoint(x=-50, y=-30),
            colour="orange",
        ),
        Feature(
            slug="dot-west-blue",
            kind="dot",
            position=WorldPoint(x=-50, y=0),
            colour="blue",
        ),
        Feature(
            slug="dot-north-green",
            kind="dot",
            position=WorldPoint(x=0, y=30),
            colour="green",
        ),
    )


@pytest.fixture
def query() -> FeatureQuery:
    return FeatureQuery(features=_main_playfield_fixture())


def _slugs(matches: tuple[Feature, ...]) -> set[str]:
    return {f.slug for f in matches}


# ---------------------------------------------------------------------------
# Basic wiring
# ---------------------------------------------------------------------------


def test_feature_query_is_a_real_playfield_interfaces_feature_query(query: FeatureQuery):
    assert isinstance(query, FeatureQueryBase)


# ---------------------------------------------------------------------------
# UC-08.S-1 -- fuzzy match on a unique feature, three equivalent phrasings.
# ---------------------------------------------------------------------------


def test_name_and_colour_match_the_blue_dot(query: FeatureQuery):
    matches = query.find("the blue dot")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert matches[0].position == WorldPoint(x=-50, y=0)


def test_name_colour_and_cardinal_match_blue_dot_west(query: FeatureQuery):
    matches = query.find("blue dot west")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert matches[0].position == WorldPoint(x=-50, y=0)


def test_exact_slug_matches_dot_west_blue(query: FeatureQuery):
    matches = query.find("dot-west-blue")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert matches[0].position == WorldPoint(x=-50, y=0)


def test_exact_slug_of_any_feature_always_matches_that_feature(query: FeatureQuery):
    for feature in _main_playfield_fixture():
        matches = query.find(feature.slug)
        assert feature.slug in _slugs(matches), f"{feature.slug!r} did not match its own slug"


# ---------------------------------------------------------------------------
# UC-08.S-2 -- ambiguous query returns every match, never an arbitrary pick.
# ---------------------------------------------------------------------------


def test_ambiguous_colour_query_returns_all_four_orange_dots(query: FeatureQuery):
    matches = query.find("the orange dot")

    assert _slugs(matches) == {
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    }
    positions = {f.slug: f.position for f in matches}
    assert positions["dot-northwest-orange"] == WorldPoint(x=-50, y=30)
    assert positions["dot-northeast-orange"] == WorldPoint(x=50, y=30)
    assert positions["dot-southeast-orange"] == WorldPoint(x=50, y=-30)
    assert positions["dot-southwest-orange"] == WorldPoint(x=-50, y=-30)


# ---------------------------------------------------------------------------
# UC-08.S-3 -- no match returns empty, never the nearest feature.
# ---------------------------------------------------------------------------


def test_no_match_returns_empty_tuple_not_nearest_guess(query: FeatureQuery):
    matches = query.find("the orange triangle")

    assert matches == ()


def test_query_with_only_stopwords_returns_empty_not_everything(query: FeatureQuery):
    """An empty token set must not trivially 'match' every feature."""
    matches = query.find("the a an")

    assert matches == ()


# ---------------------------------------------------------------------------
# Bare "tag" tie-break: prefer april_tag over aruco_tag.
# ---------------------------------------------------------------------------


def test_bare_tag_query_returns_only_the_april_tag_match(query: FeatureQuery):
    matches = query.find("tag")

    assert _slugs(matches) == {"apriltag-center-a1"}


def test_explicit_aruco_tag_query_returns_only_the_aruco_tag_match(query: FeatureQuery):
    matches = query.find("aruco tag")

    assert _slugs(matches) == {"aruco-northwest-u1"}


def test_april_tag_query_is_distinct_from_aruco(query: FeatureQuery):
    matches = query.find("april tag")

    assert _slugs(matches) == {"apriltag-center-a1"}


# ---------------------------------------------------------------------------
# Colour ambiguity across kinds is still disambiguated by kind tokens.
# ---------------------------------------------------------------------------


def test_blue_rectangle_and_blue_dot_are_distinct(query: FeatureQuery):
    dot_matches = query.find("blue dot")
    rect_matches = query.find("blue rectangle")

    assert _slugs(dot_matches) == {"dot-west-blue"}
    assert _slugs(rect_matches) == {"rect-southwest-blue"}


def test_square_synonym_matches_rectangle(query: FeatureQuery):
    matches = query.find("blue square")

    assert _slugs(matches) == {"rect-southwest-blue"}


# ---------------------------------------------------------------------------
# Against the real main-playfield definition loaded from disk.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_main_playfield_query() -> FeatureQuery:
    assert MAIN_PLAYFIELD_PATH.exists(), f"fixture missing: {MAIN_PLAYFIELD_PATH}"
    definition = ConfigLoader().load_playfield(MAIN_PLAYFIELD_PATH)
    return FeatureQuery(features=definition.features())


def test_real_main_playfield_blue_dot_west(real_main_playfield_query: FeatureQuery):
    matches = real_main_playfield_query.find("the blue dot")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert matches[0].position == WorldPoint(x=-50, y=0)


def test_real_main_playfield_ambiguous_orange_dot_returns_all_four(
    real_main_playfield_query: FeatureQuery,
):
    matches = real_main_playfield_query.find("the orange dot")

    assert _slugs(matches) == {
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    }


def test_real_main_playfield_orange_triangle_returns_empty(
    real_main_playfield_query: FeatureQuery,
):
    matches = real_main_playfield_query.find("the orange triangle")

    assert matches == ()


def test_real_main_playfield_bare_tag_prefers_april_tag(
    real_main_playfield_query: FeatureQuery,
):
    matches = real_main_playfield_query.find("tag")

    assert matches != ()
    assert all(f.kind == "april_tag" for f in matches)


def test_real_main_playfield_exact_slug_always_matches(
    real_main_playfield_query: FeatureQuery,
):
    for feature in real_main_playfield_query.features:
        matches = real_main_playfield_query.find(feature.slug)
        assert feature.slug in _slugs(matches), f"{feature.slug!r} did not match its own slug"
