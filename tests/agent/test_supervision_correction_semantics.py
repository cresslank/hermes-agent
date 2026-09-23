"""Native nonnumeric source event, separately granted judgment and real notice."""
import json
import queue

import pytest

from tests.agent.test_supervision_claim_uses import native, prepare, append, record  # noqa: F401
from tests.agent.test_supervision_corrections import configured, sink, drain_source, rows
from tests.agent.test_supervision_original_output import setup, finalize


@pytest.mark.parametrize('native', ['correction-semantic'], indirect=True)
@pytest.mark.parametrize('outcome', ['incompatible', 'compatible', 'different_scope', 'insufficient',
                                     'missing-egress', 'revoked-before-send', 'revoked-after-answer'])
def test_native_semantic_correction_is_fenced(native, monkeypatch, tmp_path, outcome):
    from tools.process_registry import process_registry
    n = native
    configured(n)
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    first = record()
    first['quantity'] = dict(kind='non_quantity', unit=None)
    first['value'] = 'running'
    # Only change the synthetic original input; all native declaration/source,
    # accepted-final, original-output and source-event paths remain production.
    monkeypatch.setattr('tests.agent.test_supervision_original_output.prepare',
                        lambda n: prepare(n, first_data=first))
    messages, _ = setup(n, monkeypatch)
    with sink(n, monkeypatch, tmp_path/'semantic-sink', 'cli') as output:
        output.original(finalize(n, messages, monkeypatch))
        second = {**first, 'value': 'stopped'}
        new = append(n, second)
        event = process_registry.completion_queue.get_nowait()
        drain_source(n, event)
        source = n.ctx.supervision._literal_source_registration
        if outcome == 'missing-egress':
            source.correction_recipients = frozenset()
        elif outcome == 'revoked-before-send':
            admit = n.facade.admit_dispatch
            def revoke(request):
                source.correction_recipients = frozenset()
                return admit(request)
            monkeypatch.setattr(n.facade, 'admit_dispatch', revoke)
        elif outcome == 'revoked-after-answer':
            n.mode['on_response'] = lambda: setattr(source, 'correction_recipients', frozenset())
        else:
            n.mode['choice'] = outcome
        n.agent.notice_callback = output.notice
        n.rt.bind_turn()
        n.rt.finish_turn()
        output.flush()
        result, = rows(n)
        emitted = outcome == 'incompatible'
        assert (result['status'] == 'emitted') is emitted, (result, list(n.rt.receipts.values()), n.bridge.supervisor.inspect())
        assert ('Correction:' in output.read()) is emitted
        assert result['truth_winner'] is None
        assert result['source_refs'][1] == new[2]
        if emitted:
            assert result['classification'] == 'semantic_relation'
            assert len(n.calls) == 1
            facts = n.calls[0]['state']['facts']
            assert facts['a']['text'] != facts['b']['text']
            assert json.loads(facts['a']['text'])['value'] == 'running'
            assert facts['delivered_consequential'] and facts['relation_only']
        if outcome in {'missing-egress', 'revoked-before-send'}:
            assert not n.calls
        before = output.read()
        n.rt.bind_turn()
        n.rt.finish_turn()
        output.flush()
        assert output.read() == before
