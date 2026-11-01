"""Image-output shaping: already-encoded bytes in, ``base64``-inline-or-
``file`` MCP content out (ticket 026-001; sprint.md's Architecture Step
2 "Image output shaping" responsibility and Step 3 ``mcp/images.py``
row). Used by ``get_frame`` (ticket 002) and any other tool that hands
back image bytes; factored out on its own because output-format policy
is a distinct concern from *which* bytes to fetch in the first place.

**Never both, never an unrequested path.** Spec principle 7 — "clients
see handles and services, never file paths" — plus the issue's explicit
statement that this applies doubly to an MCP agent, which typically has
no filesystem of its own to make sense of a path in: ``format="base64"``
returns an inline ``ImageContent`` and writes nothing to disk;
``format="file"`` writes to disk and returns *only* that path, never
both, and never a path the caller did not explicitly ask for.

**No decode, no re-encode, no cv2.** *data* passes through byte-for-byte
from wherever it was already encoded — ``Daemon.get_frame``'s JPEG/PNG
bytes, unchanged. Base64-encoding those bytes for ``format="base64"``
is wire transport, not a pixel operation: MCP's own ``ImageContent``
shape requires base64 text, the same way HTTP requires a Content-Length
header — it is not decoding or re-encoding the image itself, and this
module never imports ``cv2`` or opens the bytes as an image at all.
"""

from __future__ import annotations

import base64
import tempfile
import uuid
from pathlib import Path
from typing import Union

from mcp.types import ImageContent, TextContent

__all__ = ["create_temp_dir", "image_response"]

#: Encoding name (as ``Daemon.get_frame``'s ``encoding`` parameter and
#: ``ImageFrame.encoding`` use it, e.g. ``"jpeg"``, ``"png"``) -> MIME
#: type for ``ImageContent.mimeType``. An encoding not listed here still
#: works (falls back to ``image/{encoding}``) rather than raising --
#: this module has no authority to reject an encoding the daemon
#: already produced and sent.
_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
}


def create_temp_dir() -> Path:
    """A fresh, per-server-process temp directory for ``format="file"``
    output — created **once** at server startup
    (``mcp/server.py``'s ``build_server``) and reused for the life of
    the process.

    One named subdirectory (``tempfile.mkdtemp(prefix="aprilcam-mcp-")``),
    not the bare system temp root (sprint.md's Design Rationale): a long
    session's file output stays grouped under one path, easy to find and
    trivially bulk-removable by an operator without touching unrelated
    temp files. No automatic cleanup this sprint — files accumulate for
    the life of the directory, matching v1's behaviour and the sprint's
    explicit Open Questions entry deferring a TTL policy.
    """
    return Path(tempfile.mkdtemp(prefix="aprilcam-mcp-"))


def _mime_type(encoding: str) -> str:
    return _MIME_TYPES.get(encoding.lower(), f"image/{encoding.lower()}")


def image_response(
    data: bytes, *, encoding: str, format: str, temp_dir: Path, placeholder: bool = False
) -> Union[ImageContent, TextContent]:
    """Shape already-encoded *data* into the MCP content *format* asks
    for.

    ``format="base64"`` returns an inline :class:`~mcp.types.ImageContent`
    (``data`` base64-encoded, ``mimeType`` derived from *encoding*) —
    nothing touches disk. ``format="file"`` writes *data* unchanged to a
    new file under *temp_dir* and returns a :class:`~mcp.types.TextContent`
    whose text is *only* that path — no bytes, no extra metadata. Any
    The returned block's ``_meta`` always carries ``{"placeholder":
    <bool>}`` (sprint 047) so a caller can tell a daemon-served NO SIGNAL
    test card from a real camera frame without inspecting pixels. Any
    other *format* raises ``ValueError`` naming the two valid values; a
    calling tool's own error-mapping (``mcp/errors.py``) turns that into
    a normal tool-level error like any other bad input, same as every
    other argument-validation failure.
    """
    if format == "base64":
        encoded = base64.b64encode(data).decode("ascii")
        return ImageContent(
            type="image",
            data=encoded,
            mimeType=_mime_type(encoding),
            _meta={"placeholder": placeholder},
        )
    if format == "file":
        suffix = f".{encoding.lower()}" if encoding else ""
        path = temp_dir / f"aprilcam-{uuid.uuid4().hex}{suffix}"
        path.write_bytes(data)
        return TextContent(type="text", text=str(path), _meta={"placeholder": placeholder})
    raise ValueError(f'format must be "base64" or "file", got {format!r}')
