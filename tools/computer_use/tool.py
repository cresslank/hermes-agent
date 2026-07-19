"""Entry point for the `computer_use` tool.

Universal (any-model) desktop control across macOS, Windows, and Linux via
cua-driver's background computer-use primitive. Replaces #4562's
Anthropic-native `computer_20251124` approach — the schema here is standard
OpenAI function-calling so every tool-capable model can drive it.

Linux is the most recent runtime (X11 + Wayland, via cua-driver-rs's
AT-SPI tree path); it is enabled here alongside macOS and Windows. When a
host's display server or accessibility stack isn't reachable, cua-driver's
`health_report` (surfaced by `hermes computer-use doctor`) reports the
exact blocked check rather than the toolset silently failing.

Return contract
---------------
For text-only results (wait, key, list_apps, focus_app, failures, etc.):
  JSON string.

For captures / actions with `capture_after=True`:
  A dict wrapped as the OpenAI-style multi-part tool-message content:

      {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "<human-readable summary + SOM index>"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<b64>"}},
        ],
        "text_summary": "<text used for fallback string content>",
      }

  run_agent.py's tool-message builder inspects `_multimodal` and emits a
  list-shaped `content` for OpenAI-compatible providers. The Anthropic
  adapter splices the base64 image into a `tool_result` block (see
  `agent/anthropic_adapter.py`). Every provider that supports multi-part
  tool content gets the image; text-only providers see the summary only.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, cast

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval & safety
# ---------------------------------------------------------------------------

_approval_callback = None


def set_approval_callback(cb) -> None:
    """Register a callback for computer_use approval prompts (used by CLI).

    Matches the terminal_tool._approval_callback pattern. The callback
    receives (action, args, summary) and returns one of:
      "approve_once" | "approve_session" | "always_approve" | "deny".
    """
    global _approval_callback
    _approval_callback = cb


# Actions that read, not mutate. Always allowed.
_SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps"})

# Actions that mutate user-visible state. Go through approval.
_DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
})

# Hard-blocked key combinations. Mirrored from #4562 — these are destructive
# regardless of approval level (e.g. logout kills the session Hermes runs in).
_BLOCKED_KEY_COMBOS = {
    frozenset({"cmd", "shift", "backspace"}),   # empty trash
    frozenset({"cmd", "option", "backspace"}),   # force delete
    frozenset({"cmd", "ctrl", "q"}),             # lock screen
    frozenset({"cmd", "shift", "q"}),            # log out
    frozenset({"cmd", "option", "shift", "q"}),  # force log out
    # Windows secure/session shortcuts. The Windows driver accepts Win-key
    # combos, and Alt is canonicalized to option below, so block the
    # destructive variants before any backend sees them.
    frozenset({"win", "l"}),
    frozenset({"ctrl", "option", "delete"}),
    frozenset({"ctrl", "option", "del"}),
    frozenset({"option", "f4"}),
}

_KEY_ALIASES = {
    "command": "cmd", "control": "ctrl", "alt": "option", "⌘": "cmd", "⌥": "option",
    "windows": "win", "super": "win", "meta": "win",
}


def _canon_key_combo(keys: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)


# Dangerous text patterns for the `type` action. Same list as #4562.
_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork bomb
]


def _is_blocked_type(text: str) -> Optional[str]:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ---------------------------------------------------------------------------
# Trusted per-run backend context
# ---------------------------------------------------------------------------

# This is intentionally an internal contract.  It is not a model-facing
# selector and no field from the computer_use argument object is accepted as
# a run key.  The dispatcher supplies task/session/tool-call metadata through
# kwargs (and a ContextVar for plugin hooks); the tool handler normalizes it
# again at its final boundary.
_RUN_CONTEXT_API_VERSION = "hermes.run-backend-context.v1"
_RUN_CONTEXT_REQUIRED_CAPABILITIES = (
    "run_lookup",
    "per_run_backend",
    "per_run_call_lock",
    "target_proof",
    "exact_recapture",
    "exact_focus",
    "snapshot_map",
    "capture_authorization",
    "foreground_authorization",
    "structured_results",
    "release",
)
_RUN_CONTEXT_CAPABILITY_MANIFEST = MappingProxyType({
    "api_version": _RUN_CONTEXT_API_VERSION,
    "version": _RUN_CONTEXT_API_VERSION,
    "capabilities": tuple(_RUN_CONTEXT_REQUIRED_CAPABILITIES),
    "authorization": MappingProxyType({
        "single_use": True,
        "bound_fields": ("run_key", "tool_call_id", "action", "target", "deadline"),
    }),
})
RUN_BACKEND_CONTEXT_CAPABILITY_MANIFEST = _RUN_CONTEXT_CAPABILITY_MANIFEST
RUN_BACKEND_CONTEXT_CAPABILITIES = _RUN_CONTEXT_REQUIRED_CAPABILITIES
_AUTHORIZATION_TTL_SECONDS = 5.0


def _normalize_dispatch_value(value: Any) -> str:
    """Normalize trusted dispatch identifiers without accepting booleans."""
    if value is None or isinstance(value, bool):
        return ""
    value = str(value).strip()
    return value


def normalize_computer_use_run_key(
    *, task_id: Any = None, session_id: Any = None
) -> str:
    """Return the one authoritative key used by all computer-use state.

    A task/run id is more specific than a transport session id.  Prefixes
    avoid collisions such as task ``"session:x"`` and session ``"x"`` and
    make the default bucket explicit for the classic single-run CLI.
    """
    task = _normalize_dispatch_value(task_id)
    if task:
        return f"task:{task[:240]}"
    session = _normalize_dispatch_value(session_id)
    if session:
        return f"session:{session[:237]}"
    return "default"


# Kept as a short internal alias for callers/tests that used the old helper.
def _run_key(
    kwargs: Optional[Mapping[str, Any]] = None, *, task_id: Any = None, session_id: Any = None
) -> str:
    if kwargs is not None:
        task_id = kwargs.get("task_id")
        session_id = kwargs.get("session_id")
    return normalize_computer_use_run_key(task_id=task_id, session_id=session_id)


_REQUIRED_NATIVE_WAYLAND_TARGET_CAPABILITIES = frozenset(
    {
        "exact-target-v2",
        "workspace-proof",
        "unoccluded-target-v1",
        "atomic-target-capture-v1",
        "foreground-revalidate-v1",
        "foreground-reconcile-v1",
        "target-bound-raw-input-v1",
    }
)


@dataclass(frozen=True)
class TargetProof:
    """Immutable identity of the native target established by a capture.

    ``helper_epoch``, ``target_id`` and ``identity_capabilities`` are
    optional because older cua-driver versions do not publish them.  When a
    backend publishes one, it is part of equality: a pair of matching
    pid/window integers is not enough to silently accept a new target.
    """

    pid: int
    window_id: int
    helper_epoch: Optional[str] = None
    target_id: Optional[str] = None
    identity_capabilities: Optional[Tuple[str, ...]] = None
    native_window_id: Optional[int] = None
    workspace_id: Optional[str] = None
    monitor_id: Optional[str] = None

    @classmethod
    def from_value(cls, value: Any) -> Optional["TargetProof"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        try:
            pid = int(value.get("pid"))
            window_id = int(value.get("window_id"))
        except (TypeError, ValueError):
            return None
        if pid <= 0 or window_id <= 0:
            return None
        caps_value = value.get(
            "identity_capabilities",
            value.get("capabilities", value.get("capability_manifest")),
        )
        if caps_value is None:
            caps = None
        elif isinstance(caps_value, Mapping):
            caps = tuple(sorted(str(key) for key in caps_value))
        elif isinstance(caps_value, (list, tuple, set, frozenset)):
            caps = tuple(sorted(str(item) for item in caps_value))
        else:
            caps = (str(caps_value),)
        native_window_id = value.get("native_window_id")
        try:
            native_window_id = int(native_window_id) if native_window_id is not None else None
        except (TypeError, ValueError):
            native_window_id = None
        target_identity = value.get(
            "target_id", value.get("target_identity", value.get("identity"))
        )
        if isinstance(target_identity, Mapping):
            target_identity = json.dumps(target_identity, sort_keys=True, separators=(",", ":"))
        return cls(
            pid=pid,
            window_id=window_id,
            helper_epoch=_normalize_dispatch_value(
                value.get("helper_epoch", value.get("epoch"))
            ) or None,
            target_id=_normalize_dispatch_value(target_identity) or None,
            identity_capabilities=caps,
            native_window_id=native_window_id,
            workspace_id=_normalize_dispatch_value(
                value.get(
                    "workspace_id",
                    value.get("workspace", value.get("workspace_index")),
                )
            ) or None,
            monitor_id=_normalize_dispatch_value(
                value.get("monitor_id", value.get("monitor"))
            ) or None,
        )

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "pid": self.pid,
            "window_id": self.window_id,
        }
        if self.helper_epoch is not None:
            result["helper_epoch"] = self.helper_epoch
        if self.target_id is not None:
            result["target_id"] = self.target_id
        if self.identity_capabilities is not None:
            result["identity_capabilities"] = list(self.identity_capabilities)
        if self.native_window_id is not None:
            result["native_window_id"] = self.native_window_id
        if self.workspace_id is not None:
            result["workspace_id"] = self.workspace_id
        if self.monitor_id is not None:
            result["monitor_id"] = self.monitor_id
        return result

    @property
    def capabilities(self) -> Optional[Tuple[str, ...]]:
        """Compatibility spelling used by some plugin probes."""
        return self.identity_capabilities

    @property
    def target_identity(self) -> Optional[str]:
        """Compatibility spelling for the immutable backend target id."""
        return self.target_id

    @property
    def has_incarnation_identity(self) -> bool:
        capabilities = set(self.identity_capabilities or ())
        return bool(
            self.helper_epoch
            and self.target_id
            and self.target_id.startswith(f"{self.helper_epoch}:")
            and _REQUIRED_NATIVE_WAYLAND_TARGET_CAPABILITIES.issubset(capabilities)
        )


def _target_strength_is_sufficient(
    backend: ComputerUseBackend, target: Optional[TargetProof]
) -> bool:
    requires = getattr(backend, "requires_incarnation_target_proof", None)
    try:
        strong_required = bool(callable(requires) and requires())
    except Exception:
        strong_required = True
    return target is not None and (
        not strong_required or target.has_incarnation_identity
    )


def _action_fingerprint(action: str, args: Optional[Mapping[str, Any]] = None) -> str:
    """Canonicalize the exact action payload for authorization binding."""
    payload: Dict[str, Any] = {"action": action}
    if args:
        # Delivery and post-capture controls do not change the input action;
        # all target selectors, element tokens, drag modifiers/buttons and
        # values remain in the fingerprint.
        payload.update({
            str(key): value
            for key, value in args.items()
            if key not in {"delivery_mode", "capture_after"}
        })
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(sorted(payload.items(), key=lambda item: item[0]))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunAuthorization:
    """One expiring, single-use authorization for one exact tool call."""

    kind: str
    run_key: str
    tool_call_id: str
    action: str
    target: Optional[TargetProof]
    deadline: float
    action_fingerprint: Optional[str] = None

    @property
    def target_proof(self) -> Optional[TargetProof]:
        return self.target

    @property
    def action_class(self) -> str:
        return self.action

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (time.monotonic() if now is None else now) >= self.deadline


@dataclass
class _BackendEntry:
    backend: ComputerUseBackend
    call_lock: threading.RLock = field(default_factory=threading.RLock)
    context: Optional["RunBackendContext"] = None
    capture_authorizations: Dict[str, RunAuthorization] = field(default_factory=dict)
    foreground_authorizations: Dict[str, RunAuthorization] = field(default_factory=dict)
    released: bool = False
    last_used: float = field(default_factory=time.monotonic)


def _target_proof_from_backend(
    backend: ComputerUseBackend, capture: Optional[CaptureResult] = None,
    fallback_args: Optional[Mapping[str, Any]] = None,
) -> Optional[TargetProof]:
    """Read a proof only from backend/capture metadata, never from app name."""
    raw: Any = None
    if capture is not None and isinstance(capture.meta, Mapping):
        raw = capture.meta.get("target", capture.meta.get("target_proof"))
        if raw is None and {"pid", "window_id"}.issubset(capture.meta):
            raw = capture.meta
    getter = getattr(backend, "get_target_proof", None)
    if raw is None and callable(getter):
        try:
            raw = getter()
        except Exception:
            raw = None
    if raw is None:
        raw = getattr(backend, "_last_target", None)
    if raw is None:
        pid = getattr(backend, "_active_pid", None)
        window_id = getattr(backend, "_active_window_id", None)
        if pid is not None and window_id is not None:
            raw = {"pid": pid, "window_id": window_id}
    if raw is None and fallback_args:
        if fallback_args.get("pid") is not None and fallback_args.get("window_id") is not None:
            raw = {"pid": fallback_args.get("pid"), "window_id": fallback_args.get("window_id")}
    return TargetProof.from_value(raw)


def _proof_matches(expected: Optional[TargetProof], actual: Optional[TargetProof]) -> bool:
    return expected is not None and actual is not None and expected == actual


def _authorization_deadline(deadline: Any = None, ttl_seconds: Any = None) -> float:
    if deadline is not None:
        try:
            return float(deadline)
        except (TypeError, ValueError):
            return time.monotonic() - 1
    try:
        ttl = float(ttl_seconds) if ttl_seconds is not None else _AUTHORIZATION_TTL_SECONDS
    except (TypeError, ValueError):
        ttl = _AUTHORIZATION_TTL_SECONDS
    return time.monotonic() + max(0.0, min(ttl, 60.0))


class RunBackendContext:
    """Narrow trusted handle for plugins and the final tool boundary.

    The accessor that returns this object is not a model tool and has no
    model-settable selector.  A plugin receives the current dispatch context
    through ``get_run_backend_context()``; explicit task/session arguments
    are retained only as a testing/in-process integration convenience.
    """

    def __init__(self, run_key: str, entry: _BackendEntry) -> None:
        self._run_key = run_key
        self._entry = entry

    @property
    def run_key(self) -> str:
        return self._run_key

    @property
    def backend(self) -> ComputerUseBackend:
        return self._entry.backend

    @property
    def session_id(self) -> Optional[str]:
        value = getattr(self._entry.backend, "_session_id", None)
        return str(value) if value is not None else None

    @property
    def released(self) -> bool:
        return bool(self._entry.released)

    @property
    def target_proof(self) -> Optional[TargetProof]:
        return getattr(self, "_target", None)

    @property
    def snapshot_tokens(self) -> Mapping[int, str]:
        getter = getattr(self._entry.backend, "get_snapshot_tokens", None)
        tokens: Any = None
        if callable(getter):
            try:
                tokens = getter()
            except Exception:
                tokens = None
        if tokens is None:
            tokens = getattr(self._entry.backend, "_snapshot_tokens", {})
        return MappingProxyType(dict(tokens or {}))

    @property
    def capability_manifest(self) -> Dict[str, Any]:
        return _capability_manifest_for_context(self)

    @property
    def capabilities(self) -> Tuple[str, ...]:
        return tuple(self.capability_manifest.get("capabilities", ()))

    def supports_capability(self, capability: str) -> bool:
        return capability in self.capabilities and bool(self.capability_manifest.get("complete"))

    def _set_target_from_capture(
        self, capture: CaptureResult, args: Optional[Mapping[str, Any]] = None
    ) -> Optional[TargetProof]:
        proof = _target_proof_from_backend(self.backend, capture, args)
        if proof is not None:
            self._target = proof
        return proof

    def _current_backend_target(self) -> Optional[TargetProof]:
        return _target_proof_from_backend(self.backend)

    def target_is_current(self) -> bool:
        expected = self.target_proof
        if expected is None:
            return False
        actual = self._current_backend_target()
        return _proof_matches(expected, actual)

    def _authorize(
        self,
        kind: str,
        *,
        tool_call_id: Any,
        action: str,
        target_proof: Any = None,
        target: Any = None,
        deadline: Any = None,
        expires_at: Any = None,
        ttl_seconds: Any = None,
        action_args: Optional[Mapping[str, Any]] = None,
        action_payload: Optional[Mapping[str, Any]] = None,
    ) -> RunAuthorization:
        call_id = _normalize_dispatch_value(tool_call_id)
        if self._entry.released:
            raise RuntimeError("run backend context has been released")
        if not call_id:
            raise ValueError("tool_call_id is required for a run authorization")
        action_name = _normalize_dispatch_value(action)
        if not action_name:
            raise ValueError("action is required for a run authorization")
        if target_proof is None:
            target_proof = target
        target = TargetProof.from_value(target_proof)
        if target_proof is None:
            target = self.target_proof
        if target is None:
            raise ValueError("target_proof is required for a run authorization")
        if deadline is None:
            deadline = expires_at
        if action_args is None:
            action_args = action_payload
        authorization = RunAuthorization(
            kind=kind,
            run_key=self.run_key,
            tool_call_id=call_id,
            action=action_name,
            target=target,
            deadline=_authorization_deadline(deadline, ttl_seconds),
            action_fingerprint=(
                _action_fingerprint(action_name, action_args) if action_args is not None else None
            ),
        )
        with self._entry.call_lock:
            table = (
                self._entry.capture_authorizations
                if kind == "capture" else self._entry.foreground_authorizations
            )
            table[call_id] = authorization
        return authorization

    def authorize_capture(
        self, tool_call_id: Any = None, action: str = "", target_proof: Any = None, **kwargs
    ) -> RunAuthorization:
        """Grant one exact capture/recapture operation to a plugin."""
        if tool_call_id is not None:
            kwargs["tool_call_id"] = tool_call_id
        if action:
            kwargs["action"] = action
        if target_proof is not None:
            kwargs["target_proof"] = target_proof
        return self._authorize("capture", **kwargs)

    def authorize_foreground(
        self, tool_call_id: Any = None, action: str = "", target_proof: Any = None, **kwargs
    ) -> RunAuthorization:
        """Grant one exact foreground action to a plugin."""
        if tool_call_id is not None:
            kwargs["tool_call_id"] = tool_call_id
        if action:
            kwargs["action"] = action
        if target_proof is not None:
            kwargs["target_proof"] = target_proof
        return self._authorize("foreground", **kwargs)

    def _consume_authorization(
        self, kind: str, *, tool_call_id: Any, action: str,
        target: Optional[TargetProof], action_args: Optional[Mapping[str, Any]] = None,
    ) -> Optional[RunAuthorization]:
        if self._entry.released:
            return None
        call_id = _normalize_dispatch_value(tool_call_id)
        if not call_id:
            return None
        with self._entry.call_lock:
            table = (
                self._entry.capture_authorizations
                if kind == "capture" else self._entry.foreground_authorizations
            )
            authorization = table.get(call_id)
            if authorization is None:
                return None
            # A failed match or expiry does not consume a grant; this lets a
            # plugin diagnose a bad call id/target while still preventing a
            # mismatched action from using it.
            if authorization.is_expired():
                table.pop(call_id, None)
                return None
            if authorization.action != action:
                return None
            if authorization.target != target:
                return None
            if (
                authorization.action_fingerprint is not None
                and authorization.action_fingerprint != _action_fingerprint(action, action_args)
            ):
                return None
            # Single use: remove before the backend call, so an exception or
            # concurrent retry cannot spend the same authorization twice.
            table.pop(call_id, None)
            return authorization

    def _discard_authorization(self, kind: str, tool_call_id: Any) -> None:
        call_id = _normalize_dispatch_value(tool_call_id)
        if not call_id:
            return
        with self._entry.call_lock:
            table = (
                self._entry.capture_authorizations
                if kind == "capture" else self._entry.foreground_authorizations
            )
            table.pop(call_id, None)

    def recapture_exact(
        self, target_proof: Any = None, mode: str = "som", *,
        tool_call_id: Any = None,
    ) -> CaptureResult:
        """Recapture the current target, refusing stale/retargeted results."""
        expected = TargetProof.from_value(target_proof) if target_proof is not None else self.target_proof
        if expected is None:
            return _failed_capture(mode, "stale_target: no established target proof")
        if not _target_strength_is_sufficient(self.backend, expected):
            return _failed_capture(
                mode,
                "stale_target: native Wayland exact recapture requires helper-incarnation identity",
            )
        with self._entry.call_lock:
            if not _proof_matches(expected, self._current_backend_target()):
                return _failed_capture(mode, "stale_target: target proof changed")
            # A direct trusted recapture carrying a tool-call id must spend a
            # matching capture grant.  Internal capture-after calls omit the
            # id because the parent action already passed its boundary check.
            if tool_call_id:
                if self._consume_authorization(
                    "capture", tool_call_id=tool_call_id, action="capture",
                    target=expected, action_args={"mode": mode},
                ) is None:
                    return _failed_capture(
                        mode, "capture_foreground_required: capture authorization missing"
                    )
            method = getattr(self.backend, "recapture_exact", None)
            if not callable(method):
                return _failed_capture(mode, "stale_target: exact recapture unsupported")
            try:
                capture = method(expected, mode=mode)
            except TypeError:
                # A small compatibility concession for early trusted backends
                # that used positional mode; never synthesize a new target.
                try:
                    capture = method(expected, mode)
                except Exception as exc:
                    return _failed_capture(mode, f"stale_target: {exc}")
            except Exception as exc:
                return _failed_capture(mode, f"stale_target: {exc}")
            actual = _target_proof_from_backend(self.backend, capture)
            if not isinstance(capture, CaptureResult) or not _proof_matches(expected, actual):
                return _failed_capture(mode, "stale_target: recapture returned a different target")
            self._target = expected
            return capture

    def release(self) -> bool:
        return release_run_backend_context(self)


@dataclass(frozen=True)
class _DispatchIdentity:
    run_key: str
    tool_call_id: str = ""


_CURRENT_DISPATCH: contextvars.ContextVar[Optional[_DispatchIdentity]] = contextvars.ContextVar(
    "hermes_computer_use_dispatch", default=None
)


def bind_run_backend_context(
    *, task_id: Any = None, session_id: Any = None, tool_call_id: Any = None
):
    """Bind trusted dispatcher metadata for plugin hooks (internal API)."""
    identity = _DispatchIdentity(
        run_key=normalize_computer_use_run_key(task_id=task_id, session_id=session_id),
        tool_call_id=_normalize_dispatch_value(tool_call_id),
    )
    return _CURRENT_DISPATCH.set(identity)


def reset_run_backend_context(token) -> None:
    _CURRENT_DISPATCH.reset(token)


def _capability_manifest_for_context(context: RunBackendContext) -> Dict[str, Any]:
    backend = context.backend
    native_hooks = all(callable(getattr(backend, name, None)) for name in (
        "get_target_proof", "get_snapshot_tokens", "recapture_exact", "focus_exact",
    ))
    # The abstract compatibility hooks exist on ComputerUseBackend even for
    # old backends; a native contract is complete only when recapture_exact is
    # actually overridden (or the concrete backend advertises the hook).
    recapture_impl = getattr(type(backend), "recapture_exact", None)
    focus_impl = getattr(type(backend), "focus_exact", None)
    try:
        from tools.computer_use.backend import ComputerUseBackend as _BackendBase
        native_hooks = native_hooks and recapture_impl is not _BackendBase.recapture_exact
        native_hooks = native_hooks and focus_impl is not _BackendBase.focus_exact
    except Exception:
        pass
    probes = {
        "run_lookup": _run_backends.get(context.run_key) is context._entry,
        "target_proof": callable(getattr(backend, "get_target_proof", None)),
        "capture_authorization": callable(getattr(context, "authorize_capture", None)),
        "foreground_authorization": callable(getattr(context, "authorize_foreground", None)),
        "structured_results": callable(globals().get("_text_response")),
        "release": callable(globals().get("release_run_backend_context")),
    }
    probes["target_proof_strength"] = (
        context.target_proof is None
        or _target_strength_is_sufficient(backend, context.target_proof)
    )
    probes["exact_recapture"] = native_hooks
    probes["exact_focus"] = native_hooks
    result = {
        "api_version": _RUN_CONTEXT_API_VERSION,
        "version": _RUN_CONTEXT_API_VERSION,
        "capabilities": list(_RUN_CONTEXT_REQUIRED_CAPABILITIES),
        "complete": bool(native_hooks and all(probes.values())),
        "probes": probes,
        "authorization": {
            "single_use": True,
            "bound_fields": ["run_key", "tool_call_id", "action", "target", "deadline"],
        },
    }
    driver_manifest = getattr(backend, "capability_manifest", None)
    if isinstance(driver_manifest, Mapping):
        result["driver"] = dict(driver_manifest)
    return result


def get_run_backend_capability_manifest(
    context: Optional[RunBackendContext] = None,
) -> Dict[str, Any]:
    """Return a serializable copy of the trusted run-context manifest."""
    if context is None:
        identity = _CURRENT_DISPATCH.get()
        if identity is not None:
            context = get_run_backend_context()
        else:
            with _backend_lock:
                entry = _run_backends.get("default")
            context = entry.context if entry is not None else None
    if context is None:
        return {
            "api_version": _RUN_CONTEXT_API_VERSION,
            "version": _RUN_CONTEXT_API_VERSION,
            "capabilities": list(_RUN_CONTEXT_REQUIRED_CAPABILITIES),
            "complete": False,
            "probes": {name: False for name in (
                "run_lookup", "target_proof", "capture_authorization",
                "foreground_authorization", "structured_results", "release",
                "exact_recapture", "exact_focus",
            )},
            "authorization": {
                "single_use": True,
                "bound_fields": ["run_key", "tool_call_id", "action", "target", "deadline"],
            },
        }
    return _capability_manifest_for_context(context)


def get_run_backend_context(
    *, task_id: Any = None, session_id: Any = None, tool_call_id: Any = None
) -> Optional[RunBackendContext]:
    """Return the current run's trusted context for plugin code.

    With no identifiers this reads the dispatcher ContextVar, so a model
    cannot select another run.  Explicit identifiers are only for trusted
    in-process callers/tests and are normalized by this same function.
    """
    identity = _CURRENT_DISPATCH.get()
    if task_id is not None or session_id is not None:
        identity = _DispatchIdentity(
            run_key=normalize_computer_use_run_key(task_id=task_id, session_id=session_id),
            tool_call_id=(
                _normalize_dispatch_value(tool_call_id)
                if tool_call_id is not None
                else (identity.tool_call_id if identity is not None else "")
            ),
        )
    elif tool_call_id is not None:
        identity = _DispatchIdentity(
            run_key=identity.run_key if identity is not None else "default",
            tool_call_id=_normalize_dispatch_value(tool_call_id),
        )
    if identity is None:
        # Outside a model dispatch the explicit no-selector form addresses
        # the compatibility/default run.  A named run still requires its
        # trusted task/session metadata.
        identity = _DispatchIdentity(run_key="default")
    entry = _get_or_attach_backend_entry(identity.run_key)
    return entry.context


def authorize_capture_for_run(
    *, task_id: Any = None, session_id: Any = None, **kwargs
) -> RunAuthorization:
    """Trusted plugin convenience wrapper for one capture authorization."""
    context = get_run_backend_context(task_id=task_id, session_id=session_id)
    if context is None:
        raise RuntimeError("no active computer-use run")
    return context.authorize_capture(**kwargs)


def authorize_foreground_for_run(
    *, task_id: Any = None, session_id: Any = None, **kwargs
) -> RunAuthorization:
    """Trusted plugin convenience wrapper for one foreground authorization."""
    context = get_run_backend_context(task_id=task_id, session_id=session_id)
    if context is None:
        raise RuntimeError("no active computer-use run")
    return context.authorize_foreground(**kwargs)


def release_run_backend_context(
    context: Optional[RunBackendContext] = None, *,
    task_id: Any = None, session_id: Any = None,
) -> bool:
    """Release only the selected trusted run's backend/session state."""
    expected_entry: Optional[_BackendEntry] = None
    if context is not None:
        run_key = context.run_key
        expected_entry = context._entry
    elif task_id is not None or session_id is not None:
        run_key = normalize_computer_use_run_key(task_id=task_id, session_id=session_id)
    else:
        identity = _CURRENT_DISPATCH.get()
        if identity is None:
            return False
        run_key = identity.run_key
    return _release_backend_for_run(run_key, expected_entry=expected_entry)


def release_backend_for_run(
    run_key_or_context: Any = None, *, task_id: Any = None, session_id: Any = None
) -> bool:
    """Compatibility release wrapper; accepts a context or normalized key."""
    if isinstance(run_key_or_context, RunBackendContext):
        return release_run_backend_context(run_key_or_context)
    if isinstance(run_key_or_context, str) and run_key_or_context:
        # This spelling is used only by trusted in-process callers.  It does
        # not appear in the model schema and does not alter dispatch kwargs.
        if run_key_or_context == "default" or run_key_or_context.startswith(("task:", "session:")):
            key = run_key_or_context
        else:
            key = normalize_computer_use_run_key(task_id=run_key_or_context)
        return _release_backend_for_run(key)
    return release_run_backend_context(task_id=task_id, session_id=session_id)


# ---------------------------------------------------------------------------
# Backend selection — env-swappable for tests
# ---------------------------------------------------------------------------

# One entry per authoritative run key.  ``_backend`` remains only as a
# backwards-compatible default-run test seam; it is never consulted for a
# named task/session run.
_backend_lock = threading.RLock()
_backend: Optional[ComputerUseBackend] = None
_run_backends: Dict[str, _BackendEntry] = {}
# Alias used by a few in-process integrations and deterministic tests.
_backends = _run_backends
# Approval state, scoped per authoritative run key so concurrent sessions
# cannot leak one run's "always approve" unlock into another.
_approval_lock = threading.Lock()
_session_auto_approve: Dict[str, bool] = {}
_always_allow: Dict[str, set] = {}


def _new_backend() -> ComputerUseBackend:
    backend_name = os.environ.get("HERMES_COMPUTER_USE_BACKEND", "cua").lower()
    if backend_name in {"cua", "cua-driver", ""}:
        from tools.computer_use.cua_backend import CuaDriverBackend
        return CuaDriverBackend()
    if backend_name == "noop":  # pragma: no cover
        return _NoopBackend()
    raise RuntimeError(f"Unknown HERMES_COMPUTER_USE_BACKEND={backend_name!r}")


def _entry_for_backend(run_key: str, backend: ComputerUseBackend) -> _BackendEntry:
    entry = _BackendEntry(backend=backend)
    entry.context = RunBackendContext(run_key, entry)
    _run_backends[run_key] = entry
    return entry


def _get_backend_entry(run_key: str = "default") -> _BackendEntry:
    """Get/create one backend/session/sticky-target state for ``run_key``."""
    global _backend
    with _backend_lock:
        entry = _run_backends.get(run_key)
        if entry is not None and not entry.released:
            entry.last_used = time.monotonic()
            return entry
        if entry is not None:
            _run_backends.pop(run_key, None)

        # Preserve the historical monkeypatch seam for the default run only.
        # A named run must receive a distinct backend even when _backend is
        # set by a test or by legacy code.
        if run_key == "default" and _backend is not None:
            backend = _backend
            entry = _entry_for_backend(run_key, backend)
            return entry
        backend = _new_backend()
        try:
            backend.start()
        except Exception:
            raise
        entry = _entry_for_backend(run_key, backend)
        if run_key == "default":
            _backend = backend
        return entry


def _get_backend(run_key: str = "default") -> ComputerUseBackend:
    return _get_backend_entry(run_key).backend


def _get_or_attach_backend_entry(run_key: str) -> _BackendEntry:
    """Resolve through the injectable backend seam and attach it to ``run_key``."""
    with _backend_lock:
        entry = _run_backends.get(run_key)
        if entry is not None and not entry.released:
            return entry
        backend = _get_backend(run_key)
        entry = _run_backends.get(run_key)
        if entry is None or entry.backend is not backend or entry.released:
            entry = _entry_for_backend(run_key, backend)
        return entry


def _release_backend_for_run(
    run_key: str, *, expected_entry: Optional[_BackendEntry] = None
) -> bool:
    global _backend
    with _backend_lock:
        entry = _run_backends.get(run_key)
        if expected_entry is not None and entry is not expected_entry:
            return False
        entry = _run_backends.pop(run_key, None)
        if entry is None:
            if run_key == "default":
                _backend = None
            return False
        entry.released = True
        if run_key == "default" and _backend is entry.backend:
            _backend = None
    # Wait for an in-flight call on this run, then stop only this backend.  A
    # separate named run has an independent lock and lifecycle.
    with entry.call_lock:
        entry.capture_authorizations.clear()
        entry.foreground_authorizations.clear()
        try:
            entry.backend.stop()
        except Exception:
            logger.debug("computer_use backend stop failed", exc_info=True)
    return True


def reset_backend_for_tests() -> None:  # pragma: no cover
    """Test helper — tear down all cached run backends and approval state."""
    with _backend_lock:
        keys = list(_run_backends)
    for key in keys:
        _release_backend_for_run(key)
    # A legacy test may set _backend without placing it in the map.
    global _backend
    with _backend_lock:
        orphan = _backend
        _backend = None
    if orphan is not None:
        try:
            orphan.stop()
        except Exception:
            pass
    with _approval_lock:
        _session_auto_approve.clear()
        _always_allow.clear()


class _NoopBackend(ComputerUseBackend):  # pragma: no cover
    """Test/CI stub. Records calls; returns trivial results."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._started = False
        self._session_id = f"noop-{id(self):x}"
        self._last_target: Optional[Dict[str, Any]] = None
        self._snapshot_tokens: Dict[int, str] = {}

    def start(self) -> None: self._started = True
    def stop(self) -> None: self._started = False
    def is_available(self) -> bool: return True

    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> CaptureResult:
        self.calls.append((
            "capture",
            {"mode": mode, "app": app, "pid": pid, "window_id": window_id},
        ))
        if pid is not None and window_id is not None:
            self._last_target = {"pid": pid, "window_id": window_id}
        target_meta = dict(self._last_target) if self._last_target is not None else {}
        return CaptureResult(mode=mode, width=1024, height=768, png_b64=None,
                             elements=[], app=app or "", window_title="",
                             meta={"target": target_meta} if target_meta else {})

    def get_target_proof(self) -> Optional[Dict[str, Any]]:
        return dict(self._last_target) if self._last_target is not None else None

    def get_snapshot_tokens(self) -> Dict[int, str]:
        return dict(self._snapshot_tokens)

    def recapture_exact(self, target_proof: Any, mode: str = "som") -> CaptureResult:
        expected = TargetProof.from_value(target_proof)
        actual = TargetProof.from_value(self._last_target)
        if expected is None or actual != expected:
            return _failed_capture(mode, "stale_target: noop target changed")
        return self.capture(mode=mode, pid=expected.pid, window_id=expected.window_id)

    def click(self, **kw) -> ActionResult:
        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click")

    def drag(self, **kw) -> ActionResult:
        self.calls.append(("drag", kw))
        return ActionResult(ok=True, action="drag")

    def scroll(self, **kw) -> ActionResult:
        self.calls.append(("scroll", kw))
        return ActionResult(ok=True, action="scroll")

    def type_text(self, text: str, **kw) -> ActionResult:
        self.calls.append(("type", {"text": text, **kw}))
        return ActionResult(ok=True, action="type")

    def key(self, keys: str, **kw) -> ActionResult:
        self.calls.append(("key", {"keys": keys, **kw}))
        return ActionResult(ok=True, action="key")

    def list_apps(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_apps", {}))
        return []

    def list_windows(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_windows", {}))
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self.calls.append(("focus_app", {"app": app, "raise": raise_window}))
        return ActionResult(ok=True, action="focus_app")

    def focus_exact(
        self, pid: int, window_id: int, raise_window: bool = False,
    ) -> ActionResult:
        self.calls.append(("focus_exact", {
            "pid": pid, "window_id": window_id, "raise": raise_window,
        }))
        return ActionResult(ok=True, action="focus_app")

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        self.calls.append(("set_value", {"value": value, "element": element}))
        return ActionResult(ok=True, action="set_value")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle_computer_use(args: Dict[str, Any], **kwargs) -> Any:
    """Final tool boundary with authoritative run-key normalization."""
    identity = _DispatchIdentity(
        run_key=normalize_computer_use_run_key(
            task_id=kwargs.get("task_id"), session_id=kwargs.get("session_id")
        ),
        tool_call_id=_normalize_dispatch_value(kwargs.get("tool_call_id")),
    )
    token = _CURRENT_DISPATCH.set(identity)
    try:
        return _handle_computer_use_impl(args, identity, kwargs)
    finally:
        # Grants are scoped to one tool call even when a caller/plugin issued
        # them speculatively.  A consumed grant is already gone; unused
        # capture/foreground grants cannot be replayed on a later call.
        entry = _run_backends.get(identity.run_key)
        if entry is not None and entry.context is not None:
            entry.context._discard_authorization("capture", identity.tool_call_id)
            entry.context._discard_authorization("foreground", identity.tool_call_id)
        _CURRENT_DISPATCH.reset(token)


def _handle_computer_use_impl(
    args: Dict[str, Any], identity: _DispatchIdentity, kwargs: Mapping[str, Any]
) -> Any:
    """Main entry point — dispatched by tools.registry.

    Returns either a JSON string (text-only) or a dict marked `_multimodal`
    (image + summary) which run_agent.py wraps into the tool message.
    """
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing `action`"})
    # The key was normalized by handle_computer_use; no model argument can
    # select a different backend/session.
    run_key = identity.run_key
    tool_call_id = identity.tool_call_id

    # Safety: validate actions before approval prompt.
    if action == "type":
        text = args.get("text", "")
        pat = _is_blocked_type(text)
        if pat:
            return json.dumps({
                "error": f"blocked pattern in type text: {pat!r}",
                "hint": "Dangerous shell patterns cannot be typed via computer_use.",
            })

    if action == "key":
        keys = args.get("keys", "")
        combo = _canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo) and len(blocked) <= len(combo):
                return json.dumps({
                    "error": f"blocked key combo: {sorted(blocked)}",
                    "hint": "Destructive system shortcuts are hard-blocked.",
                })

    # Resolve the run before approval whenever a trusted foreground/capture
    # authorization may be needed.  Ordinary background actions still use
    # the existing approval callback and are lazy in the same way as before.
    try:
        entry = _get_or_attach_backend_entry(run_key)
        context = entry.context
        assert context is not None
    except Exception as e:
        return json.dumps({
            "error": f"computer_use backend unavailable: {e}",
            "hint": "If the cua-driver binary is missing, run `hermes computer-use install`. "
                    "If a Python dependency is missing, the error above shows the exact install command.",
        })

    if (
        context.target_proof is not None
        and action in _DESTRUCTIVE_ACTIONS
        and not context.target_is_current()
    ):
        return _typed_action_error(
            action, "stale_target",
            "the run's exact target proof is no longer current",
        )

    foreground_requested = (
        args.get("delivery_mode") == "foreground"
        or (action == "focus_app" and bool(args.get("raise_window")))
    )
    capture_foreground_requested = action == "capture" and args.get("delivery_mode") == "foreground"

    if (
        (foreground_requested or capture_foreground_requested)
        and context.target_proof is not None
        and not context.target_is_current()
    ):
        return _typed_action_error(
            action, "stale_target",
            "the trusted target proof is stale; recapture the intended window before escalating",
        )

    if action in _DESTRUCTIVE_ACTIONS and not foreground_requested:
        err = _request_approval(action, args, run_key)
        if err is not None:
            return err
    elif foreground_requested and action != "capture":
        # The noop backend has no desktop side effect and is retained as a
        # legacy routing test double.  Native/trusted backends always take the
        # authorization path below, including when tool_call_id is absent.
        legacy_noop_foreground = isinstance(entry.backend, _NoopBackend) and not tool_call_id
        if not legacy_noop_foreground and not _ensure_one_shot_authorization(
            context, "foreground", action, args, tool_call_id
        ):
            return _typed_action_error(
                action, "foreground_required",
                "foreground delivery requires a trusted, single-action authorization",
            )

    if capture_foreground_requested and not (
        isinstance(entry.backend, _NoopBackend) and not tool_call_id
    ) and not _ensure_one_shot_authorization(
        context, "capture", "capture", args, tool_call_id
    ):
        return _typed_action_error(
            action, "capture_foreground_required",
            "foreground capture requires a trusted, single-action authorization",
        )

    try:
        with entry.call_lock:
            if entry.released:
                return _typed_action_error(
                    action, "stale_target",
                    "the run backend was released before this action acquired its lock",
                )
            if (
                context.target_proof is not None
                and action in _DESTRUCTIVE_ACTIONS
                and not context.target_is_current()
            ):
                return _typed_action_error(
                    action, "stale_target",
                    "the run's exact target proof is no longer current",
                )
            entry.last_used = time.monotonic()
            result = _dispatch(
                entry.backend, action, args, context=context, tool_call_id=tool_call_id
            )
            # A typed background refusal may climb exactly once, and only via
            # a matching foreground grant.  Generic errors, unverifiable or
            # suspected-noop verdicts, stale targets, and transport failures
            # never trigger a retry.
            if not foreground_requested and action in _DESTRUCTIVE_ACTIONS:
                result = _maybe_escalate_background(
                    entry, context, action, args, tool_call_id, result
                )
            return result
    except Exception as e:
        logger.exception("computer_use %s failed", action)
        return json.dumps({"error": f"{action} failed: {e}"})


def _typed_action_error(
    action: str, code: str, message: str, meta: Optional[Mapping[str, Any]] = None
) -> str:
    payload: Dict[str, Any] = {
        "ok": False,
        "action": action,
        "code": code,
        "message": message,
    }
    if meta:
        payload["meta"] = dict(meta)
    return json.dumps(payload, ensure_ascii=False)


def _failed_capture(mode: str, message: str) -> CaptureResult:
    code = "stale_target" if message.startswith("stale_target:") else "capture_failed"
    return CaptureResult(
        mode=mode, width=0, height=0, png_b64=None, elements=[],
        window_title=message, meta={"code": code, "message": message},
    )


def _authorization_matches(
    context: RunBackendContext, kind: str, *, tool_call_id: str,
    action: str, target: Optional[TargetProof], action_args: Mapping[str, Any],
) -> bool:
    call_id = _normalize_dispatch_value(tool_call_id)
    if not call_id:
        return False
    table = (
        context._entry.capture_authorizations
        if kind == "capture" else context._entry.foreground_authorizations
    )
    authorization = table.get(call_id)
    if authorization is None:
        return False
    if authorization.is_expired():
        table.pop(call_id, None)
        return False
    return (
        authorization.action == action
        and authorization.target == target
        and (
            authorization.action_fingerprint is None
            or authorization.action_fingerprint == _action_fingerprint(action, action_args)
        )
    )


def _ensure_one_shot_authorization(
    context: RunBackendContext, kind: str, action: str,
    args: Mapping[str, Any], tool_call_id: str,
) -> bool:
    """Consume a trusted grant, or obtain one from the user callback once."""
    target = context.target_proof
    if not _target_strength_is_sufficient(context.backend, target):
        return False
    with context._entry.call_lock:
        if _authorization_matches(
            context, kind, tool_call_id=tool_call_id, action=action,
            target=target, action_args=args,
        ):
            return context._consume_authorization(
                kind, tool_call_id=tool_call_id, action=action,
                target=target, action_args=args,
            ) is not None

    # A model-provided delivery_mode is only a request.  The optional CLI
    # callback is a trusted user boundary; its verdict is converted to a
    # single-use grant tied to this exact call rather than a reusable flag.
    if not tool_call_id or _approval_callback is None:
        return False
    prompt_args = dict(args)
    if kind == "foreground":
        prompt_args["delivery_mode"] = "foreground"
    summary = _summarize_action(action, prompt_args)
    try:
        verdict = _approval_callback(action, prompt_args, summary)
    except Exception as exc:
        logger.warning("foreground/capture authorization callback failed: %s", exc)
        verdict = "deny"
    if verdict not in {"approve_once", "approve_session", "always_approve"}:
        return False
    try:
        if kind == "foreground":
            context.authorize_foreground(
                tool_call_id=tool_call_id, action=action,
                target_proof=target, action_args=args,
            )
        else:
            context.authorize_capture(
                tool_call_id=tool_call_id, action=action,
                target_proof=target, action_args=args,
            )
    except ValueError:
        return False
    return context._consume_authorization(
        kind, tool_call_id=tool_call_id, action=action,
        target=target, action_args=args,
    ) is not None


def _request_approval(action: str, args: Dict[str, Any],
                      session_id: str = "") -> Optional[str]:
    """Return None if approved, or a JSON error string if denied.

    Approval is scoped by (action, delivery_mode) AND by session_id.
    Foreground delivery is a visible focus change, so a prior background
    approval — even ``approve_session`` on the same action — must NOT
    silently authorize it (NousResearch/hermes-agent#67052).
    ``always_approve`` (the blanket "auto-approve everything" unlock) still
    covers foreground, since the user explicitly opted into unattended
    operation. State is keyed on session_id so concurrent runs don't leak
    unlocks into one another.
    """
    is_foreground = args.get("delivery_mode") == "foreground"
    scope_key = (action, "foreground" if is_foreground else "background")
    with _approval_lock:
        if _session_auto_approve.get(session_id):
            return None
        if scope_key in _always_allow.get(session_id, set()):
            return None
    cb = _approval_callback
    if cb is None:
        # No CLI approval wired — default allow. Gateway approval is handled
        # one layer out via the normal tool-approval infra.
        return None
    summary = _summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as e:
        logger.warning("approval callback failed: %s", e)
        verdict = "deny"
    if verdict == "approve_once":
        return None
    if verdict == "approve_session" or verdict == "always_approve":
        with _approval_lock:
            _always_allow.setdefault(session_id, set()).add(scope_key)
            if verdict == "always_approve":
                _session_auto_approve[session_id] = True
        return None
    return json.dumps({"error": "denied by user", "action": action})


def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    fg = " [FOREGROUND — briefly raises the window / changes focus]" \
        if args.get("delivery_mode") == "foreground" else ""
    if action in {"click", "double_click", "right_click", "middle_click"}:
        if args.get("element") is not None:
            return f"{action} element #{args['element']}{fg}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}{fg}"
        return action + fg
    if action == "drag":
        src = args.get("from_element") or args.get("from_coordinate")
        dst = args.get("to_element") or args.get("to_coordinate")
        return f"drag {src} → {dst}{fg}"
    if action == "scroll":
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}{fg}"
    if action == "type":
        text = args.get("text", "")
        return f"type {text[:60]!r}" + ("..." if len(text) > 60 else "") + fg
    if action == "key":
        return f"key {args.get('keys', '')!r}{fg}"
    if action == "focus_app":
        return f"focus {args.get('app', '')!r}" + (" (raise)" if args.get("raise_window") else "")
    return action + fg


def _dispatch(
    backend: ComputerUseBackend, action: str, args: Dict[str, Any],
    *, context: Optional[RunBackendContext] = None, tool_call_id: str = "",
) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
        sticky = context.target_proof if context is not None else None
        requested_pid = args.get("pid")
        requested_window_id = args.get("window_id")
        if context is not None and sticky is not None:
            if (
                requested_pid is not None and int(requested_pid) != sticky.pid
            ) or (
                requested_window_id is not None and int(requested_window_id) != sticky.window_id
            ):
                return _typed_action_error(
                    action, "stale_target",
                    "capture requested a different target than this run's established target",
                )
            if context.capability_manifest.get("complete"):
                try:
                    cap = context.recapture_exact(sticky, mode=mode, tool_call_id=None)
                except RuntimeError:
                    return _typed_action_error(
                        action, "stale_target",
                        "exact capture is unavailable for this run target",
                    )
                if cap.width <= 0 or cap.height <= 0:
                    return _typed_action_error(
                        action, "stale_target",
                        "exact capture could not prove the established target",
                        meta=cap.meta,
                    )
                context._set_target_from_capture(
                    cap, {"mode": mode, "pid": sticky.pid, "window_id": sticky.window_id}
                )
                return _capture_response(
                    cap, max_elements=_coerce_max_elements(args.get("max_elements"))
                )
        capture_kwargs: Dict[str, Any] = {"mode": mode, "app": args.get("app")}
        if args.get("pid") is not None or args.get("window_id") is not None:
            capture_kwargs.update({
                "pid": args.get("pid"),
                "window_id": args.get("window_id"),
            })
        cap = backend.capture(**capture_kwargs)
        if context is not None:
            context._set_target_from_capture(cap, capture_kwargs)
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements")))

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _text_response(res)

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)})

    if action == "list_windows":
        windows = backend.list_windows()
        return json.dumps({"windows": windows, "count": len(windows)})

    if action == "focus_app":
        app = args.get("app")
        if not app:
            return json.dumps({"error": "focus_app requires `app`"})
        expected_target = context.target_proof if context is not None else None
        if context is not None and context.target_proof is not None:
            exact_focus = cast(
                Optional[Callable[..., ActionResult]],
                getattr(backend, "focus_exact", None),
            )
            if callable(exact_focus):
                target = context.target_proof
                res = exact_focus(
                    target.pid, target.window_id,
                    raise_window=bool(args.get("raise_window")),
                )
            else:
                return _typed_action_error(
                    action, "stale_target",
                    "backend cannot focus this run's established exact target",
                )
        else:
            res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))
        if expected_target is not None and context is not None and res.ok and not context.target_is_current():
            return _typed_action_error(
                action, "stale_target",
                "exact focus changed or could not prove this run's target",
            )
        if context is not None:
            proof = context._current_backend_target()
            if proof is not None:
                context._target = proof
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    # delivery_mode / bring_to_front thread through every input action so the
    # model can escalate background → foreground per cua-driver's ladder.
    delivery_mode = args.get("delivery_mode")
    bring_to_front = bool(args.get("bring_to_front"))

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        element = args.get("element")
        coord = args.get("coordinate") or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(
            element=element if element is not None else None,
            x=x, y=y, button=button or "left", click_count=click_count,
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    if action == "drag":
        has_elements = args.get("from_element") is not None and args.get("to_element") is not None
        has_coords = args.get("from_coordinate") and args.get("to_coordinate")
        if not has_elements and not has_coords:
            return json.dumps({
                "error": "drag requires from_coordinate/to_coordinate or from_element/to_element",
            })
        res = backend.drag(
            from_element=args.get("from_element"),
            to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button", "left"),
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    if action == "scroll":
        coord = args.get("coordinate") or (None, None)
        res = backend.scroll(
            direction=args.get("direction", "down"),
            amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coord[0] if coord and coord[0] is not None else None,
            y=coord[1] if coord and coord[1] is not None else None,
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    if action == "type":
        res = backend.type_text(args.get("text", ""),
                                delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    if action == "key":
        res = backend.key(args.get("keys", ""),
                          delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return json.dumps({"error": "set_value requires `value`"})
        res = backend.set_value(value=str(value), element=args.get("element"))
        return _maybe_follow_capture(
            backend, res, capture_after, context=context, tool_call_id=tool_call_id
        )

    return json.dumps({"error": f"unknown action {action!r}"})


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _text_response(res: ActionResult) -> str:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    # Surface cua-driver's structured verdict additively so the model can
    # follow the verify → escalate ladder. Only include fields the driver
    # actually returned (None = old driver / not carried). ok is transport
    # success; effect/escalation are the semantic verdict.
    if res.verified is not None:
        payload["verified"] = res.verified
    if res.effect is not None:
        payload["effect"] = res.effect
    if res.escalation is not None:
        payload["escalation"] = res.escalation
    if res.path is not None:
        payload["path"] = res.path
    if res.degraded is not None:
        payload["degraded"] = res.degraded
    if res.delivery_mode is not None:
        payload["delivery_mode"] = res.delivery_mode
    if res.code is not None:
        payload["code"] = res.code
    if res.meta:
        payload["meta"] = res.meta
    return json.dumps(payload)


# Default cap for the AX `elements` array returned by capture. Dense UIs
# (Electron apps, Obsidian, JetBrains IDEs) can publish 500+ AX nodes, which
# can exhaust session context after a single capture. The model-facing
# `max_elements` argument lets callers raise this when they need the full tree.
_DEFAULT_MAX_ELEMENTS = 100
# Hard upper bound on caller-supplied `max_elements`. Without this, a tool
# call passing a very large integer would silently disable the safeguard and
# reintroduce the original unbounded behavior.
_MAX_ALLOWED_MAX_ELEMENTS = 1000
_MIN_PROVIDER_IMAGE_DIMENSION = 8


def _image_dimensions_from_b64(image_b64: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) for common inline screenshot formats.

    Some providers reject images below 8x8 before the model sees the tool
    result. Inspecting the encoded bytes here lets computer_use fall back to
    its AX/SOM text payload instead of sending an unusable placeholder.
    """
    if not image_b64:
        return None
    try:
        raw = base64.b64decode(image_b64, validate=False)
    except Exception:
        return None

    # PNG: signature + IHDR width/height.
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        try:
            width, height = struct.unpack(">II", raw[16:24])
            return int(width), int(height)
        except Exception:
            return None

    # JPEG: scan for SOF markers that carry dimensions.
    if raw.startswith(b"\xff\xd8") and len(raw) > 4:
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            while marker == 0xFF and i < len(raw):
                marker = raw[i]
                i += 1
            if marker in {0xD8, 0xD9}:
                continue
            if marker == 0xDA:
                break
            if i + 2 > len(raw):
                break
            segment_len = int.from_bytes(raw[i:i + 2], "big")
            if segment_len < 2 or i + segment_len > len(raw):
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_len >= 7:
                height = int.from_bytes(raw[i + 3:i + 5], "big")
                width = int.from_bytes(raw[i + 5:i + 7], "big")
                return int(width), int(height)
            i += segment_len
    return None


def _coerce_max_elements(value: Any) -> int:
    """Validate the caller-supplied ``max_elements``.

    Falls back to :data:`_DEFAULT_MAX_ELEMENTS` for missing / non-integer /
    sub-1 inputs so the cap can never be silently disabled by a malformed
    tool-call argument. Clamps oversized values to
    :data:`_MAX_ALLOWED_MAX_ELEMENTS` so a caller cannot bypass the
    safeguard by passing a very large integer.
    """
    if value is None:
        return _DEFAULT_MAX_ELEMENTS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return _DEFAULT_MAX_ELEMENTS
    if n > _MAX_ALLOWED_MAX_ELEMENTS:
        return _MAX_ALLOWED_MAX_ELEMENTS
    return n


def _capture_response(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Any:
    total_elements = len(cap.elements)
    visible_elements = cap.elements[:max_elements]
    truncated_elements = max(0, total_elements - len(visible_elements))
    image_dimensions = _image_dimensions_from_b64(cap.png_b64 or "") if cap.png_b64 else None
    response_width = image_dimensions[0] if image_dimensions else cap.width
    response_height = image_dimensions[1] if image_dimensions else cap.height
    image_too_small = bool(
        image_dimensions
        and (
            image_dimensions[0] < _MIN_PROVIDER_IMAGE_DIMENSION
            or image_dimensions[1] < _MIN_PROVIDER_IMAGE_DIMENSION
        )
    )

    # Index only what's actually surfaced in the response — otherwise the
    # human-readable summary references element indices the model cannot
    # find in the JSON `elements` array (e.g. max_elements=10 vs the default
    # 40-line index window).
    element_index = _format_elements(visible_elements)
    summary_lines = [
        f"capture mode={cap.mode} {response_width}x{response_height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
        f"{total_elements} interactable element(s):",
    ]
    if element_index:
        summary_lines.extend(element_index)
    # Multimodal and AX paths both reference `summary`; build it once up-front
    # so the aux-vision routing branch (which fires before either path is
    # selected) has a valid value to hand to _route_capture_through_aux_vision.
    # The AX path appends the "truncated to N of M" note to summary_lines
    # below and rebuilds; the multimodal path keeps this version untouched.
    if image_too_small:
        summary_lines.append(
            f"  (screenshot omitted: {image_dimensions[0]}x{image_dimensions[1]} "
            f"is below the {_MIN_PROVIDER_IMAGE_DIMENSION}x{_MIN_PROVIDER_IMAGE_DIMENSION} "
            "provider minimum)"
        )
    summary = "\n".join(summary_lines)

    if cap.png_b64 and cap.mode != "ax" and not image_too_small:
        # Decide whether to hand the screenshot to the auxiliary.vision
        # pipeline (text-only result) or keep the multimodal envelope (main
        # model handles vision natively). Issue #24015: previously the
        # multimodal envelope was returned unconditionally, so non-vision
        # main models tripped HTTP 404 / 400 at the provider boundary even
        # when auxiliary.vision was explicitly configured to handle this.
        if _should_route_through_aux_vision():
            routed = _route_capture_through_aux_vision(cap, summary)
            if routed is not None:
                return routed
            # Aux routing was requested but failed (vision node down, aux call
            # raised, empty analysis, etc.). Routing being requested means the
            # main model may not be able to consume images; falling through to
            # the multimodal envelope can break the capture with a provider
            # error. Degrade to the AX/SOM text payload instead so element
            # indices remain usable while vision is unavailable.
            summary_lines.append(
                "  (vision unavailable: the auxiliary vision model could not "
                "be reached; screenshot omitted. Element-index actions still "
                "work — drive via the element list above.)"
            )
            if truncated_elements:
                summary_lines.append(
                    f"  (response truncated to {len(visible_elements)} of "
                    f"{total_elements} elements; raise max_elements or pass "
                    "app= to narrow)"
                )
            payload = {
                "mode": cap.mode,
                "width": response_width,
                "height": response_height,
                "app": cap.app,
                "window_title": cap.window_title,
                "elements": [_element_to_dict(e) for e in visible_elements],
                "total_elements": total_elements,
                "summary": "\n".join(summary_lines),
                "vision_unavailable": True,
            }
            if cap.meta:
                payload["meta"] = dict(cap.meta)
            if truncated_elements:
                payload["truncated_elements"] = truncated_elements
            return json.dumps(payload)

        # Prefer the explicit MIME type cua-driver attaches to its image
        # parts (Surface 7 of NousResearch/hermes-agent#47072 — trycua/cua#1961
        # made `mimeType` part of every MCP image-part response). Fall back
        # to base64-prefix sniffing for older cua-driver builds that didn't
        # carry the field. JPEG base64 starts with /9j/; PNG with iVBOR.
        _mime = cap.image_mime_type
        if not _mime:
            _b64_prefix = cap.png_b64[:8]
            _mime = "image/jpeg" if _b64_prefix.startswith("/9j/") else "image/png"
        # The multimodal response carries the screenshot, not the AX
        # elements array, so a "response truncated to N of M elements"
        # note would be inaccurate — skip it on this branch.
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_mime};base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {
                "mode": cap.mode, "width": response_width, "height": response_height,
                "elements": total_elements, "png_bytes": cap.png_bytes_len,
                **dict(cap.meta),
            },
        }
    # AX-only (or image-missing fallback): text path actually carries the
    # `elements` array, so the truncation note applies here.
    if truncated_elements:
        summary_lines.append(
            f"  (response truncated to {len(visible_elements)} of {total_elements} elements; "
            f"raise max_elements or pass app= to narrow)"
        )
    summary = "\n".join(summary_lines)
    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": response_width,
        "height": response_height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in visible_elements],
        "total_elements": total_elements,
        "summary": summary,
    }
    if truncated_elements:
        payload["truncated_elements"] = truncated_elements
    if cap.meta:
        payload["meta"] = dict(cap.meta)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# auxiliary.vision routing for captured screenshots (#24015)
# ---------------------------------------------------------------------------

# Longest image side handed to the aux vision model. Full-resolution desktop
# captures tokenize heavily and can overflow small local-model context windows;
# ~1456px keeps SOM badges legible while cutting per-capture vision latency.
_MAX_VISION_DIM = 1456


def _shrink_capture_for_vision(raw: bytes, ext: str,
                               max_dim: int = _MAX_VISION_DIM) -> bytes:
    """Downscale encoded image bytes so the longest side is <= max_dim.

    Returns the original bytes unchanged when the image already fits or when
    Pillow is unavailable/fails — no worse than the pre-shrink behavior.
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if max(img.size) <= max_dim:
            return raw
        img.thumbnail((max_dim, max_dim))
        out = BytesIO()
        img.save(out, format="JPEG" if ext == ".jpg" else "PNG")
        return out.getvalue()
    except Exception as exc:
        logger.debug("computer_use: vision downscale skipped: %s", exc)
        return raw

def _should_route_through_aux_vision() -> bool:
    """Return True when ``_capture_response`` should hand the PNG to aux vision.

    Reads the active main provider/model and the loaded config and asks the
    routing helper. Any failure (config import, runtime override missing,
    etc.) returns False so the existing multimodal envelope continues to be
    returned — fail open on the routing decision so a broken config can
    never silently drop the screenshot for vision-capable main models.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config
        from tools.computer_use.vision_routing import (
            should_route_capture_to_aux_vision,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing import failed: %s", exc)
        return False
    try:
        provider = _read_main_provider()
        model = _read_main_model()
        cfg = load_config()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing config read failed: %s", exc)
        return False
    try:
        return bool(should_route_capture_to_aux_vision(provider, model, cfg))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing decision failed: %s", exc)
        return False


def _route_capture_through_aux_vision(
    cap: CaptureResult,
    summary: str,
) -> Optional[str]:
    """Pre-analyse the captured PNG via ``vision_analyze`` and return a text result.

    The captured base64 PNG is materialised to ``$HERMES_HOME/cache/vision/``
    and handed to ``vision_analyze_tool`` with a generic describe prompt.
    The resulting text description is merged into the existing AX/SOM
    summary so the main model receives a single text payload that mentions
    every interactable element AND a description of what the screenshot
    looked like.

    Returns:
      A JSON-encoded text response on success.
      ``None`` on failure (caller falls back to the multimodal envelope).
    """
    if not cap.png_b64:
        return None
    try:
        import base64 as _base64
        import os as _os
        import uuid as _uuid

        from hermes_constants import get_hermes_dir
        from model_tools import _run_async
        from tools.vision_tools import vision_analyze_tool
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision import failed: %s", exc)
        return None

    temp_image_path = None
    try:
        try:
            raw = _base64.b64decode(cap.png_b64, validate=False)
        except Exception as exc:
            logger.debug("computer_use: failed to decode capture base64: %s", exc)
            return None

        # Pick an extension that matches the on-disk bytes so vision_analyze's
        # MIME sniffing returns the right content-type.
        # Surface 7: prefer the explicit MIME type cua-driver supplied.
        _mime_for_ext = cap.image_mime_type or ""
        if _mime_for_ext == "image/jpeg" or (not _mime_for_ext and cap.png_b64[:8].startswith("/9j/")):
            ext = ".jpg"
        else:
            ext = ".png"
        cache_dir = get_hermes_dir("cache/vision", "temp_vision_images")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = cache_dir / f"computer_use_{_uuid.uuid4().hex}{ext}"
        raw = _shrink_capture_for_vision(raw, ext)
        temp_image_path.write_bytes(raw)

        prompt = (
            "Describe what is visible in this desktop application screenshot in "
            "concise but specific terms. Mention the app name and window "
            "title if visible, the overall layout, any labelled buttons, "
            "menus or text fields, and any prominent text content the user "
            "would need to know about. Do not invent details that are not "
            "actually visible.\n\n"
            f"AX/SOM index for cross-reference:\n{summary}"
        )

        result_json = _run_async(
            vision_analyze_tool(str(temp_image_path), prompt)
        )
    except Exception as exc:
        logger.warning(
            "computer_use: auxiliary.vision pre-analysis failed (%s); "
            "returning to caller without aux analysis",
            exc,
        )
        return None
    finally:
        if temp_image_path is not None:
            try:
                _os.unlink(str(temp_image_path))
            except Exception:
                pass

    analysis_text = ""
    if isinstance(result_json, str):
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                analysis_text = str(parsed.get("analysis") or "").strip()
        except (TypeError, json.JSONDecodeError):
            analysis_text = result_json.strip()

    if not analysis_text:
        return None

    return json.dumps({
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in cap.elements],
        "summary": summary,
        "vision_analysis": analysis_text,
        "vision_analysis_routed_via": "auxiliary.vision",
    })


def _response_payload(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, Mapping):
        return dict(result)
    if not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _maybe_escalate_background(
    entry: _BackendEntry, context: RunBackendContext, action: str,
    args: Mapping[str, Any], tool_call_id: str, result: Any,
) -> Any:
    """Retry one typed background refusal on the exact target only."""
    verdict = _response_payload(result)
    if not verdict:
        return result
    if verdict.get("code") != "background_unavailable":
        return result
    if verdict.get("ok") is not False:
        return result
    if verdict.get("effect") in {"unverifiable", "suspected_noop", "error", "timeout", "generic_error", "failed"}:
        return result
    expected = context.target_proof
    if expected is None or not context.target_is_current():
        return _typed_action_error(
            action, "stale_target",
            "background refusal cannot escalate without the unchanged exact target proof",
            meta={"background_result": verdict},
        )
    if not _target_strength_is_sufficient(entry.backend, expected):
        return _typed_action_error(
            action,
            "exact_target_required",
            "native Wayland foreground escalation requires helper-incarnation identity",
            meta={"background_result": verdict},
        )
    if not _ensure_one_shot_authorization(
        context, "foreground", action, args, tool_call_id
    ):
        return _typed_action_error(
            action, "foreground_required",
            "background delivery was unavailable; a trusted one-shot foreground authorization is required",
            meta={"background_result": verdict},
        )
    retry_args = dict(args)
    retry_args["delivery_mode"] = "foreground"
    # No recursive escalation: the returned foreground verdict is final.
    return _dispatch(
        entry.backend, action, retry_args, context=context, tool_call_id=tool_call_id
    )


def _maybe_follow_capture(
    backend: ComputerUseBackend, res: ActionResult, do_capture: bool,
    *, context: Optional[RunBackendContext] = None, tool_call_id: str = "",
) -> Any:
    if not do_capture:
        return _text_response(res)
    # Skip the follow-up capture when the action itself failed: showing a
    # normal-looking screenshot after a failure misleads the model into thinking
    # the action succeeded. Return the error text instead.
    if not res.ok:
        return _text_response(res)
    try:
        if context is not None and context.target_proof is not None:
            # The proof is checked both before and after the backend call.  A
            # same-app window, reused pid, changed helper epoch, or changed
            # capability set is stale even when the app label is unchanged.
            if not context.target_is_current():
                return _typed_action_error(
                    res.action, "stale_target",
                    "capture_after refused because the exact target proof changed",
                )
            cap = context.recapture_exact(
                context.target_proof, mode="som", tool_call_id=None
            )
            if cap.width <= 0 or cap.meta.get("code") == "stale_target":
                return _typed_action_error(
                    res.action, "stale_target",
                    cap.window_title or "capture_after refused a stale/retargeted target",
                )
        elif (
            context is not None
            and context.capability_manifest.get("complete")
            and not isinstance(backend, _NoopBackend)
        ):
            # A native contract without an established target cannot safely
            # fall back to app-name recapture.
            return _typed_action_error(
                res.action, "stale_target",
                "capture_after requires an established exact target proof",
            )
        else:
            # Legacy third-party backends without the trusted hook retain the
            # historical app fallback for compatibility; native Cua backends
            # always take the exact branch above once a target is established.
            target = getattr(backend, "_last_target", None) or {}
            pid = target.get("pid")
            window_id = target.get("window_id")
            if pid is not None and window_id is not None:
                cap = backend.capture(mode="som", pid=pid, window_id=window_id)
            else:
                cap = backend.capture(mode="som", app=getattr(backend, "_last_app", None))
    except Exception as e:
        logger.warning("follow-up capture failed: %s", e)
        return _text_response(res)
    # Combine action summary with the capture.
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get("_multimodal"):
        prefix = f"[{res.action}] ok={res.ok}" + (f" — {res.message}" if res.message else "")
        resp["content"][0]["text"] = prefix + "\n\n" + resp["content"][0]["text"]
        resp["text_summary"] = prefix + "\n\n" + resp["text_summary"]
        return resp
    # Fallback: action + text capture merged.
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {"capture": resp}
    data["action"] = res.action
    data["ok"] = res.ok
    if res.message:
        data["message"] = res.message
    return json.dumps(data)


def _format_elements(elements: List[UIElement], max_lines: int = 40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        out.append(f"  #{e.index} {e.role} {label!r} @ {e.bounds}"
                   + (f" [{e.app}]" if e.app else ""))
    if len(elements) > max_lines:
        out.append(f"  ... +{len(elements) - max_lines} more (call capture with app= to narrow)")
    return out


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    return {
        "index": e.index,
        "role": e.role,
        "label": e.label,
        "bounds": list(e.bounds),
        "app": e.app,
    }


# ---------------------------------------------------------------------------
# Availability check (used by the tool registry check_fn)
# ---------------------------------------------------------------------------

def check_computer_use_requirements() -> bool:
    """Return True iff computer_use can run on this host.

    Conditions: macOS, Windows, or Linux + cua-driver binary installed (or
    override via env). cua-driver runs on all three; the Linux path is
    headed/X11 today (Wayland via XWayland), pure-Wayland progress tracked
    upstream. Linux users see specific blocked checks via
    `hermes computer-use doctor` if their session is incomplete (e.g. no
    DISPLAY set).
    """
    if sys.platform not in ("darwin", "win32", "linux"):
        return False
    from tools.computer_use.cua_backend import cua_driver_binary_available
    return cua_driver_binary_available()


def get_computer_use_schema() -> Dict[str, Any]:
    from tools.computer_use.schema import COMPUTER_USE_SCHEMA
    return COMPUTER_USE_SCHEMA
