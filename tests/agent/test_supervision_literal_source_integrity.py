"""Independent integrity, store-substitution, and recipient-fence controls."""
import hashlib
import json
import pytest
from tests.agent.test_supervision_literal_sources import native,append,call,record
from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import VERSION
from hermes_lcm.literal_record import ROW_FIELDS
from hermes_lcm.store import MessageStore

@pytest.mark.parametrize('mode',['pack','proposal','auto'])
@pytest.mark.parametrize('role',['user','tool'])
def test_exact_native_row_hash_attribution(native,mode,role):
    sid,text=append(native,role=role)
    payload,refs,values=call(native,sid,text,mode)
    assert refs and values[0],payload
    row=native.engine._store.get(sid)
    encoded=json.dumps({k:row.get(k) for k in ROW_FIELDS},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    assert values[0].record.row_hash==hashlib.sha256(encoded).hexdigest()
    assert {k:json.loads(v) for k,v in values[0].record.source_attribution}=={k:row.get(k) for k in ROW_FIELDS if k!='content'}


def test_real_other_store_same_original_row_is_not_owner(native):
    sid,text=append(native)
    _,refs,values=call(native,sid,text)
    assert values[0]
    original=native.engine._store
    other=MessageStore(str(native.home/'other.db'),ingest_protection_config=native.engine._config)
    original._conn.backup(other._conn)
    assert other.get(sid)==original.get(sid)
    native.engine._store=other
    try:
        with bind_subagent_parent(native.agent):
            assert native.recipient.literal_source(version=VERSION,ref=refs[0]) is None
    finally:
        native.engine._store=original
        other.close()

@pytest.mark.parametrize('field',['grants','data_policy'])
def test_live_recipient_revocation_after_source_read(native,monkeypatch,field):
    sid,text=append(native)
    _,refs,values=call(native,sid,text)
    assert values[0]
    original=native.source.provider.resolve_literal_source
    def resolve(*args):
        record=original(*args)
        assert record
        setattr(native.recipient._registration,field,frozenset())
        return record
    monkeypatch.setattr(native.source.provider,'resolve_literal_source',resolve)
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION,ref=refs[0]) is None


def test_owner_reregistration_revokes_old_ref(native):
    sid,text=append(native)
    _,refs,values=call(native,sid,text)
    assert values[0]
    old=native.source
    new=native.ctx.supervision.register_literal_source_owner(version=VERSION,engine=native.manager._context_engine,provider=old.provider)
    assert new is not None and new is not old and not old.active
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION,ref=refs[0]) is None


@pytest.mark.parametrize('mode',['pack','proposal','auto'])
def test_json_boolean_and_integer_pins_are_distinct(native,mode):
    pins=[]
    for v in [True,1]:
        data=record();data['quantity']={'kind':'non_quantity','unit':None};data['value']=v
        sid,text=append(native,json.dumps(data))
        payload,refs,values=call(native,sid,text,mode)
        assert refs and values[0],payload
        pins.append(dict(values[0].record.coordinate_pins)['value'])
    assert pins==['true','1']

@pytest.mark.parametrize('mode',['pack','proposal','auto'])
def test_unicode_identity_and_exact_character_spans(native,mode):
    pins=[]
    for v in ['é','e\u0301']:
        data=record();data['quantity']={'kind':'non_quantity','unit':None};data['value']=v
        sid,text=append(native,' \n'+json.dumps(data,ensure_ascii=False)+'\n ')
        payload,refs,values=call(native,sid,text,mode)
        assert refs and values[0],payload
        p=values[0]
        assert p.record.source_bytes==text.encode('utf-8')
        assert p.record.record_span==(0,len(text))
        assert p.record.exact_ref==f'lcm:{sid}:0-{len(text)}'
        pins.append(dict(p.record.coordinate_pins)['value'])
    assert pins[0]!=pins[1]

@pytest.mark.parametrize('mode',['pack','proposal','auto'])
@pytest.mark.parametrize('mutation',['escaped-duplicate','unicode-key','boolean-quantity','fractional-time'])
def test_closed_grammar_controls(native,mode,mutation):
    data=record()
    if mutation=='boolean-quantity':data['value']=True
    if mutation=='fractional-time':data['time']['start']='2026-01-01T00:00:00.001Z'
    text=json.dumps(data)
    if mutation=='escaped-duplicate':text=text[:-1]+', "v\\u0061lue": 4}'
    if mutation=='unicode-key':text=text.replace('"entity"','"entitｙ"')
    sid,text=append(native,text)
    payload,refs,values=call(native,sid,text,mode)
    assert not refs and not values,payload

@pytest.mark.parametrize('mode',['pack','proposal','auto'])
def test_session_rebind_before_lookup_is_rejected(native,mode):
    sid,text=append(native)
    payload,refs,values=call(native,sid,text,mode)
    assert values[0],payload
    native.engine.on_session_start('session-B',platform='cli')
    assert native.engine.current_session_id=='session-B'
    with bind_subagent_parent(native.agent):
        assert native.recipient.literal_source(version=VERSION,ref=refs[0]) is None
