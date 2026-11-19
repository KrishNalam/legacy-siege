"""The Hive (Blue): a self-healing ops team of agent PROCESSES whose every bit of state lives in MongoDB.

    supervisor.py   spawns `python -m agents.agent <role> <id>` per bee; refills the bench after deaths
    agent.py        process entrypoint            harness.py  heartbeat, leases, narration, checkpointer
    leases.py       atomic claim / renew / fence (findOneAndUpdate + generation fencing tokens)
    steps.py        step transitions = one transaction (fence task + incident step + ledger receipt)
    recall.py       $vectorSearch playbook memory    verify.py   postconditions by read-back
    diagnosis.py    deterministic rules per system   policy.py   chain, fix plans, symptom text
    roles/          monitor (+Undertaker), coordinator, investigator, operator, verifier, recovery, reporter — each a LangGraph
                    StateGraph compiled with MongoDBSaver(db=SWARM_DB)
"""
