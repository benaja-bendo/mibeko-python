"""Intégration `charger_etat_cible` : le slug d'un document soft-deleted bloque.

Audit drift schéma du 08/08/2026 : `legal_documents_slug_unique` est un index
unique TOTAL (sans clause WHERE), contrairement aux trois `uq_legal_documents_*`
partiels sur `deleted_at IS NULL`. Lire les slugs cibles en filtrant
`deleted_at is null` rendait invisible au plan un slug porté par un document
soft-deleted — le plan annonçait « à pousser », puis l'INSERT/COPY explosait en
duplicate key en plein `--execute` (rollback par document, mais run interrompu).

Ces tests écrivent dans la base de développement réelle (conftest refuse toute
autre cible) : un document soft-deleted éphémère, purgé physiquement en sortie
— purge légitime ici, c'est le nettoyage de notre propre ligne de test en dev.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import text  # noqa: E402

# `creer_engine_source` lit DB_* dans l'environnement sans charger le .env
# (contrairement à src.db.database) : on le charge ici pour que le test soit
# autonome, quel que soit l'ordre d'import de la session pytest.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from src.promotion.push_corpus import (  # noqa: E402
    charger_etat_cible,
    construire_plan,
    creer_engine_source,
)
from test_push_corpus import _doc  # noqa: E402


def _avec_document_soft_deleted(engine, slug: str, document_key: str):
    """Insère un document soft-deleted porteur des deux clés, purge garantie."""
    doc_id = str(uuid.uuid4())
    insertion = text(
        "insert into legal_documents (id, titre_officiel, slug, document_key, deleted_at) "
        "values (:id, :titre, :slug, :document_key, now())"
    )
    purge = text("delete from legal_documents where id = :id")

    class _Contexte:
        def __enter__(self):
            with engine.begin() as cnx:
                cnx.execute(
                    insertion,
                    {
                        "id": doc_id,
                        "titre": "Document de test — collision de slug",
                        "slug": slug,
                        "document_key": document_key,
                    },
                )
            return doc_id

        def __exit__(self, *exc):
            with engine.begin() as cnx:
                cnx.execute(purge, {"id": doc_id})
            return False

    return _Contexte()


def test_slug_soft_deleted_est_vu_et_le_plan_esquive_la_collision():
    """Le slug d'un doc soft-deleted doit entrer dans l'état cible (index
    TOTAL) et faire écarter le doc source qui le porte, au lieu de laisser
    l'exécution échouer en duplicate key."""
    marqueur = uuid.uuid4().hex[:12]
    slug = f"test-collision-slug-{marqueur}"
    document_key = f"test:collision-{marqueur}"
    engine = creer_engine_source()

    with _avec_document_soft_deleted(engine, slug, document_key):
        cible = charger_etat_cible(engine)

        # Index total : le slug soft-deleted bloque, il doit être vu.
        assert slug in cible.slugs
        # Index partiels : les autres clés d'un soft-deleted ne bloquent pas,
        # elles doivent rester invisibles (plan maximalement permissif).
        assert document_key not in cible.document_keys

        plan = construire_plan([_doc(slug=slug)], [], cible)

    assert plan.a_pousser == []
    [(_, motif)] = plan.ecartes
    assert slug in motif
