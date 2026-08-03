"""Tool-group modules for the AprilCam MCP server (sprint 026).

Each module here (``perception``, ``configuration``, ``mobile_tags``,
``annotations``) exposes exactly one
:func:`register(mcp, connection, temp_dir) <perception.register>`-shaped
entry point, called once from ``mcp/server.py``'s ``build_server`` with
the ``FastMCP`` instance and the two locals every tool group shares
(sprint.md's Architecture Step 3 ``mcp/tools/*`` rows): a
:class:`~aprilcam.mcp.connection.ConnectionManager` to resolve a
``Daemon`` through, and the per-process temp directory image-returning
tools write ``format="file"`` output under.

**Deliberately no ``from __future__ import annotations`` here** (ticket
026-005's finding) -- this file has no type annotations of its own to
postpone evaluation of, and that specific import is the one statement
in the language that also *binds a name*: ``from __future__ import X``
sets ``X`` in the importing module's namespace to the matching
``__future__._Feature`` object, not just a compiler flag. With ``X ==
"annotations"`` in *this* package's ``__init__``, that bound
``aprilcam.mcp.tools.annotations`` to a ``_Feature`` instance before the
``mcp/tools/annotations.py`` submodule was ever imported -- so
``mcp/server.py``'s ``from aprilcam.mcp.tools import annotations``
resolved the wrong object via a successful ``getattr`` on the package
and never triggered the submodule import at all (``AttributeError:
'_Feature' object has no attribute 'register'``, not an import error,
which is what made it non-obvious). Keep this file free of that
particular future-import for as long as a same-named ``tools/
annotations.py`` submodule exists.
"""
