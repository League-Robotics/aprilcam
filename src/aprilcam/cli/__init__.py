"""Unified CLI dispatcher for aprilcam."""

import sys


SUBCOMMANDS = {
    "daemon": {
        "help": "Manage the AprilCam daemon (start, status, stop)",
        "module": "aprilcam.cli.daemon_cli",
    },
    "mcp": {
        "help": "Start the MCP server",
        "module": "aprilcam.server.mcp_server",
    },
    "taggen": {
        "help": "Generate AprilTag or ArUco marker images (PDF or PNG)",
        "module": "aprilcam.cli.taggen_cli",
    },
    "calibrate": {
        "help": "Run playfield calibration for one or more cameras",
        "module": "aprilcam.cli.calibrate_cli",
    },
    "cameras": {
        "help": "List available cameras",
        "module": "aprilcam.cli.cameras_cli",
    },
    "tags": {
        "help": "Detect and list all ArUco and AprilTag markers on a camera",
        "module": "aprilcam.cli.tags_cli",
    },
    "init": {
        "help": "Configure MCP server entries for Claude Code and VS Code",
        "module": "aprilcam.cli.init_cli",
    },
    "tool": {
        "help": "List, inspect, and run MCP tools from the command line",
        "module": "aprilcam.cli.tool_cli",
    },
    "view": {
        "help": "Open a live view window fed by the AprilCam daemon",
        "module": "aprilcam.cli.view_cli",
    },
    "web": {
        "help": "Start the HTTP/WebSocket server with REST API and MCP SSE",
        "module": "aprilcam.cli.web_cli",
    },
}


# Subcommands that depend on the optional `aprilcam[daemon]` stack (OpenCV,
# mcp, mss, websockets, fpdf2, ...). The base install is the lightweight
# client only, so importing these — or their lazily-loaded heavy deps — can
# raise ModuleNotFoundError. `init` and `tool` are pure-client and omitted.
DAEMON_COMMANDS = frozenset(
    {"daemon", "mcp", "web", "taggen", "calibrate", "cameras", "tags", "view"}
)


def _get_version():
    try:
        from importlib.metadata import version
        return version("aprilcam")
    except Exception:
        return "unknown"


def _print_help():
    print(f"aprilcam {_get_version()}")
    print()
    print("usage: aprilcam <command> [options]")
    print()
    print("AprilCam -- AprilTag detection and generation toolkit")
    print()
    print("commands:")
    for name, info in SUBCOMMANDS.items():
        print(f"  {name:<12} {info['help']}")
    print()
    print("Run 'aprilcam <command> --help' for command-specific options.")


def main(argv=None):
    """Entry point for the aprilcam CLI."""
    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _print_help()
        sys.exit(0)

    if args[0] in ("-V", "--version"):
        print(f"aprilcam {_get_version()}")
        sys.exit(0)

    command = args[0]
    remaining = args[1:]

    if command not in SUBCOMMANDS:
        print(f"Unknown command: {command}")
        _print_help()
        sys.exit(1)

    # Lazy import: only load the target module when actually dispatching.
    import importlib

    try:
        mod = importlib.import_module(SUBCOMMANDS[command]["module"])
        rc = mod.main(remaining) or 0
    except ModuleNotFoundError as exc:
        # The base install ships only the lightweight gRPC client. The
        # daemon/server subcommands — and the heavy libraries they import,
        # eagerly or lazily — live in the `aprilcam[daemon]` extra. Translate
        # the missing-module error into an actionable install hint instead of
        # dumping a raw traceback.
        if command in DAEMON_COMMANDS:
            print(
                f"aprilcam: the '{command}' command requires the daemon/server "
                f"dependencies, which are not installed "
                f"(missing module '{exc.name}').\n\n"
                f"Install the full stack with one of:\n"
                f"    pipx install 'aprilcam[daemon]'\n"
                f"    pip install 'aprilcam[daemon]'",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    sys.exit(rc)
