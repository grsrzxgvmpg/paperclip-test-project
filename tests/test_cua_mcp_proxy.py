"""Tests for integrations/cua/mcp/cua_mcp_proxy.py against a fake cua-driver."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "integrations" / "cua" / "mcp" / "cua_mcp_proxy.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_cua_driver.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake driver is launched through a POSIX shebang")


@pytest.fixture()
def fake_driver(tmp_path):
    launcher = tmp_path / "cua-driver"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


def run_proxy(driver, proxy_args, requests):
    lines = [json.dumps({"jsonrpc": "2.0", **r}) for r in requests]
    completed = subprocess.run(
        [sys.executable, str(PROXY), "--driver", driver, *proxy_args],
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        timeout=30,
        check=True,
    )
    return {m["id"]: m for m in map(json.loads, completed.stdout.decode().splitlines())}


def call(request_id, name, arguments):
    return {"id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_allow_filters_tools_list_only(fake_driver):
    out = run_proxy(fake_driver, ["--allow", "get_window_state,parse_visual_regions,click"],
                    [{"id": 1, "method": "tools/list"}])
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["get_window_state", "parse_visual_regions", "click"]


def test_without_flags_responses_pass_through_unchanged(fake_driver):
    out = run_proxy(fake_driver, [], [{"id": 1, "method": "tools/list"},
                                      call(2, "get_window_state", {"pid": 1, "window_id": 7})])
    assert len(out[1]["result"]["tools"]) == 5
    assert [c["type"] for c in out[2]["result"]["content"]] == ["text", "image"]


def test_strip_images_replaces_screenshot_but_keeps_capture(fake_driver):
    out = run_proxy(fake_driver, ["--strip-images"], [call(1, "get_window_state", {"pid": 1, "window_id": 7})])
    result = out[1]["result"]
    assert [c["type"] for c in result["content"]] == ["text", "text"]
    assert "parse_visual_regions" in result["content"][1]["text"]
    assert result["structuredContent"]["capture_id"] == "capture_abc_0"


def test_inline_structured_exposes_capture_id_and_regions(fake_driver):
    out = run_proxy(fake_driver, ["--inline-structured"], [
        call(1, "get_window_state", {"pid": 1, "window_id": 7}),
        call(2, "parse_visual_regions", {"capture_id": "capture_abc_0"}),
        call(3, "parse_visual_regions", {"capture_id": "stale"}),
    ])
    state_text = out[1]["result"]["content"][-1]["text"]
    assert "capture_id=capture_abc_0" in state_text and "screenshot=800x600" in state_text
    regions_text = out[2]["result"]["content"][-1]["text"]
    assert 'text-1 text [10,20,100,30] 0.90 "Submit Order"' in regions_text
    assert 'icon-2 icon [200,20,24,24] 0.80 "icon-class-0"' in regions_text
    assert "capture_not_found" in out[3]["result"]["content"][-1]["text"]


def test_parse_defaults_fill_only_missing_options(fake_driver):
    args = ["--parse-min-confidence", "0.5", "--parse-max-regions", "150"]
    out = run_proxy(fake_driver, args, [
        call(1, "parse_visual_regions", {"capture_id": "capture_abc_0"}),
        call(2, "parse_visual_regions", {"capture_id": "capture_abc_0", "options": {"min_confidence": 0.8}}),
    ])
    assert out[1]["result"]["structuredContent"]["received_options"] == {"min_confidence": 0.5, "max_regions": 150}
    assert out[2]["result"]["structuredContent"]["received_options"] == {"min_confidence": 0.8, "max_regions": 150}


def test_socket_is_forwarded_and_malformed_input_survives(fake_driver):
    completed = subprocess.run(
        [sys.executable, str(PROXY), "--driver", fake_driver, "--socket", r"\\.\pipe\cua-driver",
         "--parse-max-regions", "5"],
        input=b'not json\n{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}\n'
              b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}\n',
        capture_output=True, timeout=30, check=True,
    )
    replies = {m["id"]: m for m in map(json.loads, completed.stdout.decode().splitlines())}
    assert "error" in replies[1]
    assert replies[2]["result"]["serverInfo"]["argv"] == ["mcp", "--socket", r"\\.\pipe\cua-driver"]


def test_render_is_utf8_clean(fake_driver):
    sys.path.insert(0, str(PROXY.parent))
    try:
        import cua_mcp_proxy
    finally:
        sys.path.pop(0)
    text = cua_mcp_proxy.render_structured("parse_visual_regions", {
        "capture": {"capture_id": "c"},
        "regions": [{"id": "text-1", "kind": "text", "text": "Größe ×", "confidence": 1,
                     "bounds": {"x": 0, "y": 0, "width": 1, "height": 1}}],
    })
    assert '"Größe ×"' in text
