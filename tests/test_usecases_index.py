"""Regression test for ``clasi/usecases.md`` (ticket 036-001, issue
``get-use-case-coverage-reports-zero-reads-the-wrong-file.md``).

CLASI's ``get_use_case_coverage()`` MCP tool (implemented outside this
repo, in the installed ``clasi`` package) reads ``clasi/usecases.md``
and parses lines matching ``##\\s+(UC-\\d+):\\s*(.+)`` to build its
``total_use_cases`` count. Before ticket 036-001, that file did not
exist, so the tool always reported zero and several sprint exit
criteria that treat it as a traceability audit passed vacuously.
``scripts/generate_usecases_index.py`` now generates
``clasi/usecases.md`` from the real use-case headings in
``docs/design/use-cases/*.md``.

This test does **not** import that generator. It re-implements the
heading parse independently, straight from
``docs/design/use-cases/*.md``, so a bug shared between the generator
and a test that merely called back into it would not go undetected --
and so that a future use case added, renamed, or removed in
``docs/design/use-cases/`` without regenerating the index fails this
test rather than silently drifting the index back toward matching
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
USE_CASES_DIR = REPO_ROOT / "docs" / "design" / "use-cases"
USECASES_MD = REPO_ROOT / "clasi" / "usecases.md"

#: Independent re-derivation of the source heading shape, e.g.
#: "### UC-01 -- Start the daemon and configure it". Deliberately not
#: shared with scripts/generate_usecases_index.py's _HEADING_RE.
_SOURCE_HEADING_RE = re.compile(r"^###\s+(UC-\d+)\s*—\s*(.+?)\s*$")

#: The exact regex clasi/tools/process_tools.py's get_use_case_coverage()
#: uses against clasi/usecases.md -- duplicated here (not imported;
#: that module lives in a different, installed package) so this test
#: fails if the index ever stops matching what the real tool parses.
_COVERAGE_REGEX = re.compile(r"##\s+(UC-\d+):\s*(.+)")

#: README.md is the authoring template -- its own
#: "### UC-nn -- <goal, phrased as the actor's outcome>" heading is a
#: placeholder, not a real use case. The two *-interface-trace.md
#: files are generated traceability artifacts that reference UC ids
#: rather than defining them. Neither is a source of index entries.
_EXCLUDED_NAMES = frozenset({"README.md"})
_EXCLUDED_SUFFIXES = ("-interface-trace.md",)


def _expected_use_cases() -> List[Tuple[str, str]]:
    """Independently parse docs/design/use-cases/*.md for the real
    UC id/title pairs, sorted in ascending numeric order."""
    entries = {}
    for path in sorted(USE_CASES_DIR.glob("*.md")):
        if path.name in _EXCLUDED_NAMES or path.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        for line in path.read_text().splitlines():
            match = _SOURCE_HEADING_RE.match(line)
            if match:
                entries[match.group(1)] = match.group(2)
    return sorted(entries.items(), key=lambda item: int(item[0].split("-")[1]))


def _indexed_use_cases() -> List[Tuple[str, str]]:
    """Parse clasi/usecases.md with the exact regex
    get_use_case_coverage() uses, in file order."""
    content = USECASES_MD.read_text()
    return [(m.group(1), m.group(2).strip()) for m in _COVERAGE_REGEX.finditer(content)]


def test_usecases_md_exists() -> None:
    assert USECASES_MD.is_file(), (
        "clasi/usecases.md is missing -- run "
        "`uv run python scripts/generate_usecases_index.py` to generate it"
    )


def test_source_use_cases_are_found() -> None:
    """Sanity check on this test's own independent parse, so a
    regex/exclusion bug here doesn't silently pass by comparing empty
    lists to empty lists."""
    expected = _expected_use_cases()
    assert expected, (
        f"expected at least one '### UC-NN -- Title' heading under "
        f"{USE_CASES_DIR}"
    )
    assert len(expected) == len(set(uc_id for uc_id, _ in expected)), (
        "duplicate UC ids found across docs/design/use-cases/*.md"
    )


def test_usecases_md_matches_source_use_cases_exactly() -> None:
    """clasi/usecases.md's UC id/title entries -- as CLASI's own
    get_use_case_coverage() regex would parse them -- must exactly
    match docs/design/use-cases/*.md's real UC-NN headings, in
    ascending numeric order.

    If this fails, either a use case was added, renamed, or removed
    under docs/design/use-cases/ without regenerating the index, or
    clasi/usecases.md was hand-edited out of sync. Fix by running:

        uv run python scripts/generate_usecases_index.py
    """
    expected = _expected_use_cases()
    actual = _indexed_use_cases()
    assert actual == expected


def test_usecases_md_total_matches_real_use_case_count() -> None:
    """Directly exercises the acceptance criterion that
    get_use_case_coverage()'s total_use_cases count (== the number of
    ##-level UC entries parsed from clasi/usecases.md) equals the real
    number of use cases documented under docs/design/use-cases/."""
    expected = _expected_use_cases()
    actual = _indexed_use_cases()
    assert len(actual) == len(expected)
    assert len(actual) > 0
