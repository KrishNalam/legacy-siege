"""W17's opt-in contract, deterministic coalescing, and repeat-to-note behavior."""
from types import SimpleNamespace
from unittest.mock import Mock

from common import comms, events
from swarm.roles import sentinel


def test_switch_preserves_legacy_message_and_no_notes(monkeypatch):
    messages = Mock()
    monkeypatch.setattr(events, 'col', lambda _: messages)
    monkeypatch.delenv('SWARM_COMMS', raising=False)
    events.say('a', 'hive', 'status', 'hello')
    doc = messages.insert_one.call_args.args[0]
    assert set(doc) == {'from', 'to', 'kind', 'body', 'ts'}
    monkeypatch.setattr(sentinel, 'col', Mock(side_effect=AssertionError('off must not read DB')))
    sentinel.note_repeated_problems(None)
    monkeypatch.setenv('SWARM_COMMS', '1')
    events.say('a', 'hive', 'status', {'incident': 'INC-0001', 'severity': 'critical'})
    doc = messages.insert_one.call_args.args[0]
    assert doc['priority'] == 100 and doc['source'] == 'a' and doc['incident'] == 'INC-0001'


def test_duplicate_merging_priority_and_boundaries():
    def msg(text, **kw):
        return dict(to='hive', kind='status', body=text, incident='INC-0001', system='unix', **kw)
    lines = [msg('queue 12 stalled'), msg('queue 13  stalled'),
             {**msg('queue 12 stalled'), 'incident': 'INC-0002'},
             msg('failed', severity='critical'), msg('approval', severity='error')]
    groups = comms.prioritise(lines, {'INC-0001'})
    assert groups[0]['body'] == 'failed'
    assert [g['count'] for g in groups if g['body'].startswith('queue')] == [2, 1]
    assert sum(g['count'] for g in groups) == len(lines)
    assert len(comms.prioritise([msg('HTTP 200'), msg('HTTP 500')])) == 2
    assert len(comms.prioritise([msg({'value': 1}), msg({'value': 2})])) == 2
    assert comms.prioritise([msg('ordinary', severity='info'),
        {**msg('ordinary', severity='info'), 'incident': 'INC-0002'}], {'INC-0002'})[0]['incident'] == 'INC-0002'


def test_repeat_note_once_and_lesson_repeat(db, monkeypatch):
    monkeypatch.setenv('SWARM_COMMS', '1')
    incidents = Mock()
    rows = [{'_id': f'INC-{i}', 'system': 'portal', 'root_cause_system': 'mainframe',
             'fault_class': 'poison_record'} for i in range(3)]
    incidents.find.return_value = rows[:2]
    lessons = Mock()
    lessons.find.return_value = []
    db.gardener_notes.delete_many({})
    monkeypatch.setattr(sentinel, 'col', lambda name: {'incidents': incidents, 'lessons': lessons,
                                                     'gardener_notes': db.gardener_notes}[name])
    emit = Mock()
    monkeypatch.setattr(sentinel, 'say', emit)
    agent = SimpleNamespace(id='sentinel-1', think=Mock())
    sentinel.note_repeated_problems(agent)
    assert db.gardener_notes.count_documents({}) == 0
    incidents.find.return_value = rows
    sentinel.note_repeated_problems(agent)
    sentinel.note_repeated_problems(agent)
    assert emit.call_count == 1
    assert db.gardener_notes.find_one()['count'] == 3
    incidents.find.return_value = []
    lessons.find.return_value = [{'_id': 'LS:x', 'system': 'unix', 'count': 4,
                                 'why': 'ACK did nothing', 'incidents': ['INC-1']}]
    sentinel.note_repeated_problems(agent)
    assert db.gardener_notes.count_documents({}) == 2
    assert emit.call_count == 2
    db.gardener_notes.delete_many({})


def test_sentinel_tells_queen_only_with_switch(db, monkeypatch):
    """Structure v3 step 1: with SWARM_COMMS=1 opening an incident also posts sentinel -> queen on the bus."""
    from swarm.roles.sentinel import build_graphs
    agent = SimpleNamespace(id='sentinel-1', think=Mock(),
                            systems=SimpleNamespace(health=lambda s: {'status': 'degraded'}))
    detect = build_graphs(agent)[0]
    for on in (False, True):
        db.incidents.delete_many({})
        db.messages.delete_many({})
        if on:
            monkeypatch.setenv('SWARM_COMMS', '1')
        else:
            monkeypatch.delenv('SWARM_COMMS', raising=False)
        out = detect.invoke({'kind': 'chain', 'segment': ['portal'], 'symptoms': {'portal': 'portal stale'}},
                            {'configurable': {'thread_id': f'test:v3-notify:{on}'}}, durability='exit')
        assert out['action'] == 'opened'
        sent = list(db.messages.find({'from': 'sentinel-1', 'to': 'queen'}))
        assert len(sent) == (1 if on else 0)
        if on:
            assert sent[0]['kind'] == 'incident' and sent[0]['incident'] == out['incident']
    db.incidents.delete_many({})
    db.messages.delete_many({})
