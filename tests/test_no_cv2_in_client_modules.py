"""Static discipline gate: no ``cv2`` import anywhere under the
client-side packages (ticket 022-002).

``docs/design/adapters.md`` rule 2 -- "adapters do no vision" -- and
``client/interfaces.py``'s own module docstring ("Contains no OpenCV...
no client module may import it") have stated this as a design rule
since sprint 020, but nothing enforced it mechanically; this is that
enforcement, added by ticket 022-002 alongside the new ``view/``
subsystem (which the ticket brief specifically calls out as needing
the same discipline -- the daemon serves already-encoded frames, and
``view/viewer.py`` decodes them with Pillow, never ``cv2``).

An AST scan rather than an import-and-inspect-``sys.modules`` check
(``tests/test_typing.py``'s subprocess style would also work, but is
heavier for a check this simple): it catches a forbidden import even
if the import is never reached at runtime by whatever this test
happens to exercise, and needs no daemon, camera, or display.

Scope is exactly the client-side packages named in ``adapters.md``/
``specification.md`` as needing to stay ``cv2``-free: ``client/`` (the
substrate every adapter sits on) and ``view/`` (this ticket's new
subsystem, built entirely on ``client/``). ``camera/``, ``vision/``,
and ``daemon/`` are the vision authority and legitimately import
``cv2`` -- scanning them would make this test fail by design, not
catch a real regression.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "aprilcam"

#: Packages this test asserts are cv2-free. See the module docstring
#: for why exactly these two and not the whole tree.
_CV2_FREE_PACKAGES = ("client", "view")


def _python_files(package: str) -> Iterator[Path]:
    package_dir = SRC_ROOT / package
    assert package_dir.is_dir(), f"expected a package at {package_dir}"
    yield from package_dir.rglob("*.py")


def _cv2_imports(path: Path) -> List[str]:
    """Names of any ``cv2``/``cv2.*`` imports in *path*, as they'd read
    in the source (e.g. ``"import cv2"``, ``"from cv2 import aruco"``).
    Empty if none."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "cv2" or alias.name.startswith("cv2."):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "cv2" or (node.module or "").startswith("cv2."):
                found.append(f"from {node.module} import ...")
    return found


@pytest.mark.parametrize("package", _CV2_FREE_PACKAGES)
def test_package_imports_no_cv2(package: str) -> None:
    offenders = {}
    for path in _python_files(package):
        imports = _cv2_imports(path)
        if imports:
            offenders[str(path.relative_to(REPO_ROOT))] = imports

    assert not offenders, (
        f"aprilcam.{package} must stay cv2-free (adapters.md rule 2 / "
        f"client/interfaces.py's module docstring), but found:\n"
        + "\n".join(f"  {file}: {imports}" for file, imports in offenders.items())
    )
