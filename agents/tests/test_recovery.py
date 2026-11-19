"""Red-fleet recovery must outlast lies and diagnose a different fault after learning."""
from agents.tests.conftest import reset_runtime, run_until_resolved, start_swarm


def test_recovery_switch(monkeypatch):
    from agents import recovery
    monkeypatch.delenv('RED_FLEET', raising=False)
    monkeypatch.delenv('SWARM_RECOVERY', raising=False)
    assert not recovery.enabled()
    monkeypatch.setenv('RED_FLEET', '1')
    assert not recovery.enabled()  # RED_FLEET alone must not change the lie / round-2 timing
    monkeypatch.setenv('SWARM_RECOVERY', '1')
    assert recovery.enabled()


def test_lies_slow_apply_then_disk_fill(db, fake):
    from common.db import col
    reset_runtime()
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'RECOVERY_BACKOFF_S': '0.1'}, log_name='recovery')
    try:
        fake.post('/mainframe/_chaos/lying_success', json={'params': {'count': 5}})
        fake.post('/mainframe/_chaos/slow_apply', json={'params': {'count': 1}})
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        first = run_until_resolved(1)
        assert first['recovery_rounds'] >= 1
        assert len(first['steps'][0]['attempts']) == 6
        assert col('thoughts').find_one({'incident': first['_id'], 'text': {'$regex': 'Recovery 1/'}})
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        second = run_until_resolved(2)
        assert second['_id'] != first['_id']
        assert second['fault_class'] == 'disk_fill'
        assert all(s['status'] == 'verified' for s in second['steps'])
        assert 'purge_spool' in [s['op'] for s in second['steps']]
    finally:
        hive.stop()


def test_disk_fill_mid_repair_replans_instead_of_looping(db, fake):
    """R1 finding 3: a second fault lands on a system mid-repair. Recovery re-diagnoses it, puts the
    remediation in front of the unverified steps, and resolves (bounded: no endless re-send of the same op)."""
    from common.db import col
    reset_runtime()
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'RECOVERY_BACKOFF_S': '0.1'}, log_name='recovery_replan')
    fired = []

    def strike():
        # the restart_batch approval is up: the mainframe half of the plan is done, unix is next
        if not fired and col('approvals').find_one({'desc': {'$regex': '^restart_batch'}}):
            fake.post('/unix/_chaos/disk_fill', json={'params': {}})
            fired.append(True)
    try:
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        inc = run_until_resolved(1, timeout=180, on_tick=strike)
        assert fired
        assert inc['fault_class'] == 'batch_abort'
        replans = [r['replan'] for r in inc.get('recoveries') or [] if r.get('replan')]
        assert replans and replans[0]['fault_class'] == 'disk_fill' and replans[0]['system'] == 'unix'
        live = [s for s in inc['steps'] if not s.get('superseded_by_replan')]
        assert [s['op'] for s in live if s['n'] > 4][:3] == ['purge_spool', 'rerun_transfer', 'refresh_cache']
        assert all(s['status'] == 'verified' for s in live)
        assert inc['recovery_rounds'] <= 2
        assert col('thoughts').find_one({'incident': inc['_id'], 'text': {'$regex': 'Re-planning: purge_spool'}})
    finally:
        hive.stop()


def test_second_drift_replaces_stale_map_field():
    """A second schema drift mid-repair: map_field to the newer field replaces the pending one (no stale retry)."""
    from agents import recovery
    steps = [{"step_id": "I-s1", "n": 1, "system": "portal", "op": "map_field", "params": {"field": "balance",
              "source": "acct_bal"}, "status": "failed", "destructive": False},
             {"step_id": "I-s2", "n": 2, "system": "portal", "op": "refresh_cache", "params": {}, "status": "planned",
              "destructive": False}]
    out, added = recovery.replan_steps(steps, "I", {"fault_class": "schema_drift", "params": {
        "field": "balance", "to": "ledger_bal"}}, None)
    live = [s for s in out if not s.get("superseded_by_replan")]
    assert [(s["op"], (s.get("params") or {}).get("source")) for s in live] == [
        ("map_field", "ledger_bal"), ("refresh_cache", None)]
