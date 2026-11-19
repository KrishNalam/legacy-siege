"""Read-back evidence for Red reflections. Observations are temporal associations, not proof of causality."""
from common.db import now, swarm, world


def outcome(event: dict) -> dict:
    rows = []
    target = event.get('target')
    ids = list(event.get('red', {}).get('incident_ids', []))
    if (event.get('result') or {}).get('incident'):
        ids.append(event['result']['incident'])
    planted = (event.get('result') or {}).get('playbook') if event.get('fault') == 'poison_memory' else None
    if planted:  # poisoned memory lands at the NEXT recall, usually a later night: follow the plant, not the run
        query = {'$or': [{'recall.rejected': planted}, {'recall.memory_rejected': planted},
                         {'recall.hits.playbook_id': planted, 'opened_at': {'$gte': event['ts']}}]}
    else:
        query = {'$or': [{'_id': {'$in': ids}}, {'opened_at': {'$gte': event['ts']}}]}
    if event.get('run_id') and not planted:
        query['run_id'] = event['run_id']
    for inc in swarm().incidents.find(query).sort('opened_at', -1).limit(12):
        if target != 'swarm' and target not in [inc.get('system'), inc.get('root_cause_system'), *(inc.get('affected') or [])]:
            continue
        steps = inc.get('steps') or []
        resolved = inc.get('closed_at') if inc.get('status') == 'resolved' else None
        rows.append({'incident': inc['_id'], 'status': inc.get('status'),
                     'guard_evidence': [{'op': s.get('op'), 'status': s.get('status'), 'evidence': s.get('evidence')}
                                        for s in steps if s.get('evidence') or s.get('status') == 'failed'][:5],
                     'failed_attempts': list(swarm().ledger.find({'incident': inc['_id'], 'status': 'failed', 'updated_at': {'$gte': event['ts']}},
                          {'_id': 0, 'action_id': 1, 'evidence': 1}).limit(5)),
                     'handoffs': inc.get('handoffs', []),
                     'memory_rejected': (inc.get('recall') or {}).get('rejected'),
                     'recovery_s': max(0, (resolved - event['ts']).total_seconds()) if resolved else None})
    failed = [f for row in rows for f in row.get('failed_attempts', [])]
    caught = True if (any('ACK ≠ APPLIED' in str(f.get('evidence', '')) for f in failed)
                      or any(r.get('memory_rejected') for r in rows)) else None   # read-back lie, or a recalled repair rejected
    recovered = [r['recovery_s'] for r in rows if r.get('recovery_s') is not None]
    return {'observed_at': now(), 'attack_status': event.get('status'), 'incidents': rows,
            'guard_caught': caught, 'recovery_s': max(recovered) if recovered and all(r.get('status') == 'resolved' for r in rows) else None,
            'caveat': 'Temporal association only; no incident means no observed defence, not successful recovery.'}


def remember(agent_id: str, event: dict, evidence: dict, reflection: str, lesson: str, planner: str):
    row = {'event': event['_id'], 'fault': event['fault'], 'target': event.get('target'),
           'outcome': evidence, 'reflection': reflection, 'lesson': lesson, 'planner': planner, 'ts': now()}
    world().chaos_events.update_one({'_id': event['_id'], 'ts': event['ts']}, {'$set': {'red.reflection': row, 'outcome': evidence, 'lesson': lesson}})
    world().red_agents.update_one({'_id': agent_id}, {'$set': {'last_reflection': row},
        '$push': {'lessons': {'$each': [row], '$slice': -6}}}, upsert=True)
    return row
