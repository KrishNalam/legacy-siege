"""Platform Sentinel delivery: waggle-bus notices + W19/W23 Gardener requests.

The common envelope is {kind, from, system, problem, fault_class, payload, ts, status,
answer}. payload carries a registry reference plus the vetted parameterised spec. Receivers
must still read the current registry: a delivered copy never authorizes a retired tool.
"""
from __future__ import annotations

import threading

from common.db import col, now
from . import platform
from .harness import log


def envelope(tool, sender='platform-1'):
    return {'kind': 'tool', 'from': sender, 'system': tool['preconditions']['system'],
            'problem': tool['fault_class'], 'fault_class': tool['fault_class'],
            'payload': {'tool_id': tool['_id'], 'name': tool['name'], 'version': tool['version'],
                        'status': tool['status'], 'ops': tool['ops'], 'preconditions': tool['preconditions'],
                        'source': tool['source'], 'built_from': tool['built_from']},
            'ts': now(), 'status': 'open', 'answer': None}


def publish(tool, sender='platform-1'):
    """Idempotent fan-out. Called on every sweep to recover a publisher crash after registry insert."""
    if not platform.enabled():
        return
    body = envelope(tool, sender)
    key = f"GT-{tool['_id']}:{tool['status']}"
    col('gardener_requests').update_one({'_id': key}, {'$setOnInsert': body}, upsert=True)
    col('messages').update_one({'_id': key}, {'$setOnInsert': {
        'from': sender, 'to': 'scouts', 'kind': 'tool', 'body': body, 'ts': body['ts'],
        'source': sender, 'system': body['system'], 'incident': None, 'severity': 'info', 'priority': 20}}, upsert=True)


def refresh(agent, via='snapshot'):
    if not platform.enabled():
        return []
    versions, tools = {}, []
    for tool in col('tools').find({'status': 'active'}).limit(100):
        fault = tool['fault_class']
        if not platform.valid(tool.get('ops'), fault):
            continue
        system = platform.SUPPORTED[fault]
        if system not in versions:
            versions[system] = agent.systems.version(system)
        if tool['preconditions']['system_version'] == versions[system]:
            tools.append(tool['_id'])
    # This is the Scout's actual received catalogue, also visible to Comms roster readers.
    col('agents').update_one({'_id': agent.id}, {'$set': {
        'platform_tools': tools, 'platform_tools_received_at': now(), 'platform_tools_via': via}})
    return tools


def start(agent):
    """Watch bus deliveries even while a Scout is waiting on a Worker or approval.

    Open stream before taking a snapshot to close the startup race. Snapshot on reconnect
    covers dropped/expired messages. One receiver thread per Scout, not another process.
    """
    if not platform.enabled() or getattr(agent, '_platform_delivery', False):
        return
    agent._platform_delivery = True

    def receive():
        while not agent.stop.is_set():
            try:
                with col('messages').watch([{'$match': {'operationType': 'insert',
                        'fullDocument.to': 'scouts', 'fullDocument.kind': 'tool'}}],
                        max_await_time_ms=500) as stream:
                    refresh(agent)
                    while not agent.stop.is_set():
                        event = stream.try_next()
                        if event:
                            refresh(agent, 'waggle_bus')
            except Exception as exc:
                log(agent.id, f'platform delivery reconnect: {type(exc).__name__}')
                agent.stop.wait(1)
    threading.Thread(target=receive, name='platform-delivery', daemon=True).start()
