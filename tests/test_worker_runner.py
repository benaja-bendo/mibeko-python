"""Tests du worker de la file `ingestion_jobs` (mibeko-python#23) — contre une
VRAIE base Postgres (comme `tests/test_document_deletion_shared_media.py`) :
la sémantique `SELECT … FOR UPDATE SKIP LOCKED` et la reprise de bail expiré
ne peuvent pas être prouvées contre une session fake, elles dépendent du
comportement réel du moteur (MVCC Postgres).
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.acquisition.manifest import Manifest, ManifestEntry
from src.db.database import SessionLocal
from src.db.models import IngestionJob
from src.worker.runner import (
    _classify_structuration_motif,
    backoff_seconds,
    classify_error,
    finalize_job,
    process_job,
    renew_lease,
    reserve_job,
)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _make_job(db, **overrides) -> IngestionJob:
    job = IngestionJob(kind=IngestionJob.KIND_DEPOT, **overrides)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _cleanup(*job_ids):
    cleanup_db = SessionLocal()
    try:
        cleanup_db.query(IngestionJob).filter(IngestionJob.id.in_(job_ids)).delete(synchronize_session=False)
        cleanup_db.commit()
    finally:
        cleanup_db.close()


def test_reserve_job_recupere_un_travail_pending(db):
    job = _make_job(db)
    try:
        reserved = reserve_job(db)

        assert reserved is not None
        assert reserved.id == job.id
        assert reserved.status == IngestionJob.STATUS_RUNNING
        assert reserved.locked_by  # hôte:pid, non vide
        assert reserved.fencing_token == 1
    finally:
        _cleanup(job.id)


def test_reserve_job_renvoie_none_sans_travail_eligible(db):
    # Base de test partagée : ne présume pas qu'elle est vide, épuise juste
    # ce qu'il y a et vérifie qu'un second appel ne trouve plus rien de NEUF.
    drained = []
    while True:
        job = reserve_job(db)
        if job is None:
            break
        drained.append(job.id)
    assert reserve_job(db) is None
    if drained:
        _cleanup(*drained)


def test_reserve_job_ignore_une_ligne_deja_verrouillee_par_une_autre_session(db):
    """Le primitif exact dont deux workers concurrents dépendent : SKIP LOCKED
    ne bloque JAMAIS sur une ligne déjà prise, il passe à la suivante."""
    job_verrouille = _make_job(db)
    job_libre = _make_job(db)

    autre_session = SessionLocal()
    try:
        # `autre_session` verrouille job_verrouille et NE COMMIT PAS encore —
        # simule un worker en plein traitement.
        (
            autre_session.query(IngestionJob)
            .filter(IngestionJob.id == job_verrouille.id)
            .with_for_update()
            .first()
        )

        reserved = reserve_job(db)

        assert reserved is not None
        assert reserved.id == job_libre.id  # jamais job_verrouille, malgré created_at antérieur
    finally:
        autre_session.rollback()
        autre_session.close()
        _cleanup(job_verrouille.id, job_libre.id)


def test_reserve_job_reprend_un_bail_expire(db):
    """Incident (d) du plan « boîte de réception » : un worker mort sans
    finaliser laisse un travail `running` avec un bail dépassé — un AUTRE
    worker doit pouvoir le reprendre, avec un jeton incrémenté."""
    job = _make_job(
        db,
        status=IngestionJob.STATUS_RUNNING,
        locked_at=datetime.utcnow() - timedelta(minutes=45),
        locked_by="hote-mort:1234",
        fencing_token=3,
    )
    try:
        reserved = reserve_job(db)

        assert reserved is not None
        assert reserved.id == job.id
        assert reserved.fencing_token == 4  # incrémenté, jamais réutilisé
        assert reserved.locked_by != "hote-mort:1234"
    finally:
        _cleanup(job.id)


def test_reserve_job_ne_reprend_pas_un_bail_encore_valide(db):
    job = _make_job(
        db,
        status=IngestionJob.STATUS_RUNNING,
        locked_at=datetime.utcnow() - timedelta(minutes=5),
        locked_by="hote-vivant:1234",
        fencing_token=1,
    )
    drained = []
    try:
        # Épuise les autres travaux `pending` éventuels avant l'assertion,
        # pour ne pas dépendre de l'état exact de la base partagée.
        while True:
            other = reserve_job(db)
            if other is None or other.id == job.id:
                break
            drained.append(other.id)

        reprised = reserve_job(db)
        assert reprised is None or reprised.id != job.id
    finally:
        _cleanup(job.id, *drained)


def test_finalize_job_ecrit_le_resultat_si_le_jeton_est_valide(db):
    job = _make_job(db)
    try:
        reserved = reserve_job(db)
        ok = finalize_job(
            db, reserved.id, reserved.fencing_token,
            status=IngestionJob.STATUS_DONE, step=IngestionJob.STEP_TERMINE,
            result={"document_ids": ["abc"]},
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_DONE
        assert relu.result == {"document_ids": ["abc"]}
    finally:
        _cleanup(job.id)


def test_finalize_job_abandonne_si_le_jeton_a_change(db):
    """Le worker A réserve, puis son bail expire et le worker B reprend le
    même travail (jeton incrémenté) AVANT que A ait fini — A ne doit jamais
    pouvoir écrire son résultat par-dessus celui de B."""
    job = _make_job(db)
    try:
        reserved_par_a = reserve_job(db)
        jeton_de_a = reserved_par_a.fencing_token

        # B reprend le même travail (simule un bail expiré pendant que A
        # continuait de travailler dessus sans le savoir).
        autre_session = SessionLocal()
        try:
            row = autre_session.query(IngestionJob).filter(IngestionJob.id == job.id).first()
            row.fencing_token += 1
            row.locked_by = "worker-b:9999"
            autre_session.commit()
        finally:
            autre_session.close()

        ok = finalize_job(
            db, job.id, jeton_de_a,
            status=IngestionJob.STATUS_DONE, step=IngestionJob.STEP_TERMINE,
            result={"document_ids": ["ecrit-par-a-a-tort"]},
        )

        assert ok is False
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.result != {"document_ids": ["ecrit-par-a-a-tort"]}
        assert relu.locked_by == "worker-b:9999"
    finally:
        _cleanup(job.id)


def test_renew_lease_prolonge_si_le_jeton_est_valide(db):
    job = _make_job(db)
    try:
        reserved = reserve_job(db)
        ancien_locked_at = reserved.locked_at

        ok = renew_lease(db, reserved, reserved.fencing_token)

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.locked_at >= ancien_locked_at
    finally:
        _cleanup(job.id)


def test_renew_lease_echoue_si_le_jeton_a_change(db):
    job = _make_job(db)
    try:
        reserved = reserve_job(db)

        autre_session = SessionLocal()
        try:
            row = autre_session.query(IngestionJob).filter(IngestionJob.id == job.id).first()
            row.fencing_token += 1
            autre_session.commit()
        finally:
            autre_session.close()

        ok = renew_lease(db, reserved, reserved.fencing_token)
        assert ok is False
    finally:
        _cleanup(job.id)


def test_classify_error_reseau_est_transitoire():
    assert classify_error(TimeoutError("délai dépassé")) == IngestionJob.ERROR_TRANSITOIRE
    assert classify_error(ConnectionError("connexion refusée")) == IngestionJob.ERROR_TRANSITOIRE


def test_classify_error_autre_est_definitive():
    assert classify_error(ValueError("payload invalide")) == IngestionJob.ERROR_DEFINITIVE


def test_classify_structuration_motif_echec_minio_est_transitoire():
    """Revue technique du 15/09/2026 : un échec de stockage MinIO (réseau,
    quota, service temporairement indisponible) était classé `definitive` —
    aucun réessai possible pour l'exemple même d'un incident transitoire."""
    assert _classify_structuration_motif("échec de stockage MinIO pour le PDF source") == IngestionJob.ERROR_TRANSITOIRE
    assert (
        _classify_structuration_motif("échec d'insertion DB : échec de stockage MinIO pour le PDF source")
        == IngestionJob.ERROR_TRANSITOIRE
    )
    assert _classify_structuration_motif("échec de stockage MinIO pour le markdown (JO)") == IngestionJob.ERROR_TRANSITOIRE


def test_classify_structuration_motif_echec_db_hors_minio_reste_definitive():
    assert (
        _classify_structuration_motif("échec d'insertion DB : contrainte de clé étrangère violée")
        == IngestionJob.ERROR_DEFINITIVE
    )


def test_backoff_seconds_croit_puis_plafonne():
    assert backoff_seconds(1) == 30.0
    assert backoff_seconds(2) == 60.0
    assert backoff_seconds(3) == 120.0
    assert backoff_seconds(20) == 1800.0  # plafond


# ---------------------------------------------------------------------------
# process_job : orchestration réelle (IngestionJob sur vraie base Postgres,
# comme ci-dessus) avec le pipeline (parse/structure) INJECTÉ — pas de MinIO
# ni de Mistral réels ici, cf. tests/test_structuration_*.py pour les tests
# du pipeline lui-même. Ce qui est prouvé : l'enchaînement des étapes, la
# reprise depuis job.step/job.result, la classification des échecs, et
# l'abandon quand le jeton expire en cours de route.
# ---------------------------------------------------------------------------

def _entry(entry_id: str = "sgg-jo/test-entry", **overrides) -> ManifestEntry:
    defaults = dict(
        id=entry_id,
        fichier="sources/sgg/test.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="journal_officiel",
        statut="telecharge",
    )
    defaults.update(overrides)
    return ManifestEntry(**defaults)


def _write_manifest_entry(data_dir: Path, source_key: str, entry: ManifestEntry) -> None:
    manifest = Manifest(data_dir / "manifests" / f"{source_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()


def test_process_job_enchaine_parse_puis_structure(db, tmp_path):
    entry = _entry()
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(db, manifest_id=entry.id)
    try:
        reserved = reserve_job(db)
        assert reserved.id == job.id

        def fake_process_entry(data_dir, e, force=False):
            assert e.id == entry.id
            return {"id": e.id, "skipped": False, "methode": "native"}

        def fake_structure_document(db_, data_dir, e, dry_run=False):
            assert e.id == entry.id
            return {"statut": "structure", "document_id": "doc-123", "motif": None}

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=fake_process_entry,
            structure_document_fn=fake_structure_document,
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_DONE
        assert relu.step == IngestionJob.STEP_TERMINE
        assert relu.result["parse"]["methode"] == "native"
        assert relu.result["structure"]["document_ids"] == ["doc-123"]

        manifest_relu = Manifest(tmp_path / "manifests" / "sgg-jo.jsonl")
        assert manifest_relu.get(entry.id).statut == "structure"
    finally:
        _cleanup(job.id)


def test_process_job_reprend_depuis_l_etape_structure_sans_rejouer_le_parse(db, tmp_path):
    entry = _entry(entry_id="sgg-jo/reprise-entry", statut="parse")
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(
        db, manifest_id=entry.id,
        step=IngestionJob.STEP_PARSE,
        result={"parse": {"methode": "native"}},
    )
    try:
        reserved = reserve_job(db)

        def fake_process_entry(*a, **k):
            raise AssertionError("ne doit jamais être rappelé : l'étape parse a déjà réussi")

        def fake_structure_document(db_, data_dir, e, dry_run=False):
            return {"statut": "structure", "document_id": "doc-999", "motif": None}

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=fake_process_entry,
            structure_document_fn=fake_structure_document,
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_DONE
        # le résultat de parse déjà enregistré est préservé, pas écrasé
        assert relu.result["parse"]["methode"] == "native"
        assert relu.result["structure"]["document_ids"] == ["doc-999"]
    finally:
        _cleanup(job.id)


def test_process_job_manifeste_introuvable_echoue_en_definitive(db, tmp_path):
    job = _make_job(db, manifest_id="sgg-jo/inexistant")
    try:
        reserved = reserve_job(db)
        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=lambda *a, **k: {}, structure_document_fn=lambda *a, **k: {},
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_FAILED
        assert relu.error_class == IngestionJob.ERROR_DEFINITIVE
        assert "introuvable" in relu.last_error
    finally:
        _cleanup(job.id)


def test_process_job_echec_transitoire_repasse_pending_si_tentatives_restantes(db, tmp_path):
    entry = _entry(entry_id="sgg-jo/transitoire-entry")
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(db, manifest_id=entry.id, max_attempts=3, attempts=0)
    try:
        reserved = reserve_job(db)
        # Capturé AVANT tout commit ultérieur : `reserved` reste la même
        # identité dans la session `db`, `expire_on_commit` rafraîchirait
        # sinon cet attribut à sa toute dernière valeur en base au moment de
        # l'assertion, pas à la valeur voulue ici (piège de test, pas du code
        # — `process_job` ne lit jamais l'objet après le premier commit, il
        # capture `job.fencing_token` dans une variable locale dès l'entrée).
        jeton_initial = reserved.fencing_token

        def fake_process_entry(*a, **k):
            raise ConnectionError("panne réseau simulée")

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=fake_process_entry, structure_document_fn=lambda *a, **k: {},
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_PENDING
        assert relu.error_class == IngestionJob.ERROR_TRANSITOIRE
        assert relu.attempts == 1

        # éligible à une nouvelle réservation immédiate (pas de colonne de
        # planification dédiée — cf. docstring de _finalize_failure).
        reprise = reserve_job(db)
        assert reprise is not None and reprise.id == job.id
        assert reprise.fencing_token == jeton_initial + 1
    finally:
        _cleanup(job.id)


def test_process_job_echec_transitoire_definitif_apres_epuisement_des_tentatives(db, tmp_path):
    entry = _entry(entry_id="sgg-jo/transitoire-epuise")
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(db, manifest_id=entry.id, max_attempts=1, attempts=0)
    try:
        reserved = reserve_job(db)

        def fake_process_entry(*a, **k):
            raise ConnectionError("panne réseau simulée")

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=fake_process_entry, structure_document_fn=lambda *a, **k: {},
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_FAILED  # max_attempts=1 atteint
        assert relu.attempts == 1
    finally:
        _cleanup(job.id)


def test_process_job_echec_validation_llm_classe_information_manquante(db, tmp_path):
    entry = _entry(entry_id="sgg-jo/info-manquante", statut="parse")
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(
        db, manifest_id=entry.id,
        step=IngestionJob.STEP_PARSE,
        result={"parse": {"methode": "native"}},
    )
    try:
        reserved = reserve_job(db)

        def fake_structure_document(db_, data_dir, e, dry_run=False):
            return {
                "statut": "erreur", "document_id": None,
                "motif": "validation du schéma en échec : nature manquante",
            }

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=lambda *a, **k: {}, structure_document_fn=fake_structure_document,
        )

        assert ok is True
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.status == IngestionJob.STATUS_FAILED
        assert relu.error_class == IngestionJob.ERROR_INFORMATION_MANQUANTE

        # Revue technique du 15/09 : sans la resynchronisation du manifeste
        # sur l'échec de structuration, cette entrée restait invisible pour
        # toujours à _deposer_jobs_veille (qui ne redépose que
        # "telecharge"/"erreur", jamais "parse").
        manifest_relu = Manifest(tmp_path / "manifests" / "sgg-jo.jsonl")
        assert manifest_relu.get(entry.id).statut == "erreur"
    finally:
        _cleanup(job.id)


def test_process_job_abandonne_si_le_bail_expire_entre_parse_et_structure(db, tmp_path):
    """Incident (c)/(d) du plan « boîte de réception » : un autre worker
    reprend le job (bail expiré) PENDANT que celui-ci exécute encore l'étape
    parse — l'écriture du checkpoint parse doit être refusée et l'étape
    structure ne doit JAMAIS démarrer."""
    entry = _entry(entry_id="sgg-jo/bail-perdu")
    _write_manifest_entry(tmp_path, "sgg-jo", entry)
    job = _make_job(db, manifest_id=entry.id)
    try:
        reserved = reserve_job(db)

        def fake_process_entry(*a, **k):
            autre_session = SessionLocal()
            try:
                row = autre_session.query(IngestionJob).filter(IngestionJob.id == job.id).first()
                row.fencing_token += 1
                row.locked_by = "worker-b:9999"
                autre_session.commit()
            finally:
                autre_session.close()
            return {"id": entry.id, "skipped": False, "methode": "native"}

        structure_appele = []

        def fake_structure_document(*a, **k):
            structure_appele.append(1)
            return {"statut": "structure", "document_id": "jamais-ecrit", "motif": None}

        ok = process_job(
            db, tmp_path, reserved,
            process_entry_fn=fake_process_entry, structure_document_fn=fake_structure_document,
        )

        assert ok is False
        assert structure_appele == []  # jamais appelé : le jeton était déjà perdu
        relu = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert relu.locked_by == "worker-b:9999"  # jamais écrasé par le worker évincé
    finally:
        _cleanup(job.id)
