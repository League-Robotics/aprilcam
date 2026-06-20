---
status: draft
sprint: '015'
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 015 Use Cases

## SUC-001: MCP server imports cleanly without OpenCV

**Actor**: Developer installing `aprilcam` (base/client install, no `[daemon]` extra)

**Preconditions**: The daemon is running on another machine. The client machine has
`pipx install aprilcam` (base install, no `[daemon]` extra) — `opencv-contrib` is
not installed.

**Main Flow**:
1. Developer runs `aprilcam mcp` on the client machine.
2. `mcp_server.py` imports cleanly with no `cv2` import at module level or in any
   code path reachable during normal operation.
3. Agent connects to MCP and calls `get_tags`, `get_objects`, `stream_tags`,
   `where`, `get_frame` — all succeed via daemon RPCs.

**Postconditions**: No `ImportError` for `cv2`; MCP server functional.

**Acceptance Criteria**:
- [ ] `import aprilcam.server.mcp_server` succeeds with `cv2` monkeypatched to raise
  `ImportError`.
- [ ] No `import cv2` or `import cv2 as cv` anywhere in `mcp_server.py`.

---

## SUC-002: MCP agent gets live tag detections from daemon stream

**Actor**: AI agent using the MCP protocol

**Preconditions**: Daemon is running and detection is active on a camera.

**Main Flow**:
1. Agent calls `open_camera` → receives `camera_id`.
2. Agent calls `stream_tags(source_id=camera_id)` → MCP connects to daemon
   `GetTagStream` socket; buffers tag records (plain dicts) in a client-side ring
   buffer. No `DetectionLoop`, `AprilCam`, or `cv2` is instantiated.
3. Agent calls `get_tags(source_id=camera_id)` → MCP returns latest tag-record dict
   from the ring buffer. Gripper world-xy (pure math on dict fields) computed inline.
4. Agent calls `get_tag_history(source_id=camera_id, num_frames=30)` → MCP returns
   last 30 buffered tag-record dicts.
5. Agent calls `stop_stream(source_id=camera_id)` → MCP closes daemon stream subscription.

**Postconditions**: Agent has tag data; no in-process vision has run.

**Acceptance Criteria**:
- [ ] `stream_tags` does not instantiate `DetectionLoop`, `AprilCam`, or `DaemonCapture`.
- [ ] `get_tags` reads from a dict-based (not numpy-based) ring buffer.
- [ ] Gripper world-xy computation still works and is covered by a unit test.

---

## SUC-003: MCP agent gets detected objects from daemon

**Actor**: AI agent using the MCP protocol

**Preconditions**: Daemon running; `GetObjects` RPC available; daemon's object
detection pipeline is active for the requested camera.

**Main Flow**:
1. Agent calls `open_camera` → `camera_id`.
2. Agent calls `get_objects(source_id=camera_id)`.
3. MCP server calls daemon `GetObjects(cam_name=camera_id)` RPC → receives structured
   object records (color, center_px, world_xy, bbox) from the daemon.
4. MCP returns the object list as JSON. No `cv2`, no `ColorClassifier`, no numpy
   pixel ops in `mcp_server.py`.

**Postconditions**: Agent receives object list; all pixel work happened in the daemon.

**Acceptance Criteria**:
- [ ] `proto/aprilcam.proto` contains `GetObjects` RPC and associated messages.
- [ ] `daemon/grpc_server.py` has a `GetObjects` handler that runs `ColorClassifier`
  on the daemon camera pipeline frames.
- [ ] `_handle_get_objects` in `mcp_server.py` only calls `client.get_objects(...)`;
  no `import cv2`.

---

## SUC-004: web hub connects directly to daemon (no mcp_server import)

**Actor**: Browser client or web API consumer

**Preconditions**: Daemon is running; `web_server.py` rewritten as direct daemon client.

**Main Flow**:
1. `aprilcam web` starts; `web_server.py` creates a `DaemonControl` connection.
2. Browser calls `GET /api/tags` → web server calls `client.get_tags(cam_name)`.
3. Browser opens WebSocket `/ws/tags` → web server subscribes to `GetTagStream`,
   forwards tag frames as JSON.
4. Browser calls `GET /api/frame` → web server calls `client.capture_frame()`,
   returns JPEG bytes directly (no decode/re-encode).
5. Browser calls `GET /api/objects` → web server calls `client.get_objects()`.
6. Browser calls `POST /api/overlay` → web server calls `client.publish_overlay()`.

**Postconditions**: Web hub serves all endpoints; no `mcp_server` import; no cv2.

**Acceptance Criteria**:
- [ ] `web_server.py` does not import `mcp_server` or any symbol from it.
- [ ] `import aprilcam.server.web_server` succeeds with cv2 monkeypatched unavailable.
- [ ] REST endpoints `list_cameras`, `tags`, `objects`, `where`, `frame` respond
  correctly.
- [ ] WebSocket `/ws/tags` streams tag frames from daemon.

---

## SUC-005: aprilcam view decodes JPEG with Pillow (no cv2)

**Actor**: Developer or operator running `aprilcam view` on a client machine

**Preconditions**: Daemon is streaming JPEG frames; Pillow is installed (base dep).

**Main Flow**:
1. `aprilcam view` starts; `view_cli.py` connects to daemon's image stream.
2. JPEG bytes arrive from `ImageStreamConsumer`.
3. `view_cli.py` calls `Image.open(BytesIO(jpeg_bytes))` (Pillow) to decode; converts
   to `ImageTk.PhotoImage` for tkinter display.
4. Object box annotation (previously `cv2.rectangle`/`cv2.putText`) is replaced with
   Pillow `ImageDraw` or removed.

**Postconditions**: Live JPEG display works; no cv2 in `view_cli.py`.

**Acceptance Criteria**:
- [ ] `import aprilcam.cli.view_cli` succeeds with cv2 monkeypatched unavailable.
- [ ] `view_cli.py` contains no `import cv2` or `from cv2 ...` lines.
- [ ] Live frame display works via Pillow + tkinter.

---

## SUC-006: Base install has no opencv-contrib dependency

**Actor**: Developer running `pipx install aprilcam` (base install)

**Preconditions**: `pyproject.toml` updated so `opencv-contrib-python` is daemon-only.

**Main Flow**:
1. Developer runs `pipx install aprilcam` on a client machine without OpenCV.
2. `mcp`, `web`, `view`, `cameras`, `tags` all start without `ImportError`.
3. Running `aprilcam daemon` shows "install aprilcam[daemon]" hint as before.
4. `DAEMON_COMMANDS` in `cli/__init__.py` contains only genuinely daemon-extra
   commands: `daemon`, `taggen`, `calibrate`.

**Postconditions**: Client install is lightweight; no opencv import at start.

**Acceptance Criteria**:
- [ ] `opencv-contrib-python` appears only in `[project.optional-dependencies.daemon]`
  in `pyproject.toml`.
- [ ] `pillow` appears in `[project.dependencies]` (base).
- [ ] `mcp`, `web`, `view`, `cameras`, `tags` removed from `DAEMON_COMMANDS`.

---

## SUC-007: Docs and project rules reflect the new vision boundary

**Actor**: Future developer reading project documentation

**Preconditions**: Sprint 015 is complete.

**Main Flow**:
1. Developer reads `.claude/rules/project-overview.md` — "Image Processing Tools"
   section is absent; new boundary documented.
2. Developer reads `AGENT_GUIDE.md` / `ROBOT_API_GUIDE.md` — no mention of deleted
   tools; updated tool list.
3. `docs/wiki/*` files (if present) reflect same.

**Postconditions**: Documentation is accurate and consistent with the codebase.

**Acceptance Criteria**:
- [ ] `.claude/rules/project-overview.md` "Image Processing Tools" section removed.
- [ ] Sprint-5 roadmap entry updated or removed.
- [ ] `AGENT_GUIDE.md` and `ROBOT_API_GUIDE.md` tool lists accurate.
- [ ] `docs/wiki/` updated (if applicable).
