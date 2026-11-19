"""W17's opt-in contract, deterministic coalescing, and repeat-to-note behavior."""
from types import SimpleNamespace
from unittest.mock import Mock

from common import comms, events
from agents.roles import monitor


def test_switch_preserves_legacy_message_and_no_notes(monkeypatch):
    messages = Mock()
    monkeypatch.setattr(events, 'col', lambda _: messages)
    monkeypatch.delenv('AGENTS_COMMS', raising=False)
    events.say('a', 'hive', 'status', 'hello')
    doc = messages.insert_one.call_args.args[0]
    assert set(doc) == {'from', 'to', 'kind', 'body', 'ts'}
    monkeypatch.setattr(monitor, 'col', Mock(side_effect=AssertionError('off must not read DB')))
    monitor.note_repeated_problems(None)
    monkeypatch.setenv('AGENTS_COMMS', '1')
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
    monkeypatch.setenv('AGENTS_COMMS', '1')
    incidents = Mock()
    rows = [{'_id': f'INC-{i}', 'system': 'portal', 'root_cause_system': 'mainframe',
             'fault_class': 'poison_record'} for i in range(3)]
    incidents.find.return_value = rows[:2]
    lessons = Mock()
    lessons.find.return_value = []
    db.gardener_notes.delete_many({})
    monkeypatch.setattr(monitor, 'col', lambda name: {'incidents': incidents, 'lessons': lessons,
                                                     'gardener_notes': db.gardener_notes}[name])
    emit = Mock()
    monkeypatch.setattr(monitor, 'say', emit)
    agent = SimpleNamespace(id='monitor-1', think=Mock())
    monitor.note_repeated_problems(agent)
    assert db.gardener_notes.count_documents({}) == 0
    incidents.find.return_value = rows
    monitor.note_repeated_problems(agent)
    monitor.note_repeated_problems(agent)
    assert emit.call_count == 1
    assert db.gardener_notes.find_one()['count'] == 3
    incidents.find.return_value = []
    lessons.find.return_value = [{'_id': 'LS:x', 'system': 'unix', 'count': 4,
                                 'why': 'ACK did nothing', 'incidents': ['INC-1']}]
    monitor.note_repeated_problems(agent)
    assert db.gardener_notes.count_documents({}) == 2
    assert emit.call_count == 2
    db.gardener_notes.delete_many({})


def test_monitor_tells_coordinator_only_with_switch(db, monkeypatch):
    """Structure v3 step 1: with AGENTS_COMMS=1 opening an incident also posts monitor -> coordinator on the bus."""
    from agents.roles.monitor import build_graphs
    agent = SimpleNamespace(id='monitor-1', think=Mock(),
                            systems=SimpleNamespace(health=lambda s: {'status': 'degraded'}))
    detect = build_graphs(agent)[0]
    for on in (False, True):
        db.incidents.delete_many({})
        db.messages.delete_many({})
        if on:
            monkeypatch.setenv('AGENTS_COMMS', '1')
        else:
            monkeypatch.delenv('AGENTS_COMMS', raising=False)
        out = detect.invoke({'kind': 'chain', 'segment': ['portal'], 'symptoms': {'portal': 'portal stale'}},
                            {'configurable': {'thread_id': f'test:v3-notify:{on}'}}, durability='exit')
        assert out['action'] == 'opened'
        sent = list(db.messages.find({'from': 'monitor-1', 'to': 'coordinator'}))
        assert len(sent) == (1 if on else 0)
        if on:
            assert sent[0]['kind'] == 'incident' and sent[0]['incident'] == out['incident']
    db.incidents.delete_many({})
    db.messages.delete_many({})


def _until(pred, timeout=20.0, what='condition'):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


def test_log_flood_bounded_batches_and_merged_counts(db, monkeypatch):
    """W29: 1,200 estate WARN/ERROR/ABEND lines through the real change stream come out as a handful of
    source=<system> feed messages, every batch <= 256, totals in stats:comms exact; INFO lines are ignored."""
    import threading
    from common.db import now, world
    from agents.roles import reporter
    from agents.tests.conftest import reset_runtime
    reset_runtime()
    world().system_logs.delete_many({})
    monkeypatch.setenv('AGENTS_COMMS', '1')
    batches, real_post = [], reporter.post_batch

    def recording(agent, lines, **kw):
        batches.append(len(lines))
        return real_post(agent, lines, **kw)
    monkeypatch.setattr(reporter, 'post_batch', recording)
    agent = SimpleNamespace(id='reporter-1', stop=threading.Event(), nap=lambda s: agent.stop.wait(s),
                            pause_point=lambda: None)
    t = threading.Thread(target=reporter.run_prioritiser, args=(agent,), daemon=True)
    t.start()
    try:
        probe = {'system': 'unix', 'level': 'WARN', 'code': 'PROBE', 'line': 'probe'}
        _until(lambda: world().system_logs.insert_one({**probe, 'ts': now()}) and
               (db.stats.find_one({'_id': 'comms'}) or {}).get('logs'), what='prioritiser stream to open')
        settled = lambda: (db.stats.find_one({'_id': 'comms'}) or {}).get('logs')  # noqa: E731
        prev = None
        while prev != settled():  # stray probes still in flight are counted before the baseline
            prev = settled()
            agent.stop.wait(1.0)
        base = db.stats.find_one({'_id': 'comms'})
        batches.clear()
        db.messages.delete_many({'kind': 'log', 'system': 'unix', 'body': {'$regex': 'PROBE'}})
        lines = []
        for i in range(400):
            lines.append({'system': 'mainframe', 'level': 'ERROR', 'code': 'IEF450I',
                          'line': f'IEF450I NIGHTBAT ABEND=S0C7 U0000 STEP020 POSTTRN REC={100000 + i}'})
            lines.append({'system': 'portal', 'level': 'ERROR', 'code': 'SOAP-FAULT',
                          'line': f'SEVERE [http-8080-exec-{i % 12 + 1}] c.bank.soap.XferClient - SOAP Fault'})
            lines.append({'system': 'unix', 'level': 'WARN', 'code': 'XFER',
                          'line': f'CRON[{4000 + i}]: xfer_pull.sh retry {i} of feed queue 7'})
            lines.append({'system': 'unix', 'level': 'INFO', 'code': 'CRON', 'line': f'CRON[{i}]: ok'})
        world().system_logs.insert_many([{**d, 'ts': now()} for d in lines])
        _until(lambda: db.stats.find_one({'_id': 'comms'})['logs'] - base['logs'] >= 1200, 30, 'flood to drain')
    finally:
        agent.stop.set()
        t.join(5)
    stats = db.stats.find_one({'_id': 'comms'})
    feed = list(db.messages.find({'kind': 'log', 'comms_processed': True}))
    assert stats['logs'] - base['logs'] == 1200 and stats['messages'] - base['messages'] == 1200
    assert batches and max(batches) <= reporter.BATCH_MAX and len(batches) >= 1200 // reporter.BATCH_MAX
    assert stats['merged'] - base['merged'] == 1200 - len(feed)
    assert len(feed) <= 3 * len(batches)  # one feed message per signature per batch
    by_source = {}
    for m in feed:
        by_source[m['source']] = by_source.get(m['source'], 0) + m['count']
    assert by_source == {'mainframe': 400, 'portal': 400, 'unix': 400}
    assert {m['severity'] for m in feed if m['source'] == 'mainframe'} == {'critical'}  # ABEND outranks ERROR
    assert not db.messages.find_one({'body': {'$regex': 'CRON\\[\\d+\\]: ok'}})
    world().system_logs.delete_many({})


def _coordinator_first_claim(db, monkeypatch, switch):
    import threading
    from common.db import now
    from agents import harness
    from datetime import timedelta
    from agents import leases
    from agents.roles import coordinator, investigator
    from agents.tests.conftest import reset_runtime
    reset_runtime()
    if switch:
        monkeypatch.setenv('AGENTS_COMMS', '1')
    else:
        monkeypatch.delenv('AGENTS_COMMS', raising=False)
    db.agents.insert_one({'_id': 'investigator-1', 'role': 'investigator', 'status': 'idle', 'phase': None, 'last_seen': now()})
    base = {'active': True, 'status': 'open', 'assigned_to': None, 'owner': None, 'opened_at': now(), 'symptoms': []}
    db.incidents.insert_many([
        {**base, '_id': 'INC-0001', 'system': 'mainframe', 'affected': ['mainframe'], 'correlation_key': 'chain:mainframe'},
        {**base, '_id': 'INC-0002', 'system': 'portal', 'affected': ['portal'], 'correlation_key': 'chain:portal'}])
    # the feed says the mainframe one is loud (ABEND), but the portal one is the customer-facing one
    db.messages.insert_one({'from': 'reporter-1', 'to': 'hive', 'kind': 'log', 'body': 'mainframe IEF450I: ABEND',
                            'ts': now(), 'incident': 'INC-0001', 'system': 'mainframe', 'priority': 101,
                            'comms_processed': True, 'count': 40})
    agent = harness.Agent('coordinator', 'coordinator-1')
    agent.register()
    t = threading.Thread(target=coordinator.run, args=(agent,), daemon=True)
    t.start()
    try:
        _until(lambda: db.thoughts.count_documents({'agent': 'coordinator-1', 'text': {'$regex': 'opened'}}) >= 2 and
               db.incidents.count_documents({'assigned_to': 'investigator-1'}), what='coordinator to triage both')
    finally:
        agent.stop.set()
        t.join(5)
    for i, iid in enumerate(('INC-0001', 'INC-0002')):  # both self-claimable now; INC-0001 is the older one
        db.incidents.update_one({'_id': iid}, {'$set': {'opened_at': now() - timedelta(seconds=6 - i)}})
    inc = leases.claim(db.incidents, investigator.claim_query('investigator-1'), 'investigator-1', sort=investigator.claim_sort())
    return inc['_id']


def test_coordinator_claims_customer_facing_incident_first(db, monkeypatch):
    """W29: two unassigned incidents, one idle investigator, which then claims. Switch off: oldest first (INC-0001).
    AGENTS_COMMS=1: the portal (customer-facing) incident is assigned and claimed first, and the Coordinator says why."""
    assert _coordinator_first_claim(db, monkeypatch, switch=False) == 'INC-0001'
    assert _coordinator_first_claim(db, monkeypatch, switch=True) == 'INC-0002'
    assert db.thoughts.find_one({'agent': 'coordinator-1', 'text': {'$regex': 'INC-0002 first \\(customer-facing\\)'}})
    ranked = sorted([{'_id': 'INC-1', 'affected': ['unix']}, {'_id': 'INC-2', 'affected': ['mainframe']}],
                    key=lambda i: comms.incident_rank(i, {'INC-1': 20, 'INC-2': 100}[i['_id']]))
    assert ranked[0]['_id'] == 'INC-2'  # neither customer-facing: the feed's priority decides
    from agents.tests.conftest import reset_runtime
    reset_runtime()  # leave no investigator-1/coordinator-1 agents behind for later modules (test_roster inserts investigator-1)
