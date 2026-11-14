"""A poisoned recalled repair fails Guard read-back, is retired, and gets a fresh policy plan."""
import pytest
from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


def test_poisoned_recalled_repair_is_rejected_and_replaced(db, fake):
    from common.db import col
    from swarm import recall
    reset_runtime()
    col('lessons').delete_many({})
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'FAILURE_MEMORY': '1', 'RECOVERY_BACKOFF_S': '0.1'},
                       log_name='poison')
    try:
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        first = run_until_resolved(1)
        pb = col('playbooks').find_one({'incident': first['_id']})
        # Right symptom/fault label, but a repair directed at the wrong system.
        poisoned = [{'system': 'mainframe', 'op': 'purge_spool', 'params': {'path': '/var/spool/xfer'},
                     'desc': 'Poisoned memory', 'destructive': False}]
        col('playbooks').update_one({'_id': pb['_id']}, {'$set': {'fix': poisoned}})
        wait_for(lambda: recall.search(pb['symptom_text'])[1], 30)
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        second = run_until_resolved(2)
        assert second['recall']['used']
        assert second['recall']['rejected'] == pb['_id']
        assert second['memory_replanned']
        assert col('playbooks').find_one({'_id': pb['_id']})['stale']
        assert col('lessons').find_one({'kind': 'misfit', 'playbook': pb['_id']})
        assert col('thoughts').find_one({'incident': second['_id'], 'kind': 'reject',
                                        'text': {'$regex': 'Rejected memory'}})
        assert any(s.get('superseded_by_replan') and s['status'] == 'failed' for s in second['steps'])
        learned = col('playbooks').find_one({'incident': second['_id']})
        assert all(s['system'] != 'mainframe' for s in learned['fix'])
        assert not any(h['_id'] == pb['_id'] for h in recall.search(pb['symptom_text'])[1])
    finally:
        hive.stop()
        col('lessons').delete_many({})


def test_poison_rejection_requires_both_switches(monkeypatch):
    from swarm import recovery
    monkeypatch.setattr('common.db.col', lambda *a: (_ for _ in ()).throw(AssertionError('unexpected DB access')))
    monkeypatch.delenv('RED_FLEET', raising=False)
    monkeypatch.delenv('SWARM_RECOVERY', raising=False)
    monkeypatch.setenv('FAILURE_MEMORY', '1')
    assert recovery.reject_bad_memory('I', 'failure', 'guard-1') is None
    monkeypatch.setenv('SWARM_RECOVERY', '1')
    monkeypatch.setenv('FAILURE_MEMORY', '0')
    assert recovery.reject_bad_memory('I', 'failure', 'guard-1') is None


def test_good_batch_abort_playbook_survives_a_lie(db, monkeypatch):
    """Dynamic-fact class: today's record id differs from the remembered one. A lying ACK alone must not
    retire the good playbook; a remembered op of the wrong kind still does."""
    from common.db import col, now
    from swarm import recovery
    monkeypatch.setenv('SWARM_RECOVERY', '1')
    monkeypatch.setenv('FAILURE_MEMORY', '1')
    col('lessons').delete_many({})
    fix = [{'system': 'mainframe', 'op': 'quarantine_record', 'params': {'record_id': 'TX900001'}},
           {'system': 'mainframe', 'op': 'restart_batch', 'params': {'job': 'NIGHTBAT'}},
           {'system': 'unix', 'op': 'rerun_transfer', 'params': {}},
           {'system': 'portal', 'op': 'refresh_cache', 'params': {}}]
    today = [{**f, 'step_id': f'S{i}', 'status': 'planned'} for i, f in enumerate(fix)]
    today[0] = {**today[0], 'params': {'record_id': 'TX900007'}, 'status': 'failed'}
    for pid, pfix in (('PB-GOOD', fix), ('PB-BAD', [{**fix[0], 'op': 'purge_spool'}] + fix[1:])):
        col('playbooks').insert_one({'_id': pid, 'fix': pfix, 'system': 'mainframe', 'system_version': 1,
                                     'fault_class': 'batch_abort', 'stale': False, 'created_at': now()})
        col('incidents').insert_one({'_id': f'INC-{pid}', 'fault_class': 'batch_abort', 'steps': today,
                                     'status': 'fixing', 'system': 'portal', 'opened_at': now(),
                                     'recall': {'used': True, 'query': 'q', 'hits': [{'playbook_id': pid, 'usable': True}]}})
    assert recovery.reject_bad_memory('INC-PB-GOOD', 'ACK ≠ APPLIED: quarantined=[]', 'guard-1') is None
    assert not col('playbooks').find_one({'_id': 'PB-GOOD'})['stale']
    assert recovery.reject_bad_memory('INC-PB-BAD', 'ACK ≠ APPLIED: quarantined=[]', 'guard-1') == 'PB-BAD'
    assert col('playbooks').find_one({'_id': 'PB-BAD'})['stale']
    col('lessons').delete_many({})


@pytest.mark.parametrize('fault,first,second,shape', [
    ('batch_abort', {'record_id': 'REC-00042'}, {'record_id': 'REC-00077'}, 'wrong_record'),
    ('disk_fill', {}, {}, 'wrong_system'),
])
def test_red_poison_each_shape_is_visibly_rejected(db, fake, monkeypatch, fault, first, second, shape):
    """Red's Memory Striker (chaos.faults.poison_memory) in both shapes -> 'Rejected memory', stale, lesson, recovery."""
    from chaos import faults
    from common.db import col
    from swarm import recall
    reset_runtime()
    col('lessons').delete_many({})
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'FAILURE_MEMORY': '1', 'RECOVERY_BACKOFF_S': '0.1'},
                       log_name=f'poison-{shape}')
    try:
        system = 'mainframe' if fault == 'batch_abort' else 'unix'
        fake.post(f'/{system}/_chaos/{fault}', json={'params': first})
        pb = col('playbooks').find_one({'incident': run_until_resolved(1)['_id']})
        monkeypatch.setenv('CHAOS_POISON_MEMORY', '1')
        res = faults.poison_memory({'playbook': pb['_id']})
        assert res['ok'] and res['shape'] == shape
        planted = res['playbook']
        wait_for(lambda: any(h['_id'] == planted for h in recall.search(pb['symptom_text'])[1]), 30)
        fake.post(f'/{system}/_chaos/{fault}', json={'params': second})
        inc = run_until_resolved(2)
        assert planted in ((inc['recall'].get('memory_rejected') or []) + [inc['recall'].get('rejected')])
        assert col('playbooks').find_one({'_id': planted})['stale']
        assert col('lessons').find_one({'kind': 'misfit', 'playbook': planted})
        assert col('thoughts').find_one({'incident': inc['_id'], 'kind': 'reject',
                                         'text': {'$regex': f'^Rejected memory {planted}'}})
        if shape == 'wrong_record':  # today's record was quarantined, never the planted one
            assert [s['params']['record_id'] for s in inc['steps'] if s['op'] == 'quarantine_record'] == ['REC-00077']
            # the verified repair from the same incident is restored and used: the round-2 speedup survives
            assert inc['recall']['used'] and inc['recall']['rejected'] == planted
            assert not col('playbooks').find_one({'_id': pb['_id']})['stale']
            assert inc.get('diagnosis_path') == [system]
    finally:
        hive.stop()
        faults.clear_planted_memory()
        col('lessons').delete_many({})
