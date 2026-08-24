"""Installed-package tests -- exercise the *built, pip-installed*
``aprilcam`` distribution rather than the editable dev checkout.

Distinct from every other subpackage under ``tests/``, which imports
``aprilcam`` directly from the dev venv (editable install, full
``[daemon]`` extra, ``cv2`` always present). This subpackage instead
shells out to ``uv build``/``uv venv``/``uv pip install`` to prove
claims about what a *real* end user's ``pip install aprilcam`` actually
pulls in -- see ``test_installed_package.py`` (sprint 035, ticket 013).
"""
