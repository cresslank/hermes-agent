from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from tools.computer_use.backend import ActionResult, CaptureResult
from tools.computer_use.cua_backend import CuaDriverBackend, _ingest_windows
from tools.computer_use import tool as computer_tool


@pytest.fixture(autouse=True)
def _reset_run_backends():
    computer_tool.reset_backend_for_tests()
    old = os.environ.get("HERMES_COMPUTER_USE_BACKEND")
    os.environ["HERMES_COMPUTER_USE_BACKEND"] = "noop"
    yield
    computer_tool.reset_backend_for_tests()
    if old is None:
        os.environ.pop("HERMES_COMPUTER_USE_BACKEND", None)
    else:
        os.environ["HERMES_COMPUTER_USE_BACKEND"] = old


def _window(*, visible: bool = False) -> Dict[str, Any]:
    return {
        "app_name": "Firefox",
        "pid": 101,
        "window_id": 9001,
        "native_window_id": 77,
        "target_id": "epoch-a:77",
        "helper_epoch": "epoch-a",
        "is_on_screen": visible,
        "title": "Agent page",
        "z_index": 4,
        "workspace_index": 11,
        "workspace_active": visible,
        "sticky": False,
        "monitor": 1,
        "capture_current": visible,
    }


def _backend_for_window(
    window: Dict[str, Any],
) -> tuple[CuaDriverBackend, list[tuple[str, Dict[str, Any]]]]:
    backend = CuaDriverBackend()
    calls: list[tuple[str, Dict[str, Any]]] = []

    def call_tool(name: str, args: Dict[str, Any], timeout: float = 30.0):
        calls.append((name, dict(args)))
        if name == "list_windows":
            return {
                "data": None,
                "images": [],
                "structuredContent": {"windows": [window]},
                "isError": False,
            }
        if name == "get_window_state":
            return {
                "data": '1 elements\n- [1] AXButton "Continue"',
                "images": [],
                "structuredContent": {
                    "elements": [
                        {
                            "index": 1,
                            "role": "AXButton",
                            "label": "Continue",
                            "frame": {"x": 1, "y": 2, "w": 3, "h": 4},
                        }
                    ]
                },
                "isError": False,
            }
        raise AssertionError(f"unexpected tool {name}")

    backend._session = MagicMock()
    backend._session.call_tool.side_effect = call_tool
    backend._session.capabilities_discovered = True
    backend._session._has_tool.return_value = False
    return backend, calls


def test_ingest_preserves_epoch_workspace_and_native_identity():
    [window] = _ingest_windows([_window()])
    assert window["window_id"] == 9001
    assert window["native_window_id"] == 77
    assert window["target_id"] == "epoch-a:77"
    assert window["helper_epoch"] == "epoch-a"
    assert window["workspace_index"] == 11
    assert window["monitor"] == 1
    assert window["capture_current"] is False


def test_inactive_workspace_som_returns_fresh_tree_without_requesting_pixels():
    backend, calls = _backend_for_window(_window(visible=False))

    result = backend.capture(mode="som", app="Firefox")

    gws_args = next(args for name, args in calls if name == "get_window_state")
    assert gws_args["include_screenshot"] is False
    assert result.png_b64 is None
    assert result.meta["capture_status"] == "tree_only"
    assert result.meta["tree_status"] == "current"
    assert result.meta["code"] == "capture_foreground_required"
    assert result.meta["target"]["workspace_index"] == 11


def test_inactive_workspace_vision_fails_typed_without_capture_or_activation():
    backend, calls = _backend_for_window(_window(visible=False))

    result = backend.capture(mode="vision", app="Firefox")

    assert [name for name, _ in calls] == ["list_windows"]
    assert result.png_b64 is None
    assert result.meta["code"] == "capture_foreground_required"
    assert result.meta["capture_status"] == "unavailable_in_background"


def test_raw_action_gets_exact_window_and_foreground_only_after_trusted_grant():
    backend = CuaDriverBackend()
    backend._active_pid = 101
    backend._active_window_id = 9001
    captured: list[tuple[str, Dict[str, Any]]] = []
    backend._action = lambda name, args: (
        captured.append((name, dict(args))) or ActionResult(ok=True, action=name)
    )

    backend.click(x=10, y=20)
    assert captured[-1][1]["window_id"] == 9001
    assert "delivery_mode" not in captured[-1][1]

    backend.set_foreground_authorized(True)
    backend.click(x=10, y=20)
    assert captured[-1][1]["delivery_mode"] == "foreground"

    backend.click(element=1)
    assert "delivery_mode" not in captured[-1][1]


def test_run_keys_own_distinct_backend_instances():
    computer_tool.handle_computer_use(
        {"action": "list_apps"}, task_id="agent-a", session_id="s"
    )
    computer_tool.handle_computer_use(
        {"action": "list_apps"}, task_id="agent-b", session_id="s"
    )

    a = computer_tool._backends["task:agent-a"].backend
    b = computer_tool._backends["task:agent-b"].backend
    assert a is not b


class _ConcurrentBackend:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def is_available(self):
        return True

    def list_apps(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return []


def test_same_run_calls_are_serialized():
    backend = _ConcurrentBackend()
    computer_tool._backends["task:same"] = computer_tool._BackendEntry(backend=backend)  # type: ignore[arg-type]

    threads = [
        threading.Thread(
            target=computer_tool.handle_computer_use,
            args=({"action": "list_apps"},),
            kwargs={"task_id": "same"},
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert backend.max_active == 1


def test_distinct_runs_can_execute_concurrently():
    barrier = threading.Barrier(2)
    backend_a = _ConcurrentBackend(barrier)
    backend_b = _ConcurrentBackend(barrier)
    computer_tool._backends["task:a"] = computer_tool._BackendEntry(backend=backend_a)  # type: ignore[arg-type]
    computer_tool._backends["task:b"] = computer_tool._BackendEntry(backend=backend_b)  # type: ignore[arg-type]

    threads = [
        threading.Thread(
            target=computer_tool.handle_computer_use,
            args=({"action": "list_apps"},),
            kwargs={"task_id": key},
        )
        for key in ("a", "b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert backend_a.max_active == 1
    assert backend_b.max_active == 1


def test_raise_window_requires_separate_foreground_authorization():
    result = json.loads(
        computer_tool.handle_computer_use(
            {"action": "focus_app", "app": "Firefox", "raise_window": True},
            task_id="agent-a",
        )
    )
    assert result["code"] == "foreground_authorization_required"
    assert "briefly activate" in result["hint"]


def test_approve_once_grant_is_consumed_by_one_raw_action(monkeypatch):
    class Backend(_ConcurrentBackend):
        _last_app = "Firefox"

        def __init__(self):
            super().__init__()
            self.foreground: list[bool] = []

        def set_foreground_authorized(self, value: bool):
            self.foreground.append(value)

        def focus_app(self, app, raise_window=False):
            return ActionResult(ok=True, action="focus_app")

        def click(self, **kwargs):
            return ActionResult(ok=True, action="click")

    backend = Backend()
    computer_tool._backends["task:agent-a"] = computer_tool._BackendEntry(
        backend=backend
    )  # type: ignore[arg-type]
    monkeypatch.setattr(computer_tool, "_approval_callback", lambda *_: "approve_once")

    computer_tool.handle_computer_use(
        {"action": "focus_app", "app": "Firefox", "raise_window": True},
        task_id="agent-a",
    )
    assert computer_tool._backends["task:agent-a"].foreground_uses == 1

    computer_tool.handle_computer_use(
        {"action": "click", "coordinate": [10, 20]},
        task_id="agent-a",
    )
    assert computer_tool._backends["task:agent-a"].foreground_uses == 0
    assert True in backend.foreground
