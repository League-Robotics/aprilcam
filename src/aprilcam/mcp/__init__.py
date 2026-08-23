"""``aprilcam.mcp`` — the MCP server: a pure daemon client speaking MCP
tool calls over stdio (ticket 026-001; ``docs/design/adapters.md``'s
"MCP server" row; ``docs/design/specification.md`` §4.7).

Same adapter discipline every other translation layer in this package
states in its own module docstring (``client/interfaces.py``,
``daemon/interfaces.py``): this package is a **translation layer and
nothing more**. It converts between MCP tool calls and
``client.interfaces.Daemon``/``Discovery``/``Guides`` — the only
sanctioned way any adapter talks to a daemon (``client/interfaces.py``'s
own docstring). Concretely:

* **No authoritative state.** Anything that must survive this server's
  restart lives in the daemon or on disk — ``adapters.md`` rule 1. The
  connection cache in :mod:`aprilcam.mcp.connection` is a pure
  performance optimisation (skip a redundant reconnect), never a source
  of truth; losing it costs one extra ``Discovery.connect`` call, never
  correctness.
* **No vision.** Nothing under this package imports ``cv2`` —
  ``adapters.md`` rule 2, mechanically enforced by
  ``tests/test_no_cv2_in_client_modules.py``'s scan of this package
  (extended to include ``"mcp"`` by this same ticket). Image-returning
  tools pass already-encoded bytes straight through
  (:mod:`aprilcam.mcp.images`); nothing here decodes or re-encodes a
  pixel.
* **Talks only to the daemon, never to another adapter** — ``adapters.md``
  rule 3.
* **A new tool must not require a new daemon capability.** Every tool
  this sprint adds calls an RPC that already exists on
  ``daemon.interfaces.Daemon`` — ``adapters.md`` rule 5, and the signal
  the boundary between this package and the daemon is drawn correctly.

One exception, documented where it lives rather than here: ``get_guide``
(:mod:`aprilcam.mcp.server`) reads ``client.guides.Guides`` directly,
with no ``Discovery``/``Daemon`` involved at all — the one capability
that must work before any daemon exists (UC-27.S-1), mirroring
``cli/guide.py``'s identical, equally deliberate exception.
"""

from __future__ import annotations
