"""Keep ``tests/testdev/`` out of pytest collection (ticket 020-007).

``testdev/`` holds ad hoc developer tooling -- standalone scripts run
directly (``python testdev/detect_test.py --camera 2``, per that
script's own module docstring), not a pytest suite. ``detect_test.py``
incidentally matches pytest's default ``*_test.py`` discovery glob,
though, so a plain ``pytest tests/`` collects it as a module. It has no
``test_*`` functions -- there is nothing for pytest to actually run --
but it does an unconditional top-level ``import cv2``, so on any
install without the ``daemon`` extra (this project's base install is
explicitly cv2-free; see ``docs/design/design.md`` and this repo's
``project-overview.md`` "Key Constraints") collection fails outright
with ``ModuleNotFoundError: No module named 'cv2'`` before a single
real test runs -- a whole-suite ``pytest`` should never depend on
optional extras just to *collect*.

``collect_ignore`` (not a broader ``python_files`` change in
``pyproject.toml``) is deliberate: it excludes exactly this one
non-test file from collection without narrowing the discovery glob
project-wide, which would risk silently hiding a genuine test file
that happens to end in ``_test.py`` somewhere else in the tree.
"""

from __future__ import annotations

collect_ignore = ["detect_test.py"]
