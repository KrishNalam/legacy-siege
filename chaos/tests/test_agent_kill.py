"""agent_kill against real dummy subprocesses registered in the (isolated) swarm.agents collection."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest

from chaos import runner
from common.db import col, now, world


@pytest.fixture
def spawn():
    procs: list[subprocess.Popen] = []

    def _spawn(agent_id: str, **fields) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)  # swarm.agent dummy"])
        procs.append(p)
        doc = {"_id": agent_id, "role": "worker", "specialty": "mainframe", "status": "working",
               "phase": "diagnosing", "pid": p.pid, "last_seen": now(), "started_at": now(),
               "incident": "INC-0001", "task": "INC-0001-t1", "generation": 1, **fields}
        col("agents").insert_one(doc)
        return p

    yield _spawn
    for p in procs:  # never leave dummies behind
        if p.poll() is None:
            p.kill()
        p.wait(5)


def _killed(p: subprocess.Popen) -> bool:
    try:
        return p.wait(5) == -9
    except subprocess.TimeoutExpired:
        return False


def test_kill_waits_for_step_sent_phase(spawn):
    bystander = spawn("worker-unix-1", specialty="unix")
    victim = spawn("worker-mainframe-1")

    def becomes_step_sent():  # the worker sends its step a moment later
        time.sleep(0.6)
        col("agents").update_one({"_id": "worker-mainframe-1"}, {"$set": {"phase": "step_sent", "last_seen": now()}})

    threading.Thread(target=becomes_step_sent, daemon=True).start()
    ev = runner.fire("agent_kill", "swarm", {"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 10})
    assert ev["status"] == "ok"
    r = ev["result"]
    assert r["agent"] == "worker-mainframe-1" and r["pid"] == victim.pid and r["matched"] == "selector"
    # phase is only step_sent after the 0.6 s delay, so matching it proves the kill waited; waited_s itself is
    # load-sensitive (the wait starts late on a busy box), so only require that some waiting happened
    assert r["phase"] == "step_sent" and r["task"] == "INC-0001-t1" and r["waited_s"] > 0
    assert _killed(victim) and bystander.poll() is None
    assert world().chaos_events.find_one({"_id": ev["_id"]})["result"]["pid"] == victim.pid


def test_kill_by_agent_id(spawn):
    p = spawn("scout-1", role="scout", status="idle", phase=None)
    ev = runner.fire("agent_kill", params={"selector": {"agent": "scout-1"}, "wait_s": 1})
    assert ev["status"] == "ok" and ev["result"]["agent"] == "scout-1" and _killed(p)


def test_gives_up_instead_of_killing_a_random_busy_worker(spawn):
    """No worker reaches step_sent within the wait: the kill is skipped. A busy worker in another
    phase is NOT killed as a fallback (a late press must not spoil the round-2 timing)."""
    busy = spawn("worker-web-1", specialty="web", phase="diagnosing")
    ev = runner.fire("agent_kill", params={"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 0.3})
    r = ev["result"]
    assert ev["status"] == "failed" and r["ok"] is False and r["how"] == "gave_up" and r["skipped"] is True
    assert r["selector"] == {"role": "worker", "phase": "step_sent"} and r["waited_s"] >= 0.3
    assert r["error"].startswith("kill skipped: no worker mid-step")
    assert "agent" not in r and "pid" not in r
    time.sleep(0.2)
    assert busy.poll() is None  # still alive
    assert world().chaos_events.find_one({"_id": ev["_id"]})["result"]["how"] == "gave_up"


def test_agent_id_selector_unchanged_when_missing(spawn):
    ev = runner.fire("agent_kill", params={"selector": {"agent": "queen-9"}, "wait_s": 0.2})
    assert ev["status"] == "failed" and ev["result"]["how"] == "none"
    assert ev["result"]["error"] == "no live agent matched"


def test_no_kill_for_stale_or_dead_agents(spawn):
    stale = spawn("worker-mainframe-1", phase="step_sent", last_seen=now() - timedelta(minutes=5))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    col("agents").insert_one({"_id": "worker-unix-1", "role": "worker", "status": "working",
                              "phase": "step_sent", "pid": gone.pid, "last_seen": now()})
    ev = runner.fire("agent_kill", params={"wait_s": 0.3})
    assert ev["status"] == "failed" and ev["result"]["how"] == "gave_up"
    assert ev["result"]["error"].startswith("kill skipped")
    assert stale.poll() is None  # a stale heartbeat means the swarm already considers it gone


def test_refuses_recycled_pid(spawn):
    # agent doc claims a last heartbeat long before this process was started -> pid was reused
    p = spawn("worker-mainframe-1", phase="step_sent")
    col("agents").update_one({"_id": "worker-mainframe-1"},
                             {"$set": {"last_seen": now() - timedelta(seconds=10)}})  # still "fresh"
    ev = runner.fire("agent_kill", params={"wait_s": 0.2})
    assert ev["status"] == "failed" and p.poll() is None


def test_stray_kill_never_crosses_into_the_next_night(spawn):
    """A kill requested in night 1 (here from another process's point of view: no in-process reset
    epoch bump) must not strike a worker in night 2."""
    w = world()
    w.runs.insert_one({"_id": "RUN-0001", "started_at": now() - timedelta(minutes=2), "ended_at": None})

    def next_night():  # Reset ▸ Next round ends night 1, then night 2 starts and a worker gets busy
        time.sleep(0.4)
        w.runs.update_one({"_id": "RUN-0001"}, {"$set": {"ended_at": now()}})
        w.runs.insert_one({"_id": "RUN-0002", "started_at": now(), "ended_at": None})
        spawn("worker-mainframe-1", phase="diagnosing", incident="INC-0002", task="INC-0002-d1")

    threading.Thread(target=next_night, daemon=True).start()
    ev = runner.fire("agent_kill", params={"selector": {"role": "worker"}, "wait_s": 3}, run_id="RUN-0001")
    assert ev["status"] == "failed" and ev["result"]["error"].startswith("called off")
    victim = col("agents").find_one({"_id": "worker-mainframe-1"})
    assert victim is not None
    time.sleep(0.2)
    import os  # noqa: PLC0415
    os.kill(victim["pid"], 0)  # still alive: raises ProcessLookupError if it was killed


def test_kill_within_the_same_night_still_lands(spawn):
    world().runs.insert_one({"_id": "RUN-0002", "started_at": now(), "ended_at": None})
    p = spawn("worker-web-1", specialty="web", phase="step_sent")
    ev = runner.fire("agent_kill", params={"selector": {"role": "worker", "phase": "step_sent"}, "wait_s": 0.3},
                     run_id="RUN-0002")
    assert ev["status"] == "ok" and ev["result"]["matched"] == "selector" and _killed(p)


def test_freeze_is_opt_in_and_thaws_and_reset_wakes(spawn, monkeypatch):
    import os
    from chaos import faults
    monkeypatch.delenv('CHAOS_AGENT_FREEZE', raising=False)
    with pytest.raises(faults.FaultError):
        runner.fire('agent_freeze')
    monkeypatch.setenv('CHAOS_AGENT_FREEZE', '1')
    p = spawn('worker-mainframe-1', phase='step_sent')
    result = {}
    t = threading.Thread(target=lambda: result.update(runner.fire('agent_freeze', params={'freeze_s': 1, 'wait_s': 0})))
    t.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if 'T' in open(f'/proc/{p.pid}/status').read().split('State:')[1].splitlines()[0]:
            break
        time.sleep(.02)
    else:
        pytest.fail('worker was never stopped')
    runner.cancel()
    os.kill(p.pid, 0)
    t.join(5)
    assert not t.is_alive() and result['status'] == 'ok'
    assert 'T' not in open(f'/proc/{p.pid}/status').read().split('State:')[1].splitlines()[0]
    assert col('frozen_agents').count_documents({}) == 0


def test_freeze_guardian_thaws_even_if_attacker_dies(spawn, monkeypatch):
    import os
    from chaos import faults
    monkeypatch.setenv('CHAOS_AGENT_FREEZE', '1')
    p = spawn('worker-mainframe-1', phase='step_sent')
    attacker = subprocess.Popen([sys.executable, '-c',
        "from chaos import runner; runner.fire('agent_freeze', params={'freeze_s': 2, 'wait_s': 0})"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = open(f'/proc/{p.pid}/status').read().split('State:')[1].splitlines()[0]
            if 'T' in state:
                break
            time.sleep(.02)
        else:
            pytest.fail('attacker never froze worker')
        attacker.kill()
        attacker.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if 'T' not in open(f'/proc/{p.pid}/status').read().split('State:')[1].splitlines()[0]:
                break
            time.sleep(.05)
        else:
            pytest.fail('guardian failed to thaw after attacker death')
        os.kill(p.pid, 0)
    finally:
        if attacker.poll() is None:
            attacker.kill()
        attacker.wait(5)
        faults._thaw(p.pid, faults._proc_start_ticks(p.pid))


def test_thaw_identity_survives_wall_clock_changes(spawn, monkeypatch):
    import os
    import signal
    from chaos import faults
    p = spawn('worker-mainframe-1', phase='step_sent')
    ticks = faults._proc_start_ticks(p.pid)
    assert ticks is not None
    monkeypatch.setattr(faults, '_proc_start_epoch', lambda pid: -999999)
    os.kill(p.pid, signal.SIGSTOP)
    try:
        assert not faults._thaw(p.pid, ticks + 1)  # recycled PID must not match
        assert faults._thaw(p.pid, ticks)
    finally:
        os.kill(p.pid, signal.SIGCONT)


def test_reset_continues_cleanup_after_memory_failure(monkeypatch):
    from chaos import faults
    calls = []
    monkeypatch.setattr(faults, 'thaw_overdue', lambda **kw: calls.append('thaw'))
    monkeypatch.setattr(faults, 'cancel_pending_hunts', lambda: calls.append('hunts'))
    def fail():
        calls.append('memory')
        raise RuntimeError('Mongo unavailable')
    monkeypatch.setattr(faults, 'clear_planted_memory', fail)
    assert runner.cancel() == []
    assert calls == ['thaw', 'hunts', 'memory']


def test_next_round_reset_keeps_planted_memory(monkeypatch):
    """Poisoned memory is memory: a next-round reset keeps it (Night 2 recalls it); a full reset removes it."""
    from chaos import faults
    calls = []
    monkeypatch.setattr(faults, 'thaw_overdue', lambda **kw: calls.append('thaw'))
    monkeypatch.setattr(faults, 'cancel_pending_hunts', lambda: calls.append('hunts'))
    monkeypatch.setattr(faults, 'clear_planted_memory', lambda: calls.append('memory'))
    runner.cancel(keep_memory=True)
    assert calls == ['thaw', 'hunts']
    runner.cancel()
    assert calls[2:] == ['thaw', 'hunts', 'memory']
