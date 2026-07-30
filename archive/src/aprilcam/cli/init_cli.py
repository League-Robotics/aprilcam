"""aprilcam init — configure MCP server entries for editors."""

import json
from pathlib import Path

CLAUDE_CODE_ENTRY = {
    "command": "aprilcam",
    "args": ["mcp"],
}

VSCODE_ENTRY = {
    "type": "stdio",
    "command": "aprilcam",
    "args": ["mcp"],
}


def _upsert_mcp_config(path, parent_key, entry):
    """Read (or create) a JSON config file and upsert the aprilcam entry."""
    data = {}
    if path.exists():
        try:
            text = path.read_text().strip()
            if text:
                data = json.loads(text)
        except json.JSONDecodeError:
            print(f"  Warning: {path} contains invalid JSON, overwriting")
            data = {}
    if parent_key not in data:
        data[parent_key] = {}
    data[parent_key]["aprilcam"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def main(argv=None):
    """Entry point for ``aprilcam init``."""
    cwd = Path.cwd()

    mcp_path = cwd / ".mcp.json"
    _upsert_mcp_config(mcp_path, "mcpServers", CLAUDE_CODE_ENTRY)
    print(f"  Updated {mcp_path}")

    vscode_path = cwd / ".vscode" / "mcp.json"
    _upsert_mcp_config(vscode_path, "servers", VSCODE_ENTRY)
    print(f"  Updated {vscode_path}")

    print("AprilCam MCP server configured. Restart your editor to connect.")
    return 0
