"""Shared guide-file reader used by the CLI and MCP server."""
from pathlib import Path

_GUIDE_DIR = Path(__file__).parent  # src/aprilcam/

_GUIDE_MAP = {
    "agent": "AGENT_GUIDE.md",
    "robot": "ROBOT_API_GUIDE.md",
}


def read_guide(name: str) -> str | None:
    """Return the text of a packaged guide file, or None if the name is unknown.

    name: 'agent' -> AGENT_GUIDE.md
          'robot' -> ROBOT_API_GUIDE.md
    """
    filename = _GUIDE_MAP.get(name.lower().strip())
    if filename is None:
        return None
    return (_GUIDE_DIR / filename).read_text(encoding="utf-8")
