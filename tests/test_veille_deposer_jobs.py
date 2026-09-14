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
from src.db.models import IngestionJob
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
        cleanup_db.commit()
    finally:
        cleanup_db.close()


def test_depose_un_job_pour_une_entree_eligible(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/nouvelle-entree"))

    try:
        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport == {"deposes": ["sgg-jo/nouvelle-entree"], "deja_en_file": []}
        job = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/nouvelle-entree").first()
        assert job is not None
        assert job.kind == IngestionJob.KIND_VEILLE
        assert job.status == IngestionJob.STATUS_PENDING
        assert job.requested_by == "veille-corpus"
    finally:
        _cleanup_par_manifest_id("sgg-jo/nouvelle-entree")


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
        assert second == {"deposes": [], "deja_en_file": ["sgg-jo/deja-en-file"]}
        total = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/deja-en-file").count()
        assert total == 1
    finally:
        _cleanup_par_manifest_id("sgg-jo/deja-en-file")


def test_redepose_apres_un_echec_definitif_du_premier_job(db, tmp_path):
    """Un job déjà `failed` (échec définitif, pas de réessai automatique côté
    worker) ne bloque pas indéfiniment un futur dépôt — seuls `pending`/
    `running` comptent comme « déjà en file »."""
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/reprise-apres-echec"))

    try:
        job_echoue = IngestionJob(
            kind=IngestionJob.KIND_VEILLE,
            manifest_id="sgg-jo/reprise-apres-echec",
            status=IngestionJob.STATUS_FAILED,
        )
        db.add(job_echoue)
        db.commit()

        rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

        assert rapport["deposes"] == ["sgg-jo/reprise-apres-echec"]
        total = db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/reprise-apres-echec").count()
        assert total == 2
    finally:
        _cleanup_par_manifest_id("sgg-jo/reprise-apres-echec")


def test_ignore_les_entrees_non_eligibles(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/deja-structure", statut="structure"))

    rapport = _deposer_jobs_veille(db, manifest, dry_run=False)

    assert rapport == {"deposes": [], "deja_en_file": []}
    assert db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/deja-structure").first() is None


def test_dry_run_liste_sans_rien_deposer(db, tmp_path):
    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(_entry("sgg-jo/dry-run-entree"))

    rapport = _deposer_jobs_veille(db, manifest, dry_run=True)

    assert rapport == {"deposes": ["sgg-jo/dry-run-entree"], "deja_en_file": []}
    assert db.query(IngestionJob).filter(IngestionJob.manifest_id == "sgg-jo/dry-run-entree").first() is None
