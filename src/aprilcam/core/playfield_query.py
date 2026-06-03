"""aprilcam.core.playfield_query — natural-language "where is X" resolution.

This module powers the ``where`` MCP tool and the ``WhereIs`` daemon RPC.
It loads the static playfield map (``playfield.json``) and answers
natural-language location questions in two stages:

1. **Full-text keyword search.**  Every feature (AprilTag, ArUco tag,
   rectangle, dot) is reduced to a bag of keyword tokens drawn from its
   ``type``, ``color``, ``cardinal``, ``slug`` (tag name), ``id`` and
   ``size`` fields.  The compound tag types (``april_tag`` / ``aruco_tag``)
   are split so the user can say "april tag" or "aruco tag", and a small
   set of synonyms (eastern→east, square→rectangle, one→1, …) widens the
   net.  A feature matches when *every* meaningful query token is present
   in its token set.

2. **LLM fallback (caller-driven).**  When the keyword search finds no
   match, :func:`where` returns ``status="not_found"``.  The caller (the
   MCP tool / daemon wrapper) then hands the raw query plus the whole
   ``playfield.json`` back to the calling agent, which resolves the
   reference and re-invokes the tool with an exact slug.

The location reported for each match is the static world position from
``playfield.json`` (cm, A1-centred — origin at AprilTag 1, +x east,
+y north).  Callers may additionally merge a live detected position via
the ``live_tags`` argument.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional


# Categories in playfield.json that hold lists of feature records.
_FEATURE_CATEGORIES = ("april_tags", "aruco_tags", "rectangles", "dots")

# Query words that carry no selective meaning and are dropped before matching.
_STOPWORDS = frozenset(
    {
        "where", "is", "are", "the", "a", "an", "of", "at", "on", "in", "to",
        "me", "my", "whats", "what", "s", "find", "locate", "show", "tell",
        "please", "and", "located", "location", "position", "number", "no",
        "nbr", "num", "marker", "labeled", "label", "named", "name", "with",
        "id", "thats", "that", "this", "near", "around", "you", "we", "can",
    }
)

# Extra keyword tokens added for each feature type so the user can use
# natural words ("square" for a rectangle, "aruco" for an aruco_tag, …).
_TYPE_SYNONYMS = {
    "april_tag": {"april", "apriltag", "tag"},
    "aruco_tag": {"aruco", "arucotag", "tag"},
    "rectangle": {"rectangle", "rect", "square", "box"},
    "dot": {"dot", "circle", "spot", "disc", "disk"},
}

# Cardinal-direction synonyms mapped to the canonical cardinal token.
_CARDINAL_SYNONYMS = {
    "northern": "north",
    "southern": "south",
    "eastern": "east",
    "western": "west",
    "upper": "north",
    "lower": "south",
    "left": "west",
    "right": "east",
    "top": "north",
    "bottom": "south",
    "centre": "center",
    "middle": "center",
    "nw": "northwest",
    "ne": "northeast",
    "sw": "southwest",
    "se": "southeast",
}

# Spelled-out numbers → digit string, so "tag number one" matches id 1.
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_playfield_path(data_dir: Path) -> Path:
    """Return the conventional playfield.json path under *data_dir*."""
    return Path(data_dir) / "playfield.json"


def load_playfield(path: Path) -> dict:
    """Load and parse ``playfield.json`` from *path*.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the file is not valid JSON.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"playfield map not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"playfield map is not valid JSON: {exc}") from exc


def iter_features(playfield: dict) -> list[dict]:
    """Flatten all feature categories into one list of record dicts.

    Each returned dict is the original record plus a ``"category"`` key
    naming the list it came from (e.g. ``"dots"``).
    """
    features: list[dict] = []
    for category in _FEATURE_CATEGORIES:
        for record in playfield.get(category, []) or []:
            if isinstance(record, dict):
                features.append({**record, "category": category})
    return features


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


def _split(text: str) -> list[str]:
    """Lowercase *text* and split on any non-alphanumeric run."""
    return [t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t]


def _normalise_query_token(tok: str) -> str:
    """Map a single query token through the number/cardinal synonym tables."""
    if tok in _NUMBER_WORDS:
        return _NUMBER_WORDS[tok]
    if tok in _CARDINAL_SYNONYMS:
        return _CARDINAL_SYNONYMS[tok]
    return tok


def tokenize_query(query: str) -> list[str]:
    """Reduce a natural-language query to meaningful, normalised tokens.

    Stopwords are dropped; number words and cardinal synonyms are mapped
    to their canonical forms.  Order is preserved and duplicates removed.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _split(query):
        if raw in _STOPWORDS:
            continue
        tok = _normalise_query_token(raw)
        if tok in _STOPWORDS or not tok:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def feature_tokens(feature: dict) -> set[str]:
    """Build the searchable keyword token set for a single feature.

    Tokens are drawn from the feature's ``type`` (split on ``_`` and
    expanded with type synonyms), ``color``, ``cardinal`` (with synonym),
    ``size``, ``slug`` (split on ``-`` — this yields the tag name such as
    ``u1`` / ``a1``) and ``id``.
    """
    toks: set[str] = set()

    ftype = str(feature.get("type", ""))
    for part in ftype.split("_"):
        if part:
            toks.add(part.lower())
    toks.update(_TYPE_SYNONYMS.get(ftype, set()))

    color = feature.get("color")
    if color:
        toks.add(str(color).lower())

    cardinal = feature.get("cardinal")
    if cardinal:
        toks.add(str(cardinal).lower())

    size = feature.get("size")
    if size:
        toks.add(str(size).lower())

    for part in _split(feature.get("slug", "")):
        toks.add(part)

    fid = feature.get("id")
    if fid is not None:
        toks.add(str(fid))

    return toks


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _location(feature: dict) -> Optional[dict]:
    """Return the static world location dict for *feature*, or None."""
    if "x" in feature and "y" in feature:
        return {"x": feature["x"], "y": feature["y"], "units": "cm", "frame": "a1-centred"}
    return None


def _is_tag(feature: dict) -> bool:
    return str(feature.get("type", "")).endswith("_tag")


def _match_dict(
    feature: dict,
    live_tags: Optional[dict[int, dict]] = None,
) -> dict:
    """Build the per-match result dict for *feature*, merging live data."""
    record = {k: v for k, v in feature.items() if k != "category"}
    match: dict[str, Any] = {
        "slug": feature.get("slug"),
        "type": feature.get("type"),
        "category": feature.get("category"),
        "location": _location(feature),
        "record": record,
    }
    # Merge a live detected position when this feature is a tag whose id
    # is currently being detected.
    if live_tags and _is_tag(feature):
        fid = feature.get("id")
        try:
            live = live_tags.get(int(fid)) if fid is not None else None
        except (TypeError, ValueError):
            live = None
        if live is not None:
            match["live_detection"] = live
    return match


def where(
    query: str,
    features: Iterable[dict],
    live_tags: Optional[dict[int, dict]] = None,
) -> dict:
    """Resolve a natural-language location query against *features*.

    Args:
        query: The user's question, e.g. ``"where is the blue dot"``.
        features: Feature records (see :func:`iter_features`).
        live_tags: Optional ``{tag_id: {...}}`` map of live detections to
            merge into matched tag features.

    Returns:
        A dict with ``status`` one of:

          - ``"ok"``        — exactly one feature matched; ``matches`` has 1 entry.
          - ``"ambiguous"`` — several features matched all query tokens;
                              ``matches`` lists the candidates.
          - ``"not_found"`` — no feature matched (caller should fall back to
                              the LLM by handing over the full playfield map).

        Always includes ``query`` and ``tokens`` (the normalised search
        tokens).  Each entry in ``matches`` carries ``slug``, ``type``,
        ``location``, the full ``record`` and (for live tags) a
        ``live_detection`` block.
    """
    tokens = tokenize_query(query)
    token_set = set(tokens)
    feature_list = list(features)

    scored: list[tuple[int, dict]] = []
    if tokens:
        for feat in feature_list:
            keywords = feature_tokens(feat)
            if token_set <= keywords:  # every query token present
                scored.append((len(token_set & keywords), feat))

    if not scored:
        return {"status": "not_found", "query": query, "tokens": tokens, "matches": []}

    # A bare "tag" reference means the AprilTag, not the ArUco marker. When the
    # query says "tag" without explicitly saying "aruco" and both an AprilTag
    # and an ArUco tag match, keep only the AprilTag(s).
    if not (token_set & {"aruco", "arucotag"}):
        if any(f.get("type") == "april_tag" for _s, f in scored):
            scored = [(s, f) for s, f in scored if f.get("type") != "aruco_tag"]

    matches = [_match_dict(feat, live_tags) for _score, feat in scored]
    status = "ok" if len(matches) == 1 else "ambiguous"
    return {"status": status, "query": query, "tokens": tokens, "matches": matches}
