"""
aprilcam.daemon.stream — ImageStreamProducer and TagStreamProducer.

Each producer owns a set of server sockets (Unix and/or TCP) for one
stream type.  Clients connect and receive length-prefixed protobuf
messages.  No other daemon code creates or reads stream sockets.

Wire framing (same for both producers)
---------------------------------------
    [ uint32 big-endian length (4 bytes) ][ protobuf payload ]

ImageStreamProducer publishes ``aprilcam_pb2.ImageFrame`` messages.
TagStreamProducer publishes ``aprilcam_pb2.TagFrame`` messages with
adaptive rate control and a 1-second heartbeat.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import queue
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aprilcam.proto import aprilcam_pb2
from aprilcam.client.models import StreamEndpoint, TagFrame

log = logging.getLogger(__name__)

# ── Wire framing ─────────────────────────────────────────────────────────────

_LENGTH_FMT = ">I"   # 4-byte big-endian unsigned int
_LENGTH_SIZE = struct.calcsize(_LENGTH_FMT)

_MAX_SENDER_QUEUE = 2   # drop silently when full; prevents slow subscribers
                         # from blocking the pipeline


def _frame_bytes(proto_msg) -> bytes:
    """Serialize proto_msg wrapped in StreamMessage to length-prefixed bytes."""
    if isinstance(proto_msg, aprilcam_pb2.TagFrame):
        wrapper = aprilcam_pb2.StreamMessage(tag_frame=proto_msg)
    elif isinstance(proto_msg, aprilcam_pb2.OverlayFrame):
        wrapper = aprilcam_pb2.StreamMessage(overlay=proto_msg)
    else:
        raise TypeError(f"Unsupported message type: {type(proto_msg)}")
    payload = wrapper.SerializeToString()
    prefix = struct.pack(_LENGTH_FMT, len(payload))
    return prefix + payload


# ── Socket helpers ────────────────────────────────────────────────────────────


def _bind_unix_socket(path: Path, *, backlog: int = 5) -> Optional[socket.socket]:
    """Create and bind a UNIX stream socket at *path*, retrying on EADDRINUSE."""
    path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(2):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            sock.listen(backlog)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE and attempt == 0:
                log.warning("Stale socket %s — removing and retrying", path)
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            log.error("Cannot bind Unix socket %s: %s", path, exc)
            return None
    return None  # unreachable; satisfies type checker


def _bind_tcp_socket(*, backlog: int = 5) -> Optional[socket.socket]:
    """Bind a TCP stream socket on an OS-assigned ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(backlog)
        return sock
    except OSError as exc:
        sock.close()
        log.error("Cannot bind TCP socket: %s", exc)
        return None


# ── _BaseProducer ─────────────────────────────────────────────────────────────


class _BaseProducer:
    """Common accept-loop and fan-out logic for stream producers.

    Subclasses must implement nothing extra; they call ``_publish_bytes``
    to broadcast to all connected clients.
    """

    def __init__(
        self,
        cam_name: str,
        socket_dir: Path,
        stream_prefix: str,
        *,
        enable_unix: bool = True,
        enable_tcp: bool = True,
    ) -> None:
        self._cam_name = cam_name
        self._socket_dir = socket_dir
        self._stream_prefix = stream_prefix
        self._enable_unix = enable_unix
        self._enable_tcp = enable_tcp

        self._stop_event = threading.Event()

        # Listening sockets
        self._unix_sock: Optional[socket.socket] = None
        self._unix_path: Optional[Path] = None
        self._tcp_sock: Optional[socket.socket] = None
        self._tcp_port: int = 0

        # Per-connection sender queues  conn → Queue[bytes]
        self._senders: Dict[socket.socket, queue.Queue] = {}
        self._senders_lock = threading.Lock()

        self._threads: List[threading.Thread] = []

    # ── Public lifecycle ──────────────────────────────────────────────────────

    def start(self) -> StreamEndpoint:
        """Create socket(s) and start accept-loop thread(s).

        Returns:
            ``StreamEndpoint`` describing how clients can connect.
        """
        socket_path: Optional[str] = None
        tcp_port: int = 0

        unique_suffix = str(uuid.uuid4())[:8]

        if self._enable_unix:
            path = (
                self._socket_dir
                / self._cam_name
                / f"{self._stream_prefix}-{unique_suffix}.sock"
            )
            sock = _bind_unix_socket(path)
            if sock is not None:
                self._unix_sock = sock
                self._unix_path = path
                socket_path = str(path)
                self._start_accept_thread(sock, name=f"accept-unix-{self._cam_name}")

        if self._enable_tcp:
            sock = _bind_tcp_socket()
            if sock is not None:
                self._tcp_sock = sock
                self._tcp_port = sock.getsockname()[1]
                tcp_port = self._tcp_port
                self._start_accept_thread(sock, name=f"accept-tcp-{self._cam_name}")

        return StreamEndpoint(
            socket_path=socket_path,
            tcp_port=tcp_port if tcp_port != 0 else None,
        )

    def stop(self) -> None:
        """Signal the accept loops and all sender threads to shut down."""
        self._stop_event.set()

        for sock in (self._unix_sock, self._tcp_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        if self._unix_path is not None:
            try:
                self._unix_path.unlink()
            except OSError:
                pass

        # Wake and discard all sender queues so their threads can exit
        with self._senders_lock:
            for q in self._senders.values():
                try:
                    q.put_nowait(None)  # sentinel
                except queue.Full:
                    pass

        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> Optional[str]:
        return str(self._unix_path) if self._unix_path else None

    @property
    def tcp_port(self) -> int:
        return self._tcp_port

    # ── Internal ─────────────────────────────────────────────────────────────

    def _start_accept_thread(
        self, server_sock: socket.socket, *, name: str
    ) -> None:
        t = threading.Thread(
            target=self._accept_loop,
            args=(server_sock,),
            name=name,
            daemon=True,
        )
        t.start()
        self._threads.append(t)

    def _accept_loop(self, server_sock: socket.socket) -> None:
        server_sock.settimeout(1.0)
        while not self._stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            q: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=_MAX_SENDER_QUEUE)
            with self._senders_lock:
                self._senders[conn] = q

            t = threading.Thread(
                target=self._sender_loop,
                args=(conn, q),
                name=f"sender-{self._cam_name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _sender_loop(
        self, conn: socket.socket, q: queue.Queue
    ) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    data = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if data is None:  # sentinel from stop()
                    break
                try:
                    conn.sendall(data)
                except OSError:
                    break
        finally:
            with self._senders_lock:
                self._senders.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass

    def has_subscribers(self) -> bool:
        """Return True when at least one subscriber is currently connected."""
        with self._senders_lock:
            return bool(self._senders)

    def _publish_bytes(self, data: bytes) -> None:
        """Enqueue *data* for all active connections (drop if queue full)."""
        with self._senders_lock:
            queues = list(self._senders.values())
        for q in queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # slow consumer — silent drop


# ── ImageStreamProducer ───────────────────────────────────────────────────────


class ImageStreamProducer(_BaseProducer):
    """Publishes ``ImageFrame`` protobuf messages to all connected subscribers.

    Usage::

        producer = ImageStreamProducer("cam0", config)
        endpoint = producer.start()
        ...
        producer.publish(frame_id, ts_mono_ns, ts_wall_ms, jpeg, width, height)
        ...
        producer.stop()
    """

    def __init__(
        self,
        cam_name: str,
        config,
        *,
        enable_unix: bool = True,
        enable_tcp: bool = True,
    ) -> None:
        """
        Args:
            cam_name:    Camera name slug (used in socket path).
            config:      ``Config`` instance supplying ``socket_dir``.
            enable_unix: Create a Unix-domain socket when True.
            enable_tcp:  Create a TCP socket on an ephemeral port when True.
        """
        super().__init__(
            cam_name,
            config.socket_dir,
            stream_prefix="images",
            enable_unix=enable_unix,
            enable_tcp=enable_tcp,
        )

    def publish(
        self,
        frame_id: int,
        ts_mono_ns: int,
        ts_wall_ms: int,
        jpeg: bytes,
        width: int,
        height: int,
    ) -> None:
        """Serialize and broadcast an ``ImageFrame`` to all subscribers.

        Args:
            frame_id:    Monotonic frame counter (shared with TagStreamProducer).
            ts_mono_ns:  ``time.monotonic_ns()`` at capture.
            ts_wall_ms:  ``int(time.time() * 1000)`` at capture.
            jpeg:        JPEG-encoded frame bytes.
            width:       Frame width in pixels.
            height:      Frame height in pixels.
        """
        msg = aprilcam_pb2.ImageFrame(
            frame_id=frame_id,
            ts_mono_ns=ts_mono_ns,
            jpeg=jpeg,
            width=width,
            height=height,
        )
        payload = msg.SerializeToString()
        prefix = struct.pack(_LENGTH_FMT, len(payload))
        self._publish_bytes(prefix + payload)


# ── TagStreamProducer ─────────────────────────────────────────────────────────


class TagStreamProducer(_BaseProducer):
    """Publishes ``TagFrame`` protobuf messages with adaptive rate control.

    Publish policy
    --------------
    - Publishes immediately when any tag moved > ``change_threshold_px`` px,
      or any tag entered / left the scene — subject to ``max_hz`` rate cap.
    - Fires a heartbeat publish every 1 second when no change has occurred.

    Usage::

        producer = TagStreamProducer("cam0", config)
        endpoint = producer.start()
        ...
        producer.publish_if_changed(tag_frame_proto)
        ...
        producer.stop()
    """

    def __init__(
        self,
        cam_name: str,
        config,
        max_hz: float = 20.0,
        change_threshold_px: float = 8.0,
        *,
        enable_unix: bool = True,
        enable_tcp: bool = True,
    ) -> None:
        """
        Args:
            cam_name:            Camera name slug.
            config:              ``Config`` instance.
            max_hz:              Maximum publish rate for changes.  0 = unlimited.
            change_threshold_px: Minimum tag movement (pixels) to trigger publish.
            enable_unix:         Create a Unix-domain socket when True.
            enable_tcp:          Create a TCP socket when True.
        """
        super().__init__(
            cam_name,
            config.socket_dir,
            stream_prefix="tags",
            enable_unix=enable_unix,
            enable_tcp=enable_tcp,
        )
        self._max_hz = float(max_hz)
        self._change_threshold_px = float(change_threshold_px)

        # State for change detection — maps tag id → (cx_px, cy_px)
        self._last_positions: Dict[int, Tuple[float, float]] = {}
        self._last_positions_lock = threading.Lock()

        # Rate limiting
        self._last_publish_ts: float = 0.0
        self._last_publish_lock = threading.Lock()

        # Heartbeat state: last TagFrame proto seen (for heartbeat callback)
        self._latest_tag_frame: Optional[aprilcam_pb2.TagFrame] = None
        self._latest_lock = threading.Lock()

        self._heartbeat_thread: Optional[threading.Thread] = None

    # ── Lifecycle override ────────────────────────────────────────────────────

    def start(self) -> StreamEndpoint:
        endpoint = super().start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"tag-heartbeat-{self._cam_name}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return endpoint

    def stop(self) -> None:
        super().stop()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    # ── Public publish API ────────────────────────────────────────────────────

    def publish_if_changed(self, tag_frame: aprilcam_pb2.TagFrame) -> None:
        """Publish *tag_frame* if a significant change is detected.

        A "significant change" is:
        - Any tag id entered or left the scene, or
        - Any tag moved more than ``change_threshold_px`` pixels.

        Even if a change is detected, the publish is rate-limited to
        ``max_hz``.  When ``max_hz == 0``, every change publishes.

        Also updates the latest frame for the heartbeat thread.
        """
        with self._latest_lock:
            self._latest_tag_frame = tag_frame

        if self._has_changed(tag_frame):
            if self._rate_ok():
                self._do_publish(tag_frame)

    def force_publish(self, tag_frame: aprilcam_pb2.TagFrame) -> None:
        """Publish *tag_frame* unconditionally (used by heartbeat timer).

        Updates change-detection state so the heartbeat does not reset the
        timer incorrectly.
        """
        self._do_publish(tag_frame)

    def publish_overlay(self, overlay_frame: aprilcam_pb2.OverlayFrame) -> None:
        """Broadcast overlay_frame immediately to all subscribers, bypassing rate limiting."""
        self._publish_bytes(_frame_bytes(overlay_frame))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _has_changed(self, tag_frame: aprilcam_pb2.TagFrame) -> bool:
        """Return True if *tag_frame* differs from the last published frame."""
        new_positions: Dict[int, Tuple[float, float]] = {
            tag.id: (tag.cx_px, tag.cy_px) for tag in tag_frame.tags
        }

        with self._last_positions_lock:
            old_positions = self._last_positions

        # Different set of tag IDs visible?
        if set(new_positions.keys()) != set(old_positions.keys()):
            return True

        # Any tag moved more than the threshold?
        for tag_id, (nx, ny) in new_positions.items():
            ox, oy = old_positions[tag_id]
            dist = math.sqrt((nx - ox) ** 2 + (ny - oy) ** 2)
            if dist > self._change_threshold_px:
                return True

        return False

    def _rate_ok(self) -> bool:
        """Return True if enough time has elapsed since the last publish."""
        if self._max_hz <= 0.0:
            return True
        min_interval = 1.0 / self._max_hz
        with self._last_publish_lock:
            return (time.monotonic() - self._last_publish_ts) >= min_interval

    def _do_publish(self, tag_frame: aprilcam_pb2.TagFrame) -> None:
        """Serialize and broadcast *tag_frame*; update rate and position state."""
        now = time.monotonic()

        with self._last_publish_lock:
            self._last_publish_ts = now

        # Update change-detection state
        new_positions: Dict[int, Tuple[float, float]] = {
            tag.id: (tag.cx_px, tag.cy_px) for tag in tag_frame.tags
        }
        with self._last_positions_lock:
            self._last_positions = new_positions

        self._publish_bytes(_frame_bytes(tag_frame))

    def _heartbeat_loop(self) -> None:
        """Fire force_publish every ~1 second when no change publish has occurred."""
        _HEARTBEAT_INTERVAL = 1.0
        while not self._stop_event.is_set():
            time.sleep(0.1)  # check at 10 Hz for responsiveness

            with self._last_publish_lock:
                elapsed = time.monotonic() - self._last_publish_ts

            if elapsed >= _HEARTBEAT_INTERVAL:
                with self._latest_lock:
                    frame = self._latest_tag_frame
                if frame is not None:
                    self.force_publish(frame)
