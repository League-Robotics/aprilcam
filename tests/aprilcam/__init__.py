"""Tests for the ``aprilcam`` v2 package (headless by default).

Mirrors ``src/aprilcam/``'s subpackage layout — one test subpackage per
source subpackage. Tests requiring real capture hardware are marked
``@pytest.mark.camera`` (registered in ``pyproject.toml``) and are
expected to skip cleanly on machines with no camera attached.
"""
