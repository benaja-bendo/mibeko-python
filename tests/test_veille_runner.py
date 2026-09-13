"""Orchestration d'un passage de veille (mibeko-python#21).

Chaque étage est mocké : ce test vérifie l'enchaînement, la propagation
d'échec et les pings healthcheck — pas la logique d'acquisition/parsing/
structuration elle-même, déjà couverte par leurs propres suites.
"""

import src.db.database as database_module
import src.parsing.batch as parsing_batch
import src.structuration.batch as structuration_batch
import src.veille.runner as runner


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_pings(monkeypatch):
    appels = []
    monkeypatch.setattr(runner, "ping", lambda event="": appels.append(event))
    return appels


def test_run_once_chemin_heureux(monkeypatch):
    appels_ping = _patch_pings(monkeypatch)
    monkeypatch.setattr(runner, "run_acquire", lambda *a, **k: {"telecharges": 2})
    monkeypatch.setattr(parsing_batch, "run_batch", lambda *a, **k: {"traites": 2, "erreurs": []})
    monkeypatch.setattr(structuration_batch, "run_batch", lambda *a, **k: {"traites": 2, "erreurs": []})
    session = FakeSession()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: session)

    report = runner.run_once()

    assert report["echec"] is None
    assert report["acquisition"] == {"telecharges": 2}
    assert report["parsing"] == {"traites": 2, "erreurs": []}
    assert report["structuration"] == {"traites": 2, "erreurs": []}
    assert appels_ping == ["/start", ""]
    assert session.closed is True


def test_run_once_echec_acquisition_arrete_tout(monkeypatch):
    appels_ping = _patch_pings(monkeypatch)

    def acquisition_en_panne(*a, **k):
        raise RuntimeError("sgg.cg injoignable")

    monkeypatch.setattr(runner, "run_acquire", acquisition_en_panne)
    parsing_appele = []
    monkeypatch.setattr(parsing_batch, "run_batch", lambda *a, **k: parsing_appele.append(1))

    report = runner.run_once()

    assert "acquisition" in report["echec"]
    assert report["parsing"] is None
    assert parsing_appele == []
    assert appels_ping == ["/start", "/fail"]


def test_run_once_echec_structuration_ferme_quand_meme_la_session(monkeypatch):
    appels_ping = _patch_pings(monkeypatch)
    monkeypatch.setattr(runner, "run_acquire", lambda *a, **k: {"telecharges": 0})
    monkeypatch.setattr(parsing_batch, "run_batch", lambda *a, **k: {"traites": 0, "erreurs": []})

    def structuration_en_panne(*a, **k):
        raise RuntimeError("mistral indisponible")

    monkeypatch.setattr(structuration_batch, "run_batch", structuration_en_panne)
    session = FakeSession()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: session)

    report = runner.run_once()

    assert "structuration" in report["echec"]
    assert appels_ping == ["/start", "/fail"]
    assert session.closed is True


def test_run_once_dry_run_appelle_les_previsualisations(monkeypatch):
    _patch_pings(monkeypatch)
    acquire_kwargs = {}
    monkeypatch.setattr(
        runner, "run_acquire", lambda *a, **k: acquire_kwargs.update(k) or {"prevus": []}
    )
    monkeypatch.setattr(parsing_batch, "dry_run_report", lambda *a, **k: {"apercu": "parsing"})
    monkeypatch.setattr(structuration_batch, "dry_run_report", lambda *a, **k: {"apercu": "structuration"})
    monkeypatch.setattr(database_module, "SessionLocal", lambda: FakeSession())

    parsing_run_batch_appele = []
    monkeypatch.setattr(parsing_batch, "run_batch", lambda *a, **k: parsing_run_batch_appele.append(1))

    report = runner.run_once(dry_run=True)

    assert acquire_kwargs.get("dry_run") is True
    assert report["parsing"] == {"apercu": "parsing"}
    assert report["structuration"] == {"apercu": "structuration"}
    assert parsing_run_batch_appele == []  # run_batch réel non appelé en dry-run
