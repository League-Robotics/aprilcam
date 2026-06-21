"""Unified CLI dispatcher for aprilcam."""

import os
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
    "probe": {
        "help": "Discover daemons and update the host/camera code store",
        "module": "aprilcam.cli.probe_cli",
    },
    "config": {
        "help": "Show the version and resolved configuration",
        "module": "aprilcam.cli.config_cli",
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
# raise ModuleNotFoundError. `init`, `tool`, `cameras`, `tags`, `view`, `mcp`,
# and `web` are opencv-free thin clients and are omitted here so they do not
# falsely print the "install aprilcam[daemon]" hint.
DAEMON_COMMANDS = frozenset({"daemon", "taggen", "calibrate"})


def _get_version():
    try:
        from importlib.metadata import version
        return version("aprilcam")
    except Exception:
        return "unknown"


def _print_help():
    from ..config import CONFIG_VARS

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
    print()
    print("global options (accepted before OR after the command):")
    print("  --host HOST       Daemon hostname, IP, or single-letter host code (sets APRILCAM_DAEMON_HOST)")
    print("  --port PORT       Daemon TCP port (sets APRILCAM_DAEMON_PORT)")
    print()
    print("flags:")
    print("  --agent [NAME]    Print the AI-agent instructions guide (NAME: agent [default], robot)")
    print()
    print("Configuration:")
    print("  Source precedence (lowest to highest):")
    print("    /etc/aprilcam.env")
    print("    /etc/aprilcam/aprilcam.env")
    print("    ~/.aprilcam")
    print("    .aprilcam  (walk up from CWD)")
    print("    .env       (walk up from CWD, via dotenv)")
    print("    APRILCAM_* environment variables  (highest)")
    print()
    print("  Run 'aprilcam config' to see all resolved paths and current values.")
    print()
    print("Environment variables:")
    header_key = "VARIABLE"
    header_default = "DEFAULT"
    header_desc = "DESCRIPTION"
    print(f"  {header_key:<36}{header_default:<32}{header_desc}")
    for var in CONFIG_VARS:
        key = var["key"]
        default = var["default"]
        description = var["description"]
        print(f"  {key:<36}{default:<32}{description}")


_HOST_FLAGS = {"--host", "--daemon-host"}
_PORT_FLAGS = {"--port", "--daemon-port"}


def _extract_global_flags(args: list[str]) -> tuple[list[str], str | None, str | None]:
    """Scan *args* for root-level ``--host``/``--port`` flags in any position.

    Removes matched flag+value pairs from *args* and returns the remaining
    list along with the extracted host and port values (or ``None`` if not
    provided).

    Handles both ``--host VALUE`` and ``--host=VALUE`` forms for all four
    accepted flag names (``--host``, ``--daemon-host``, ``--port``,
    ``--daemon-port``).

    Args:
        args: Raw argument list (``sys.argv[1:]``).

    Returns:
        ``(remaining, host, port)`` where *remaining* is *args* with the
        global flags stripped, and *host* / *port* are the extracted values
        or ``None``.
    """
    remaining: list[str] = []
    host: str | None = None
    port: str | None = None
    i = 0
    while i < len(args):
        token = args[i]

        # -- host flags (--host VALUE or --host=VALUE) ---------------------
        matched_host = False
        for flag in _HOST_FLAGS:
            if token == flag:
                if i + 1 < len(args):
                    host = args[i + 1]
                    i += 2
                    matched_host = True
                    break
                else:
                    i += 1
                    matched_host = True
                    break
            if token.startswith(flag + "="):
                host = token[len(flag) + 1:]
                i += 1
                matched_host = True
                break
        if matched_host:
            continue

        # -- port flags (--port VALUE or --port=VALUE) ---------------------
        matched_port = False
        for flag in _PORT_FLAGS:
            if token == flag:
                if i + 1 < len(args):
                    port = args[i + 1]
                    i += 2
                    matched_port = True
                    break
                else:
                    i += 1
                    matched_port = True
                    break
            if token.startswith(flag + "="):
                port = token[len(flag) + 1:]
                i += 1
                matched_port = True
                break
        if matched_port:
            continue

        remaining.append(token)
        i += 1

    return remaining, host, port


def main(argv=None):
    """Entry point for the aprilcam CLI."""
    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _print_help()
        sys.exit(0)

    if args[0] in ("-V", "--version"):
        print(f"aprilcam {_get_version()}")
        sys.exit(0)

    if args[0] == "--agent":
        guide_name = args[1] if len(args) > 1 else "agent"
        from aprilcam.guides import read_guide
        content = read_guide(guide_name)
        if content is None:
            available = "agent, robot"
            print(
                f"aprilcam: unknown guide '{guide_name}'. Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(content)
        sys.exit(0)

    # Extract global --host/--port flags from ANY position in argv and expose
    # them as environment variables so every downstream command picks them up
    # via the existing Config resolver precedence.
    args, _host, _port = _extract_global_flags(list(args))
    if _host is not None:
        os.environ["APRILCAM_DAEMON_HOST"] = _host
    if _port is not None:
        os.environ["APRILCAM_DAEMON_PORT"] = _port

    if not args:
        _print_help()
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
