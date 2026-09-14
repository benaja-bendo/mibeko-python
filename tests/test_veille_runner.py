"""Orchestration d'un passage de veille (mibeko-python#21, modifié par
mibeko-python#23 § 3.4 : la veille acquiert et dépose un travail dans la file
`ingestion_jobs`, le worker traite tout le reste — plus de parsing ni de
structuration directs ici).

Chaque étage est mocké : ce test vérifie l'enchaînement, la propagation
d'échec et les pings healthcheck — pas la logique de dépôt elle-même (voir
tests/test_veille_deposer_jobs.py, contre une vraie base Postgres, pour la
déduplication).
"""

import src.db.database as database_module
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
    monkeypatch.setattr(
        runner, "_deposer_jobs_veille",
        lambda *a, **k: {"deposes": ["sgg-jo/x"], "deja_en_file": []},
    )
    session = FakeSession()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: session)

    report = runner.run_once()

    assert report["echec"] is None
    assert report["acquisition"] == {"telecharges": 2}
    assert report["depots"] == {"deposes": ["sgg-jo/x"], "deja_en_file": []}
    assert appels_ping == ["/start", ""]
    assert session.closed is True


def test_run_once_echec_acquisition_arrete_tout(monkeypatch):
    appels_ping = _patch_pings(monkeypatch)

    def acquisition_en_panne(*a, **k):
        raise RuntimeError("sgg.cg injoignable")

    monkeypatch.setattr(runner, "run_acquire", acquisition_en_panne)
    depot_appele = []
    monkeypatch.setattr(runner, "_deposer_jobs_veille", lambda *a, **k: depot_appele.append(1))

    report = runner.run_once()

    assert "acquisition" in report["echec"]
    assert report["depots"] is None
    assert depot_appele == []
    assert appels_ping == ["/start", "/fail"]


def test_run_once_echec_depot_ferme_quand_meme_la_session(monkeypatch):
    appels_ping = _patch_pings(monkeypatch)
    monkeypatch.setattr(runner, "run_acquire", lambda *a, **k: {"telecharges": 0})

    def depot_en_panne(*a, **k):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(runner, "_deposer_jobs_veille", depot_en_panne)
    session = FakeSession()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: session)

    report = runner.run_once()

    assert "depots" in report["echec"]
    assert appels_ping == ["/start", "/fail"]
    assert session.closed is True


def test_run_once_dry_run_propage_dry_run_a_l_acquisition_et_au_depot(monkeypatch):
    _patch_pings(monkeypatch)
    acquire_kwargs = {}
    monkeypatch.setattr(
        runner, "run_acquire", lambda *a, **k: acquire_kwargs.update(k) or {"prevus": []}
    )
    depot_kwargs = {}

    def fake_depot(db, manifest, dry_run=False):
        depot_kwargs["dry_run"] = dry_run
        return {"deposes": [], "deja_en_file": []}

    monkeypatch.setattr(runner, "_deposer_jobs_veille", fake_depot)
    monkeypatch.setattr(database_module, "SessionLocal", lambda: FakeSession())

    report = runner.run_once(dry_run=True)

    assert acquire_kwargs.get("dry_run") is True
    assert depot_kwargs.get("dry_run") is True
    assert report["depots"] == {"deposes": [], "deja_en_file": []}
