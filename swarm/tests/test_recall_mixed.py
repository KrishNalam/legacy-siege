"""Recall must survive mixed vector spaces (some playbooks embedded by Voyage, some by the local fallback)."""
import pytest

from common import embed
from common.db import swarm
from swarm import recall
from swarm.tests.conftest import reset_runtime

SYMPTOMS = "portal checkout 502 unix queue depth rising mainframe batch NIGHTBAT ABEND S0C7"


@pytest.fixture()
def clean(db):
    reset_runtime()
    yield
    reset_runtime()


def _playbook(model: str, text: str = SYMPTOMS) -> dict:
    # the vector itself is irrelevant here (a fake "voyage" space); symptom_text is what cross-space recall uses
    return {"_id": "PB-T1", "system": "mainframe", "system_version": 1, "fault_class": "batch_abort",
            "symptom_text": text, "embedding": embed._local("unrelated " + model), "embedding_model": model,
            "stale": False}


def test_voyage_playbook_recalled_when_query_falls_back_to_local(clean):
    swarm().playbooks.insert_one(_playbook("voyage-4"))
    flt, hits = recall.search(SYMPTOMS)  # test env has no key -> local-hash query
    assert flt["embedding_model"] == embed.LOCAL_MODEL
    assert hits and hits[0]["_id"] == "PB-T1" and hits[0]["score"] >= 0.99 and hits[0]["via"] == "text-match"
    usable, summary = recall.evaluate(hits, lambda s: 1)
    assert usable and summary[0]["via"] == "text-match"


def test_local_playbook_recalled_when_query_is_voyage(clean, monkeypatch):
    swarm().playbooks.insert_one(_playbook(embed.LOCAL_MODEL))
    monkeypatch.setattr(embed, "embed_with_model", lambda t, it="document": ([0.0] * 1023 + [1.0], "voyage-4"))
    flt, hits = recall.search(SYMPTOMS)
    assert flt["embedding_model"] == "voyage-4"
    assert hits and hits[0]["_id"] == "PB-T1" and hits[0]["score"] >= 0.99


def test_unrelated_text_is_not_a_cross_space_hit(clean):
    swarm().playbooks.insert_one(_playbook("voyage-4", "totally different disk full on the console box"))
    _, hits = recall.search(SYMPTOMS)
    usable, _ = recall.evaluate(hits, lambda s: 1)
    assert usable is None


def test_stale_playbooks_are_skipped(clean):
    pb = _playbook("voyage-4")
    pb["stale"] = True
    swarm().playbooks.insert_one(pb)
    assert recall.search(SYMPTOMS)[1] == []
