"""Cua 0.35 compact reads must retain snapshot-bound clicks, without bloating vision reads."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tools.computer_use import cua_backend, tool
from tools.computer_use.cua_backend_capture import _gws_is_empty


_TREE = '- [0] AXWindow "Fixture"\n  - [7] AXButton "Increment"'
_SNAPSHOT = "s1234abcd"
_ELEMENT = {"element_index": 7, "element_token": f"{_SNAPSHOT}:7", "role": "AXButton",
            "label": "Increment", "frame": {"x": 10, "y": 20, "w": 30, "h": 40}}
_FORMAT = {"type": "string", "enum": ["markdown", "elements", "both"]}


def _backend(monkeypatch, properties=None):
    backend = cua_backend.CuaDriverBackend()
    backend._active_pid, backend._active_window_id = 101, 202
    backend._session_id = "test-session"
    backend._session._tool_schemas = {
        "get_window_state": {"type": "object", "properties": properties or {}},
        "click": {"properties": {"element_token": {"type": "string"}}},
    }
    backend._session._capabilities = {"get_window_state": set(), "click": set()}
    monkeypatch.setattr(cua_backend, "_cua_configured_ax_max_elements", lambda: 200)
    return backend


def _legacy_args(bound=200):
    args = {"pid": 101, "window_id": 202, "session": "test-session"}
    if bound:
        args["max_elements"] = bound
    return args


@pytest.mark.parametrize("bound", [0, 200, 500])
@pytest.mark.parametrize(("properties", "supported"), [
    ({}, False),
    ({"tree_format": _FORMAT}, True),
    ({"tree_format": {"type": "string"}}, True),
    ({"tree_format": {}}, True),
    ({"tree_format": {"enum": ["elements"]}}, True),
    ({"tree_format": {"enum": ["markdown", "both"]}}, False),
    ({"tree_format": {"enum": []}}, False),
    ({"tree_format": {"enum": "elements"}}, False),
    ({"tree_format": {"type": "integer"}}, False),
    ({"tree_format": {"const": "markdown"}}, False),
    ({"tree_format": {"pattern": "^markdown$"}}, False),
    ({"tree_format": False}, False),
    ({"tree_format": None}, False),
    ({"tree_format": {"$ref": "https://invalid.example/schema"}}, False),
])
def test_gws_schema_gate_preserves_legacy_args(monkeypatch, properties, supported, bound):
    backend = _backend(monkeypatch, properties)
    monkeypatch.setattr(cua_backend, "_cua_configured_ax_max_elements", lambda: bound)
    expected = _legacy_args(bound)
    if supported:
        expected["tree_format"] = "elements"
    # Serialized equality also catches extra/default flags or changed insertion order.
    assert json.dumps(backend._gws_args()) == json.dumps(expected)


@pytest.mark.parametrize("mode", ["ax", "som"])
def test_structured_capture_preserves_tokens_frames_and_followup(monkeypatch, mode):
    backend = _backend(monkeypatch, {"tree_format": _FORMAT})
    requests = []

    def transport(name, args, **kwargs):
        requests.append((name, dict(args)))
        if name == "click":
            assert args["element_token"] == _ELEMENT["element_token"]
            return {"data": "clicked", "isError": False}
        assert name == "get_window_state"
        assert args == {**_legacy_args(), "tree_format": "elements"}
        # Conflicting markdown must not replace canonical elements/tokens/frames.
        return {"structuredContent": {"elements": [_ELEMENT], "snapshot_id": "s00000001"},
                "data": 'Window\n- [99] AXButton "Wrong"', "images": ["cG5n"]}

    monkeypatch.setattr(backend._session, "call_tool", transport)
    cap = backend.capture(mode=mode, pid=101, window_id=202)
    assert [(e.index, e.bounds, e.element_token) for e in cap.elements] == [
        (7, (10, 20, 30, 40), _ELEMENT["element_token"])]
    assert backend.click(element=7).ok
    monkeypatch.setattr(tool, "_capture_after_mode", lambda: "ax")
    monkeypatch.setattr(tool, "_capture_response", lambda cap, **kw: json.dumps({"n": len(cap.elements)}))
    followed = json.loads(tool._maybe_follow_capture(backend, backend.click(element=7), True))
    assert followed["ok"] and followed["n"] == 1
    assert [args for name, args in requests if name == "get_window_state"] == [
        {**_legacy_args(), "tree_format": "elements"}] * 2


@pytest.mark.parametrize("discovery_failed", [False, True])
@pytest.mark.parametrize("source", ["structured", "header", "both", "structured_tree"])
def test_compact_capture_click_uses_this_responses_snapshot(monkeypatch, discovery_failed, source):
    backend = _backend(monkeypatch, {"tree_format": _FORMAT})
    if discovery_failed:
        async def broken_discovery():
            raise RuntimeError("tools/list unavailable")
        asyncio.run(backend._session._populate_capabilities(SimpleNamespace(list_tools=broken_discovery)))
        assert not backend._session.capabilities_discovered
        assert backend._gws_args() == _legacy_args()
    sc = {"snapshot_id": _SNAPSHOT} if source != "header" else {}
    data = f"Window: 101/202 snapshot_id={_SNAPSHOT}\n{_TREE}"
    if source == "structured":
        data = f"Window: 101/202\n{_TREE}"
    if source == "structured_tree":
        sc["tree_markdown"], data = _TREE, ""
    out = {"structuredContent": sc, "data": data, "images": []}
    clicked = []

    def transport(name, args, **kwargs):
        if name == "get_window_state":
            return out  # Driver ignores tree_format (or we could not request it).
        assert name == "click"
        assert args["pid"] == 101 and args["element_index"] == 7
        if args.get("element_token") != f"{_SNAPSHOT}:7":
            return {"isError": True, "data": "element_index requires the element_token from get_window_state"}
        clicked.append(args["element_index"])
        return {"isError": False, "data": "clicked"}

    monkeypatch.setattr(backend._session, "call_tool", transport)
    cap = backend.capture(mode="ax", pid=101, window_id=202)
    assert _gws_is_empty(out) is False
    assert cap.window_title == "Fixture"
    assert cap.elements[1].bounds == (0, 0, 0, 0)
    assert backend.click(element=7).ok
    assert clicked == [7]
    assert backend._snapshot_tokens == {0: f"{_SNAPSHOT}:0", 7: f"{_SNAPSHOT}:7"}


@pytest.mark.parametrize("snapshot", [None, "", 12, "s123", " s1234abcd", "s1234abcd ", "s1234abcd:7"])
def test_missing_or_malformed_snapshot_does_not_create_tokens(monkeypatch, snapshot):
    backend = _backend(monkeypatch)
    backend._snapshot_tokens = {7: "s00000001:7"}
    # A token-looking label/body line is not a response header.
    out = {"structuredContent": {"snapshot_id": snapshot},
           "data": f'Window\n{_TREE}\nsnapshot_id={_SNAPSHOT}', "images": []}
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: out)
    cap = backend.capture(mode="ax", pid=101, window_id=202)
    assert cap.elements and all(e.element_token is None for e in cap.elements)
    assert backend._snapshot_tokens == {}


def test_header_without_snapshot_stays_tokenless(monkeypatch):
    backend = _backend(monkeypatch)
    out = {"data": f"Window\n{_TREE}", "images": []}
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: out)
    assert all(e.element_token is None for e in backend.capture("ax", pid=101, window_id=202).elements)


def test_structured_snapshot_takes_precedence_over_header(monkeypatch):
    backend = _backend(monkeypatch)
    out = {"structuredContent": {"snapshot_id": _SNAPSHOT},
           "data": f"Window snapshot_id=s00000001\n{_TREE}"}
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: out)
    assert backend.capture("ax", pid=101, window_id=202).elements[1].element_token == f"{_SNAPSHOT}:7"


def test_known_old_action_schema_still_receives_no_token(monkeypatch):
    backend = _backend(monkeypatch)
    backend._session._tool_schemas["click"] = {"properties": {}, "additionalProperties": False}
    out = {"data": f"Window snapshot_id={_SNAPSHOT}\n{_TREE}"}
    clicks = []

    def transport(name, args, **kwargs):
        if name == "get_window_state":
            return out
        clicks.append(dict(args))
        return {"data": "clicked", "isError": False}

    monkeypatch.setattr(backend._session, "call_tool", transport)
    backend.capture("ax", pid=101, window_id=202)
    assert backend.click(element=7).ok
    assert "element_token" not in clicks[0]


@pytest.mark.parametrize("refetch", [False, True])
def test_vision_uses_legacy_args_on_both_transports(monkeypatch, refetch):
    backend = _backend(monkeypatch, {"tree_format": _FORMAT})
    requests = []
    out = {"data": f"Window\n{_TREE}", "images": ["cG5n"], "structuredContent": {"elements": [_ELEMENT]}}

    def mcp(name, args, **kwargs):
        requests.append((name, dict(args)))
        return {} if refetch else out

    def cli(name, args, timeout):
        requests.append((name, dict(args)))
        return out

    monkeypatch.setattr(backend._session, "call_tool", mcp)
    monkeypatch.setattr(backend._session, "_call_tool_via_cli", cli)
    cap = backend.capture("vision", pid=101, window_id=202)
    assert cap.png_b64 == "cG5n" and cap.elements == []
    assert backend._snapshot_tokens == {} and cap.ax_max_elements == 0
    assert requests == [("get_window_state", _legacy_args())] * (2 if refetch else 1)
    if not refetch:
        assert cap.window_title == "Fixture"


def test_vision_keeps_standalone_screenshot_preference(monkeypatch):
    backend = _backend(monkeypatch, {"tree_format": _FORMAT})
    backend._session._capabilities["screenshot"] = set()
    requests = []
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: requests.append((name, args)) or
                        {"images": ["cG5n"], "image_mime_types": ["image/jpeg"]})
    cap = backend.capture("vision", pid=101, window_id=202)
    assert cap.png_b64 == "cG5n" and cap.image_mime_type == "image/jpeg"
    assert requests == [("screenshot", {"window_id": 202, "format": "jpeg", "quality": 85,
                                        "session": "test-session"})]


@pytest.mark.parametrize("mode", ["ax", "vision"])
@pytest.mark.parametrize("compact", [False, True])
def test_empty_refetch_preserves_args_and_tokens(monkeypatch, compact, mode):
    from pathlib import Path

    from tools.computer_use import cua_backend_session

    backend = _backend(monkeypatch, {"tree_format": _FORMAT})
    backend._session._embedded_daemon = None
    requests, shot_files = [], []
    monkeypatch.setattr(backend._session, "call_tool", lambda name, args: requests.append(dict(args)) or {})
    monkeypatch.setattr("tools.computer_use.cua_backend_driver.resolve_cua_driver_cmd", lambda: "unused-driver")

    def cli_json(cmd, env, name, timeout):
        assert cmd[:3] == ["unused-driver", "call", "get_window_state"]
        args = json.loads(cmd[3])
        shot = Path(args.pop("screenshot_out_file"))
        assert shot.is_file()
        shot_files.append(shot)
        requests.append(args)
        tree = {"tree_markdown": _TREE} if compact else {"elements": [_ELEMENT]}
        return {**tree, "snapshot_id": _SNAPSHOT, "screenshot_png_b64": "cG5n"}

    monkeypatch.setattr(cua_backend_session, "_cli_run_json", cli_json)
    cap = backend.capture(mode, pid=101, window_id=202)
    expected = _legacy_args()
    if mode != "vision":
        expected["tree_format"] = "elements"
        assert next(e for e in cap.elements if e.index == 7).element_token == f"{_SNAPSHOT}:7"
    else:
        assert cap.elements == [] and backend._snapshot_tokens == {}
    assert requests == [expected] * 2
    assert cap.png_b64 == "cG5n"
    assert all(not shot.exists() for shot in shot_files)
