"""Tool-group modules for the AprilCam MCP server (sprint 026).

Each module here (``perception``, and later ``configuration``,
``mobile_tags``, ``annotations``) exposes exactly one
:func:`register(mcp, connection, temp_dir) <perception.register>`-shaped
entry point, called once from ``mcp/server.py``'s ``build_server`` with
the ``FastMCP`` instance and the two locals every tool group shares
(sprint.md's Architecture Step 3 ``mcp/tools/*`` rows): a
:class:`~aprilcam.mcp.connection.ConnectionManager` to resolve a
``Daemon`` through, and the per-process temp directory image-returning
tools write ``format="file"`` output under.
"""

from __future__ import annotations
