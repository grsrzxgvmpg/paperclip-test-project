#!/usr/bin/env python3
"""Smoke-test and benchmark Cua Perception through one persistent Cua Driver MCP session.

Runs the documented loop on a real window:

    get_window_state -> parse_visual_regions -> (optional) capture-bound click -> reobserve

Standard library only (Python 3.9+), so it runs unchanged on Windows, Linux and macOS.

Examples
--------
Measure parse latency on a window (observe only, no input is sent):

    python perception_smoke.py --window-title "Notepad" --runs 5

Click the single OCR region reading "Submit Order", then verify the window title changed:

    python perception_smoke.py --window-title "Canvas Fixture" \\
        --click-text "Submit Order" --expect-title "Submit Order"

Talk to an already-running daemon (required over SSH on Windows):

    python perception_smoke.py --socket \\\\.\\pipe\\cua-driver --window-title "Paint"
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"
CALL_TIMEOUT_S = 120.0


class McpError(RuntimeError):
    pass


class McpSession:
    """Minimal newline-delimited JSON-RPC client for `cua-driver mcp` over stdio."""

    def __init__(self, driver: str, socket: Optional[str]) -> None:
        argv = [driver, "mcp"] + (["--socket", socket] if socket else [])
        # Explicit UTF-8: on Windows the locale default (e.g. cp1252) corrupts
        # non-ASCII OCR text and window titles.
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._next_id = 0
        self._inbox: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.stderr_tail: List[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                self.stderr_tail.append("non-JSON stdout: " + line[:200])
        self._inbox.put(None)

    def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def _send(self, message: Dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + CALL_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"{method}: no response within {CALL_TIMEOUT_S:.0f}s")
            try:
                incoming = self._inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if incoming is None:
                raise McpError(f"{method}: server exited. stderr: {' | '.join(self.stderr_tail)}")
            if "method" in incoming and "id" in incoming:
                # Server-initiated request (roots/list, sampling, ...): decline it.
                self._send({"jsonrpc": "2.0", "id": incoming["id"],
                            "error": {"code": -32601, "message": "not supported by smoke client"}})
                continue
            if incoming.get("id") != request_id:
                continue
            if "error" in incoming:
                raise McpError(f"{method}: {incoming['error']}")
            return incoming["result"]

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


def structured(result: Dict[str, Any]) -> Dict[str, Any]:
    return result.get("structuredContent") or {}


def text_of(result: Dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def error_code(result: Dict[str, Any]) -> Optional[str]:
    """Return a stable error/refusal code if the tool result carries one."""
    sc = structured(result)
    for candidate in (sc.get("code"), (sc.get("error") or {}).get("code") if isinstance(sc.get("error"), dict) else None):
        if candidate:
            return str(candidate)
    if result.get("isError"):
        return "tool_error: " + text_of(result)[:300]
    return None


def pick_window(session: McpSession, args: argparse.Namespace) -> Dict[str, Any]:
    # A bounded runtime with desktop.display:false refuses an unscoped
    # list_windows but allows one scoped to a manifest app's pid.
    listing = session.call_tool("list_windows", {"pid": args.pid} if args.pid is not None else {})
    code = error_code(listing)
    if code:
        raise SystemExit(f"list_windows refused: {code}. In bounded mode pass --pid (e.g. (Get-Process <name>).Id)")
    windows = structured(listing).get("windows", [])
    matches = [
        w for w in windows
        if (args.pid is None or w.get("pid") == args.pid)
        and (args.window_id is None or w.get("window_id") == args.window_id)
        and (args.window_title is None or args.window_title.lower() in str(w.get("title", "")).lower())
    ]
    if len(matches) != 1:
        listing = "\n".join(f"  pid={w.get('pid')} window_id={w.get('window_id')} title={w.get('title')!r}" for w in windows)
        raise SystemExit(f"expected exactly one matching window, found {len(matches)}. Visible windows:\n{listing}")
    return matches[0]


def observe(session: McpSession, window: Dict[str, Any], native: bool) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {
        "pid": window["pid"],
        "window_id": window["window_id"],
        # The tree is not needed for the visual path; keep the walk cheap.
        "max_elements": 25,
        "max_depth": 4,
    }
    if native:
        # OCR accuracy drops on downscaled captures; parse native pixels.
        arguments["max_image_dimension"] = 0
    started = time.perf_counter()
    result = session.call_tool("get_window_state", arguments)
    elapsed_ms = (time.perf_counter() - started) * 1000
    code = error_code(result)
    if code:
        raise SystemExit(f"get_window_state failed: {code}")
    sc = structured(result)
    if not sc.get("capture_id"):
        raise SystemExit("get_window_state returned no capture_id; this Driver build is too old for capture-bound actions")
    return {"sc": sc, "ms": elapsed_ms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--driver", default=shutil.which("cua-driver"), help="cua-driver executable (default: from PATH)")
    parser.add_argument("--socket", help="daemon endpoint, e.g. \\\\.\\pipe\\cua-driver on Windows")
    parser.add_argument("--window-title", help="case-insensitive substring of the target window title")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--window-id", type=int)
    parser.add_argument("--runs", type=int, default=3, help="capture+parse repetitions for timing (default 3)")
    parser.add_argument("--kinds", default="text,icon", help="comma list of region kinds: text,icon")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--max-regions", type=int, default=100)
    parser.add_argument("--downscaled", action="store_true",
                        help="parse the Driver's default downscaled capture instead of native pixels")
    parser.add_argument("--click-text", help="click the single text region containing this string (sends real input)")
    parser.add_argument("--delivery-mode", choices=["background", "foreground"], default="background")
    parser.add_argument("--expect-title", help="after the click, require the reobserved window title to contain this")
    parser.add_argument("--check-replay", action="store_true",
                        help="after the click, resend it with the consumed capture_id and require a refusal. "
                             "Use on test fixtures only")
    parser.add_argument("--json-out", help="write the full report as JSON to this path")
    args = parser.parse_args()

    if not args.driver:
        parser.error("cua-driver not found on PATH; pass --driver")
    if args.window_title is None and args.pid is None:
        parser.error("pass --window-title or --pid")
    if (args.expect_title or args.check_replay) and not args.click_text:
        parser.error("--expect-title and --check-replay require --click-text")

    report: Dict[str, Any] = {"runs": [], "ok": False}
    session = McpSession(args.driver, args.socket)
    try:
        init = session.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "perception-smoke", "version": "1"},
        })
        session.notify("notifications/initialized")
        report["server"] = init.get("serverInfo")
        tools = {t["name"]: t for t in session.request("tools/list").get("tools", [])}
        report["tool_count"] = len(tools)
        print(f"server {report['server']} exposes {len(tools)} tools")
        if "parse_visual_regions" not in tools:
            raise SystemExit("this Driver build does not advertise parse_visual_regions; upgrade Cua Driver")
        if "capture_id" not in tools.get("click", {}).get("inputSchema", {}).get("properties", {}):
            raise SystemExit("click has no capture_id input; upgrade Cua Driver")

        window = pick_window(session, args)
        report["window"] = {k: window.get(k) for k in ("pid", "window_id", "title", "app_name")}
        print(f"target pid={window['pid']} window_id={window['window_id']} title={window.get('title')!r}")

        options = {
            "kinds": [k.strip() for k in args.kinds.split(",") if k.strip()],
            "min_confidence": args.min_confidence,
            "max_regions": args.max_regions,
        }
        last_regions: List[Dict[str, Any]] = []
        last_capture: Dict[str, Any] = {}
        for run in range(1, args.runs + 1):
            obs = observe(session, window, native=not args.downscaled)
            capture_id = obs["sc"]["capture_id"]
            started = time.perf_counter()
            parsed = session.call_tool("parse_visual_regions", {"capture_id": capture_id, "options": options})
            parse_ms = (time.perf_counter() - started) * 1000
            code = error_code(parsed)
            if code:
                hint = {
                    "not_installed": "install it: cua-driver extension install cua-perception --catalog <catalog.json>",
                    "incompatible_protocol": "Driver and extension disagree; run cua-driver extension status cua-perception",
                    "artifact_invalid": "installed artifact failed verification; reinstall from a signed catalog",
                }.get(code, "")
                raise SystemExit(f"parse_visual_regions failed: {code} {hint}")
            psc = structured(parsed)
            regions = psc.get("regions", [])
            entry = {
                "run": run,
                "capture_ms": round(obs["ms"]),
                "parse_wall_ms": round(parse_ms),
                "parse_worker_ms": (psc.get("timing") or {}).get("duration_ms"),
                "screenshot": f"{obs['sc'].get('screenshot_width')}x{obs['sc'].get('screenshot_height')}",
                "regions": len(regions),
                "text_regions": sum(1 for r in regions if r.get("kind") == "text"),
                "icon_regions": sum(1 for r in regions if r.get("kind") == "icon"),
            }
            report["runs"].append(entry)
            print(f"run {run}: capture {entry['capture_ms']} ms, parse {entry['parse_wall_ms']} ms "
                  f"(worker {entry['parse_worker_ms']} ms) on {entry['screenshot']}: "
                  f"{entry['text_regions']} text + {entry['icon_regions']} icon regions")
            last_regions, last_capture = regions, obs["sc"]
            report["parser"] = psc.get("parser")

        walls = [r["parse_wall_ms"] for r in report["runs"]]
        report["parse_ms"] = {"median": statistics.median(walls), "min": min(walls), "max": max(walls)}
        print(f"parse latency: median {report['parse_ms']['median']:.0f} ms, min {min(walls)} ms, max {max(walls)} ms")
        print("text seen:", ", ".join(repr(r.get("text")) for r in last_regions if r.get("kind") == "text")[:600])

        if args.click_text:
            needle = args.click_text.lower()
            hits = [r for r in last_regions if r.get("kind") == "text" and needle in str(r.get("text", "")).lower()]
            if len(hits) != 1:
                raise SystemExit(f"expected exactly one text region containing {args.click_text!r}, "
                                 f"found {len(hits)}; refusing to guess")
            b = hits[0]["bounds"]
            # Bounds are half-open [x, x+width) x [y, y+height) in screenshot pixels.
            x, y = b["x"] + b["width"] // 2, b["y"] + b["height"] // 2
            click_args = {
                "pid": window["pid"],
                "window_id": window["window_id"],
                "x": x,
                "y": y,
                "capture_id": last_capture["capture_id"],
                "delivery_mode": args.delivery_mode,
            }
            clicked = session.call_tool("click", click_args)
            code = error_code(clicked)
            report["click"] = {"args": click_args, "refusal": code, "text": text_of(clicked)[:300]}
            if code:
                if "background_unavailable" in code:
                    print("background delivery refused; rerun with --delivery-mode foreground if foreground input is acceptable")
                raise SystemExit(f"click refused: {code}")
            print(f"clicked region {hits[0].get('id')} {hits[0].get('text')!r} at ({x},{y}) via {args.delivery_mode}")

            if args.check_replay:
                replay = session.call_tool("click", click_args)
                replay_code = error_code(replay) or text_of(replay)
                report["replay_refusal"] = replay_code
                if "capture_not_found" not in str(replay_code):
                    raise SystemExit(f"SAFETY FAIL: replayed capture was not refused as capture_not_found: {replay_code}")
                print("replayed capture refused with capture_not_found (capture binding works)")

            time.sleep(0.5)
            after = observe(session, window, native=False)["sc"]
            report["after_title"] = after.get("window_title")
            print(f"reobserved title: {after.get('window_title')!r}")
            if args.expect_title and args.expect_title.lower() not in str(after.get("window_title", "")).lower():
                raise SystemExit(f"postcondition failed: title does not contain {args.expect_title!r}")

        report["ok"] = True
        print("PASS")
        return 0
    finally:
        session.close()
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)


if __name__ == "__main__":
    sys.exit(main())
