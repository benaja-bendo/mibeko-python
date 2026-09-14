"""Dépôt simultané du même PDF par deux requêtes concurrentes — scénario (a)
des six incidents du plan « boîte de réception » (mibeko-python#23, critère
de clôture de L1). Preuve au niveau base : `ingestion_provenances.manifest_id`
est UNIQUE (schema_postgres.sql) et `manifest_id` est dérivé du SHA-256 —
deux sessions qui déposent la MÊME entrée ne peuvent donc jamais toutes les
deux réussir. C'est exactement l'exception (`IntegrityError`) que
`POST /api/v1/depots` attrape pour répondre 409 au lieu de planter en 500
(src/api/main.py::deposer_document).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.exc import IntegrityError

from src.db.database import SessionLocal
from src.db.models import IngestionJob, IngestionProvenance


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def test_deux_sessions_qui_deposent_la_meme_entree_une_seule_gagne(db):
    manifest_id = "depots/concurrence-test-entree"
    sha = "1" * 64

    session_b = SessionLocal()
    try:
        db.add(IngestionProvenance(manifest_id=manifest_id, type_source="acte", sha256=sha))
        db.add(IngestionJob(kind=IngestionJob.KIND_DEPOT, manifest_id=manifest_id))
        db.commit()  # le "premier arrivé" gagne la course

        session_b.add(IngestionProvenance(manifest_id=manifest_id, type_source="acte", sha256=sha))
        with pytest.raises(IntegrityError):
            session_b.commit()  # le perdant : manifest_id déjà pris
        session_b.rollback()

        total_provenances = (
            db.query(IngestionProvenance).filter(IngestionProvenance.manifest_id == manifest_id).count()
        )
        total_jobs = db.query(IngestionJob).filter(IngestionJob.manifest_id == manifest_id).count()
        assert total_provenances == 1
        assert total_jobs == 1  # le perdant n'a jamais commité son job non plus (même transaction)
    finally:
        session_b.close()
        db.query(IngestionJob).filter(IngestionJob.manifest_id == manifest_id).delete()
        db.query(IngestionProvenance).filter(IngestionProvenance.manifest_id == manifest_id).delete()
        db.commit()
