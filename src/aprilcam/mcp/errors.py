"""Error mapping: ``aprilcam.errors`` -> MCP tool-level errors (ticket
026-001; sprint.md's Architecture Step 3 ``mcp/errors.py`` row and its
Design Rationale "a single shared error-mapping layer" entry). The
MCP-side analogue of ``daemon/rpc.py``'s ``_STATUS_CODES``/``_abort``
pair, one layer further out: that module maps typed exceptions to gRPC
status codes at the wire boundary; this one maps the same taxonomy to
an MCP tool-level failure at the tool-call boundary, so no individual
tool function in ``mcp/tools/*`` (tickets 002-005) re-derives "is this
fatal" or formats its own message.

**The concrete SDK mechanism (confirmed 2026-08-03 against the pinned
``mcp==1.26.0`` package this project actually installs — read from the
SDK's own source, not guessed, per this ticket's brief):**

Raising an exception from inside a function registered with
``@FastMCP.tool()`` produces a clean ``CallToolResult(isError=True,
content=[TextContent(text=...)])`` — never a crashed transport, never
an exception that escapes past the server into the agent's connection.
Two layers make this true:

1. ``mcp.server.fastmcp.tools.base.Tool.run`` wraps *any* exception
   raised by the tool function body in
   ``mcp.server.fastmcp.exceptions.ToolError`` (prefixing the message
   with ``"Error executing tool {name}: "``).
2. ``mcp.server.lowlevel.server.Server.call_tool``'s registered request
   handler wraps the whole tool invocation in a ``try/except Exception``
   that converts *any* exception (already a ``ToolError`` by the time
   it gets here, but the handler does not care) into
   ``self._make_error_result(str(e))`` — a ``CallToolResult`` with
   ``isError=True`` and the message as its one text content block.

This was verified directly, not inferred from reading alone: a
throwaway ``FastMCP`` instance with a tool that raises ``ToolError``,
called through the *actual* registered ``CallToolRequest`` handler
(``mcp.server.lowlevel.server``'s ``_mcp_server.request_handlers``),
produced exactly ``CallToolResult(isError=True, content=[TextContent(
text="Error executing tool boom: <message>")])`` — no traceback, no
raised exception observable outside the handler, the server object
still fully usable for the next call. An unrelated, un-mapped exception
(a bare ``ValueError``) produced the identical shape. So: **raising**
is the mechanism this module uses — never a hand-rolled ``{"error":
...}`` sentinel return (v1's pattern), which the SDK does not need and
this codebase does not replicate.

Because *any* exception is caught and reported this way regardless of
type, this module still raises the SDK's own ``ToolError`` rather than
a bare ``Exception`` subtype — it is the SDK's documented vocabulary
for "this is a deliberate tool-level failure," even though functionally
(in this SDK version) the outcome would be identical either way; using
the blessed type is what stays correct if a future SDK version treats
different exception types differently at this boundary.

**Error-mapping table** (sprint.md's Architecture Design Rationale,
reproduced here since this module is its implementation): every
``aprilcam.errors.AprilCamError`` the client library raises is passed
through as ``str(exc)`` verbatim — each type's own ``__init__`` already
builds an agent-legible message (naming what was requested and what is
available, per ``errors.py``'s own module docstring). Any other
exception becomes a generic ``"internal error: {type name}"`` tool
failure; the *original* exception, with its full traceback, is logged
server-side (``logger.exception``) and never echoed to the agent —
never a leaked traceback or stack frame, per this ticket's Definition
of Done.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError

from aprilcam.errors import AprilCamError

__all__ = ["handle_tool_errors"]

logger = logging.getLogger(__name__)


def handle_tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap *fn* (an MCP tool function's body) so every
    ``aprilcam.errors`` exception it raises becomes a clean tool-level
    error carrying that exception's own already agent-legible message,
    and every other exception becomes a generic internal error with no
    leaked traceback — see the module docstring for the mapping table
    and the confirmed SDK mechanism this implements.

    Every tool function in ``mcp/tools/*`` (tickets 002-005) applies
    this decorator; ``mcp/server.py``'s ``get_guide`` does too, proving
    the mechanism end to end against a real tool call.

    Transparent to sync and async tool functions alike: the returned
    wrapper is a coroutine function iff *fn* is, so FastMCP's own
    sync/async detection (``Tool.from_function`` ->
    ``_is_async_callable``) sees the same shape after wrapping as
    before — and ``functools.wraps`` preserves *fn*'s signature via
    ``__wrapped__``, so FastMCP's argument-schema introspection still
    sees the real parameter list, not ``(*args, **kwargs)`` (verified
    directly against a wrapped tool's generated ``inputSchema``).
    """

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except AprilCamError as exc:
                raise ToolError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - boundary: map every error
                logger.exception("unhandled error in MCP tool %r", fn.__name__)
                raise ToolError(f"internal error: {type(exc).__name__}") from exc

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AprilCamError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - boundary: map every error
            logger.exception("unhandled error in MCP tool %r", fn.__name__)
            raise ToolError(f"internal error: {type(exc).__name__}") from exc

    return _sync_wrapper
