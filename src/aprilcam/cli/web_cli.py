"""CLI subcommand: aprilcam web — Start the HTTP/WebSocket server."""

import argparse
from aprilcam.cli._daemon import add_daemon_args


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aprilcam web",
        description="Start the AprilCam HTTP server with REST API, MCP SSE, and WebSocket streaming",
    )
    parser.add_argument("--port", type=int, default=17439, help="Port to listen on (default: 17439)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    add_daemon_args(parser)
    args = parser.parse_args(argv)

    import uvicorn
    from aprilcam.server.web_server import create_app

    app = create_app()

    # For display URLs, use "localhost" instead of "0.0.0.0" so they're clickable
    display_host = "localhost" if args.host == "0.0.0.0" else args.host
    print(f"Starting AprilCam web server on {args.host}:{args.port}")
    print(f"  REST API:    http://{display_host}:{args.port}/api/")
    print(f"  MCP SSE:     http://{display_host}:{args.port}/mcp/sse")
    print(f"  WebSocket:   ws://{display_host}:{args.port}/ws/tags/<source_id>")
    print(f"  Live UI:     http://{display_host}:{args.port}/")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)
    return 0
