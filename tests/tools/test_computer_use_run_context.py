"""Deterministic tests for Hermes' trusted computer-use run contract."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from tools.computer_use.backend import ActionResult, CaptureResult
from tools.computer_use import tool as cu


class TrustedBackend:
    """Small in-process backend with an explicit exact-target contract."""

    def __init__(self, name: str, *, background_unavailable: bool = False) -> None:
        self.name = name
        self.calls: List[Any] = []
        self.recapture_proofs: List[Any] = []
        self.target: Optional[Dict[str, Any]] = None
        self.started = 0
        self.stopped = 0
        self.background_unavailable = background_unavailable
        self._session_id = f"session-{name}"
        self._snapshot_tokens = {1: f"{name}:snapshot:1"}
        self._active = 0
        self.max_active = 0
        self.serialized = True
        self._guard = threading.Lock()

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def is_available(self) -> bool:
        return True

    def capture(self, mode: str = "som", app: Optional[str] = None,
                pid: Optional[int] = None, window_id: Optional[int] = None) -> CaptureResult:
        self.calls.append(("capture", {"mode": mode, "app": app, "pid": pid, "window_id": window_id}))
        if pid is not None and window_id is not None:
            self.target = {
                "app_name": app or "Same App",
                "pid": pid,
                "window_id": window_id,
                "helper_epoch": "epoch-1",
                "target_id": "target-A",
                "identity_capabilities": ["workspace-proof", "element-tokens"],
                "workspace_id": "workspace-3",
            }
        return CaptureResult(
            mode=mode, width=100, height=80, app=app or "Same App",
            meta={"target": dict(self.target)} if self.target else {},
        )

    def get_target_proof(self) -> Optional[Dict[str, Any]]:
        return dict(self.target) if self.target else None

    def get_snapshot_tokens(self) -> Dict[int, str]:
        return dict(self._snapshot_tokens)

    def recapture_exact(self, proof: Any, mode: str = "som") -> CaptureResult:
        self.recapture_proofs.append(proof)
        return CaptureResult(
            mode=mode, width=100, height=80, app="Same App",
            meta={"target": dict(self.target)} if self.target else {},
        )

    def _action(self, name: str, kwargs: Dict[str, Any]) -> ActionResult:
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active != 1:
                self.serialized = False
        try:
            self.calls.append((name, dict(kwargs)))
            # Make overlap observable to a test that would fail without the
            # per-run call lock.
            time.sleep(0.002)
            if (
                name == "click"
                and self.background_unavailable
                and kwargs.get("delivery_mode") != "foreground"
            ):
                return ActionResult(
                    ok=False, action="click", code="background_unavailable",
                    message="background path unavailable",
                )
            return ActionResult(
                ok=True, action=name, effect="confirmed",
                delivery_mode=kwargs.get("delivery_mode"),
            )
        finally:
            with self._guard:
                self._active -= 1

    def click(self, **kwargs: Any) -> ActionResult:
        return self._action("click", kwargs)

    def drag(self, **kwargs: Any) -> ActionResult:
        return self._action("drag", kwargs)

    def scroll(self, **kwargs: Any) -> ActionResult:
        return self._action("scroll", kwargs)

    def type_text(self, text: str, **kwargs: Any) -> ActionResult:
        return self._action("type", {"text": text, **kwargs})

    def key(self, keys: str, **kwargs: Any) -> ActionResult:
        return self._action("key", {"keys": keys, **kwargs})

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return self._action("set_value", {"value": value, "element": element})

    def list_apps(self) -> List[Dict[str, Any]]:
        return []

    def list_windows(self) -> List[Dict[str, Any]]:
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        return self._action("focus_app", {"app": app, "raise_window": raise_window})

    def focus_exact(
        self, pid: int, window_id: int, raise_window: bool = False,
    ) -> ActionResult:
        return self._action(
            "focus_app", {
                "pid": pid, "window_id": window_id,
                "raise_window": raise_window,
            },
        )


class LegacyBackend(TrustedBackend):
    """Backend-shaped object without the exact-target hooks."""

    get_target_proof = None  # type: ignore[assignment]
    get_snapshot_tokens = None  # type: ignore[assignment]
    recapture_exact = None  # type: ignore[assignment]


class WeakWaylandBackend(TrustedBackend):
    def requires_incarnation_target_proof(self) -> bool:
        return True

    def capture(self, mode="som", app=None, pid=None, window_id=None):
        result = super().capture(mode=mode, app=app, pid=pid, window_id=window_id)
        if self.target is not None:
            self.target = {"pid": self.target["pid"], "window_id": self.target["window_id"]}
            result.meta["target"] = dict(self.target)
        return result


@pytest.fixture(autouse=True)
def reset_state():
    cu.set_approval_callback(None)
    cu.reset_backend_for_tests()
    yield
    cu.set_approval_callback(None)
    cu.reset_backend_for_tests()


def _install_factory(monkeypatch: pytest.MonkeyPatch, *backends: TrustedBackend) -> None:
    queue = list(backends)

    def factory() -> TrustedBackend:
        if not queue:
            return TrustedBackend("extra")
        return queue.pop(0)

    monkeypatch.setattr(cu, "_new_backend", factory)


def _capture_target(task_id: str = "run-a", tool_call_id: str = "capture-1") -> cu.RunBackendContext:
    cu.handle_computer_use(
        {
            "action": "capture", "mode": "ax", "pid": 101, "window_id": 202,
        },
        task_id=task_id, tool_call_id=tool_call_id,
    )
    context = cu.get_run_backend_context(task_id=task_id)
    assert context is not None
    assert context.target_proof is not None
    return context


def test_default_and_named_runs_have_independent_backend_and_target_state(monkeypatch):
    default_backend = TrustedBackend("default")
    named_backend = TrustedBackend("named")
    _install_factory(monkeypatch, default_backend, named_backend)

    cu.handle_computer_use(
        {"action": "capture", "mode": "ax", "pid": 11, "window_id": 12}
    )
    cu.handle_computer_use(
        {"action": "capture", "mode": "ax", "pid": 21, "window_id": 22},
        task_id="named",
    )

    default_context = cu.get_run_backend_context()
    named_context = cu.get_run_backend_context(task_id="named")
    assert default_context is not None and named_context is not None
    assert default_context.backend is default_backend
    assert named_context.backend is named_backend
    assert default_context.backend is not named_context.backend
    assert default_context.target_proof != named_context.target_proof
    assert default_context.snapshot_tokens[1] == "default:snapshot:1"
    assert named_context.snapshot_tokens[1] == "named:snapshot:1"
    assert set(cu._run_backends) == {"default", "task:named"}


def test_releasing_named_run_does_not_release_default_run(monkeypatch):
    default_backend = TrustedBackend("default")
    named_backend = TrustedBackend("named")
    _install_factory(monkeypatch, default_backend, named_backend)

    cu.handle_computer_use({"action": "capture", "mode": "ax"})
    cu.handle_computer_use({"action": "capture", "mode": "ax"}, task_id="named")
    old_named_context = cu.get_run_backend_context(task_id="named")
    assert old_named_context is not None
    assert cu.release_run_backend_context(task_id="named") is True

    assert named_backend.stopped == 1
    assert default_backend.stopped == 0
    assert "task:named" not in cu._run_backends
    assert "default" in cu._run_backends
    cu.handle_computer_use({"action": "capture", "mode": "ax"})
    assert default_backend.stopped == 0
    new_named_context = cu.get_run_backend_context(task_id="named")
    assert new_named_context is not None
    assert old_named_context.release() is False
    assert new_named_context.released is False


def test_capture_after_recaptures_exact_same_app_target_and_retains_proof(monkeypatch):
    backend = TrustedBackend("same-app")
    _install_factory(monkeypatch, backend)
    context = _capture_target()
    proof = context.target_proof
    assert proof is not None
    assert proof.helper_epoch == "epoch-1"
    assert proof.target_id == "target-A"
    assert proof.identity_capabilities == ("element-tokens", "workspace-proof")
    assert proof.workspace_id == "workspace-3"

    focused = cu.handle_computer_use(
        {"action": "focus_app", "app": "Different Same-App Label"},
        task_id="run-a", tool_call_id="focus-1",
    )
    assert json.loads(focused)["ok"] is True
    focus_call = next(call for call in backend.calls if call[0] == "focus_app")
    assert focus_call[1]["pid"] == 101
    assert focus_call[1]["window_id"] == 202

    result = cu.handle_computer_use(
        {"action": "click", "element": 1, "button": "right",
         "modifiers": ["ctrl"], "capture_after": True},
        task_id="run-a", tool_call_id="click-1",
    )
    assert json.loads(result)["action"] == "click"
    assert len(backend.recapture_proofs) == 1
    assert backend.recapture_proofs[0] == proof
    click_call = next(call for call in backend.calls if call[0] == "click")
    assert click_call[1]["button"] == "right"
    assert click_call[1]["modifiers"] == ["ctrl"]
    assert not any(call[0] == "capture" and call[1].get("app") == "Same App"
                   for call in backend.calls[1:])


def test_capture_after_refuses_same_app_retarget(monkeypatch):
    backend = TrustedBackend("retarget")
    _install_factory(monkeypatch, backend)
    _capture_target()
    backend.target = {
        "app_name": "Same App", "pid": 303, "window_id": 404,
        "helper_epoch": "epoch-2", "target_id": "target-B",
        "identity_capabilities": ["workspace-proof", "element-tokens"],
        "workspace_id": "workspace-3",
    }

    result = cu.handle_computer_use(
        {"action": "click", "element": 1, "capture_after": True},
        task_id="run-a", tool_call_id="click-1",
    )
    payload = json.loads(result)
    assert payload["code"] == "stale_target"
    assert backend.recapture_proofs == []


def test_unauthorized_background_refusal_is_typed_and_not_retried(monkeypatch):
    backend = TrustedBackend("unauthorized", background_unavailable=True)
    _install_factory(monkeypatch, backend)
    context = _capture_target()

    result = cu.handle_computer_use(
        {"action": "click", "element": 1},
        task_id="run-a", tool_call_id="click-unauthorized",
    )
    payload = json.loads(result)
    assert payload["code"] == "foreground_required"
    clicks = [call for call in backend.calls if call[0] == "click"]
    assert len(clicks) == 1
    assert clicks[0][1]["delivery_mode"] is None
    assert context._entry.foreground_authorizations == {}


def test_authorized_background_refusal_escalates_once_with_exact_action(monkeypatch):
    backend = TrustedBackend("authorized", background_unavailable=True)
    _install_factory(monkeypatch, backend)
    context = _capture_target()
    assert context.target_proof is not None
    context.authorize_foreground(
        tool_call_id="click-authorized", action="click",
        target_proof=context.target_proof,
        action_args={"element": 1},
    )

    result = cu.handle_computer_use(
        {"action": "click", "element": 1},
        task_id="run-a", tool_call_id="click-authorized",
    )
    payload = json.loads(result)
    assert payload["ok"] is True
    clicks = [call for call in backend.calls if call[0] == "click"]
    assert len(clicks) == 2
    assert clicks[0][1]["delivery_mode"] is None
    assert clicks[1][1]["delivery_mode"] == "foreground"
    # The one-shot grant cannot authorize a later call with the same id.
    second = cu.handle_computer_use(
        {"action": "click", "element": 1},
        task_id="run-a", tool_call_id="click-authorized",
    )
    assert json.loads(second)["code"] == "foreground_required"


def test_per_run_call_lock_serializes_actions(monkeypatch):
    backend = TrustedBackend("locked")
    _install_factory(monkeypatch, backend)
    _capture_target()

    threads = [
        threading.Thread(
            target=cu.handle_computer_use,
            args=({"action": "click", "element": index},),
            kwargs={"task_id": "run-a", "tool_call_id": f"call-{index}"},
        )
        for index in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert backend.serialized is True
    assert backend.max_active == 1


def test_capability_manifest_is_complete_only_for_full_backend_contract(monkeypatch):
    trusted = TrustedBackend("manifest")
    _install_factory(monkeypatch, trusted)
    context = cu.get_run_backend_context(task_id="full")
    assert context is not None
    manifest = context.capability_manifest
    assert manifest["complete"] is True
    assert set(cu.RUN_BACKEND_CONTEXT_CAPABILITIES) == set(manifest["capabilities"])
    assert manifest["authorization"]["single_use"] is True
    assert set(manifest["authorization"]["bound_fields"]) == {
        "run_key", "tool_call_id", "action", "target", "deadline",
    }
    assert context.supports_capability("exact_recapture") is True

    cu.release_run_backend_context(task_id="full")
    legacy = LegacyBackend("legacy")
    _install_factory(monkeypatch, legacy)
    legacy_context = cu.get_run_backend_context(task_id="legacy")
    assert legacy_context is not None
    assert legacy_context.capability_manifest["complete"] is False
    assert legacy_context.supports_capability("exact_recapture") is False


def test_weak_native_wayland_proof_never_authorizes_foreground_retry(monkeypatch):
    backend = WeakWaylandBackend("weak-wayland", background_unavailable=True)
    _install_factory(monkeypatch, backend)
    context = _capture_target()
    assert context.target_proof is not None
    assert context.target_proof.has_incarnation_identity is False
    assert context.capability_manifest["complete"] is False
    cu.set_approval_callback(lambda *_args: "approve_once")

    result = cu.handle_computer_use(
        {"action": "click", "element": 1},
        task_id="run-a", tool_call_id="weak-click",
    )
    payload = json.loads(result)
    assert payload["code"] == "exact_target_required"
    clicks = [call for call in backend.calls if call[0] == "click"]
    assert len(clicks) == 1
    assert clicks[0][1]["delivery_mode"] is None


def test_authorization_deadline_is_expiring_and_capture_is_separate(monkeypatch):
    backend = TrustedBackend("expiry")
    _install_factory(monkeypatch, backend)
    context = _capture_target()
    assert context.target_proof is not None
    context.authorize_capture(
        tool_call_id="expired", action="capture",
        target_proof=context.target_proof, deadline=0,
    )
    assert context._consume_authorization(
        "capture", tool_call_id="expired", action="capture",
        target=context.target_proof, action_args={"mode": "som"},
    ) is None
    assert context._entry.foreground_authorizations == {}
    context.authorize_foreground(
        tool_call_id="fg", action="click", target_proof=context.target_proof,
    )
    assert context._entry.capture_authorizations == {}
    assert context._consume_authorization(
        "foreground", tool_call_id="fg", action="click",
        target=context.target_proof, action_args=None,
    ) is not None


def test_foreground_capture_requires_its_own_grant(monkeypatch):
    backend = TrustedBackend("capture-auth")
    _install_factory(monkeypatch, backend)
    context = _capture_target()
    assert context.target_proof is not None

    denied = cu.handle_computer_use(
        {"action": "capture", "mode": "ax", "pid": 101, "window_id": 202,
         "delivery_mode": "foreground"},
        task_id="run-a", tool_call_id="capture-call",
    )
    assert json.loads(denied)["code"] == "capture_foreground_required"

    # A foreground/input grant is intentionally not accepted for capture.
    context.authorize_foreground(
        tool_call_id="capture-call", action="capture",
        target_proof=context.target_proof,
    )
    still_denied = cu.handle_computer_use(
        {"action": "capture", "mode": "ax", "pid": 101, "window_id": 202,
         "delivery_mode": "foreground"},
        task_id="run-a", tool_call_id="capture-call",
    )
    assert json.loads(still_denied)["code"] == "capture_foreground_required"

    context.authorize_capture(
        tool_call_id="capture-call", action="capture",
        target_proof=context.target_proof,
        action_args={"mode": "ax", "pid": 101, "window_id": 202},
    )
    allowed = cu.handle_computer_use(
        {"action": "capture", "mode": "ax", "pid": 101, "window_id": 202,
         "delivery_mode": "foreground"},
        task_id="run-a", tool_call_id="capture-call",
    )
    assert json.loads(allowed)["mode"] == "ax"


def test_cua_exact_recapture_rejects_fresh_helper_identity_drift(monkeypatch):
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend()
    expected = {
        "pid": 101,
        "window_id": 202,
        "helper_epoch": "epoch-1",
        "target_id": "target-A",
        "identity_capabilities": ["exact-target-v2", "workspace-proof"],
    }
    backend._active_pid = 101
    backend._active_window_id = 202
    backend._last_target = dict(expected)

    def drifting_capture(**_kwargs):
        actual = {
            **expected,
            "helper_epoch": "epoch-2",
            "target_id": "target-B",
        }
        backend._last_target = actual
        return CaptureResult(
            mode="som", width=100, height=80,
            meta={"target": dict(actual)},
        )

    monkeypatch.setattr(backend, "capture", drifting_capture)
    result = backend.recapture_exact(expected, mode="som")

    assert result.width == 0 and result.height == 0
    assert result.meta["code"] == "capture_failed"
    assert "changed target identity" in result.meta["message"]
    assert backend.get_target_proof() is None


def test_native_wayland_incarnation_requires_complete_safety_manifest():
    full_manifest = [
        "exact-target-v2",
        "workspace-proof",
        "unoccluded-target-v1",
        "atomic-target-capture-v1",
        "foreground-revalidate-v1",
        "foreground-reconcile-v1",
        "target-bound-raw-input-v1",
    ]
    proof = cu.TargetProof.from_value(
        {
            "pid": 101,
            "window_id": 202,
            "helper_epoch": "epoch-1",
            "target_id": "epoch-1:202",
            "identity_capabilities": full_manifest,
        }
    )
    assert proof is not None and proof.has_incarnation_identity is True

    for capability in full_manifest:
        incomplete = cu.TargetProof.from_value(
            {
                "pid": 101,
                "window_id": 202,
                "helper_epoch": "epoch-1",
                "target_id": "epoch-1:202",
                "identity_capabilities": [
                    item for item in full_manifest if item != capability
                ],
            }
        )
        assert incomplete is not None
        assert incomplete.has_incarnation_identity is False
