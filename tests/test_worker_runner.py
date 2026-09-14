"""Tests du worker de la file `ingestion_jobs` (mibeko-python#23) — contre une
VRAIE base Postgres (comme `tests/test_document_deletion_shared_media.py`) :
la sémantique `SELECT … FOR UPDATE SKIP LOCKED` et la reprise de bail expiré
ne peuvent pas être prouvées contre une session fake, elles dépendent du
comportement réel du moteur (MVCC Postgres).
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.db.database import SessionLocal
from src.db.models import IngestionJob
from src.worker.runner import (
    backoff_seconds,
    classify_error,
    finalize_job,
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


def test_backoff_seconds_croit_puis_plafonne():
    assert backoff_seconds(1) == 30.0
    assert backoff_seconds(2) == 60.0
    assert backoff_seconds(3) == 120.0
    assert backoff_seconds(20) == 1800.0  # plafond
