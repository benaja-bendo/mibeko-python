"""Connexion périmée dans le pool (mibeko-python#53), contre une vraie base
Postgres : le défaut ne se voit qu'avec un vrai backend coupé côté serveur.

Le conteneur `veille` n'ouvre la base qu'une fois par nuit ; la connexion
gardée au repos peut être coupée entre deux passages. Sans `pool_pre_ping`, la
première requête du passage suivant échouait en
« server closed the connection unexpectedly ».
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text

import src.db.database as database


@pytest.fixture
def admin_engine():
    engine = create_engine(database.DATABASE_URL, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


def test_connexion_coupee_pendant_le_repos_du_pool_est_remplacee(admin_engine):
    # Pool vide : la connexion rendue ci-dessous est la seule, donc c'est
    # forcément elle que la session suivante reprend.
    database.engine.dispose()

    session = database.SessionLocal()
    pid = session.execute(text("select pg_backend_pid()")).scalar()
    session.commit()
    session.close()

    with admin_engine.connect() as admin:
        admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})

    session = database.SessionLocal()
    try:
        # Même première requête que le dépôt des travaux de veille.
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:cle))"), {"cle": "test-pool-53"})
        nouveau_pid = session.execute(text("select pg_backend_pid()")).scalar()
    finally:
        session.rollback()
        session.close()

    assert nouveau_pid != pid
