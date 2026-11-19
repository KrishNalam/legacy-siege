"""The opt-in lease deadline revokes external authority even if renewal stalls."""
from unittest.mock import Mock

import httpx
import pytest

from agents import leases
from agents.harness import Agent


def test_deadline_switch_and_freeze_resume(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('agents.harness.time.monotonic', lambda: clock[0])
    monkeypatch.setattr(leases, 'renew', Mock(return_value=True))
    a = Agent('operator', 'operator-test')
    try:
        a.hold('tasks', 'T', 1)
        clock[0] += 100
        monkeypatch.delenv('AGENTS_LEASE_DEADLINE', raising=False)
        a.check('tasks', 'T')
        monkeypatch.setenv('AGENTS_LEASE_DEADLINE', '1')
        with pytest.raises(leases.LeaseLost):
            a.check('tasks', 'T')
        # A late heartbeat must not resurrect the expired authority.
        clock[0] = 100
        with pytest.raises(leases.LeaseLost):
            a.check_held()
    finally:
        a.systems.close()


def test_exception_in_renewal_cannot_extend_deadline(monkeypatch):
    monkeypatch.setenv('AGENTS_LEASE_DEADLINE', '1')
    clock = [100.0]
    monkeypatch.setattr('agents.harness.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('agents.harness.col', lambda _: Mock(find_one_and_update=Mock(return_value={})))
    monkeypatch.setattr(leases, 'renew', Mock(return_value=True))
    a = Agent('operator', 'operator-test')
    try:
        a.hold('tasks', 'T', 1)
        monkeypatch.setattr(leases, 'renew', Mock(side_effect=TimeoutError))
        with pytest.raises(TimeoutError):
            a.beat()
        clock[0] += 100
        with pytest.raises(leases.LeaseLost):
            a.check('tasks', 'T')
    finally:
        a.systems.close()


def test_http_retry_rechecks_permission(monkeypatch):
    monkeypatch.setenv('AGENTS_LEASE_DEADLINE', '1')
    monkeypatch.setattr(leases, 'renew', Mock(return_value=True))
    a = Agent('operator', 'operator-test')
    a.hold('tasks', 'T', 1)
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        a._deadlines['tasks/T'] = 0
        raise httpx.ConnectError('disconnected')
    monkeypatch.setattr(a.systems._c, 'post', post)
    try:
        with pytest.raises(leases.LeaseLost):
            a.systems.post_op('unix', 'purge_spool', 'A', {})
        assert len(calls) == 1
    finally:
        a.systems.close()


def test_expired_lease_cannot_renew_or_fence(db, monkeypatch):
    monkeypatch.setenv('AGENTS_LEASE_DEADLINE', '1')
    db.tasks.insert_one({'_id': 'expired', 'owner': 'old', 'generation': 1, 'lease_until': leases.EPOCH})
    try:
        assert not leases.renew(db.tasks, 'expired', 'old', 1)
        from common.db import txn
        with pytest.raises(leases.Fenced):
            txn(lambda session: leases.fence(db.tasks, 'expired', 'old', 1, session))
    finally:
        db.tasks.delete_one({'_id': 'expired'})


def test_reaped_lease_blocks_retry_after_heartbeat_clears_held(monkeypatch):
    monkeypatch.setenv('AGENTS_LEASE_DEADLINE', '1')
    monkeypatch.setattr(leases, 'renew', Mock(return_value=True))
    a = Agent('operator', 'operator-test')
    try:
        a.hold('tasks', 'T', 1)
        # The resumed heartbeat observes the reaper's dead stamp.
        a._lost.update(a._held)
        a._held.clear()
        with pytest.raises(leases.LeaseLost):
            a.check_held()
        a.drop('tasks', 'T')
        a.check_held()
    finally:
        a.systems.close()
