"""Single-owner broker for concurrent control of one live browser profile.

One browser process owns the profile. Agents share this broker and address exact
CDP browsing-context ids. Page-local work is serialized per context but can run
in parallel across contexts; browser/profile-global mutations use one wider
lock. CUA compositor surface tokens are associated explicitly and immutably —
titles are discovery hints, never action identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import json
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Protocol


class CDPTransport(Protocol):
    def call_cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]: ...


@dataclass
class BrowserContext:
    target_id: str
    title: str = ""
    url: str = ""
    session_id: Optional[str] = None
    surface_target: Optional[str] = None
    live: bool = True
    updated_at: float = field(default_factory=time.monotonic)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "url": self.url,
            "session_id": self.session_id,
            "surface_target": self.surface_target,
            "live": self.live,
        }


_CONTEXT_PREFIXES = (
    "Accessibility.",
    "CSS.",
    "DOM.",
    "Emulation.",
    "Input.",
    "Network.",
    "Page.",
    "Runtime.",
)


def _validate_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{label} must be a non-empty token without whitespace")
    return value


def _validate_surface_target(value: str) -> str:
    value = _validate_id(value, "surface_target")
    if ":" not in value:
        raise ValueError("surface_target must include an incarnation/epoch boundary")
    return value


class BrowserProfileBroker:
    """Coordinate multiple agents against one browser profile owner."""

    def __init__(
        self,
        profile_id: str,
        transport: CDPTransport,
        *,
        profile_dir: Optional[Path] = None,
    ) -> None:
        self.profile_id = _validate_id(profile_id, "profile_id")
        self._transport = transport
        self._state_lock = threading.RLock()
        self._global_lock = threading.RLock()
        self._contexts: Dict[str, BrowserContext] = {}
        self._surface_to_target: Dict[str, str] = {}
        self._profile_lock_file = None
        if profile_dir is not None:
            self._acquire_profile_broker_lock(profile_dir)

    def _acquire_profile_broker_lock(self, profile_dir: Path) -> None:
        profile_dir = profile_dir.expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = profile_dir / ".hermes-browser-broker.lock"
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeError(
                f"browser profile {self.profile_id!r} already has another broker owner"
            ) from exc
        self._profile_lock_file = lock_file

    def discover(self, *, timeout: float = 10.0) -> list[Dict[str, Any]]:
        """Refresh page targets without selecting by title."""
        response = self.global_call("Target.getTargets", timeout=timeout)
        infos = response.get("result", {}).get("targetInfos", [])
        seen: set[str] = set()
        with self._state_lock:
            for info in infos:
                if info.get("type") != "page":
                    continue
                target_id = _validate_id(info.get("targetId", ""), "target_id")
                seen.add(target_id)
                context = self._contexts.setdefault(
                    target_id, BrowserContext(target_id)
                )
                context.title = str(info.get("title") or "")
                context.url = str(info.get("url") or "")
                context.live = True
                context.updated_at = time.monotonic()
            for target_id, context in self._contexts.items():
                if target_id not in seen:
                    context.live = False
                    context.updated_at = time.monotonic()
            return [self._contexts[target_id].snapshot() for target_id in sorted(seen)]

    def _context(self, target_id: str) -> BrowserContext:
        target_id = _validate_id(target_id, "target_id")
        with self._state_lock:
            return self._contexts.setdefault(target_id, BrowserContext(target_id))

    def attach(self, target_id: str, *, timeout: float = 10.0) -> str:
        context = self._context(target_id)
        # Target attachment mutates browser-global protocol state. Hold both the
        # global lock and the exact context lock in a fixed order.
        with self._global_lock, context.lock:
            if context.session_id:
                return context.session_id
            response = self._transport.call_cdp(
                "Target.attachToTarget",
                {"targetId": context.target_id, "flatten": True},
                timeout=timeout,
            )
            session_id = response.get("result", {}).get("sessionId")
            if not session_id:
                raise RuntimeError(
                    f"browser did not attach target {context.target_id!r}"
                )
            context.session_id = _validate_id(session_id, "session_id")
            context.live = True
            context.updated_at = time.monotonic()
            return context.session_id

    def bind_surface(self, target_id: str, surface_target: str) -> Dict[str, Any]:
        """Bind one CDP context to one incarnation-qualified compositor target."""
        context = self._context(target_id)
        surface_target = _validate_surface_target(surface_target)
        with self._global_lock, context.lock, self._state_lock:
            current_target = self._surface_to_target.get(surface_target)
            if current_target and current_target != context.target_id:
                raise RuntimeError(
                    f"surface target is already bound to CDP target {current_target!r}"
                )
            if context.surface_target and context.surface_target != surface_target:
                raise RuntimeError(
                    "context already has a different surface incarnation; unbind it explicitly"
                )
            self._surface_to_target[surface_target] = context.target_id
            context.surface_target = surface_target
            context.updated_at = time.monotonic()
            return context.snapshot()

    def prove_and_bind_surface(
        self,
        target_id: str,
        resolve_surface: Callable[[str], Iterable[str]],
        *,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Prove a CDP-context ↔ compositor-surface join with a random nonce.

        The page title is changed only for the bounded discovery handshake. The
        resolver must return incarnation-qualified compositor target tokens for
        windows containing that nonce. Exactly one match is required; the title
        is restored in ``finally`` and all later operations use the exact sticky
        ids, never the title.
        """
        context = self._context(target_id)
        nonce = f"__hermes_surface_join_{secrets.token_hex(16)}__"
        session_id = self.attach(target_id, timeout=timeout)
        with context.lock:
            old_title_response = self._transport.call_cdp(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const old = document.title; "
                        f"document.title = {json.dumps(nonce)}; return old; }})()"
                    ),
                    "returnByValue": True,
                },
                session_id=session_id,
                timeout=timeout,
            )
            old_title = str(
                old_title_response.get("result", {})
                .get("result", {})
                .get("value", context.title)
                or ""
            )
            deadline = time.monotonic() + timeout
            matches: list[str] = []
            try:
                while time.monotonic() < deadline:
                    matches = sorted(set(resolve_surface(nonce)))
                    if len(matches) == 1:
                        break
                    if len(matches) > 1:
                        raise RuntimeError(
                            f"surface join nonce matched {len(matches)} windows"
                        )
                    time.sleep(0.05)
                if len(matches) != 1:
                    raise TimeoutError("surface join nonce was not observed")
            finally:
                self._transport.call_cdp(
                    "Runtime.evaluate",
                    {
                        "expression": f"document.title = {json.dumps(old_title)}",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                    timeout=timeout,
                )
        return self.bind_surface(target_id, matches[0])

    def unbind_surface(self, target_id: str) -> None:
        context = self._context(target_id)
        with self._global_lock, context.lock, self._state_lock:
            if context.surface_target:
                self._surface_to_target.pop(context.surface_target, None)
                context.surface_target = None
                context.updated_at = time.monotonic()

    def target_for_surface(self, surface_target: str) -> Optional[str]:
        surface_target = _validate_surface_target(surface_target)
        with self._state_lock:
            return self._surface_to_target.get(surface_target)

    def context_call(
        self,
        target_id: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Run one page-local call, serialized only with the same context."""
        if not method.startswith(_CONTEXT_PREFIXES):
            raise ValueError(
                f"{method!r} is not page-local; route it through global_call()"
            )
        context = self._context(target_id)
        session_id = self.attach(target_id, timeout=timeout)
        with context.lock:
            return self._transport.call_cdp(
                method,
                dict(params or {}),
                session_id=session_id,
                timeout=timeout,
            )

    def global_call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Run a browser/profile-global operation under the wider lock."""
        with self._global_lock:
            return self._transport.call_cdp(
                method,
                dict(params or {}),
                timeout=timeout,
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "profile_id": self.profile_id,
                "contexts": {
                    target_id: context.snapshot()
                    for target_id, context in sorted(self._contexts.items())
                },
            }

    def close(self) -> None:
        with self._global_lock:
            contexts = list(self._contexts.values())
            for context in contexts:
                with context.lock:
                    if context.session_id:
                        try:
                            self._transport.call_cdp(
                                "Target.detachFromTarget",
                                {"sessionId": context.session_id},
                                timeout=2.0,
                            )
                        except Exception:
                            pass
                        context.session_id = None
            if self._profile_lock_file is not None:
                fcntl.flock(self._profile_lock_file, fcntl.LOCK_UN)
                self._profile_lock_file.close()
                self._profile_lock_file = None


class BrowserProfileBrokerRegistry:
    """Process-local single owner per browser profile id."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._brokers: Dict[str, BrowserProfileBroker] = {}

    def get_or_create(
        self,
        profile_id: str,
        transport: CDPTransport,
        *,
        profile_dir: Optional[Path] = None,
    ) -> BrowserProfileBroker:
        profile_id = _validate_id(profile_id, "profile_id")
        with self._lock:
            broker = self._brokers.get(profile_id)
            if broker is None:
                broker = BrowserProfileBroker(
                    profile_id,
                    transport,
                    profile_dir=profile_dir,
                )
                self._brokers[profile_id] = broker
            elif broker._transport is not transport:
                raise RuntimeError(
                    f"profile {profile_id!r} already has a different transport owner"
                )
            return broker

    def release(self, profile_id: str) -> None:
        with self._lock:
            broker = self._brokers.pop(profile_id, None)
        if broker is not None:
            broker.close()

    def stop_all(self) -> None:
        with self._lock:
            brokers = list(self._brokers.values())
            self._brokers.clear()
        for broker in brokers:
            broker.close()


browser_profile_brokers = BrowserProfileBrokerRegistry()
