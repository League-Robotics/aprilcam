---
id: '004'
title: Rewrite web_server.py as direct daemon client
status: done
use-cases:
- SUC-004
depends-on:
- '003'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Rewrite web_server.py as direct daemon client

## Description

Rewrite `src/aprilcam/server/web_server.py` to connect directly to the daemon via
`DaemonControl` instead of importing and delegating to `mcp_server.py`. The file
currently imports 15+ symbols from `mcp_server` and calls `_handle_*` functions.
After this ticket it will import zero symbols from `mcp_server`.

The rewritten `web_server.py` is a thin HTTP/WebSocket bridge over daemon RPCs:
each endpoint is a 1:1 mapping of one daemon RPC call. No pixel work, no cv2,
no MCP-tool indirection.

Depends on ticket 003 because `web_server.py` currently imports `detection_registry`
from `mcp_server` for the WebSocket streaming path. After ticket 003, that registry
has changed structure; this ticket rewires the web layer to not use the MCP registry
at all.

## Acceptance Criteria

- [x] `web_server.py` contains no `from aprilcam.server.mcp_server import ...` line.
- [x] `web_server.py` contains no `import cv2` line.
- [x] `import aprilcam.server.web_server` succeeds with cv2 monkeypatched to raise
  `ImportError`.
- [x] HTTP endpoints exist and respond correctly (tested against a mock
  `DaemonControl`):
  - `POST /api/list_cameras` → calls `client.enumerate_cameras()`
  - `POST /api/tags` (body: `{source_id}`) → calls `client.get_tags(cam_name)`
  - `POST /api/objects` (body: `{source_id}`) → calls `client.get_objects(cam_name)`
  - `POST /api/where` (body: `{query, source_id}`) → calls `client.where_is(...)`
  - `GET /api/frame` (query: `source_id`) → calls `client.capture_frame()`, returns
    JPEG bytes with `Content-Type: image/jpeg` (no decode, no re-encode)
  - `POST /api/overlay` → calls `client.publish_overlay(...)`
- [x] WebSocket endpoint `WS /ws/tags` subscribes to `GetTagStream`, receives
  `TagFrame` messages, and forwards them as JSON to the browser client.
- [x] `GET /` API discovery endpoint returns the updated tool list (deleted tools
  absent).
- [x] `uv run pytest` green.

## Implementation Plan

### New `web_server.py` structure

```python
"""Starlette web application — thin HTTP/WS bridge over AprilCam daemon RPCs."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from aprilcam.client.control import DaemonControl
from aprilcam.client.discovery import resolve_daemon_target
from aprilcam.client.stream import TagStreamConsumer
from aprilcam.config import Config


def _get_client() -> DaemonControl:
    """Return a connected DaemonControl; raises if daemon unreachable."""
    config = Config.load()
    host, port, unix_path = resolve_daemon_target(config)
    dc = DaemonControl(host=host, port=port, unix_path=unix_path)
    dc.connect()
    return dc
```

Each handler function resolves a client and makes one RPC call.

### Endpoint handlers

```python
async def handle_list_cameras(request: Request) -> JSONResponse:
    dc = _get_client()
    resp = dc.enumerate_cameras()
    cameras = [{"index": c.index, "name": c.name, "slug": c.slug}
               for c in resp.cameras]
    return JSONResponse({"cameras": cameras})

async def handle_tags(request: Request) -> JSONResponse:
    body = await request.json()
    source_id = body.get("source_id", "")
    dc = _get_client()
    resp = dc.get_tags(source_id)
    return JSONResponse(_tag_frame_to_dict(resp))

async def handle_objects(request: Request) -> JSONResponse:
    body = await request.json()
    source_id = body.get("source_id", "")
    dc = _get_client()
    resp = dc.get_objects(source_id)
    return JSONResponse(_objects_response_to_dict(resp))

async def handle_where(request: Request) -> JSONResponse:
    body = await request.json()
    dc = _get_client()
    resp = dc.where_is(query=body.get("query", ""),
                       cam_name=body.get("source_id", ""))
    return JSONResponse(_where_response_to_dict(resp))

async def handle_frame(request: Request) -> Response:
    source_id = request.query_params.get("source_id", "")
    dc = _get_client()
    resp = dc.capture_frame(source_id)
    return Response(content=resp.jpeg, media_type="image/jpeg")

async def handle_overlay(request: Request) -> JSONResponse:
    body = await request.json()
    dc = _get_client()
    # Build PublishOverlayRequest from body dict; call dc.publish_overlay(...)
    ...
    return JSONResponse({"status": "ok"})
```

### WebSocket handler (`WS /ws/tags`)

```python
async def ws_tags(websocket: WebSocket) -> None:
    await websocket.accept()
    source_id = websocket.query_params.get("source_id", "")
    dc = _get_client()
    endpoint = dc.get_tag_stream(source_id)
    consumer = TagStreamConsumer(...)
    try:
        async for tag_frame_dict in consumer.aiter():
            await websocket.send_text(json.dumps(tag_frame_dict))
    except WebSocketDisconnect:
        pass
    finally:
        consumer.stop()
```

If `TagStreamConsumer` is synchronous (runs a background thread), wrap it with
`asyncio.get_event_loop().run_in_executor` or use `asyncio.Queue` to bridge.

### Routing

```python
def create_app() -> Starlette:
    routes = [
        Route("/", endpoint=handle_discovery),
        Route("/api/list_cameras", endpoint=handle_list_cameras, methods=["POST"]),
        Route("/api/tags", endpoint=handle_tags, methods=["POST"]),
        Route("/api/objects", endpoint=handle_objects, methods=["POST"]),
        Route("/api/where", endpoint=handle_where, methods=["POST"]),
        Route("/api/frame", endpoint=handle_frame, methods=["GET"]),
        Route("/api/overlay", endpoint=handle_overlay, methods=["POST"]),
        WebSocketRoute("/ws/tags", endpoint=ws_tags),
    ]
    return Starlette(routes=routes)
```

### Helper serializers

Add private functions `_tag_frame_to_dict`, `_objects_response_to_dict`,
`_where_response_to_dict` to convert proto responses to plain dicts. These
replace the `_handle_*` delegates from `mcp_server`.

### DaemonControl connection management

`_get_client()` should use a module-level singleton or app state (Starlette `app.state`)
so a new gRPC channel is not created per request. Wire `app.state.daemon_client` in
an `on_startup` handler.

### Testing (`tests/test_015_004_web_server.py`)

Mock `DaemonControl` with `unittest.mock.MagicMock`. Test:
- `POST /api/tags` returns JSON with `tags` key.
- `GET /api/frame` returns `image/jpeg` content type.
- `POST /api/objects` returns JSON with `objects` key.
- `import aprilcam.server.web_server` succeeds with cv2 blocked.

Use Starlette `TestClient` for HTTP tests.

### Files to modify/create

- `src/aprilcam/server/web_server.py` — full rewrite
- `tests/test_015_004_web_server.py` — new test file
- Any existing web server tests — update or replace
