"""Native-Wayland action-targeting regressions for the cua-driver backend."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from tools.computer_use import cua_backend


class _RecordingCuaSession:
    """Small fake of the CUA session at the transport boundary."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def supports_capability(self, capability: str, tool: Optional[str] = None) -> bool:
        return capability == "accessibility.element_tokens"

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(args)))
        return {"isError": False, "data": {}}


def _backend_with_active_window(window_id: Optional[int] = 7):
    backend = object.__new__(cua_backend.CuaDriverBackend)
    backend._active_pid = 123
    backend._active_window_id = window_id
    backend._snapshot_tokens = {}
    backend._session_id = "test-session"
    session = _RecordingCuaSession()
    setattr(backend, "_session", session)
    return backend, session


def _set_linux_display(monkeypatch, *, wayland: bool) -> None:
    monkeypatch.setattr(cua_backend.sys, "platform", "linux")
    if wayland:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    else:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def test_native_wayland_detection(monkeypatch):
    _set_linux_display(monkeypatch, wayland=True)
    assert cua_backend._is_native_wayland() is True

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert cua_backend._is_native_wayland() is False

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert cua_backend._is_native_wayland() is True

    monkeypatch.setattr(cua_backend.sys, "platform", "darwin")
    assert cua_backend._is_native_wayland() is False


def test_wayland_actions_target_active_window_and_foreground(monkeypatch):
    _set_linux_display(monkeypatch, wayland=True)
    backend, session = _backend_with_active_window()

    backend.click(x=10, y=20)
    backend.click(x=11, y=21, click_count=2)
    backend.drag(
        from_xy=(10, 20),
        to_xy=(30, 40),
        button="right",
        modifiers=["shift"],
    )
    backend.scroll(direction="down", amount=4, x=50, y=60)
    backend.type_text("hello")
    backend.key("escape")
    backend.key("ctrl+shift+a")

    assert session.calls == [
        (
            "click",
            {
                "pid": 123,
                "button": "left",
                "x": 10,
                "y": 20,
                "window_id": 7,
                "delivery_mode": "foreground",
                "session": "test-session",
            },
        ),
        (
            "double_click",
            {
                "pid": 123,
                "button": "left",
                "x": 11,
                "y": 21,
                "window_id": 7,
                "delivery_mode": "foreground",
                "session": "test-session",
            },
        ),
        (
            "drag",
            {
                "pid": 123,
                "from_x": 10,
                "from_y": 20,
                "to_x": 30,
                "to_y": 40,
                "button": "right",
                "modifier": ["shift"],
                "window_id": 7,
                "delivery_mode": "foreground",
                "session": "test-session",
            },
        ),
        (
            "scroll",
            {
                "pid": 123,
                "direction": "down",
                "amount": 4,
                "window_id": 7,
                "delivery_mode": "foreground",
                "x": 50,
                "y": 60,
                "session": "test-session",
            },
        ),
        (
            "type_text",
            {
                "pid": 123,
                "text": "hello",
                "window_id": 7,
                "delivery_mode": "foreground",
                "session": "test-session",
            },
        ),
        (
            "press_key",
            {
                "pid": 123,
                "window_id": 7,
                "delivery_mode": "foreground",
                "key": "escape",
                "session": "test-session",
            },
        ),
        (
            "hotkey",
            {
                "pid": 123,
                "window_id": 7,
                "delivery_mode": "foreground",
                "keys": ["ctrl", "shift", "a"],
                "session": "test-session",
            },
        ),
    ]


def test_wayland_element_token_is_preserved_with_exact_target(monkeypatch):
    _set_linux_display(monkeypatch, wayland=True)
    backend, session = _backend_with_active_window()
    backend._snapshot_tokens = {3: "snapshot-token-3"}

    backend.click(element=3)

    assert session.calls == [
        (
            "click",
            {
                "pid": 123,
                "button": "left",
                "element_index": 3,
                "window_id": 7,
                "delivery_mode": "foreground",
                "element_token": "snapshot-token-3",
                "session": "test-session",
            },
        )
    ]


def test_wayland_element_drag_and_scroll_keep_exact_target(monkeypatch):
    _set_linux_display(monkeypatch, wayland=True)
    backend, session = _backend_with_active_window()

    backend.drag(from_element=3, to_element=4)
    backend.scroll(direction="down", element=3)

    assert [
        (name, args["window_id"], args["delivery_mode"]) for name, args in session.calls
    ] == [
        ("drag", 7, "foreground"),
        ("scroll", 7, "foreground"),
    ]


def test_non_wayland_actions_keep_their_existing_target_shape(monkeypatch):
    _set_linux_display(monkeypatch, wayland=False)
    backend, session = _backend_with_active_window()

    backend.click(x=10, y=20)
    backend.drag(
        from_xy=(10, 20),
        to_xy=(30, 40),
        button="right",
        modifiers=["shift"],
    )
    backend.scroll(direction="down", amount=4, x=50, y=60)
    backend.type_text("hello")
    backend.key("escape")
    backend.key("ctrl+a")

    assert session.calls == [
        (
            "click",
            {"pid": 123, "button": "left", "x": 10, "y": 20, "session": "test-session"},
        ),
        (
            "drag",
            {
                "pid": 123,
                "from_x": 10,
                "from_y": 20,
                "to_x": 30,
                "to_y": 40,
                "button": "right",
                "modifier": ["shift"],
                "session": "test-session",
            },
        ),
        (
            "scroll",
            {
                "pid": 123,
                "direction": "down",
                "amount": 4,
                "x": 50,
                "y": 60,
                "session": "test-session",
            },
        ),
        ("type_text", {"pid": 123, "text": "hello", "session": "test-session"}),
        ("press_key", {"pid": 123, "key": "escape", "session": "test-session"}),
        (
            "hotkey",
            {"pid": 123, "keys": ["ctrl", "a"], "session": "test-session"},
        ),
    ]


def test_native_wayland_requires_exact_window_target_before_transport(monkeypatch):
    _set_linux_display(monkeypatch, wayland=True)
    backend, session = _backend_with_active_window(window_id=None)

    results = [
        backend.click(x=10, y=20),
        backend.click(x=10, y=20, click_count=2),
        backend.drag(from_xy=(10, 20), to_xy=(30, 40)),
        backend.scroll(direction="down", amount=4, x=50, y=60),
        backend.type_text("hello"),
        backend.key("escape"),
        backend.key("ctrl+a"),
    ]

    assert all(not result.ok for result in results)
    assert session.calls == []


def test_coordinate_drag_preserves_button_and_modifiers(monkeypatch):
    _set_linux_display(monkeypatch, wayland=False)
    backend, session = _backend_with_active_window()

    backend.drag(
        from_xy=(1, 2),
        to_xy=(3, 4),
        button="middle",
        modifiers=["ctrl", "alt"],
    )

    assert session.calls[0][1]["button"] == "middle"
    assert session.calls[0][1]["modifier"] == ["ctrl", "alt"]
