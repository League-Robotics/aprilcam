#!/usr/bin/env python3
"""Compile proto/aprilcam.proto → src/aprilcam/proto/aprilcam_pb2*.py.

Run from the project root:
    python scripts/compile_proto.py

After generation the script fixes the absolute import in aprilcam_pb2_grpc.py
so it works as a relative import inside the aprilcam.proto package.
"""

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTO_SRC    = PROJECT_ROOT / "proto"
PROTO_FILE   = PROTO_SRC / "aprilcam.proto"
OUT_DIR      = PROJECT_ROOT / "src" / "aprilcam" / "proto"


def compile_proto() -> None:
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{PROTO_SRC}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        str(PROTO_FILE),
    ]
    print("Running:", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        sys.exit(result.returncode)
    print("Proto compilation succeeded.")


def fix_grpc_imports() -> None:
    """grpc_tools generates an absolute import like:
         import aprilcam_pb2 as aprilcam__pb2
    inside aprilcam_pb2_grpc.py.  When the file lives inside the
    aprilcam.proto package that import fails.  Replace with relative form.
    """
    grpc_file = OUT_DIR / "aprilcam_pb2_grpc.py"
    if not grpc_file.exists():
        print(f"WARNING: {grpc_file} not found — skipping import fix.")
        return

    text = grpc_file.read_text()
    fixed = re.sub(
        r"^import aprilcam_pb2 as aprilcam__pb2",
        "from . import aprilcam_pb2 as aprilcam__pb2",
        text,
        flags=re.MULTILINE,
    )
    if fixed == text:
        print("Import fix: pattern not found — file may already be correct.")
    else:
        grpc_file.write_text(fixed)
        print("Import fix applied to aprilcam_pb2_grpc.py.")


if __name__ == "__main__":
    compile_proto()
    fix_grpc_imports()
    print("Done.")
