#!/usr/bin/env python3
"""Stdio MCP proxy in front of `cua-driver mcp` for local models.

Derived from Cua's cua-mcp-filter.py (MIT License, trycua/cua
docs/public/examples/local-models/cua-mcp-filter.py @ 0dd44d62226fe09dd2ee1a5bf0952d4572077b83)
with three additions:

  --allow          advertise only these tools in tools/list. Measured on Cua Driver
                   0.30.3: all 62 tool schemas ~= 45k tokens, the 7-tool visual set
                   ~= 10k. This is NOT an authorization boundary; use a bounded
                   capability manifest for enforcement.
  --strip-images   replace image blocks in tool results with a short text note. Needed
                   for text-only GGUF models: llama-server rejects any image input when
                   no --mmproj is loaded ("image input is not supported"). The capture
                   stays registered server-side, so parse_visual_regions still reads it.
  --inline-structured
                   append a compact text rendering of structuredContent for
                   get_window_state / get_desktop_state / parse_visual_regions. Cua Driver
                   0.30.3 puts capture_id and the parsed regions ONLY in structuredContent
                   (the text block just says "Parsed N visual regions"), so a client that
                   shows the model only `content` gives it nothing to act on.
  --socket         forward to a running daemon (`cua-driver mcp --socket ...`), e.g.
                   \\\\.\\pipe\\cua-driver on Windows when called over SSH or from WSL.

Standard library only; stdio is handled as UTF-8 bytes so it behaves the same on Windows.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading

IMAGE_NOTE = (
    "[screenshot omitted: this model is text-only. Call parse_visual_regions with this "
    "result's capture_id to get OCR text and icon regions with pixel bounds.]"
)


CAPTURE_TOOLS = {"get_window_state", "get_desktop_state"}


def render_structured(tool: str, sc: dict) -> str:
    """Compact, model-readable text for the fields a visual action needs."""
    if tool in CAPTURE_TOOLS:
        return (
            f"capture_id={sc.get('capture_id')} screenshot={sc.get('screenshot_width')}x{sc.get('screenshot_height')} "
            f"coordinate_frame={sc.get('coordinate_frame', 'window')}. Pass this capture_id to "
            "parse_visual_regions and to the one click derived from it; capture again after any action."
        )
    if tool == "parse_visual_regions":
        if sc.get("code"):
            return f"parse_visual_regions error: {sc.get('code')} - {sc.get('message', '')}"
        capture = sc.get("capture") or {}
        lines = [
            f"capture_id={capture.get('capture_id')} regions={len(sc.get('regions') or [])} "
            "(bounds are [x,y,w,h] screenshot pixels; click the centre x+w//2, y+h//2 with this capture_id):"
        ]
        for r in sc.get("regions") or []:
            b = r.get("bounds") or {}
            label = r.get("text") if r.get("kind") == "text" else r.get("label", "")
            lines.append(
                f"{r.get('id')} {r.get('kind')} [{b.get('x')},{b.get('y')},{b.get('width')},{b.get('height')}] "
                f"{float(r.get('confidence') or 0):.2f} {json.dumps(label, ensure_ascii=False)}"
            )
        return "\n".join(lines)
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow", help="comma-separated tool names to advertise (default: all)")
    parser.add_argument("--strip-images", action="store_true", help="replace image content blocks with a text note")
    parser.add_argument("--inline-structured", action="store_true",
                        help="append compact text for capture_id and parsed regions from structuredContent")
    parser.add_argument("--parse-min-confidence", type=float,
                        help="default parse_visual_regions options.min_confidence when the caller omits it (e.g. 0.5)")
    parser.add_argument("--parse-max-regions", type=int,
                        help="default parse_visual_regions options.max_regions when the caller omits it (e.g. 150)")
    parser.add_argument("--socket", help="daemon endpoint passed to `cua-driver mcp --socket`")
    parser.add_argument("--driver", default=shutil.which("cua-driver"), help="cua-driver executable (default: from PATH)")
    args = parser.parse_args()
    if not args.driver:
        parser.error("cua-driver was not found on PATH; pass --driver")
    return args


def main() -> int:
    args = parse_args()
    allowed = {n.strip() for n in args.allow.split(",") if n.strip()} if args.allow else None
    argv = [args.driver, "mcp"] + (["--socket", args.socket] if args.socket else [])
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None

    pending_tool_lists: set = set()
    pending_calls: dict = {}  # request id -> tool name
    lock = threading.Lock()

    def apply_parse_defaults(message: object, line: bytes) -> bytes:
        """Fill omitted parse options; local models rarely pass optional arguments."""
        if args.parse_min_confidence is None and args.parse_max_regions is None:
            return line
        if not (isinstance(message, dict) and message.get("method") == "tools/call"):
            return line
        params = message.get("params") or {}
        if params.get("name") != "parse_visual_regions" or not isinstance(params.get("arguments"), dict):
            return line
        options = params["arguments"].get("options")
        if options is None:
            options = params["arguments"]["options"] = {}
        if not isinstance(options, dict):
            return line
        if args.parse_min_confidence is not None:
            options.setdefault("min_confidence", args.parse_min_confidence)
        if args.parse_max_regions is not None:
            options.setdefault("max_regions", args.parse_max_regions)
        return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

    def parent_to_child() -> None:
        try:
            for line in sys.stdin.buffer:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict) and "id" in message:
                        with lock:
                            if message.get("method") == "tools/list":
                                pending_tool_lists.add(message["id"])
                            elif message.get("method") == "tools/call":
                                pending_calls[message["id"]] = (message.get("params") or {}).get("name")
                    line = apply_parse_defaults(message, line)
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
                    pass  # forward anything unexpected untouched
                child.stdin.write(line)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    def forward_stderr() -> None:
        for chunk in iter(lambda: child.stderr.read(8192), b""):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    threading.Thread(target=parent_to_child, daemon=True).start()
    threading.Thread(target=forward_stderr, daemon=True).start()

    try:
        for line in child.stdout:
            output = line
            try:
                message = json.loads(line)
                changed = False
                is_tool_list, tool_name = False, None
                if isinstance(message, dict) and "id" in message and "method" not in message:
                    # A response (result or error): retire its bookkeeping either way.
                    with lock:
                        is_tool_list = message["id"] in pending_tool_lists
                        pending_tool_lists.discard(message["id"])
                        tool_name = pending_calls.pop(message["id"], None)
                result = message.get("result") if isinstance(message, dict) else None
                if isinstance(result, dict):
                    sc = result.get("structuredContent")
                    if args.inline_structured and tool_name and isinstance(sc, dict) and isinstance(result.get("content"), list):
                        rendered = render_structured(tool_name, sc)
                        if rendered:
                            result["content"].append({"type": "text", "text": rendered})
                            changed = True
                    tools = result.get("tools")
                    if allowed is not None and is_tool_list and isinstance(tools, list):
                        result["tools"] = [t for t in tools if t.get("name") in allowed]
                        changed = True
                    content = result.get("content")
                    if args.strip_images and isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "image" for c in content
                    ):
                        result["content"] = [
                            {"type": "text", "text": IMAGE_NOTE} if isinstance(c, dict) and c.get("type") == "image" else c
                            for c in content
                        ]
                        changed = True
                if changed:
                    output = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
                pass
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
    except KeyboardInterrupt:
        child.terminate()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
