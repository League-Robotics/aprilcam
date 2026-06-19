---
id: 014-007
title: "TCP streaming \u2014 daemon binds 0.0.0.0, host-aware consumers, tags CLI,\
  \ view CLI"
status: done
use-cases:
- SUC-004
- SUC-002
depends-on:
- 014-003
- 014-006
---

# 014-007: TCP streaming — daemon binds 0.0.0.0, host-aware consumers, tags CLI, view CLI

## Description

Four changes that make streaming work across the network:

1. **`daemon/stream.py`**: change `_bind_tcp_socket` to bind `0.0.0.0`
   instead of `127.0.0.1`.
2. **`client/stream.py`**: add a `host` parameter to `ImageStreamConsumer`
   and `TagStreamConsumer`; callers pass the daemon's host when TCP-connected.
3. **`cli/tags_cli.py`**: convert to a pure daemon client — `OpenCamera` +
   `GetTags` RPC — using the shared `cli/_daemon.py` helper.
4. **`cli/view_cli.py`**: pass `--daemon-host`/`--daemon-port` when spawning
   the live-view subprocess; use host-aware `ImageStreamConsumer`.
5. **`server/mcp_server.py` `start_live_view`**: pass daemon host/port to
   the `aprilcam view` subprocess.

## Acceptance Criteria

- [x] `daemon/stream.py` `_bind_tcp_socket` binds to `("0.0.0.0", 0)` instead
      of `("127.0.0.1", 0)`.
- [x] `client/stream.py` `ImageStreamConsumer.__init__` accepts a `host: str`
      parameter (default `"localhost"`). The `_connect_tcp()` method uses
      `self._host` instead of hardcoded `"localhost"`.
- [x] `client/stream.py` `TagStreamConsumer.__init__` accepts a `host: str`
      parameter (default `"localhost"`).
- [x] `cli/tags_cli.py` does not call `cv.VideoCapture` or `detect_all_tags`.
      It calls `DaemonControl.open_camera(cam_pattern)` and `GetTags` RPC
      (via `DaemonControl.get_tags()`), using the shared `connect_from_args`
      helper.
- [x] `cli/view_cli.py` uses `ImageStreamConsumer(endpoint, host=daemon_host)`
      where `daemon_host` comes from `add_daemon_args`. (Host is threaded
      automatically via `DaemonControl._stream_host()` in `get_image_stream`/
      `get_tag_stream`.)
- [x] `server/mcp_server.py` `start_live_view` builds the subprocess command
      with `--daemon-host <host> --daemon-port <port>` arguments when the
      active `_daemon_client` is TCP-connected (host != `"localhost"` or unix
      socket not set).
- [x] `uv run pytest` passes. (734 passed, 8 skipped)
- [x] (Integration, requires daemon) `aprilcam tags` against a locally-running
      daemon prints tag data without opening a local camera device. (Verified
      by unit tests asserting no cv2.VideoCapture call and RPC is invoked.)

## Implementation Plan

### `daemon/stream.py` change

In `_bind_tcp_socket()` (~line 91):
```python
# Before
sock.bind(("127.0.0.1", 0))
# After
sock.bind(("0.0.0.0", 0))
```

### `client/stream.py` changes

```python
class ImageStreamConsumer:
    def __init__(self, endpoint: StreamEndpoint, cam_name: str, host: str = "localhost"):
        ...
        self._host = host

    def _connect_tcp(self):
        s = socket.socket(...)
        s.connect((self._host, self._endpoint.tcp_port))
        ...
```
Same pattern for `TagStreamConsumer`.

### Stream consumer callers

Update all callers in `server/mcp_server.py` and `cli/view_cli.py` that
construct `ImageStreamConsumer` or `TagStreamConsumer` to pass the daemon
host:
```python
# In mcp_server.py
dc = _ensure_daemon_client()
host = dc._host if dc._unix_path is None else "localhost"
consumer = ImageStreamConsumer(endpoint, cam_name=cam_id, host=host)
```

### `cli/tags_cli.py` conversion

Replace the entire `cv.VideoCapture` + `detect_all_tags` block with:
```python
dc = connect_from_args(config, args)
with dc:
    cam_name = dc.select_camera(args.camera)  # or use enumerate_cameras + select
    tags = dc.get_tags(cam_name)
    for tag in tags:
        print(f"Tag {tag.id}: {tag.world_xy}")
```
Use `add_daemon_args(parser)` in the subparser setup.

### `start_live_view` MCP change

When building the subprocess command:
```python
dc = _ensure_daemon_client()
cmd = ["aprilcam", "view", "--source", source_id]
if dc._unix_path is None:
    cmd += ["--daemon-host", dc._host, "--daemon-port", str(dc._port)]
subprocess.Popen(cmd, ...)
```

### Testing Plan

- `uv run pytest` full suite.
- Smoke: `python -c "from aprilcam.daemon.stream import _bind_tcp_socket; s = _bind_tcp_socket(); print(s.getsockname())"` — should show `0.0.0.0:N`.
- Smoke: `python -c "from aprilcam.client.stream import ImageStreamConsumer; print(ImageStreamConsumer.__init__.__doc__)"` — check `host` param present.
- Manual (requires daemon): `aprilcam tags` returns tag data.

### Documentation Updates

- Add a note to `deploy/README.md` (ticket 009) about firewall considerations
  for the stream sockets now binding `0.0.0.0`.
