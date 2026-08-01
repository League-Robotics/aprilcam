"""Test suite root.

Present specifically so ``tests/aprilcam/`` — which deliberately mirrors
``src/aprilcam/``'s subpackage layout — collects under the dotted name
``tests.aprilcam...`` rather than ``aprilcam...``. Without this file,
pytest's package-name inference (walking up through ``__init__.py``
files) would stop at ``tests/`` and register the test tree in
``sys.modules`` as top-level ``aprilcam``, shadowing the real installed
package of the same name. The repo-root ``testdev/`` (ad hoc developer
tooling, not a pytest suite -- see ``testdev/conftest.py``, relocated
out of ``tests/`` by ticket 021-001) lives outside this tree entirely
and is unaffected either way.
"""
