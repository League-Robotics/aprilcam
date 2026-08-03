"""Concrete ``playfield.interfaces.FeatureQuery`` (ticket 025-001) --
the pure-logic core behind UC-08 ("where is the blue dot").

Answers "the blue dot", "blue dot west", and the exact slug
``dot-west-blue`` all reach the same feature, by reducing every
surveyed :class:`~aprilcam.config.interfaces.Feature` to a bag of
lowercase keyword tokens and reducing the query to the same shape, then
matching by **exact token-superset, no ranking, no threshold**: a
feature matches iff every one of the query's normalized tokens is
present in that feature's token set.

This is a deliberate resolution of UC-08's own open question
("ranked-best-match or all-matches-above-threshold?") in favour of
neither. It is what makes UC-08.S-2 true by construction rather than by
tie-breaking logic: "the orange dot" against ``main-playfield`` matches
four dots' token sets equally well, and *all four* come back rather
than one being silently preferred. UC-08.S-3 falls out the same way --
"the orange triangle" matches no feature's tokens, so the result is an
empty tuple, never the nearest orange feature standing in for a
triangle that does not exist.

**Token sources per feature** (adapted from v1's
``archive/src/aprilcam/core/playfield_query.py``, which operated on raw
dicts read from ``playfield.json``'s categories -- this operates on the
already-loaded ``tuple[Feature, ...]`` from
``config.interfaces.PlayfieldDefinition.features()`` instead):

* ``kind`` -- split on ``_`` (``april_tag`` -> ``april``, ``tag``) plus
  a small synonym table per kind (``aruco_tag`` -> also ``aruco``,
  ``arucotag``; ``rectangle`` -> ``rect``, ``square``, ``box``; ``dot``
  -> ``circle``, ``spot``, ``disc``, ``disk``).
* ``colour`` -- lowercased, one token, when present.
* ``slug`` -- split on ``-`` (and any other non-alphanumeric run), e.g.
  ``dot-west-blue`` -> ``dot``, ``west``, ``blue``.

**Tie-break.** When the query says "tag" without saying "aruco"/
"arucotag" and both an ``april_tag`` and an ``aruco_tag`` match, only
the ``april_tag`` match(es) are kept -- mirroring UC-09.S-3's own
family-disambiguation spirit (``main-playfield`` holds both AprilTag 1
at the centre and ArUco 1 at a corner; a bare "tag" query must not
silently blend them). Saying "aruco" explicitly needs no special-casing
at all: an ``april_tag`` feature's token set never contains "aruco", so
it is naturally excluded by the ordinary superset rule.

**Known limitation -- cardinal matching is slug-tokenization only,
not a real field.** ``Feature`` (and the loader's
``PlayfieldFeatureEntry``) deliberately carry no separate ``cardinal``
field -- see ``config/loader.py``'s own module docstring, a prior
sprint's decision this ticket does not reopen. Cardinal queries such as
"blue dot west" work here purely because ``main-playfield``'s slugs
already encode cardinal direction as a token (``dot-west-blue``,
``dot-northeast-orange``, ...). A future playfield whose own slug
convention does not encode cardinal direction would silently lose
cardinal-query support -- there is no fallback and no error raised for
it, since this module has no independent notion of "west" beyond
whatever token happens to appear in a slug. This is a recorded, open,
non-blocking gap (sprint 025's architecture review), not something this
ticket solves.

**Numeric queries are out of scope.** Unlike v1's per-record ``id``,
v2's ``Feature`` carries no numeric id at all, so "tag number one"
style queries cannot resolve here by design -- that is
``get_tag``/``get_tags``'s job (UC-09), already built. A query token
like "one" simply will not appear in any feature's token set and the
query will resolve to no match (or to whatever other tokens still
narrow the result), not to a wrong tag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aprilcam.config.interfaces import Feature
from aprilcam.playfield.interfaces import FeatureQuery as _FeatureQueryBase

__all__ = ["FeatureQuery"]

#: Query words that carry no selective meaning and are dropped before
#: matching -- otherwise "the blue dot" could never match anything,
#: since no feature's token set ever contains "the".
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "of", "at", "on", "in", "to",
        "please", "and", "for",
    }
)

#: ``Feature.kind`` -> extra keyword tokens, so a query can use natural
#: words ("square" for a rectangle, "aruco" for an aruco_tag, ...) in
#: addition to whatever ``kind.split("_")`` already yields.
_TYPE_SYNONYMS: dict[str, frozenset[str]] = {
    "april_tag": frozenset({"april", "apriltag", "tag"}),
    "aruco_tag": frozenset({"aruco", "arucotag", "tag"}),
    "rectangle": frozenset({"rect", "square", "box"}),
    "dot": frozenset({"circle", "spot", "disc", "disk"}),
}

_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> tuple[str, ...]:
    """Lowercase *text* and split on any run of non-alphanumeric characters."""
    return tuple(t for t in _SPLIT_RE.split(text.lower()) if t)


def _query_tokens(query: str) -> tuple[str, ...]:
    """Reduce a free-text query to meaningful, deduplicated tokens.

    Stopwords are dropped; order is preserved, duplicates removed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _tokenize(query):
        if tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return tuple(out)


def _feature_tokens(feature: Feature) -> frozenset[str]:
    """Build the searchable keyword token set for one surveyed feature."""
    tokens: set[str] = set()
    for part in feature.kind.split("_"):
        if part:
            tokens.add(part)
    tokens.update(_TYPE_SYNONYMS.get(feature.kind, frozenset()))
    if feature.colour:
        tokens.add(feature.colour.lower())
    tokens.update(_tokenize(feature.slug))
    return frozenset(tokens)


@dataclass(frozen=True, kw_only=True)
class FeatureQuery(_FeatureQueryBase):
    """Concrete ``FeatureQuery``. See the module docstring for the
    matching policy.

    Constructed from an already-loaded playfield's surveyed features --
    typically ``FeatureQuery(features=definition.features())`` -- so
    this module never touches a ``PlayfieldDefinition``, a camera, or
    any daemon state.
    """

    features: tuple[Feature, ...]

    def find(self, query: str) -> tuple[Feature, ...]:
        """See ``FeatureQuery.find``. Exact token-superset match, no
        ranking, no threshold; empty when no meaningful token remains in
        *query* after stopword removal (an empty token set would
        otherwise trivially "match" every feature)."""
        tokens = _query_tokens(query)
        if not tokens:
            return ()

        token_set = set(tokens)
        matches = [f for f in self.features if token_set <= _feature_tokens(f)]

        # A bare "tag" means the AprilTag, not the ArUco marker -- unless
        # the query itself said "aruco"/"arucotag", in which case the
        # ordinary superset rule already excludes april_tag features
        # (their token sets never contain "aruco").
        if not (token_set & {"aruco", "arucotag"}):
            if any(f.kind == "april_tag" for f in matches):
                matches = [f for f in matches if f.kind != "aruco_tag"]

        return tuple(matches)
