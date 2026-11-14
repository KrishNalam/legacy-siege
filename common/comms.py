"""Opt-in deterministic message metadata; no model calls."""
import os
import re


def enabled() -> bool:
    return os.environ.get('SWARM_COMMS', '').strip().lower() in ('1', 'true', 'yes', 'on')


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
    if not isinstance(body, dict):
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
