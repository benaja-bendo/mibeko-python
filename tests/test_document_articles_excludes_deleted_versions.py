"""GET /api/documents/{id}/articles n'expose plus une version soft-deletée.

dashboard#166 : `article_versions.deleted_at` (migration Laravel
2026_09_19_230955) retire les versions fermées qui ne sont pas de vrais
amendements. Sans ce filtre, cette route Python re-exposerait des artefacts
que la remédiation vient précisément de retirer côté éditeur.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from conftest import stub_service_modules  # noqa: E402
from src.db.database import SessionLocal  # noqa: E402
from src.db.models import Article, ArticleVersion, LegalDocument  # noqa: E402

with stub_service_modules():
    from src.api.routers.documents import get_document_articles  # noqa: E402


@pytest.fixture
def article_avec_une_version_soft_deletee():
    db = SessionLocal()
    document = LegalDocument(titre_officiel="Texte de test #166", document_role="FLUX", curation_status="draft")
    db.add(document)
    db.flush()

    article = Article(document_id=document.id, numero_article="1", ordre_affichage=0)
    db.add(article)
    db.flush()

    fermee = ArticleVersion(
        article_id=article.id,
        contenu_texte="Ancienne version fermée, retirée par la remédiation.",
        validity_period="[2026-01-01,2026-02-01)",
        deleted_at=datetime.utcnow() - timedelta(minutes=1),
    )
    active = ArticleVersion(
        article_id=article.id,
        contenu_texte="Version active.",
        validity_period="[2026-02-01,)",
    )
    db.add_all([fermee, active])
    db.commit()

    try:
        yield db, document.id, article.id
    finally:
        db.rollback()
        db.query(ArticleVersion).filter(ArticleVersion.article_id == article.id).delete(synchronize_session=False)
        db.query(Article).filter(Article.id == article.id).delete(synchronize_session=False)
        db.query(LegalDocument).filter(LegalDocument.id == document.id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_exclut_la_version_soft_deletee(article_avec_une_version_soft_deletee):
    db, document_id, article_id = article_avec_une_version_soft_deletee

    resultat = get_document_articles(doc_id=str(document_id), page=1, per_page=50, db=db, _user=None)

    article_out = next(a for a in resultat.items if str(a.id) == str(article_id))
    assert len(article_out.versions) == 1
    assert article_out.versions[0].contenu_texte == "Version active."
