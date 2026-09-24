"""F09 authorization and F04 reuse must compose in the real dispatcher."""
import json
import pytest

from tests.agent.test_owned_delegation_planning import base_rig, rig, native, factory
from tests.agent.test_supervision_read_supplier import requests, read
from tests.agent.test_supervision_efficiency import feature_calls


@pytest.mark.parametrize('relation,change', [
    ('within_scope', 'none'), ('outside_scope', 'none'),
    ('within_scope', 'instruction'), ('within_scope', 'arguments'),
    ('within_scope', 'revoked'), ('within_scope', 'during_consume'),
])
def test_authorization_then_reuse_are_distinct_consumable_decisions(factory, monkeypatch, relation, change):
    from agent.supervision_types import project
    from agent import tool_executor
    real_dispatch = tool_executor._dispatch_authorized_once
    inject_start = False
    def dispatch(*args, **kwargs):
        if inject_start:
            kwargs['begin_execution'] = begin
        return real_dispatch(*args, **kwargs)
    # make_planning captures the real dispatch binding when constructing p.call.
    monkeypatch.setattr(tool_executor, '_dispatch_authorized_once', dispatch)
    p = factory()
    rows = requests(p)
    p.commit(rows)
    first, original = read(p, rows[0])
    registration = p.native.bridge.native._registration
    section = p.native.rig.config['supervision']['plugins']['fixture-supervisor']
    section['grants'].append('authorize_action')
    section['egress_policy']['fields'].update({
        name: 'synthetic' for name in ('accepted_scope', 'scope_complete', 'action')})
    config_path = p.native.rig.home / 'config.yaml'
    persisted = json.loads(config_path.read_text())
    persisted['supervision']['plugins']['fixture-supervisor']['grants'] = section['grants']
    persisted['supervision']['plugins']['fixture-supervisor']['egress_policy'] = section['egress_policy']
    config_path.write_text(json.dumps(persisted))
    from hermes_cli.config import load_config_readonly
    load_config_readonly()
    registration.grants = frozenset((*registration.grants, 'authorize_action'))
    registration.data_policy = frozenset((*registration.data_policy, 'project_excerpt', 'task_text'))
    registration.egress_policy = project(section['egress_policy'])
    p.native.choice['F09'] = relation
    evaluations = []
    monkeypatch.setattr('agent.tool_executor._run_with_activity_heartbeat',
                        lambda agent, name, run: evaluations.append(name) or run())
    p.a.client.reset_mock()
    arguments = rows[1]['operation']['arguments']
    # Both selections have returned before the real start-order boundary. Reuse
    # must pass that boundary too, rather than returning early around it.
    from agent.supervision_context import accepted_input_origin
    from agent.supervision_read_reuse import ReadReuse
    advances = []
    def invalidate():
        if change == 'instruction':
            p.rt.accept_instruction(accepted_input_origin(
                'Read the source fresh instead.', kind='cli', continuation=True))
        elif change == 'arguments':
            arguments['offset'] = 2
        elif change in {'revoked', 'during_consume'}:
            registration.grants -= {'authorize_action'}
    def begin(callback):
        advances.append(True)
        if change != 'during_consume':
            invalidate()
        if callback is not None:
            callback()
    if change == 'during_consume':
        real_consume = ReadReuse.consume
        def consume(selected, owner):
            result = real_consume(selected, owner)
            assert result is not None
            invalidate()
            return result
        monkeypatch.setattr(ReadReuse, 'consume', consume)
    inject_start = True
    _, actual = p.call('read_file', arguments, settle=False)
    assert advances == [True]
    assert len(feature_calls(p.native, 'F09')) == 1
    assert evaluations == [], (p.native.bridge.supervisor.inspect(), p.rt.receipts, [list(b['questions']) for b in p.native.calls], p.owner.current())
    assert not p.a.client.mock_calls
    envelope = json.loads(actual)
    assert envelope['executed'] is False
    if change != 'none':
        assert len(feature_calls(p.native, 'F04')) == 1
        assert 'reused_from' not in envelope and envelope['type'] == 'supervision_advisory'
    elif relation == 'within_scope':
        assert len(feature_calls(p.native, 'F04')) == 1
        assert envelope['reused_from'] == 'tool:' + first
        assert envelope['result'] == original
        reasons = {r.reason for r in p.rt.receipts.values() if r.status == 'applied'}
        assert {'action_authorized', 'existing_result_reused'} <= reasons
    else:
        assert not feature_calls(p.native, 'F04')
        assert 'reused_from' not in envelope
        assert 'outside' in envelope['error']
