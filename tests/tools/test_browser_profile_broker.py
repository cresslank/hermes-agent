from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional

import pytest

from tools.browser_profile_broker import BrowserProfileBroker


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Optional[str]]] = []
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.page_barrier: Optional[threading.Barrier] = None

    def call_cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        with self._lock:
            self.calls.append((method, session_id))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {
                            "targetId": "page-a",
                            "type": "page",
                            "title": "A",
                            "url": "https://a.invalid",
                        },
                        {
                            "targetId": "page-b",
                            "type": "page",
                            "title": "B",
                            "url": "https://b.invalid",
                        },
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": f"session-{params['targetId']}"}}
        if method == "Runtime.evaluate":
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                if self.page_barrier is not None:
                    self.page_barrier.wait(timeout=2)
                time.sleep(0.03)
                return {"result": {"result": {"value": session_id}}}
            finally:
                with self._lock:
                    self.active -= 1
        if method == "Browser.getVersion":
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                return {"result": {"product": "Test"}}
            finally:
                with self._lock:
                    self.active -= 1
        return {"result": {}}


def test_discovers_and_attaches_exact_page_targets():
    transport = FakeTransport()
    broker = BrowserProfileBroker("profile-1", transport)

    contexts = broker.discover()
    result = broker.context_call("page-b", "Runtime.evaluate", {"expression": "1+1"})

    assert [context["target_id"] for context in contexts] == ["page-a", "page-b"]
    assert result["result"]["result"]["value"] == "session-page-b"
    assert ("Target.attachToTarget", None) in transport.calls
    assert ("Runtime.evaluate", "session-page-b") in transport.calls


def test_different_page_contexts_can_run_concurrently():
    transport = FakeTransport()
    transport.page_barrier = threading.Barrier(2)
    broker = BrowserProfileBroker("profile-1", transport)
    broker.discover()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                broker.context_call, target, "Runtime.evaluate", {"expression": target}
            )
            for target in ("page-a", "page-b")
        ]
        for future in futures:
            future.result(timeout=3)

    assert transport.max_active == 2


def test_same_page_context_is_serialized():
    transport = FakeTransport()
    broker = BrowserProfileBroker("profile-1", transport)
    broker.discover()
    broker.attach("page-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                broker.context_call,
                "page-a",
                "Runtime.evaluate",
                {"expression": str(i)},
            )
            for i in range(2)
        ]
        for future in futures:
            future.result(timeout=3)

    assert transport.max_active == 1


def test_browser_global_calls_are_serialized():
    transport = FakeTransport()
    broker = BrowserProfileBroker("profile-1", transport)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(broker.global_call, "Browser.getVersion") for _ in range(2)
        ]
        for future in futures:
            future.result(timeout=3)

    assert transport.max_active == 1


def test_surface_binding_is_exact_unique_and_immutable():
    broker = BrowserProfileBroker("profile-1", FakeTransport())
    broker.bind_surface("page-a", "surface:epoch-a:0001")

    assert broker.target_for_surface("surface:epoch-a:0001") == "page-a"
    with pytest.raises(RuntimeError, match="already bound"):
        broker.bind_surface("page-b", "surface:epoch-a:0001")
    with pytest.raises(RuntimeError, match="different surface incarnation"):
        broker.bind_surface("page-a", "surface:epoch-b:0001")
    with pytest.raises(ValueError, match="incarnation"):
        broker.bind_surface("page-c", "not-exact")


def test_nonce_handshake_joins_context_to_surface_then_restores_title():
    transport = FakeTransport()
    broker = BrowserProfileBroker("profile-1", transport)
    observed: list[str] = []

    def resolve(nonce: str):
        observed.append(nonce)
        return ["surface:epoch-a:0007"]

    bound = broker.prove_and_bind_surface("page-a", resolve)

    assert bound["surface_target"] == "surface:epoch-a:0007"
    assert observed and observed[0].startswith("__hermes_surface_join_")
    runtime_calls = [call for call in transport.calls if call[0] == "Runtime.evaluate"]
    assert len(runtime_calls) == 2  # set nonce, then restore prior title


def test_profile_directory_has_one_broker_owner(tmp_path: Path):
    profile = tmp_path / "browser-profile"
    first = BrowserProfileBroker("profile-1", FakeTransport(), profile_dir=profile)
    try:
        with pytest.raises(RuntimeError, match="another broker owner"):
            BrowserProfileBroker("profile-1", FakeTransport(), profile_dir=profile)
    finally:
        first.close()

    second = BrowserProfileBroker("profile-1", FakeTransport(), profile_dir=profile)
    second.close()


def test_target_domain_is_not_misclassified_as_page_local():
    broker = BrowserProfileBroker("profile-1", FakeTransport())
    with pytest.raises(ValueError, match="global_call"):
        broker.context_call("page-a", "Target.closeTarget", {"targetId": "page-a"})
