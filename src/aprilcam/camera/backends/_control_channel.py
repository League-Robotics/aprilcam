"""Control channel: the settings/metadata half of the per-camera capture
worker's IPC (sprint 038 ticket 003) -- ``get_property``/``set_property``/
``settable_names``/``frame_size`` request/response round-trips between a
``WorkerBackend`` (ticket 004) and its ``capture_worker.py`` child.

Small, synchronous, versioned JSON messages over a
``multiprocessing.connection.Connection`` (what ``multiprocessing.Pipe()``
returns) rather than the connection's own native pickle-based
``send``/``recv`` -- JSON keeps the wire format documented, inspectable,
and stable across a Python version bump on either end, which a raw
pickle stream is not. ``Connection.send_bytes``/``recv_bytes`` already
do length-prefixed framing for us, so this module only has to encode/
decode the payload, not implement its own framing.

**Heartbeat lives in the frame ring, not here** (see
``_frame_ring.py``'s ``FrameRing.peek()``) -- the sprint's
``design/camera-DESIGN.md`` overlay §4 is explicit that the heartbeat
is carried in the shared-memory header (sequence number + timestamp) so
a liveness check never has to compete with this channel's own request/
response traffic or the capture loop's cadence. This module is request/
response only.

**Roles**: a :class:`ControlChannel` is the client (``WorkerBackend``
side) that issues ``request()`` calls and blocks for a matching
response, optionally with a timeout. A :class:`ControlHandler` is the
server-side contract (``capture_worker.py`` side) that answers one of
the four known methods; :func:`serve_one` reads exactly one request off
a connection, dispatches it to a handler, and writes back exactly one
response -- the building block ``capture_worker.py``'s eventual
request loop (ticket 004) will call repeatedly. Kept as a single
blocking step, not a loop owning its own thread, so this ticket's tests
can drive it deterministically without racing a background loop.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from multiprocessing.connection import Connection

__all__ = [
    "CONTROL_PROTOCOL_VERSION",
    "ControlChannel",
    "ControlError",
    "ControlHandler",
    "ControlProtocolError",
    "ControlTimeout",
    "serve_one",
]

#: Bumped only if the wire message shape changes incompatibly. Carried
#: in every request so a version mismatch between parent and child
#: (e.g. a daemon upgraded mid-flight while an old worker is still
#: running) is a loud, named protocol error rather than a confusing
#: KeyError deep in json decoding.
CONTROL_PROTOCOL_VERSION = 1

#: The four request methods this channel knows about (sprint 038's
#: camera-DESIGN.md overlay §4). Deliberately a closed set, not an
#: open-ended RPC mechanism -- a capture worker's control surface is
#: exactly ``CaptureBackend.frame_size`` plus ``ControlBackend``'s
#: three settings methods, nothing more.
_METHODS = ("get_property", "set_property", "settable_names", "frame_size")


class ControlError(RuntimeError):
    """The remote handler reported an ordinary failure for a request
    (e.g. an unsupported property name) -- raised client-side by
    :meth:`ControlChannel.request` so a caller can ``except`` it
    without inspecting a response dict by hand."""


class ControlProtocolError(ControlError):
    """The wire protocol itself was violated -- an unknown method, a
    version mismatch, a malformed payload, or a response whose ``id``
    does not match the request that was sent. Distinct from
    :class:`ControlError` because this signals a bug (protocol drift,
    a stray/duplicate response) rather than an ordinary "that property
    doesn't exist" outcome."""


class ControlTimeout(ControlError):
    """No response arrived within the caller's timeout."""


class ControlHandler:
    """Server-side contract a capture-worker-style process implements to
    answer control-channel requests. A real base class, not
    ``typing.Protocol`` (project interface rule) -- each method raises
    ``NotImplementedError`` by default; a concrete handler (a fake in
    this ticket's tests, ``capture_worker.py`` for real in ticket 004)
    overrides all four.

    Mirrors ``camera/backends/__init__.py``'s ``CaptureBackend``/
    ``ControlBackend`` method shapes deliberately -- this is the same
    contract, carried across a process boundary instead of called
    in-process.
    """

    def get_property(self, name: str) -> Optional[float]:
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlHandler.get_property")

    def set_property(self, name: str, value: float) -> bool:
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlHandler.set_property")

    def settable_names(self) -> List[str]:
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlHandler.settable_names")

    def frame_size(self) -> Optional[Tuple[int, int]]:
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlHandler.frame_size")


def _send(conn: Connection, message: Dict[str, Any]) -> None:
    conn.send_bytes(json.dumps(message).encode("utf-8"))


def _recv(conn: Connection, *, timeout: Optional[float]) -> Optional[Dict[str, Any]]:
    if timeout is not None and not conn.poll(timeout):
        return None
    payload = conn.recv_bytes()
    result: Dict[str, Any] = json.loads(payload.decode("utf-8"))
    return result


def _dispatch(handler: ControlHandler, method: str, params: Dict[str, Any]) -> Any:
    if method == "get_property":
        return handler.get_property(params["name"])
    if method == "set_property":
        return handler.set_property(params["name"], params["value"])
    if method == "settable_names":
        return list(handler.settable_names())
    if method == "frame_size":
        return handler.frame_size()
    raise ControlProtocolError(f"unknown control channel method {method!r}")


class ControlChannel:
    """Client side of the control channel: issues a request, blocks for
    the matching response.

    One :class:`ControlChannel` per :class:`~multiprocessing.connection.
    Connection` end. Request ids are assigned locally and echoed back by
    the response, so a caller can tell a delayed/duplicate response
    (never expected in the synchronous one-request-in-flight usage this
    ticket tests, but cheap to check) from the one it is actually
    waiting for.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._next_id = 1

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, timeout: Optional[float] = None) -> Any:
        """Send *method*(*params*) and return its result.

        Raises :class:`ControlTimeout` if no response arrives within
        *timeout* seconds (``None`` means wait indefinitely),
        :class:`ControlProtocolError` for a malformed/mismatched
        response, and :class:`ControlError` for an ordinary
        handler-reported failure.
        """
        if method not in _METHODS:
            raise ControlProtocolError(f"unknown control channel method {method!r}")
        request_id = self._next_id
        self._next_id += 1
        _send(
            self._conn,
            {"v": CONTROL_PROTOCOL_VERSION, "id": request_id, "method": method, "params": params or {}},
        )
        response = _recv(self._conn, timeout=timeout)
        if response is None:
            raise ControlTimeout(f"control channel request {method!r} (id={request_id}) timed out")
        if response.get("id") != request_id:
            raise ControlProtocolError(
                f"control channel response id {response.get('id')!r} does not match "
                f"request id {request_id!r}"
            )
        if not response.get("ok", False):
            raise ControlError(response.get("error") or "control channel request failed with no error detail")
        return response.get("result")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ControlChannel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def serve_one(conn: Connection, handler: ControlHandler, *, timeout: Optional[float] = None) -> bool:
    """Read and answer exactly one request from *conn*, or do nothing
    and return ``False`` if none arrives within *timeout* seconds
    (``None`` blocks until one does).

    Returns ``True`` if a request was answered (whether the handler
    succeeded or raised -- either way a response was sent), ``False``
    only for the "nothing arrived within timeout" case. This is the
    single-step primitive; ``capture_worker.py``'s real request loop
    (ticket 004) calls this repeatedly from its own poll loop.
    """
    request = _recv(conn, timeout=timeout)
    if request is None:
        return False
    request_id = request.get("id")
    method = str(request.get("method"))
    params = request.get("params") or {}
    try:
        result = _dispatch(handler, method, params)
        _send(conn, {"v": CONTROL_PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result})
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any handler
        # failure must become an error *response*, never an exception
        # that kills the worker's request-serving loop.
        _send(conn, {"v": CONTROL_PROTOCOL_VERSION, "id": request_id, "ok": False, "error": str(exc)})
    return True
