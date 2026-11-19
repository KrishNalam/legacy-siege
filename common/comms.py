"""Opt-in deterministic message metadata; no model calls."""
import os
import re


def enabled() -> bool:
    return os.environ.get('AGENTS_COMMS', '').strip().lower() in ('1', 'true', 'yes', 'on')


def metadata(source, kind, body, *, incident=None, system=None, severity=None):
    data = body if isinstance(body, dict) else {}
    text = str(data.get('text', body)).lower()
    incident = incident or data.get('incident')
    system = system or data.get('system')
    if not incident:
        match = re.search(r'\bINC-\d+\b', str(body))
        incident = match.group() if match else None
    if not system:
        system = next((s for s in ('portal', 'unix', 'mainframe') if s in text), None)
    severity = severity or data.get('severity')
    if severity is None:
        severity = ('critical' if any(w in text for w in ('escalated', 'rejected', 'died', 'dead', 'failed'))
                    else 'warning' if kind == 'handoff' or 'opened' in text else 'info')
    priority = {'critical': 100, 'error': 80, 'warning': 60, 'info': 20, 'debug': 0}.get(severity, 20)
    return dict(source=source, incident=incident, system=system, severity=severity, priority=priority)


def signature(message):
    """Ignore volatile counters/times, preserve incident, system, kind and semantic words."""
    import json
    body = message.get('body', '')
    text = json.dumps(body, sort_keys=True, default=str) if isinstance(body, dict) else str(body)
    text = text.lower()
    if message.get('kind') == 'log':  # estate log lines: job numbers, record ids, pids, threads all vary
        text = re.sub(r'\d+', '#', text)
    elif not isinstance(body, dict):
        text = re.sub(r'\b(queue|count|seen|retry|attempt|line|seq|depth)\s+\d+\b', r'\1 #', text)
        text = re.sub(r'\b\d+(?:\.\d+)?\s*(ms|seconds?|s)\b', r'# \1', text)
    return (message.get('to'), message.get('incident'), message.get('system'), message.get('kind'),
            message.get('severity'), ' '.join(text.split()))


def prioritise(messages, waiting=()):
    groups = {}
    for original in messages:
        m = dict(original)
        m.update(metadata(m.get('source', m.get('from')), m['kind'], m['body'],
                          incident=m.get('incident'), system=m.get('system'), severity=m.get('severity')))
        m['priority'] += 30 if m.get('incident') in waiting else 0
        m['priority'] += {'portal': 3, 'unix': 2, 'mainframe': 1}.get(m.get('system'), 0)
        key = signature(m)
        if key not in groups:
            groups[key] = {**m, 'count': 0, 'members': []}
        g = groups[key]
        g['count'] += original.get('count', 1)
        g['members'].append(original)
    return sorted(groups.values(), key=lambda m: (-m['priority'], -m['count']))


# ---- estate logs as comms input (W29): world.system_logs WARN/ERROR/ABEND lines, coalesced like any message
LOG_SEVERITY = {'ABEND': 'critical', 'FATAL': 'critical', 'SEVERE': 'error', 'ERROR': 'error',
                'WARN': 'warning', 'WARNING': 'warning'}
LOG_PIPELINE = [{'$match': {'operationType': 'insert', 'fullDocument.level': {'$in': list(LOG_SEVERITY)}}}]


def from_log(doc, incident=None):
    """One world.system_logs line -> one comms input with source=<system>; None for INFO/DEBUG lines."""
    level = str(doc.get('level') or '').upper()
    severity = LOG_SEVERITY.get(level)
    if severity is None:
        return None
    line = str(doc.get('line') or '')
    if 'ABEND' in line.upper():
        severity = 'critical'
    system = doc.get('system')
    return dict(to='hive', kind='log', body=f"{system} {doc.get('code') or level}: {line}"[:240],
                source=system, system=system, severity=severity, incident=incident)


# ---- consumer: the Coordinator claims the highest-priority unassigned incident first
CUSTOMER_FACING = frozenset({'portal'})


def customer_facing(inc) -> bool:
    return bool((set(inc.get('affected') or []) | {inc.get('system')}) & CUSTOMER_FACING)


def incident_rank(inc, feed_priority=0):
    """Sort key, best first: customer-facing (portal in the blast radius), then the highest priority the
    prioritised feed has seen for it, then oldest (incident ids are sequential)."""
    return (-int(customer_facing(inc)), -int(feed_priority or 0), str(inc.get('_id')))


def feed_priorities(messages, incidents):
    """Highest feed priority per incident: messages tagged with the incident, or untagged ones (estate logs
    before triage) about a system in its affected set."""
    best = {i['_id']: 0 for i in incidents}
    for m in messages:
        for i in incidents:
            if m.get('incident') == i['_id'] or (not m.get('incident') and m.get('system') in
                                                 (set(i.get('affected') or []) | {i.get('system')})):
                best[i['_id']] = max(best[i['_id']], int(m.get('priority') or 0))
    return best
