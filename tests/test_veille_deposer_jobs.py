"""Dépôt des travaux de veille dans la file `ingestion_jobs` (mibeko-python#23,
§ 3.4) — contre une vraie base Postgres : la déduplication (jamais un second
job pour une entrée déjà en file) dépend d'une vraie requête, pas d'une
session fake (même raison que tests/test_worker_runner.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.acquisition.manifest import Manifest, ManifestEntry
from src.db.database import SessionLocal
from src.db.models import IngestionJob, IngestionProvenance
from src.veille.runner import _deposer_jobs_veille


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _entry(entry_id: str, **overrides) -> ManifestEntry:
    defaults = dict(
        id=entry_id,
        fichier="sources/sgg/test.pdf",
        sha256="0" * 64,
        size_bytes=10,
        type_source="journal_officiel",
        statut="telecharge",
    )
    defaults.update(overrides)
    return ManifestEntry(**defaults)


def _cleanup_par_manifest_id(*manifest_ids: str) -> None:
    cleanup_db = SessionLocal()
    try:
        cleanup_db.query(IngestionJob).filter(IngestionJob.manifest_id.in_(manifest_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.query(IngestionProvenance).filter(IngestionProvenance.manifest_id.in_(manifest_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.commit()
    finally:
        cleanup_db.close()


def test_depose_un_job_pour_une_entree_eligible(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/nouvelle-entree"))

    try:
        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport == {"deposes": ["sgg-jo/nouvelle-entree"], "deja_en_file": [], "echecs_definitifs_ignores": []}
        job = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/nouvelle-entree").first()
        assert job is not None
        assert job.kind == IngestionJob.KIND_VEILLE
        assert job.status == IngestionJob.STATUS_PENDING
        assert job.requested_by == "veille-corpus"
    finally:
        _cleanup_par_manifest_id("sgg-jo/nouvelle-entree")


def test_depose_aussi_la_provenance_postgres(db, tmp_path):
    """§ 3.7 du plan « boîte de réception » : avant ce correctif, seul
    POST /api/v1/depots écrivait IngestionProvenance — la veille ne
    renseignait jamais d'où venait le fichier qu'elle avait déposé."""
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(
        "sgg-jo/avec-provenance",
        source_url="https://sgg.cg/jo/2026-13",
        fetched_at="2026-09-16T08:00:00+00:00",
        sha256="a" * 64,
        jo_numero="13",
        jo_date="2026-04-01",
        jo_annee=2026,
        titre="Journal officiel n° 13 du 1er avril 2026",
    ))

    try:
        _deposer_jobs_veille(db, manifest, dry_run=False)

        provenance = (
            db.query(IngestionProvenance)
            .filter(IngestionProvenance.manifest_id == "sgg-jo/avec-provenance")
            .first()
        )
        assert provenance is not None
        assert provenance.type_source == "journal_officiel"
        assert provenance.source_url == "https://sgg.cg/jo/2026-13"
        assert provenance.sha256 == "a" * 64
        assert provenance.fetched_at is not None
        # mibeko-python#28 : les 9 champs de ManifestEntry manquants à la
        # création de la table (dashboard#140) sont désormais aussi écrits.
        assert provenance.fichier == "sources/sgg/test.pdf"
        assert provenance.statut == "telecharge"
        assert provenance.size_bytes == 10
        assert provenance.jo_numero == "13"
        assert provenance.jo_date.isoformat() == "2026-04-01"
        assert provenance.jo_annee == 2026
        assert provenance.titre == "Journal officiel n° 13 du 1er avril 2026"
        assert provenance.retroactif is False
        assert provenance.variantes_multiples is None
    finally:
        _cleanup_par_manifest_id("sgg-jo/avec-provenance")


def test_dry_run_ne_deposse_pas_la_provenance(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/dry-run-provenance"))

    _deposer_jobs_veille(db, manifest, dry_run=True)

    assert (
        db.query(IngestionProvenance)
        .filter(IngestionProvenance.manifest_id == "sgg-jo/dry-run-provenance")
        .first()
        is None
    )


def test_ne_depose_jamais_un_deuxieme_job_pour_la_meme_entree_deja_en_file(db, tmp_path):
    """Incident (a) du plan « boîte de réception » : deux passages de veille
    successifs (ou un passage relancé) avant que le worker n'ait traité le
    premier job ne doivent jamais fabriquer un doublon."""
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/deja-en-file"))

    try:
        premier = _deposer_jobs_veille(db, manifest, dry_run=False)
        second = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert premier["deposes"] == ["sgg-jo/deja-en-file"]
        assert second == {"deposes": [], "deja_en_file": ["sgg-jo/deja-en-file"], "echecs_definitifs_ignores": []}
        total = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/deja-en-file").count()
        assert total == 1
    finally:
        _cleanup_par_manifest_id("sgg-jo/deja-en-file")


def test_redepose_apres_un_echec_definitif_du_premier_job(db, tmp_path):
    """Un job déjà `failed` (sans `error_class` ici : un échec `definitive`
    à fichier inchangé, lui, n'est plus redéposé, voir plus bas) ne bloque
    pas indéfiniment un futur dépôt : seuls `pending`/
    `running` comptent comme « déjà en file ». La provenance, elle, ne doit
    JAMAIS être réécrite une deuxième fois (contrainte UNIQUE sur
    manifest_id) : sans le garde d'idempotence, ce second dépôt violerait la
    contrainte et ferait échouer tout le passage de veille."""
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/reprise-apres-echec"))

    try:
        # Simule ce qu'un premier passage de veille aurait réellement laissé
        # derrière lui : un job en échec ET sa provenance déjà écrite.
        job_echoue = IngestionJob(
            kind=IngestionJob.KIND_VEILLE,
            manifest_id="sgg-jo/reprise-apres-echec",
            status=IngestionJob.STATUS_FAILED,
        )
        db.add(job_echoue)
        db.add(IngestionProvenance(
            manifest_id="sgg-jo/reprise-apres-echec", type_source="journal_officiel", sha256="0" * 64,
        ))
        db.commit()

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == ["sgg-jo/reprise-apres-echec"]
        total = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/reprise-apres-echec").count()
        assert total == 2
        total_provenance = (
            db.query(IngestionProvenance)
            .filter(IngestionProvenance.manifest_id == "sgg-jo/reprise-apres-echec")
            .count()
        )
        assert total_provenance == 1
    finally:
        _cleanup_par_manifest_id("sgg-jo/reprise-apres-echec")


def test_ignore_les_entrees_non_eligibles(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/deja-structure", statut="structure"))

    rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

    assert rapport == {"deposes": [], "deja_en_file": [], "echecs_definitifs_ignores": []}
    assert db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/deja-structure").first() is None


def test_dry_run_liste_sans_rien_deposer(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/dry-run-entree"))

    rapport = _deposer_jobs_veille(db, manifest, dry_run=True)

    assert rapport == {"deposes": ["sgg-jo/dry-run-entree"], "deja_en_file": [], "echecs_definitifs_ignores": []}
    assert db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/dry-run-entree").first() is None


def _echec(db, entry_id, *, error_class, sha="0" * 64, avec_provenance=True, age_minutes=0):
    """Laisse derrière elle ce qu'un passage de veille puis le worker laissent
    après un échec : un job `failed` de la classe donnée et la provenance."""
    import datetime as dt

    db.add(IngestionJob(
        kind=IngestionJob.KIND_VEILLE,
        manifest_id=entry_id,
        status=IngestionJob.STATUS_FAILED,
        error_class=error_class,
        created_at=dt.datetime.utcnow() - dt.timedelta(minutes=age_minutes),
    ))
    if avec_provenance:
        db.add(IngestionProvenance(manifest_id=entry_id, type_source="journal_officiel", sha256=sha))
    db.commit()


def _nb_jobs(db, entry_id):
    return db.query(IngestionJob).filter(IngestionJob.manifest_id == entry_id).count()


def test_ne_redepose_pas_un_echec_definitif_a_fichier_inchange(db, tmp_path):
    """mibeko-python#49 : `congo-jo-2026-17` (source vide, `definitive`)
    recevait un job `failed` de plus chaque nuit."""
    entry_id = "sgg-jo/echec-definitif-inchange"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur", sha256="a" * 64))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE, sha="a" * 64)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport == {"deposes": [], "deja_en_file": [], "echecs_definitifs_ignores": [entry_id]}
        assert _nb_jobs(db, entry_id) == 1
    finally:
        _cleanup_par_manifest_id(entry_id)


def test_redepose_un_echec_definitif_si_le_fichier_a_change(db, tmp_path):
    entry_id = "sgg-jo/echec-definitif-sha-change"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur", sha256="b" * 64))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE, sha="a" * 64)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == [entry_id]
        assert rapport["echecs_definitifs_ignores"] == []
        assert _nb_jobs(db, entry_id) == 2
    finally:
        _cleanup_par_manifest_id(entry_id)


@pytest.mark.parametrize("error_class", [
    IngestionJob.ERROR_TRANSITOIRE,
    IngestionJob.ERROR_INFORMATION_MANQUANTE,
])
def test_redepose_les_echecs_qui_gardent_leur_reessai_nocturne(db, tmp_path, error_class):
    entry_id = f"sgg-jo/echec-{error_class}"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur"))

    try:
        _echec(db, entry_id, error_class=error_class)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == [entry_id]
        assert _nb_jobs(db, entry_id) == 2
    finally:
        _cleanup_par_manifest_id(entry_id)


def test_redepose_une_entree_remise_a_telecharge_a_la_main(db, tmp_path):
    """Remettre l'entrée à `telecharge` est le geste humain pour forcer un
    nouveau traitement : la règle ne vaut que pour le statut `erreur`."""
    entry_id = "sgg-jo/echec-definitif-remis-a-telecharge"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="telecharge"))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == [entry_id]
    finally:
        _cleanup_par_manifest_id(entry_id)


def test_redepose_sans_provenance_car_rien_ne_prouve_le_fichier_inchange(db, tmp_path):
    entry_id = "sgg-jo/echec-definitif-sans-provenance"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur"))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE, avec_provenance=False)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == [entry_id]
    finally:
        _cleanup_par_manifest_id(entry_id)


def test_seul_le_dernier_job_compte(db, tmp_path):
    """Un échec `definitive` ancien, suivi d'un échec `transitoire` plus
    récent : le dernier dit que le réessai est permis."""
    entry_id = "sgg-jo/echec-definitif-puis-transitoire"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur"))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE, age_minutes=120)
        _echec(db, entry_id, error_class=IngestionJob.ERROR_TRANSITOIRE, avec_provenance=False, age_minutes=0)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == [entry_id]
    finally:
        _cleanup_par_manifest_id(entry_id)


def test_dry_run_annonce_aussi_l_echec_definitif_ignore(db, tmp_path):
    entry_id = "sgg-jo/dry-run-echec-definitif"
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry(entry_id, statut="erreur"))

    try:
        _echec(db, entry_id, error_class=IngestionJob.ERROR_DEFINITIVE)

        rapport = _deposer_jobs_veille(db, manifest, dry_run=True)

        assert rapport == {"deposes": [], "deja_en_file": [], "echecs_definitifs_ignores": [entry_id]}
        assert _nb_jobs(db, entry_id) == 1
    finally:
        _cleanup_par_manifest_id(entry_id)
