"""Starlette web application — thin HTTP/WS bridge over AprilCam daemon RPCs.

Provides a ``create_app()`` factory that returns a Starlette application with:

- ``GET /`` — API discovery endpoint (HTML UI or JSON, content-negotiated).
- ``POST /api/list_cameras`` — enumerate available hardware cameras.
- ``POST /api/tags`` — latest tag frame from the daemon.
- ``POST /api/objects`` — current object detections from the daemon.
- ``POST /api/where`` — natural-language location query.
- ``GET /api/frame`` — raw JPEG passthrough from the daemon.
- ``POST /api/overlay`` — push overlay elements to the daemon.
- ``WS /ws/tags`` — live tag stream from ``GetTagStream``.

Each route is a 1:1 mapping of one daemon RPC.  No pixel work, no cv2,
no MCP-tool indirection.  The daemon is the sole vision authority.

This module does NOT start a server itself; use ``uvicorn`` or the CLI
subcommand (see ``cli.py``) to run the app.
"""

from __future__ import annotations

import asyncio
import json
import contextlib
from importlib.metadata import version as _pkg_version
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from aprilcam.client.control import DaemonControl
from aprilcam.client.discovery import resolve_daemon_target


# ---------------------------------------------------------------------------
# Daemon client singleton (stored in app state)
# ---------------------------------------------------------------------------


def _make_client(host: str, port: int, unix_path: str | None) -> DaemonControl:
    """Construct and connect a DaemonControl.  Raises on failure."""
    dc = DaemonControl(unix_path=unix_path, host=host, port=port)
    dc.connect()
    return dc


def _get_client(app: Starlette) -> DaemonControl:
    """Return the shared DaemonControl from app state.

    Connects lazily on first call; subsequent calls reuse the channel.
    Raises RuntimeError when no daemon is reachable.
    """
    dc: DaemonControl | None = getattr(app.state, "daemon_client", None)
    if dc is None:
        raise RuntimeError(
            "Daemon client not initialised — daemon unreachable at startup."
        )
    return dc


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _tag_record_to_dict(tag: Any) -> dict:
    """Convert a TagRecord (Pydantic model or dict) to a JSON-safe dict."""
    if isinstance(tag, dict):
        d = tag
    else:
        d = tag.model_dump()

    def _list(v: Any) -> list | None:
        if v is None:
            return None
        return list(v)

    return {
        "id": d["id"],
        "center_px": _list(d.get("center_px")),
        "corners_px": [_list(c) for c in (d.get("corners_px") or [])],
        "orientation_yaw": d.get("yaw"),
        "world_xy": _list(d.get("world_xy")),
        "in_playfield": d.get("in_playfield", False),
        "vel_px": _list(d.get("vel_px")),
        "speed_px": d.get("speed_px"),
        "vel_world": _list(d.get("vel_world")),
        "speed_world": d.get("speed_world"),
        "heading_rad": d.get("heading_rad"),
        "age": d.get("age", 0.0),
    }


def _tag_frame_to_dict(frame: Any) -> dict:
    """Convert a TagFrame (Pydantic model) to a JSON-safe dict."""
    d = frame.model_dump() if hasattr(frame, "model_dump") else frame
    return {
        "frame": d.get("frame_id", 0),
        "ts_mono_ns": d.get("ts_mono_ns", 0),
        "ts_wall_ms": d.get("ts_wall_ms", 0),
        "tags": [_tag_record_to_dict(t) for t in d.get("tags", [])],
        "fps": d.get("fps", 0.0),
        "field_width_cm": d.get("field_width_cm", 0.0),
        "field_height_cm": d.get("field_height_cm", 0.0),
        "origin_x": d.get("origin_x", 0.0),
        "origin_y": d.get("origin_y", 0.0),
    }


def _objects_response_to_dict(resp: Any) -> dict:
    """Convert a GetObjectsResponse proto to a JSON-safe dict."""
    objects = []
    for obj in resp.objects:
        world_xy = (
            [float(obj.wx), float(obj.wy)]
            if (obj.wx != 0.0 or obj.wy != 0.0)
            else None
        )
        objects.append({
            "center_px": [float(obj.cx_px), float(obj.cy_px)],
            "world_xy": world_xy,
            "color": obj.color,
            "bbox": [int(obj.x_bbox), int(obj.y_bbox), int(obj.w_bbox), int(obj.h_bbox)],
            "area_px": float(obj.area_px),
            "object_type": obj.object_type,
            "confidence": float(obj.confidence),
        })
    return {"objects": objects}


# ---------------------------------------------------------------------------
# Error response helper
# ---------------------------------------------------------------------------


def _error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


# ---------------------------------------------------------------------------
# HTML UI builder  (self-contained; no external assets)
# ---------------------------------------------------------------------------


def _build_html_ui() -> str:
    """Return a self-contained HTML page with embedded CSS and JavaScript."""
    return """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AprilCam Live</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #1a1a2e; color: #e0e0e0; display: flex; flex-direction: column;
         min-height: 100vh; }
  header { background: #16213e; padding: 0.75rem 1.5rem; display: flex;
           align-items: center; justify-content: space-between; }
  header h1 { font-size: 1.25rem; color: #0af; }
  .controls { display: flex; gap: 0.75rem; align-items: center; }
  select, button, input { padding: 0.4rem 0.75rem; border: 1px solid #334; border-radius: 4px;
                          background: #0d1b2a; color: #e0e0e0; font-size: 0.9rem; cursor: pointer; }
  input[type=number] { width: 5em; cursor: text; }
  .field-label { font-size: 0.8rem; color: #8af; }
  button:hover { background: #1b2838; }
  button:disabled { opacity: 0.4; cursor: default; }
  main { flex: 1; display: flex; flex-wrap: wrap; padding: 1rem; gap: 1rem; }
  .panel { background: #16213e; border-radius: 8px; padding: 1rem; }
  .video-panel { flex: 2; min-width: 320px; display: flex; flex-direction: column;
                 align-items: center; }
  .video-panel img { max-width: 100%; border-radius: 4px; background: #000; }
  .tags-panel { flex: 1; min-width: 260px; overflow-y: auto; max-height: 80vh; }
  .tags-panel h2 { font-size: 1rem; margin-bottom: 0.5rem; color: #0af; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #223; }
  th { color: #8af; }
  footer { background: #16213e; padding: 0.5rem 1.5rem; font-size: 0.8rem;
           display: flex; gap: 1.5rem; }
  .status-item { display: flex; gap: 0.3rem; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: #555; margin-top: 3px; }
  .dot.ok { background: #0f0; }
  .dot.err { background: #f33; }
  .urls-panel { flex-basis: 100%; }
  .urls-panel h2 { font-size: 1rem; margin-bottom: 0.5rem; color: #0af; }
  .url-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem;
             font-size: 0.85rem; }
  .url-label { color: #8af; min-width: 110px; }
  .url-link { color: #e0e0e0; cursor: pointer; padding: 0.2rem 0.5rem;
              background: #0d1b2a; border: 1px solid #334; border-radius: 4px;
              font-family: monospace; font-size: 0.82rem; }
  .url-link:hover { background: #1b2838; border-color: #0af; }
  .url-copied { color: #0f0; font-size: 0.75rem; opacity: 0; transition: opacity 0.2s; }
  .url-copied.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>AprilCam Live</h1>
  <div class="controls">
    <select id="cameraSelect" disabled><option>Loading cameras...</option></select>
    <button id="startBtn" disabled>View</button>
  </div>
</header>
<main>
  <div class="panel video-panel">
    <img id="liveImg" alt="No frame" width="640" height="480"
         src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
  </div>
  <div class="panel tags-panel">
    <h2>Detected Tags</h2>
    <table>
      <thead><tr><th>ID</th><th>X</th><th>Y</th><th>Yaw</th><th>Speed</th><th>WX</th><th>WY</th></tr></thead>
      <tbody id="tagBody"><tr><td colspan="7">No data</td></tr></tbody>
    </table>
  </div>
  <div class="panel urls-panel">
    <h2>Connection URLs</h2>
    <div class="url-row">
      <span class="url-label">REST API:</span>
      <span class="url-link" id="urlApi"></span>
      <span class="url-copied" id="copiedApi">copied!</span>
    </div>
    <div class="url-row">
      <span class="url-label">WebSocket:</span>
      <span class="url-link" id="urlWs"></span>
      <span class="url-copied" id="copiedWs">copied!</span>
    </div>
    <div class="url-row">
      <span class="url-label">API Discovery:</span>
      <span class="url-link" id="urlDiscovery"></span>
      <span class="url-copied" id="copiedDiscovery">copied!</span>
    </div>
  </div>
</main>
<footer>
  <div class="status-item"><span class="dot" id="frameDot"></span> Frames:
    <span id="fpsLabel">--</span> fps</div>
  <div class="status-item"><span class="dot" id="wsDot"></span> WebSocket:
    <span id="wsLabel">disconnected</span></div>
  <div class="status-item">Source: <span id="srcLabel">none</span></div>
</footer>
<script>
(function(){
  const cameraSelect = document.getElementById("cameraSelect");
  const startBtn = document.getElementById("startBtn");
  const liveImg = document.getElementById("liveImg");
  const tagBody = document.getElementById("tagBody");
  const frameDot = document.getElementById("frameDot");
  const fpsLabel = document.getElementById("fpsLabel");
  const wsDot = document.getElementById("wsDot");
  const wsLabel = document.getElementById("wsLabel");
  const srcLabel = document.getElementById("srcLabel");

  let sourceId = null;
  let frameTimer = null;
  let ws = null;
  let frameCount = 0;
  let fpsTimer = null;
  var knownTags = {};

  function renderTagTable() {
    var ids = Object.keys(knownTags).map(Number).sort(function(a, b) { return a - b; });
    if (!ids.length) {
      tagBody.innerHTML = "<tr><td colspan='7'>No data</td></tr>";
      return;
    }
    tagBody.innerHTML = ids.map(function(id) {
      var entry = knownTags[id];
      var t = entry.data;
      var style = entry.active ? "" : " style='opacity:0.4'";
      var cx = t.center_px ? t.center_px[0].toFixed(1) : "--";
      var cy = t.center_px ? t.center_px[1].toFixed(1) : "--";
      var ori = t.orientation_yaw != null ? (t.orientation_yaw * 180 / Math.PI).toFixed(1) + "°" : "--";
      var spd = t.speed_px != null ? t.speed_px.toFixed(1) + " px/s" : "--";
      var wx = t.world_xy ? t.world_xy[0].toFixed(1) : "--";
      var wy = t.world_xy ? t.world_xy[1].toFixed(1) : "--";
      return "<tr" + style + "><td>" + t.id + "</td><td>" + cx + "</td><td>" + cy + "</td><td>" + ori + "</td><td>" + spd + "</td><td>" + wx + "</td><td>" + wy + "</td></tr>";
    }).join("");
  }

  async function loadCameras() {
    try {
      const resp = await fetch("/api/list_cameras", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: "{}"});
      const data = await resp.json();
      const cams = data.cameras || [];
      cameraSelect.innerHTML = "";
      if (!cams.length) {
        cameraSelect.innerHTML = "<option>No cameras found</option>";
        return;
      }
      cams.forEach(function(c) {
        const opt = document.createElement("option");
        opt.value = c.slug || c.name;
        opt.textContent = c.name || ("Camera " + c.index);
        cameraSelect.appendChild(opt);
      });
      cameraSelect.disabled = false;
      startBtn.disabled = false;
    } catch(e) {
      cameraSelect.innerHTML = "<option>Error: " + e.message + "</option>";
    }
  }

  startBtn.addEventListener("click", function() {
    if (ws) { ws.close(); ws = null; }
    if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
    if (fpsTimer) { clearInterval(fpsTimer); fpsTimer = null; }
    sourceId = cameraSelect.value;
    srcLabel.textContent = sourceId;
    knownTags = {};
    startFramePolling();
    connectWebSocket();
  });

  function startFramePolling() {
    frameCount = 0;
    fpsLabel.textContent = "--";
    fpsTimer = setInterval(function() {
      fpsLabel.textContent = (frameCount * 2).toFixed(1);
      frameCount = 0;
    }, 500);

    async function poll() {
      if (!sourceId) return;
      try {
        const res = await fetch("/api/frame?source_id=" + encodeURIComponent(sourceId));
        if (res.ok && res.headers.get("content-type") === "image/jpeg") {
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          liveImg.src = url;
          frameCount++;
          frameDot.className = "dot ok";
        } else {
          frameDot.className = "dot err";
        }
      } catch(e) { frameDot.className = "dot err"; fpsLabel.textContent = e.message; }
      if (sourceId) frameTimer = setTimeout(poll, 500);
    }
    poll();
  }

  function connectWebSocket() {
    if (!sourceId) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/tags?source_id=" + encodeURIComponent(sourceId));
    ws.onopen = function() { wsDot.className = "dot ok"; wsLabel.textContent = "connected"; };
    ws.onclose = function() { wsDot.className = "dot"; wsLabel.textContent = "disconnected"; };
    ws.onerror = function() { wsDot.className = "dot err"; wsLabel.textContent = "error"; };
    ws.onmessage = function(evt) {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.error) { tagBody.innerHTML = "<tr><td colspan='7'>" + msg.error + "</td></tr>"; return; }
        const tags = msg.tags || [];
        Object.keys(knownTags).forEach(function(id) { knownTags[id].active = false; });
        tags.forEach(function(t) { knownTags[t.id] = { data: t, active: true }; });
        renderTagTable();
      } catch(e) {}
    };
  }

  var base = location.protocol + "//" + location.host;
  var wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  var urls = {
    Api: base + "/api/",
    Ws: wsProto + "//" + location.host + "/ws/tags?source_id=<cam>",
    Discovery: base + "/"
  };
  Object.keys(urls).forEach(function(key) {
    var el = document.getElementById("url" + key);
    var copied = document.getElementById("copied" + key);
    if (!el) return;
    el.textContent = urls[key];
    el.addEventListener("click", function() {
      navigator.clipboard.writeText(urls[key]).then(function() {
        copied.classList.add("show");
        setTimeout(function() { copied.classList.remove("show"); }, 1200);
      });
    });
  });

  loadCameras();
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API tool specs for the discovery endpoint
# ---------------------------------------------------------------------------


_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_cameras",
        "path": "/api/list_cameras",
        "method": "POST",
        "description": "Enumerate available hardware cameras (calls daemon EnumerateCameras).",
        "parameters": [],
        "returns": "{cameras: [{index, name, slug}]}",
    },
    {
        "name": "tags",
        "path": "/api/tags",
        "method": "POST",
        "description": "Get the latest detected tags from the daemon for a camera.",
        "parameters": [
            {"name": "source_id", "type": "string", "description": "Camera name (required)"},
        ],
        "returns": "{frame, tags: [...]}",
    },
    {
        "name": "objects",
        "path": "/api/objects",
        "method": "POST",
        "description": "Get current non-tag object detections from the daemon.",
        "parameters": [
            {"name": "source_id", "type": "string", "description": "Camera name (required)"},
        ],
        "returns": "{objects: [...]}",
    },
    {
        "name": "where",
        "path": "/api/where",
        "method": "POST",
        "description": "Resolve a natural-language location query via the daemon.",
        "parameters": [
            {"name": "query", "type": "string", "description": "Natural-language query (required)"},
            {"name": "source_id", "type": "string", "description": "Optional camera name for live data"},
        ],
        "returns": "{status, tokens, matches: [...]}",
    },
    {
        "name": "frame",
        "path": "/api/frame",
        "method": "GET",
        "description": "Capture a raw JPEG frame from the daemon (passthrough, no re-encode).",
        "parameters": [
            {"name": "source_id", "type": "string", "description": "Camera name (query param, required)"},
        ],
        "returns": "Binary JPEG with Content-Type: image/jpeg",
    },
    {
        "name": "overlay",
        "path": "/api/overlay",
        "method": "POST",
        "description": "Push overlay elements to the daemon for a camera.",
        "parameters": [
            {"name": "source_id", "type": "string", "description": "Camera name (required)"},
            {"name": "elements", "type": "array", "description": "List of overlay element dicts"},
            {"name": "ttl", "type": "number", "description": "Seconds before overlay expires (default 1.0)"},
        ],
        "returns": "{status: 'ok'} or {error}",
    },
]


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------


async def _discovery(request: Request) -> Response:
    """GET / — return the HTML UI or JSON discovery document."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(_build_html_ui())

    try:
        ver = _pkg_version("aprilcam")
    except Exception:
        ver = "unknown"

    return JSONResponse({
        "server": "aprilcam",
        "version": ver,
        "usage": (
            "POST JSON to /api/<endpoint> with the documented parameters. "
            "GET /api/frame?source_id=<name> for JPEG frames. "
            "WS /ws/tags?source_id=<name> for live tag stream. "
            "Error responses have {\"error\": \"...\"}."
        ),
        "endpoints": _TOOL_SPECS,
    })


async def handle_list_cameras(request: Request) -> JSONResponse:
    """POST /api/list_cameras — enumerate hardware cameras via daemon."""
    try:
        dc = _get_client(request.app)
        cameras = dc.enumerate_cameras()
        return JSONResponse({
            "cameras": [
                {"index": c.index, "name": c.name, "slug": c.slug}
                for c in cameras
            ]
        })
    except Exception as exc:
        return _error_response(str(exc), 503)


async def handle_tags(request: Request) -> JSONResponse:
    """POST /api/tags — latest tag frame for a camera."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source_id: str = body.get("source_id", "")
    if not source_id:
        return _error_response("source_id is required", 400)
    try:
        dc = _get_client(request.app)
        frame = dc.get_tags(source_id)
        result = _tag_frame_to_dict(frame)
        result["source_id"] = source_id
        return JSONResponse(result)
    except Exception as exc:
        return _error_response(str(exc), 503)


async def handle_objects(request: Request) -> JSONResponse:
    """POST /api/objects — current object detections for a camera."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source_id: str = body.get("source_id", "")
    if not source_id:
        return _error_response("source_id is required", 400)
    try:
        dc = _get_client(request.app)
        resp = dc.get_objects(source_id)
        result = _objects_response_to_dict(resp)
        result["source_id"] = source_id
        return JSONResponse(result)
    except Exception as exc:
        return _error_response(str(exc), 503)


async def handle_where(request: Request) -> JSONResponse:
    """POST /api/where — natural-language location query via daemon."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    query: str = body.get("query", "")
    source_id: str = body.get("source_id", "")
    if not query:
        return _error_response("query is required", 400)
    try:
        dc = _get_client(request.app)
        result = dc.where_is(query=query, cam_name=source_id)
        return JSONResponse(result)
    except Exception as exc:
        return _error_response(str(exc), 503)


async def handle_frame(request: Request) -> Response:
    """GET /api/frame?source_id=<name> — raw JPEG passthrough from daemon."""
    source_id: str = request.query_params.get("source_id", "")
    if not source_id:
        return _error_response("source_id query param is required", 400)
    try:
        dc = _get_client(request.app)
        jpeg: bytes = dc.capture_frame_jpeg(source_id)
        return Response(content=jpeg, media_type="image/jpeg")
    except Exception as exc:
        return _error_response(str(exc), 503)


async def handle_overlay(request: Request) -> JSONResponse:
    """POST /api/overlay — push overlay elements to the daemon."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source_id: str = body.get("source_id", "")
    elements: list = body.get("elements", [])
    ttl: float = float(body.get("ttl", 1.0))
    if not source_id:
        return _error_response("source_id is required", 400)
    try:
        dc = _get_client(request.app)
        ok = dc.publish_overlay(cam_name=source_id, elements=elements, ttl=ttl)
        return JSONResponse({"status": "ok", "accepted": ok})
    except Exception as exc:
        return _error_response(str(exc), 503)


# ---------------------------------------------------------------------------
# WebSocket endpoint — WS /ws/tags?source_id=<name>
# ---------------------------------------------------------------------------


async def ws_tags(websocket: WebSocket) -> None:
    """Stream tag detections over WebSocket.

    Connect to ``/ws/tags?source_id=<name>`` to receive one JSON message per
    tag frame containing ``source_id``, ``frame``, ``ts_mono_ns``,
    ``ts_wall_ms``, ``tags``, and playfield metadata.

    The daemon ``GetTagStream`` RPC returns a ``TagStreamConsumer`` (synchronous
    socket-based reader).  We run each blocking ``consumer.read()`` call in a
    thread executor so the asyncio event loop is not blocked.
    """
    await websocket.accept()
    source_id: str = websocket.query_params.get("source_id", "")
    if not source_id:
        await websocket.send_json({"error": "source_id query param required"})
        await websocket.close(code=1008)
        return

    try:
        dc = _get_client(websocket.app)
    except Exception as exc:
        await websocket.send_json({"error": f"Daemon unavailable: {exc}"})
        await websocket.close(code=1011)
        return

    try:
        consumer = dc.get_tag_stream(source_id)
    except Exception as exc:
        await websocket.send_json({"error": f"Cannot open tag stream: {exc}"})
        await websocket.close(code=1011)
        return

    loop = asyncio.get_event_loop()

    def _read_one():
        """Blocking read on the stream socket — runs in a thread."""
        return consumer.read()

    try:
        while True:
            try:
                msg = await loop.run_in_executor(None, _read_one)
            except EOFError:
                # Stream closed by daemon
                break
            except Exception as exc:
                await websocket.send_json({"error": str(exc)})
                break

            # msg is a TagFrame or OverlayFrame; only forward TagFrames
            from aprilcam.client.models import TagFrame as _TagFrame
            if isinstance(msg, _TagFrame):
                payload = _tag_frame_to_dict(msg)
                payload["source_id"] = source_id
                await websocket.send_text(json.dumps(payload))
            # OverlayFrame messages are daemon→viewer; skip forwarding to browser
    except WebSocketDisconnect:
        pass
    finally:
        consumer.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    daemon_host: str | None = None,
    daemon_port: int | None = None,
    daemon_unix: str | None = None,
) -> Starlette:
    """Create and return the Starlette application.

    Connection parameters override auto-discovery when provided.
    A DaemonControl singleton is created on startup and stored in
    ``app.state.daemon_client``.

    Usage::

        app = create_app()
        # Run with: uvicorn aprilcam.server.web_server:app --factory
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[type-arg]
        """Connect to the daemon on startup; close on shutdown."""
        dc: DaemonControl | None = None

        if daemon_unix or daemon_host:
            # Explicit coordinates — skip discovery
            host = daemon_host or "localhost"
            port = daemon_port or 5280
            try:
                dc = _make_client(host, port, daemon_unix)
            except Exception as exc:
                import warnings
                warnings.warn(
                    f"AprilCam daemon unreachable at startup: {exc}",
                    stacklevel=1,
                )
        else:
            # Auto-discovery via Config + resolve_daemon_target
            try:
                from aprilcam.config import Config
                config = Config.load()
                h, p, u = resolve_daemon_target(config)
                dc = _make_client(h, p, u)
            except Exception as exc:
                import warnings
                warnings.warn(
                    f"AprilCam daemon unreachable at startup: {exc}",
                    stacklevel=1,
                )

        app.state.daemon_client = dc
        try:
            yield
        finally:
            if dc is not None:
                dc.close()
            app.state.daemon_client = None

    routes = [
        Route("/", _discovery, methods=["GET"]),
        Route("/api/list_cameras", handle_list_cameras, methods=["POST"]),
        Route("/api/tags", handle_tags, methods=["POST"]),
        Route("/api/objects", handle_objects, methods=["POST"]),
        Route("/api/where", handle_where, methods=["POST"]),
        Route("/api/frame", handle_frame, methods=["GET"]),
        Route("/api/overlay", handle_overlay, methods=["POST"]),
        WebSocketRoute("/ws/tags", ws_tags),
    ]

    return Starlette(routes=routes, lifespan=lifespan)
