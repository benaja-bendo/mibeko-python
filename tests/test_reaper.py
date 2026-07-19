"""Test léger du reaper de démarrage étendu (FIX-3).

Le reaper doit basculer en « failed », après un redémarrage, non seulement les
`extraction_runs` (running/queued) mais AUSSI :
- `official_journals.transcription_status` (running/queued) — sinon un JO
  redéployé en plein MinerU reste bloqué, car la relance d'upload exclut
  queued/running ;
- `legal_documents.extraction_status` == 'processing'.

On neutralise MinIO/MinerU pour importer `src.api.main`, et on remplace
`SessionLocal` par une fausse session qui enregistre les updates émis, sans DB.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import stub_service_modules  # noqa: E402

from src.db.models import ExtractionRun, LegalDocument, OfficialJournal  # noqa: E402

with stub_service_modules():
    import src.api.main as main  # noqa: E402


class FakeRun:
    def __init__(self):
        self.status = "running"
        self.finished_at = None
        self.meta = {"foo": "bar"}


class FakeQuery:
    """Query minimale : `.filter(...).all()` pour les runs, `.update(...)` pour
    les JO et documents (chemin bulk-update du reaper)."""

    def __init__(self, model, runs, update_log):
        self._model = model
        self._runs = runs
        self._update_log = update_log

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._runs

    def update(self, values, synchronize_session=False):
        # Enregistre combien de lignes le reaper prétend mettre à jour pour ce modèle.
        count = {OfficialJournal: 2, LegalDocument: 3}.get(self._model, 0)
        self._update_log[self._model] = (values, count)
        return count


class FakeSession:
    def __init__(self, runs):
        self._runs = runs
        self.update_log = {}
        self.committed = False
        self.closed = False

    def query(self, model):
        runs = self._runs if model is ExtractionRun else []
        return FakeQuery(model, runs, self.update_log)

    def commit(self):
        self.committed = True

    def rollback(self):  # pragma: no cover - pas attendu dans ce test
        pass

    def close(self):
        self.closed = True


def test_reaper_marque_les_trois_familles_en_echec(monkeypatch):
    runs = [FakeRun(), FakeRun()]
    session = FakeSession(runs)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    main.reap_orphaned_runs()

    # 1) Les runs orphelins sont passés en failed, avec finished_at et meta.error,
    #    sans écraser le reste de meta.
    for run in runs:
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.meta["error"] == "orphaned: service restart"
        assert run.meta["foo"] == "bar"

    # 2) Les JO queued/running sont bulk-updatés vers 'failed'.
    jo_values, jo_count = session.update_log[OfficialJournal]
    assert jo_values[OfficialJournal.transcription_status] == "failed"
    assert jo_count == 2

    # 3) Les documents 'processing' sont bulk-updatés vers 'failed'.
    doc_values, doc_count = session.update_log[LegalDocument]
    assert doc_values[LegalDocument.extraction_status] == "failed"
    assert doc_count == 3

    assert session.committed is True
    assert session.closed is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
