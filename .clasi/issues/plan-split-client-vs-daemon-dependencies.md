---
status: pending
---

# Plan: Split client vs. daemon dependencies

## Context

Today `pip install aprilcam` forces **every** consumer to pull the full
heavyweight stack — `opencv-contrib-python` (~90 MB), `jupyter`,
`ipython`, `anthropic`, `mss`, `mcp`, `fpdf2`, `pillow`, etc. — even when
the user only wants the **client** functions that talk to the daemon over
gRPC. This makes client installs slow, large, and brittle on machines
that don't need (or can't build) OpenCV.

Goal: a single `aprilcam` distribution whose **base install is the
lightweight client**, with the heavy daemon/server stack moved behind an
optional `aprilcam[daemon]` extra. `pip install aprilcam` → talk to a
daemon; `pip install aprilcam[daemon]` → run the daemon/server.

This is feasible with small, low-risk changes because the CLI dispatcher
is already fully lazy ([cli/__init__.py:92-96](src/aprilcam/cli/__init__.py#L92-L96))
and the only client-path imports of heavy deps are two `cv2.imdecode`
calls. `client/models.py` is already pure-pydantic and `Config` is only
referenced under `TYPE_CHECKING`, so config's heavy imports never reach
the client at runtime.

## Decisions (confirmed with stakeholder)

- **One package + extras** (not two distributions). Daemon `.py` files
  still ship; only their heavy *dependencies* become optional.
- **Lazy `cv2`, keep `numpy` in base.** Non-image RPCs work with no
  OpenCV. Frame-decode methods raise a clear "install `aprilcam[imaging]`"
  error when OpenCV is absent.
- **Dev tooling** (jupyter/ipython/anthropic/grpcio-tools) moves to a
  `dev` extra, out of the default install.

## Dependency partition

Verified usage: `msgpack`, `zeroconf`, `websockets` are daemon/server-only;
the client's sole heavy import is `cv2.imdecode` in `control.py:240` and
`stream.py:117` (numpy `frombuffer` stays).

**Base `dependencies` (client):**
`grpcio>=1.60`, `protobuf>=4.25`, `pydantic>=2.0`, `numpy>=1.23`,
`python-dotenv>=1.0`, `rich>=13.0`

(`python-dotenv` + `rich` kept in base — tiny, pure-Python — so the
`aprilcam` console script and light subcommands like `init`/`tool` still
work on a client-only install.)

**`[imaging]` extra** (lets a thin client decode frames without the full
daemon): `opencv-contrib-python>=4.8`

**`[daemon]` extra** (full server/detection; pulls in imaging):
`aprilcam[imaging]`, `mss>=9.0`, `mcp>=1.0`, `websockets>=12.0`,
`fpdf2>=2.7`, `cv2-enumerate-cameras>=1.3`, `msgpack>=1.0`,
`pillow>=10.0`, `grpcio-reflection>=1.60`, `zeroconf>=0.131`

**`[dev]` extra:** `ipykernel>=7.2.0`, `ipython>=8.39.0`,
`jupyter>=1.1.1`, `anthropic>=0.104.0`, `grpcio-tools>=1.60`
(grpcio-tools is proto-codegen / build-time only)

**`[playfield]` extra:** unchanged (`pygame>=2.5`)

## Changes

### 1. `pyproject.toml`
Rewrite `[project] dependencies` to the base list above and replace
`[project.optional-dependencies]` with `imaging`, `daemon`, `dev`, and the
existing `playfield`. Use the self-referential `aprilcam[imaging]` inside
`daemon` so OpenCV isn't duplicated. Entry point
(`aprilcam = "aprilcam.cli:main"`) and build/discovery config stay as-is.

### 2. Lazy OpenCV in the client
Add a small shared loader so frame-decode helpers fail with guidance
instead of an ImportError at module load. New file
`src/aprilcam/client/_imaging.py`:

```python
def require_cv2():
    try:
        import cv2
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Decoding camera frames requires OpenCV. Install it with "
            "`pip install 'aprilcam[imaging]'` (or `aprilcam[daemon]`)."
        ) from e
    return cv2
```

- [client/control.py](src/aprilcam/client/control.py): remove the
  top-level `import cv2` (line 18); keep `import numpy as np`. At
  line 240 use `cv2 = require_cv2()` then `cv2.imdecode(...)`.
- `client/stream.py`: remove top-level `import cv2` (line 15); keep
  numpy; same lazy pattern at line 117.

No other client-path file imports a now-optional dependency
(`client/__init__.py` → control/stream/models/proto; proto needs only
grpcio + protobuf, both base).

### 3. Docs
Update README/install docs: `pip install aprilcam` (client) vs.
`pip install 'aprilcam[daemon]'` (server), and mention `[imaging]` for a
client that decodes frames.

## Verification

1. **Fresh client venv:** `pip install -e .` then
   `python -c "from aprilcam.client import DaemonControl; print('ok')"`
   succeeds, and `python -c "import cv2"` **fails** (confirms OpenCV not
   pulled in). `pip list` shows no opencv/jupyter/mss/mcp.
2. **Light CLI:** `aprilcam --help`, `aprilcam --version`,
   `aprilcam init`, `aprilcam tool` run on the client-only install.
3. **Decode-without-imaging:** invoking a frame-decode path raises the
   `aprilcam[imaging]` RuntimeError (not a bare ImportError).
4. **Daemon venv:** `pip install -e '.[daemon]'` then start the daemon
   (`aprilcam daemon ...`) and the MCP server (`aprilcam mcp`); confirm
   `get_version()` over MCP and a real `capture_frame` round-trip.
5. **Tests:** run `pytest`. Client tests (`tests/test_client_models.py`)
   must pass without OpenCV; daemon-dependent tests
   (`tests/test_daemon_control.py`) run under the `[daemon]` extra. Skip
   or mark any test that needs cv2 so the client-only suite stays green.

## Notes for execution
- Follow the project's git/version rule: bump `version` in
  `pyproject.toml` (`0.YYYYMMDD.N`) with the change and commit
  `chore: bump version`.
- This is a focused, cross-cutting refactor (packaging + 3 source files);
  it fits the CLASI `oop` path rather than a full sprint, unless you'd
  prefer it ticketed.
