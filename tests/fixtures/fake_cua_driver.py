#!/usr/bin/env python3
"""Stand-in for `cua-driver mcp`: answers the handful of MCP calls the proxy tests use.

The response shapes copy what Cua Driver 0.30.3 returned in a live session: capture_id and
regions only in structuredContent, a screenshot as an image block, errors with a
structuredContent code.
"""

import json
import sys

TOOLS = [
    {"name": name, "description": "x" * 50, "inputSchema": {"type": "object"}}
    for name in ("get_window_state", "parse_visual_regions", "click", "get_desktop_state", "drag")
]


def reply(message_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        if "id" not in request:
            continue
        if not isinstance(request.get("params", {}), dict):
            reply(request["id"], error={"code": -32602, "message": "params must be an object"})
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request["id"], {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake", "argv": argv}})
        elif method == "tools/list":
            reply(request["id"], {"tools": TOOLS})
        elif method == "tools/call":
            name = request["params"]["name"]
            arguments = request["params"].get("arguments", {})
            if name == "get_window_state":
                reply(request["id"], {
                    "content": [{"type": "text", "text": "window_id=7 pid=1"},
                                {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}],
                    "structuredContent": {"capture_id": "capture_abc_0", "screenshot_width": 800,
                                          "screenshot_height": 600, "coordinate_frame": "window"},
                })
            elif name == "parse_visual_regions" and arguments.get("capture_id") == "capture_abc_0":
                reply(request["id"], {
                    "content": [{"type": "text", "text": "Parsed 2 visual regions"}],
                    "structuredContent": {
                        "capture": {"capture_id": "capture_abc_0"},
                        "regions": [
                            {"id": "text-1", "kind": "text", "text": "Submit Order", "confidence": 0.9,
                             "bounds": {"x": 10, "y": 20, "width": 100, "height": 30}},
                            {"id": "icon-2", "kind": "icon", "label": "icon-class-0", "confidence": 0.8,
                             "bounds": {"x": 200, "y": 20, "width": 24, "height": 24}},
                        ],
                        "received_options": arguments.get("options"),
                    },
                })
            elif name == "parse_visual_regions":
                reply(request["id"], {
                    "content": [{"type": "text", "text": "capture_id is unknown"}],
                    "isError": True,
                    "structuredContent": {"code": "capture_not_found", "message": "capture_id is unknown"},
                })
            else:
                reply(request["id"], error={"code": -32602, "message": f"unknown tool {name}"})
        else:
            reply(request["id"], error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
