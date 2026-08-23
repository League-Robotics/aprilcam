"""L2 acceptance layer -- tests against the real daemon and real field
state (sprint 035 ticket-group 4, ``UC-29``).

Present for the same reason ``tests/__init__.py`` and
``tests/aprilcam/__init__.py`` are: without it, pytest's package-name
inference would register this tree at the top-level name ``acceptance``
instead of the dotted ``tests.acceptance...``, and sibling modules
(e.g. ``tests/aprilcam/daemon/conftest.py``'s own precedent of
importing helpers straight out of a directory's ``conftest.py``) rely on
the dotted form being importable.

Every test collected under this directory carries all three of
``acceptance``+``camera``+``integration`` markers (enforced by
``conftest.py``'s ``pytest_collection_modifyitems`` hook, not left to
convention) so the existing gate's ``-m "not camera and not
integration"`` exclusion keeps excluding this whole layer without ever
being edited (sprint.md Decision 4).
"""
