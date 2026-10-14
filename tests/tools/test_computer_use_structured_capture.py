"""Cua compact defaults must not discard snapshot-bound action tokens or geometry."""
import json
from contextlib import nullcontext

import pytest

from tools.computer_use import cua_backend, tool
from tools.computer_use.cua_backend_capture import _gws_is_empty


@pytest.mark.parametrize("advertised", [False, True])
@pytest.mark.parametrize("bound", [0, 200, 500])
def test_gws_schema_gate_preserves_legacy_args(monkeypatch, advertised, bound):
    backend = cua_backend.CuaDriverBackend()
    backend._active_pid, backend._active_window_id = 101, 202
    backend._session_id = "test-session"
    backend._session._tool_schemas = {"get_window_state": {
        "properties": {"tree_format": {}} if advertised else {}}}
    monkeypatch.setattr(cua_backend, "_cua_configured_ax_max_elements", lambda: bound)
    expected = {"pid": 101, "window_id": 202, "session": "test-session"}
    if bound:
        expected["max_elements"] = bound
    if advertised:
        expected["tree_format"] = "elements"
    assert backend._gws_args() == expected


@pytest.mark.parametrize("mode", ["ax", "som", "vision"])
def test_compact_driver_capture_keeps_tokens_and_geometry(monkeypatch, mode):
    backend = cua_backend.CuaDriverBackend()
    session = backend._session
    session._tool_schemas = {"get_window_state": {"properties": {"tree_format": {}}}}
    session._capabilities = {"get_window_state": set(), "click": {"accessibility.element_tokens"}}
    calls = []
    element = {"element_index": 1, "element_token": "s123:1", "role": "AXButton",
               "label": "Increment", "frame": {"x": 10, "y": 20, "w": 30, "h": 40}}

    def transport(name, args, **kwargs):
        calls.append((name, dict(args)))
        if name == "click":
            assert args["element_token"] == "s123:1"
            return {"isError": False, "data": "clicked", "structuredContent": {"effect": "unverifiable"}}
        assert name == "get_window_state"
        assert args["pid"] == 101 and args["window_id"] == 202
        assert args.get("tree_format") == "elements"
        return {"structuredContent": {"elements": [element]}, "data": "", "images": ["cG5n"]}

    monkeypatch.setattr(session, "call_tool", transport)
    monkeypatch.setattr(tool, "_backend_for_call", lambda session_id="": nullcontext(backend))
    cap = backend.capture(mode=mode, pid=101, window_id=202)
    if mode == "vision":
        assert cap.elements == [] and not backend._snapshot_tokens
        assert cap.png_b64 == "cG5n"
    else:
        assert cap.elements[0].bounds == (10, 20, 30, 40)
        assert backend._snapshot_tokens == {1: "s123:1"}
        action = backend.click(element=1)
        assert action.ok
        # The follow-up must use the same builder and exact target, not a new window lookup.
        monkeypatch.setattr(tool, "_capture_after_mode", lambda: "ax")
        monkeypatch.setattr(tool, "_capture_response", lambda cap, **kw: json.dumps({"n": len(cap.elements)}))
        followed = json.loads(tool._maybe_follow_capture(backend, action, True))
        assert followed["ok"] and followed["n"] == 1
    assert _gws_is_empty({"structuredContent": {"elements": [element]}}) is False
    assert _gws_is_empty({"structuredContent": {}, "data": "", "images": []}) is True


def test_empty_refetch_uses_the_same_structured_args(monkeypatch):
    backend = cua_backend.CuaDriverBackend()
    backend._session._tool_schemas = {"get_window_state": {"properties": {"tree_format": {}}}}
    requests = []
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: requests.append(dict(args)) or {})
    def refetch(name, args, timeout):
        requests.append(dict(args))
        return {"structuredContent": {"elements": [{"element_index": 7, "element_token": "s456:7"}]}}
    monkeypatch.setattr(backend._session, "_call_tool_via_cli", refetch)
    cap = backend.capture(mode="ax", pid=101, window_id=202)
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]["tree_format"] == "elements"
    assert cap.elements[0].element_token == "s456:7"
