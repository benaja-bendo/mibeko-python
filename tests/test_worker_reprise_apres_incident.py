"""Scénarios d'incident (b) et (c) du plan « boîte de réception » (§ L1,
mibeko-python#23) — les deux seuls des six scénarios encore non couverts :
(a) dépôt concurrent → tests/test_api_depot_concurrence.py ; (d) bail expiré
repris par un second worker → tests/test_worker_runner.py ; (e) réponse
LLM perdue puis rejouée → tests/test_structuration_structurer.py ; (f) reprise
JO interrompu → tests/test_structuration_journals_split.py.

Vraie base Postgres + vrai MinIO (docker compose local), comme
tests/test_worker_runner.py et tests/test_worker_batch_parity.py : l'objet
même de (b) est un dépôt MinIO réel resté orphelin, pas un double en mémoire
qui ne prouverait rien de l'écriture réseau elle-même.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
import pytest

import src.structuration.structurer as structurer
from src.acquisition.manifest import Manifest, ManifestEntry, sha256_file
from src.db.database import SessionLocal
from src.db.models import IngestionJob, LegalDocument
from src.services.ingestion import build_document_key, sanitize_path_component
from src.services.minio_service import minio_service as vrai_minio_service
from src.structuration.structurer import structure_document
from src.worker.runner import process_job, reserve_job


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


class FixedMetadataMistralClient:
    """Métadonnées déterministes (comme tests/test_worker_batch_parity.py) :
    la LLM elle-même n'est pas sous test ici, seule l'orchestration
    stockage/DB autour d'elle l'est."""

    async def extract_metadata(self, texte, instructions):
        return {
            "nature": None, "numero": None, "date_signature": None,
            "date_publication": "2020-01-01", "autorite": None,
        }


def _structure_avec_client_fige(db, data_dir, entry, dry_run=False):
    return structure_document(db, data_dir, entry, mistral_client=FixedMetadataMistralClient(), dry_run=dry_run)


TEXTE = (
    "CODE DE TEST INCIDENT\n"
    "ARTICLE PREMIER : Premiere disposition du code de test.\n"
    "ARTICLE 2 : Deuxieme disposition du code de test.\n"
) * 4


def _make_pdf(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 545, 792), text, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _seed_entry(data_dir: Path, manifest_key: str, entry_id: str) -> ManifestEntry:
    """PDF source réel + markdown déjà placé (étape parse déjà « faite »,
    comme tests/test_worker_batch_parity.py::_seed_markdown_entry) : ces deux
    scénarios portent sur l'étape structure, jamais sur le triage/OCR."""
    rel_path = f"sources/{entry_id}.pdf"
    pdf_path = _make_pdf(data_dir / rel_path, TEXTE)
    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(TEXTE, encoding="utf-8")

    entry = ManifestEntry(
        id=entry_id, fichier=rel_path, sha256=sha256_file(pdf_path), size_bytes=pdf_path.stat().st_size,
        type_source="code", statut="parse",
    )
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()
    return entry


def _document_key_attendu(entry: ManifestEntry) -> str:
    """Reproduit exactement le calcul de structure_document (STOCK, sans
    numéro LLM, sans titre imposé) — jamais deviné, pour que l'assertion
    porte sur la même clé que le code sous test utilise réellement."""
    basename = entry.id.split("/")[-1]
    stock_code = sanitize_path_component(basename)[:100]
    return build_document_key("STOCK", stock_code, f"Code — {basename}")


def _make_job(db, **overrides) -> IngestionJob:
    job = IngestionJob(kind=IngestionJob.KIND_DEPOT, **overrides)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _cleanup_job(*job_ids):
    cleanup_db = SessionLocal()
    try:
        cleanup_db.query(IngestionJob).filter(IngestionJob.id.in_(job_ids)).delete(synchronize_session=False)
        cleanup_db.commit()
    finally:
        cleanup_db.close()


class _MinioReelPuisEchecMarkdown:
    """Délègue le PREMIER upload (le PDF source) au vrai `minio_service` —
    un dépôt réseau authentique, pas un double en mémoire — puis simule
    l'échec du suivant (markdown) SANS toucher au réseau. Reproduit
    l'instant exact du scénario (b) : « stockage MinIO fait, écriture DB pas
    encore atteinte », sans dépendre d'un vrai kill -9 du process — l'écart
    avec un crash réel est nul du point de vue Postgres (structure_document
    capture l'échec dans son propre `except`/`rollback`, qui défait tout ce
    que la transaction avait fait, exactement ce qu'une connexion coupée
    provoquerait aussi)."""

    def __init__(self):
        self.objets_reellement_ecrits: list[str] = []

    def upload_file(self, object_name, file_path, content_type="application/pdf"):
        if content_type != "application/pdf":
            return None
        resultat = vrai_minio_service.upload_file(object_name, file_path, content_type)
        if resultat:
            self.objets_reellement_ecrits.append(object_name)
        return resultat


def test_worker_meurt_apres_stockage_minio_avant_ecriture_db(db, monkeypatch, tmp_path):
    """Scénario (b) : « arrêt du worker après stockage MinIO mais avant
    écriture DB ». Deux garanties à prouver ensemble : le PDF source atterrit
    RÉELLEMENT dans MinIO avant l'échec (sinon le scénario ne teste rien) —
    et malgré ça, aucune ligne partielle ne survit en base, et une reprise
    ultérieure ne crée jamais deux documents pour la même entrée."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "test-incident-b", "test-incident-b/entree")
    document_key = _document_key_attendu(entry)

    minio_partiel = _MinioReelPuisEchecMarkdown()
    monkeypatch.setattr(structurer, "minio_service", minio_partiel)

    document_id = None
    objet_orphelin = None
    try:
        premier = structure_document(db, data_dir, entry, mistral_client=FixedMetadataMistralClient(), dry_run=False)

        assert premier["statut"] == "erreur"
        assert "MinIO" in premier["motif"]
        assert len(minio_partiel.objets_reellement_ecrits) == 1, (
            "le PDF doit avoir été réellement stocké dans MinIO avant l'échec du markdown"
        )
        objet_orphelin = minio_partiel.objets_reellement_ecrits[0]

        # Preuve directe (pas seulement raisonnée) : l'objet existe bel et
        # bien dans MinIO — aucune exception ici prouve qu'il y est.
        vrai_minio_service.client.stat_object(vrai_minio_service.bucket_name, objet_orphelin)

        # Aucune ligne, même partielle : le rollback du bloc except de
        # structure_document défait tout ce que le bloc try avait déjà fait
        # (LegalDocument inséré par le flush() avant l'échec MinIO compris).
        assert db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first() is None

        # Reprise (même worker relancé, ou un autre) : MinIO répond de
        # nouveau normalement, plus aucune panne.
        monkeypatch.setattr(structurer, "minio_service", vrai_minio_service)
        second = structure_document(db, data_dir, entry, mistral_client=FixedMetadataMistralClient(), dry_run=False)

        assert second["statut"] == "structure"
        document_id = second["document_id"]
        # Un seul document pour cette entrée malgré la tentative avortée :
        # le document_key n'en retrouve exactement qu'un, jamais deux.
        assert (
            db.query(LegalDocument).filter(LegalDocument.document_key == document_key).count() == 1
        )
    finally:
        if document_id is not None:
            db.query(LegalDocument).filter(LegalDocument.id == document_id).delete()
            db.commit()
        if objet_orphelin is not None:
            vrai_minio_service.delete_file(objet_orphelin)


def test_document_deja_ecrit_en_db_ne_se_duplique_pas_si_le_worker_meurt_avant_de_finaliser_le_job(db, tmp_path):
    """Scénario (c) : « arrêt après écriture DB mais avant fin de job ». Le
    document est bel et bien committé en base (structure_document a fini,
    pour de vrai) — mais le worker meurt avant d'appeler finalize_job : la
    ligne ingestion_jobs reste `running`/step=parse, jamais `done`. Un futur
    worker doit reprendre ce job (bail expiré, comme le scénario (d)) sans
    fabriquer un second document, et mener le job à `done` cette fois.

    Différence avec le scénario (e) (réponse LLM perdue puis rejouée,
    tests/test_structuration_structurer.py) : ici c'est le job lui-même —
    pas la réponse LLM — qui n'a jamais été finalisé ; ce test passe par
    process_job de bout en bout pour le prouver, pas structure_document seul.
    """
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "test-incident-c", "test-incident-c/entree")
    job = _make_job(db, manifest_id=entry.id, step=IngestionJob.STEP_PARSE, result={"parse": {"methode": "native"}})

    document_id = None
    try:
        reserved = reserve_job(db)
        assert reserved.id == job.id

        # Le worker écrit RÉELLEMENT le document en base (structure_document
        # va jusqu'à son propre db.commit()) puis « meurt » : aucun appel à
        # finalize_job ne suit — exactement ce qu'un kill -9 juste après le
        # retour de structure_document laisserait derrière lui.
        premier = structure_document(db, data_dir, entry, mistral_client=FixedMetadataMistralClient(), dry_run=False)
        assert premier["statut"] == "structure"
        document_id = premier["document_id"]

        toujours_running = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert toujours_running.status == IngestionJob.STATUS_RUNNING
        assert toujours_running.step == IngestionJob.STEP_PARSE  # jamais avancé : finalize_job n'a jamais tourné

        # Simule le temps qui passe : le bail expire (même manipulation
        # directe que test_worker_runner.py::test_reserve_job_reprend_un_bail_expire).
        autre_session = SessionLocal()
        try:
            row = autre_session.query(IngestionJob).filter(IngestionJob.id == job.id).first()
            row.locked_at = datetime.utcnow() - timedelta(minutes=45)
            autre_session.commit()
        finally:
            autre_session.close()

        # Un second worker (ou le même relancé) reprend le job abandonné.
        reprise = reserve_job(db)
        assert reprise is not None and reprise.id == job.id
        assert reprise.step == IngestionJob.STEP_PARSE  # reprend bien depuis structure, pas depuis recu

        def process_entry_jamais_appele(*a, **k):
            raise AssertionError("l'étape parse a déjà réussi, jamais rejouée sur une reprise depuis structure")

        ok = process_job(
            db, data_dir, reprise,
            process_entry_fn=process_entry_jamais_appele,
            structure_document_fn=_structure_avec_client_fige,
        )

        assert ok is True
        final = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert final.status == IngestionJob.STATUS_DONE
        assert final.step == IngestionJob.STEP_TERMINE
        # structure_document a détecté le document déjà existant (document_key) :
        # jamais un second document créé pour la même entrée.
        assert final.result["structure"]["statut"] == "deja_existant"
        assert final.result["structure"]["document_ids"] == [str(document_id)]

        document_key = _document_key_attendu(entry)
        assert db.query(LegalDocument).filter(LegalDocument.document_key == document_key).count() == 1
    finally:
        if document_id is not None:
            db.query(LegalDocument).filter(LegalDocument.id == document_id).delete()
            db.commit()
        _cleanup_job(job.id)
