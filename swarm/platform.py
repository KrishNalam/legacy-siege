"""Platform tools are policy-validated data, never code. PLATFORM_ENGINEERS defaults off.

A tool saves repeated plan assembly decisions, NOT operations or Guard checks. Every use
is rebound to today's diagnosis and must equal today's deterministic safe plan.
"""
from __future__ import annotations

import copy
import json
import os

from pymongo.errors import DuplicateKeyError

from common.db import col, now, txn
from common.llm import guarded_json
from . import policy

SUPPORTED = {'batch_abort': 'mainframe', 'disk_fill': 'unix', 'cron_stop': 'unix'}
SAMPLE = {'poison_records': ['$record_id']}


def enabled():
    return os.environ.get('PLATFORM_ENGINEERS', '').lower() in ('1', 'true', 'yes', 'on')


def template(fault):
    return policy.fix_plan(fault, SAMPLE)


def valid(ops, fault):
    # Exact whitelist includes parameters, ordering, required recovery ops and approvals.
    try:
        return fault in SUPPORTED and json.dumps(ops, sort_keys=True) == json.dumps(template(fault), sort_keys=True)
    except (TypeError, ValueError):
        return False


def build(agent):
    if not enabled():
        return
    # Recover accounting if the Guard died between its durable judgement and telemetry.
    for run in col('tool_runs').find({'status': 'running'}).limit(50):
        incident = col('incidents').find_one({'_id': run['incident']}) or {}
        tagged = [s for s in incident.get('steps', []) if s.get('tool_id') == run['tool_id']]
        failed = next((s for s in tagged if s['status'] == 'failed' or len(s.get('attempts') or []) > 1), None)
        if failed:
            judged(agent, run['incident'], failed, False)
        elif tagged and all(s['status'] == 'verified' for s in tagged):
            judged(agent, run['incident'], tagged[-1], True)
    # Repeated = at least 2 independent observations of the same fault on the same system version,
    # summed across Guard-caught failures (lessons) and slow verified steps (platform_signals): night 1's
    # lie + slow restart already qualifies, so night 2 can use the tool before the Gardener upgrade.
    query = {'stale': False, 'count': {'$gte': 1}, 'fault_class': {'$in': list(SUPPORTED)}}
    groups: dict = {}
    for ev in list(col('lessons').find(query).limit(50)) + list(col('platform_signals').find(query).limit(50)):
        groups.setdefault((ev['fault_class'], ev['system'], ev['system_version']), []).append(ev)
    candidates = []
    for evs in groups.values():
        total = sum(e['count'] for e in evs)
        if total >= 2:
            candidates.append({**evs[0], 'count': total, 'evidence_docs': evs})
    for ls in candidates:
        fault = ls['fault_class']
        if ls['system'] != SUPPORTED[fault] or agent.systems.version(ls['system']) != ls['system_version']:
            continue
        key = f"TOOL:{fault}:v{ls['system_version']}:1"
        if col('tools').find_one({'_id': key}):
            continue  # retired tools cannot be silently rebuilt
        ops, source, why = template(fault), 'policy', 'Repeated verified failures; reuse a vetted recovery recipe.'
        proposal, meta = guarded_json('platform_tool',
            'Name and explain a recovery tool. Return ops unchanged from the supplied whitelist; no code.',
            json.dumps({'failure': ls.get('why'), 'count': ls['count'], 'whitelist': ops}),
            {'type': 'object', 'properties': {'name': {'type': 'string', 'maxLength': 80},
             'why': {'type': 'string', 'maxLength': 240}, 'ops': {'type': 'array', 'maxItems': 12,
             'items': {'type': 'object', 'properties': {'system': {'type': 'string'}, 'op': {'type': 'string'},
              'params': {'type': 'object', 'properties': {'record_id': {'type': 'string'}, 'job': {'type': 'string'},
                 'path': {'type': 'string'}}, 'additionalProperties': False},
              'desc': {'type': 'string'}, 'destructive': {'type': 'boolean'}},
              'required': ['system', 'op', 'params', 'desc', 'destructive'], 'additionalProperties': False}}},
             'required': ['name', 'why', 'ops'], 'additionalProperties': False}, timeout=4)
        name = f'{fault}-recovery'
        if proposal and valid(proposal['ops'], fault):
            name, why, source = proposal['name'], proposal['why'], 'claude'
        doc = {'_id': key, 'name': name, 'explanation': why, 'source': source, 'ai': meta,
               'built_from': [{'lesson': e['_id'], 'count': e['count'], 'evidence': e.get('evidence')}
                              for e in ls['evidence_docs']],
               'ops': ops, 'fault_class': fault, 'preconditions': {'system': ls['system'],
               'system_version': ls['system_version'], 'fault_class': fault, 'requires_current_diagnosis': True},
               'version': 1, 'status': 'active', 'uses': 0, 'successes': 0, 'failures': 0,
               'success_rate': 0.0, 'created_at': now()}
        try:
            col('tools').insert_one(doc)
        except DuplicateKeyError:
            continue
        slow = sum(e['count'] for e in ls['evidence_docs'] if str(e['_id']).startswith('SLOW:'))
        agent.think(f"Platform engineer built tool {name} from {ls['count'] - slow} failures + {slow} slow steps ({source}); "
                    f"{len(ops)} whitelisted ops, every step still Guard-verified.", kind='learn', pause=False,
                    system=ls['system'])

    from .tool_delivery import publish
    for tool in col('tools').find({}).limit(100):
        publish(tool, agent.id)


def lookup(fault, params, version_fn, received=None):
    if not enabled() or fault not in SUPPORTED:
        return None
    query = {'status': 'active', 'fault_class': fault}
    if received is not None:
        query['_id'] = {'$in': received}
    for tool in col('tools').find(query).limit(20):
        pre = tool.get('preconditions') or {}
        if pre != {'system': SUPPORTED[fault], 'system_version': version_fn(SUPPORTED[fault]),
                   'fault_class': fault, 'requires_current_diagnosis': True} or not valid(tool.get('ops'), fault):
            continue
        ops = []
        for op in tool['ops']:
            if op['op'] == 'quarantine_record':
                for record in params.get('poison_records') or []:
                    bound = copy.deepcopy(op)
                    bound['params']['record_id'] = record
                    bound['desc'] = policy.fix_plan('batch_abort', {'poison_records': [record]})[0]['desc']
                    ops.append(bound)
            else:
                ops.append(copy.deepcopy(op))
        if ops == policy.fix_plan(fault, params):
            return tool, ops
    return None


def used(agent, incident, step):
    if not enabled() or not step.get('tool_id'):
        return
    key = f"{step['tool_id']}:{incident}"
    def fn(session):
        if col('tool_runs').find_one({'_id': key}, session=session):
            return False
        tool = col('tools').find_one({'_id': step['tool_id'], 'status': 'active'}, session=session)
        if not tool:
            return False  # normal policy steps continue, but no longer as a tool
        col('tool_runs').insert_one({'_id': key, 'tool_id': tool['_id'], 'incident': incident,
                                    'status': 'running', 'ts': now()}, session=session)
        col('tools').update_one({'_id': tool['_id']}, {'$inc': {'uses': 1}}, session=session)
        return tool
    tool = txn(fn)
    if tool:
        agent.think(f"Worker used tool {tool['name']}; {len(tool['ops']) - 1} plan assembly steps saved; "
                    '0 operations skipped, all approvals and Guard checks retained.', kind='action',
                    incident=incident, system=step['system'], pause=False)


def judged(agent, incident, step, success):
    if not enabled():
        return
    # Repeated slow successful steps also qualify, without pretending they failed verification.
    if success and step.get('sent_at') and (now() - step['sent_at']).total_seconds() >= 10:
        incident_doc = col('incidents').find_one({'_id': incident}) or {}
        fault = incident_doc.get('fault_class')
        if fault in SUPPORTED and step['system'] == SUPPORTED[fault]:
            version = agent.systems.version(step['system'])
            key = f"SLOW:{step['system']}:{version}:{step['op']}"
            col('platform_signals').update_one({'_id': key}, {'$setOnInsert': {
                'system': step['system'], 'system_version': version, 'fault_class': fault,
                'stale': False, 'why': 'Repeated operations taking at least 10 seconds to verify',
                'evidence': 'Guard read-back verified after >=10s'},
                '$addToSet': {'actions': step['action_id']}}, upsert=True)
            col('platform_signals').update_one({'_id': key}, [{'$set': {'count': {'$size': '$actions'}}}])
    if not step.get('tool_id'):
        return
    tool_id = step['tool_id']
    doc = col('incidents').find_one({'_id': incident}) or {}
    tagged = [s for s in doc.get('steps', []) if s.get('tool_id') == tool_id]
    if success and (not tagged or any(s['status'] != 'verified' for s in tagged)):
        return
    def fn(session):
        run = col('tool_runs').update_one({'_id': f'{tool_id}:{incident}', 'status': 'running'},
                {'$set': {'status': 'success' if success else 'failed', 'finished_at': now()}}, session=session)
        if not run.modified_count:
            return False
        field = 'successes' if success else 'failures'
        updates = {'$inc': {field: 1}}
        if not success:
            updates['$set'] = {'status': 'retired', 'retired_at': now(), 'retired_reason': 'Guard verification failed'}
        col('tools').update_one({'_id': tool_id}, updates, session=session)
        tool = col('tools').find_one({'_id': tool_id}, session=session)
        col('tools').update_one({'_id': tool_id}, {'$set': {'success_rate': tool['successes'] / (tool['successes'] + tool['failures'])}}, session=session)
        return True
    if txn(fn):
        agent.think(f"Tool {tool_id}: " + ('all operations Guard-verified.' if success else
                    'retired after failed Guard verification; continuing with the normal policy.'),
                    kind='learn' if success else 'reject', incident=incident, system=step['system'], pause=False)
