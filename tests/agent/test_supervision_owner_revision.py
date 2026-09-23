"""C02: an original owner request cannot consume a later revision's proposal."""
import json

import pytest

from tests.agent.test_supervision_evidence_owners import vertical, seed
from tests.agent.test_supervision_lcm_consumption import durable
from tests.agent.supervision_test_support import accept
from tests.agent import test_supervision_mcp_recipients as mcp


def interleave(v, monkeypatch, owner, boundary):
    observe = v.runtime.observe
    wait = v.runtime._wait_for
    captured = []
    def changed(event, facts, **kw):
        if kw.get("owner") == owner:
            captured.append((event, facts, kw))
            if boundary == "observe":
                accept(v.agent, "Use the revised current task, not the earlier claim.", continuation=True)
            result = observe(event, facts, **kw)
            if boundary == "observe" and result:
                # Reproduce the review's already-ready newer proposal interleave.
                wait(kw["target_id"], kw["deadline"], v.runtime.revision)
            return result
        return observe(event, facts, **kw)
    monkeypatch.setattr(v.runtime, "observe", changed)
    if boundary == "selection":
        def waited(target, deadline, revision):
            wait(target, deadline, revision)
            event, facts, kw = captured[-1]
            accept(v.agent, "Use the revised current task, not the earlier claim.", continuation=True)
            # Drain only the now-stale old submission, through the real validator,
            # so the ordinary wait below cannot wake on an old pending entry.
            with v.runtime.lock:
                from agent.supervision_types import Action
                assert v.runtime._take(target, {Action.RANK_CANDIDATES}) is None
            # A genuine new owner observation becomes ready at this target. Even
            # though valid on its own revision it cannot rank the old request.
            observe(event, facts, **{**kw, "expected_revision": v.runtime.revision})
            wait(target, deadline, v.runtime.revision)
            assert any(p.target_id == target and p.expected == v.runtime.revision
                       for p, _ in v.runtime.pending)
        monkeypatch.setattr(v.runtime, "_wait_for", waited)
    return captured


@pytest.mark.parametrize("boundary", ["positive", "observe", "selection"])
def test_ordinary_lcm_recall_revision_interleave(vertical, monkeypatch, boundary):
    from hermes_lcm import tools
    v = vertical
    seed(v)
    initial = v.runtime.revision
    captured = interleave(v, monkeypatch, "lcm", boundary)
    result = json.loads(tools.lcm_recall({"query": "Claim", "limit": 10, "detail": "answer_ready", "seen_refs": []}, engine=v.engine))
    assert captured
    assert captured[0][2]["expected_revision"] == initial
    if boundary == "positive":
        assert len(v.calls) == 1
        assert result["provenance"]["rerank"] == "applied"
        assert any(s == "consumed" for s, _ in durable(v))
    else:
        assert v.runtime.revision != initial
        assert result["provenance"]["rerank"] != "applied"
        assert not any(s == "consumed" for s, _ in durable(v))
        assert not v.runtime.owner_selections
        assert len(v.calls) == (0 if boundary == "observe" else 2)
    for hit in result["hits"]:
        assert hit["exact_ref"].startswith("lcm:")
        assert "exact limitation retained" in v.engine._store.get(hit["store_id"])["content"]


@pytest.mark.parametrize("boundary", ["positive", "observe", "selection"])
def test_real_mcp_shared_owner_revision_control(tmp_path, monkeypatch, boundary):
    with mcp.providers(tmp_path / "profile", monkeypatch) as v:
        initial = v.runtime.revision
        captured = interleave(v, monkeypatch, "mcp", boundary)
        _, actual = mcp.invoke(v, monkeypatch)
        assert captured[0][2]["mcp_recipients"]
        expected = mcp.switchloom_payload()
        if boundary == "positive":
            expected["items"].reverse()
            assert len(v.calls[0]) == 1
            assert any(s == "consumed" for s, _, _ in mcp.stored(v))
        else:
            assert v.runtime.revision != initial
            assert not any(s == "consumed" for s, _, _ in mcp.stored(v))
            assert not v.runtime.owner_selections
            assert len(v.calls[0]) == (0 if boundary == "observe" else 2)
        assert actual == expected
