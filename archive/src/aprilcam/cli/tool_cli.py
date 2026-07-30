"""CLI interface for running MCP tools directly from the command line.

Usage:
    aprilcam tool list                  — list all available tools
    aprilcam tool <name> help           — show help for a tool
    aprilcam tool <name> [key=value...] — run a tool with arguments
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from typing import List, Optional


def _get_tools():
    """Return the list of MCP tool definitions from the server."""
    from aprilcam.server.mcp_server import server

    return asyncio.run(server.list_tools())


def _find_tool(name: str):
    """Find a tool by name, or None."""
    for t in _get_tools():
        if t.name == name:
            return t
    return None


def _print_tool_list():
    """Print all available tools."""
    tools = _get_tools()
    print(f"Available tools ({len(tools)}):\n")
    max_name = max(len(t.name) for t in tools)
    for t in tools:
        first_line = (t.description or "").split("\n")[0].strip()
        print(f"  {t.name:<{max_name}}  {first_line}")
    print()
    print("Run 'aprilcam tool <name> help' for tool details.")


def _print_tool_help(tool):
    """Print detailed help for a tool."""
    print(f"Tool: {tool.name}")
    print()
    if tool.description:
        print(tool.description.strip())
    print()

    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])
    if props:
        print("Arguments:")
        for pname, pinfo in props.items():
            req = "(required)" if pname in required else ""
            # Determine type
            ptype = pinfo.get("type", "")
            if not ptype and "anyOf" in pinfo:
                types = [t.get("type", "") for t in pinfo["anyOf"] if t.get("type") != "null"]
                ptype = types[0] if types else "any"
            default = pinfo.get("default", "<no default>")
            if pname in required:
                print(f"  {pname} ({ptype}) {req}")
            else:
                print(f"  {pname} ({ptype}) default={default}")
        print()
        print("Usage: aprilcam tool {name} key=value key=value ...".format(name=tool.name))
    else:
        print("No arguments.")


def _coerce_value(value_str: str, prop_info: dict):
    """Coerce a string value to the type specified in the JSON schema."""
    ptype = prop_info.get("type", "")
    if not ptype and "anyOf" in prop_info:
        types = [t.get("type", "") for t in prop_info["anyOf"] if t.get("type") != "null"]
        ptype = types[0] if types else "string"

    if value_str.lower() == "null" or value_str.lower() == "none":
        return None
    if ptype == "integer":
        return int(value_str)
    if ptype == "number":
        return float(value_str)
    if ptype == "boolean":
        return value_str.lower() in ("true", "1", "yes")
    return value_str


def _run_tool(tool, args: list[str]):
    """Run a tool with key=value arguments."""
    from aprilcam.server.mcp_server import server

    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])

    # Parse key=value pairs
    kwargs = {}
    for arg in args:
        if "=" not in arg:
            print(f"Error: argument must be key=value, got: {arg}")
            sys.exit(1)
        key, value = arg.split("=", 1)
        if key not in props:
            print(f"Error: unknown argument '{key}' for tool '{tool.name}'")
            print(f"Valid arguments: {', '.join(props.keys())}")
            sys.exit(1)
        kwargs[key] = _coerce_value(value, props[key])

    # Check required args
    missing = [r for r in required if r not in kwargs]
    if missing:
        print(f"Error: missing required arguments: {', '.join(missing)}")
        sys.exit(1)

    # Call the tool — call_tool returns (content_list, raw_dict)
    result = asyncio.run(server.call_tool(tool.name, kwargs))
    items = result[0] if isinstance(result, tuple) else result

    # Format output
    for item in items:
        if hasattr(item, "type"):
            if item.type == "text":
                # Pretty-print JSON if possible
                try:
                    data = json.loads(item.text)
                    print(json.dumps(data, indent=2))
                except (json.JSONDecodeError, TypeError):
                    print(item.text)
            elif item.type == "image":
                # Write image to temp file and open it
                import base64
                ext = "jpg" if "jpeg" in (item.mimeType or "") else "png"
                with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                    f.write(base64.b64decode(item.data))
                    path = f.name
                print(f"Image saved to: {path}")
                # Open on macOS
                import platform
                if platform.system() == "Darwin":
                    import subprocess
                    subprocess.run(["open", path], check=False)
            else:
                print(f"[{item.type}] {item}")
        else:
            print(item)


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print("usage: aprilcam tool <list|name> [help|key=value...]")
        print()
        print("  list              List all available MCP tools")
        print("  <name> help       Show help for a specific tool")
        print("  <name> [args...]  Run a tool with key=value arguments")
        return 0

    if args[0] == "list":
        _print_tool_list()
        return 0

    tool_name = args[0]
    remaining = args[1:]

    tool = _find_tool(tool_name)
    if tool is None:
        print(f"Unknown tool: {tool_name}")
        print()
        _print_tool_list()
        return 1

    if remaining and remaining[0] in ("help", "-h", "--help"):
        _print_tool_help(tool)
        return 0

    _run_tool(tool, remaining)
    return 0
