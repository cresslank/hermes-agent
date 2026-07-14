"""Native-Wayland targeting regressions for the cua-driver wrapper."""

from tools.computer_use import cua_backend


def _backend_with_active_window():
    backend = object.__new__(cua_backend.CuaDriverBackend)
    backend._active_pid = 123
    backend._active_window_id = 7
    calls = []

    def record(name: str, args: dict) -> cua_backend.ActionResult:
        calls.append((name, args))
        return cua_backend.ActionResult(ok=True, action=name)

    backend._action = record
    return backend, calls


def test_native_wayland_detection(monkeypatch):
    monkeypatch.setattr(cua_backend.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert cua_backend._is_native_wayland()

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert not cua_backend._is_native_wayland()

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert cua_backend._is_native_wayland()

    monkeypatch.setattr(cua_backend.sys, "platform", "darwin")
    assert not cua_backend._is_native_wayland()


def test_ingest_windows_normalizes_nullable_sort_and_text_fields():
    assert cua_backend._ingest_windows([
        {
            "pid": 123,
            "window_id": 7,
            "app_name": None,
            "title": None,
            "z_index": None,
            "is_on_screen": True,
        }
    ]) == [{
        "app_name": "",
        "pid": 123,
        "window_id": 7,
        "off_screen": False,
        "title": "",
        "z_index": 0,
    }]


def test_wayland_keyboard_actions_forward_window_and_foreground(monkeypatch):
    monkeypatch.setattr(cua_backend, "_is_native_wayland", lambda: True)
    backend, calls = _backend_with_active_window()

    backend.type_text("hello")
    backend.key("escape")
    backend.key("ctrl+shift+a")

    assert calls == [
        ("type_text", {
            "pid": 123,
            "window_id": 7,
            "text": "hello",
            "delivery_mode": "foreground",
        }),
        ("press_key", {
            "pid": 123,
            "window_id": 7,
            "delivery_mode": "foreground",
            "key": "escape",
        }),
        ("hotkey", {
            "pid": 123,
            "window_id": 7,
            "delivery_mode": "foreground",
            "keys": ["ctrl", "shift", "a"],
        }),
    ]


def test_wayland_coordinate_actions_forward_window_and_foreground(monkeypatch):
    monkeypatch.setattr(cua_backend, "_is_native_wayland", lambda: True)
    backend, calls = _backend_with_active_window()

    backend.click(x=10, y=20)
    backend.drag(
        from_xy=(10, 20),
        to_xy=(30, 40),
        button="right",
        modifiers=["shift"],
    )
    backend.scroll(direction="down", amount=4, x=50, y=60)

    assert calls == [
        ("click", {
            "pid": 123,
            "button": "left",
            "x": 10,
            "y": 20,
            "window_id": 7,
            "delivery_mode": "foreground",
        }),
        ("drag", {
            "pid": 123,
            "from_x": 10,
            "from_y": 20,
            "to_x": 30,
            "to_y": 40,
            "window_id": 7,
            "button": "right",
            "modifier": ["shift"],
            "delivery_mode": "foreground",
        }),
        ("scroll", {
            "pid": 123,
            "direction": "down",
            "amount": 4,
            "window_id": 7,
            "delivery_mode": "foreground",
            "x": 50,
            "y": 60,
        }),
    ]


def test_non_wayland_keeps_target_without_forcing_foreground(monkeypatch):
    monkeypatch.setattr(cua_backend, "_is_native_wayland", lambda: False)
    backend, calls = _backend_with_active_window()

    backend.key("escape")
    backend.click(x=10, y=20)

    assert calls == [
        ("press_key", {"pid": 123, "window_id": 7, "key": "escape"}),
        ("click", {
            "pid": 123,
            "button": "left",
            "x": 10,
            "y": 20,
            "window_id": 7,
        }),
    ]
