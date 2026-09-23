"""Optional claim consumers must not invalidate a completed native source read."""
import asyncio
import json

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import VERSION
from agent.supervision_planning_records import load
from tests.agent.test_supervision_claim_uses import native as native, pack, prepare


@pytest.mark.parametrize('mode', ['pack', 'auto', 'proposal'])
@pytest.mark.parametrize('after_consume', [False, True])
def test_optional_callback_fault_preserves_source_result(native, monkeypatch, mode, after_consume):
    n = native
    sources = prepare(n)
    owner = n.rt.dependencies.claim_uses
    original = owner.published
    attempts = []

    def unavailable(registration, refs):
        attempts.append(tuple(refs))
        if after_consume:
            original(registration, refs)
        raise RuntimeError('synthetic optional callback fault')

    monkeypatch.setattr(owner, 'published', unavailable)
    args: dict[str, object] = dict(question='What is the mass of asset A?',
                baseline_refs=[dict(exact_ref=r[2]) for r in sources],
                budgets=dict(max_retrieval_calls=0))
    name = 'lcm_evidence_pack' if mode == 'pack' else 'lcm_compile_evidence'
    if mode != 'pack':
        args['mode'] = mode
        if mode == 'proposal':
            args['proposal'] = dict(
                version='evidence-selector-v1', requested_facets=[], missing_facets=[],
                selections=[dict(claim_id=f'c{i}', facet='answer', exact_ref=r[2], quote=r[1])
                            for i, r in enumerate(sources)])
    with bind_subagent_parent(n.agent):
        result = json.loads(n.engine.handle_tool_call(name, args))
        # Lookup before the ordinary tool-round boundary: publication is intact,
        # not just an optimistic list of handles whose provider records were freed.
        refs = result['source_propositions']
        assert len(refs) == len(sources)
        assert attempts == [tuple(refs)]
        assert all(n.facade.literal_source(version=VERSION, ref=ref) is not None for ref in refs)
    n.rt.committed_batch([])
    assert len(n.calls) == int(after_consume)
    rows = [dict(row) for row in load(n.rt)]
    assert len(rows) == 1
    assert rows[0]['status'] == ('contested' if after_consume else 'claim_declared')
    # A late callback exception neither replays nor rolls back a committed effect.
    assert bool(n.rt.dependencies.contested_claims) is after_consume


@pytest.mark.parametrize('fault_kind', [KeyboardInterrupt, SystemExit, asyncio.CancelledError, RuntimeError])
def test_callback_containment_preserves_cancellation_and_source_failures(native, monkeypatch, fault_kind):
    n = native
    sources = prepare(n)
    fault = fault_kind('synthetic propagation control')

    def unavailable(*args, **kwargs):
        raise fault

    if fault_kind is RuntimeError:
        # The native source publisher itself is not the optional consumer boundary.
        monkeypatch.setattr(n.ctx.supervision._literal_source_registration, 'publish', unavailable)
    else:
        monkeypatch.setattr(n.rt.dependencies.claim_uses, 'published', unavailable)
    with pytest.raises(fault_kind) as caught:
        pack(n, sources)
    assert caught.value is fault
    assert not n.calls
    assert not n.rt.dependencies.contested_claims
