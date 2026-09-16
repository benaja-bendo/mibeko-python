"""Test de la propagation de `page_end` dans `source_locator` pour un nœud
ARTICLE (mibeko-python#24, § 3.5 du plan « boîte de réception ») — additif :
`page` reste écrit à l'identique (jamais cassé pour les lecteurs existants
front/dashboard), `page_end` s'ajoute quand le parseur l'a calculé. Même
convention DB fake que `test_ingest_tables.py`.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from conftest import stub_service_modules  # noqa: E402

from src.db.models import ArticleVersion, LegalDocument  # noqa: E402

with stub_service_modules():
    import src.api.main as main_module  # noqa: E402
    import src.services.ingestion as ingestion_module  # noqa: E402
    from src.api.main import ingest_hierarchy  # noqa: E402


class FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def delete(self, synchronize_session=False):
        return 0


class FakeSession:
    def __init__(self):
        self.added = []

    def query(self, model):
        return FakeQuery()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        return None


@pytest.fixture(autouse=True)
def _noop_clear_document_structure(monkeypatch):
    monkeypatch.setattr(ingestion_module, "clear_document_structure", lambda db, document_id: None)
    monkeypatch.setattr(main_module, "clear_document_structure", lambda db, document_id: None)


def _document() -> LegalDocument:
    return LegalDocument(id=uuid.uuid4(), titre_officiel="Document de test", document_role="FLUX")


def _ingest(hierarchy):
    document = _document()
    db = FakeSession()
    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")
    return [obj for obj in db.added if isinstance(obj, ArticleVersion)]


def test_article_sur_une_seule_page_ne_porte_pas_page_end():
    versions = _ingest([
        {"type": "ARTICLE", "number": "1", "title": "", "content": "Contenu.", "page": 3, "children": []}
    ])

    locator = versions[0].source_locator
    assert locator["page"] == 3
    assert "page_end" not in locator


def test_article_sur_plusieurs_pages_porte_page_et_page_end():
    versions = _ingest([
        {
            "type": "ARTICLE", "number": "1", "title": "", "content": "Contenu.",
            "page": 3, "page_end": 5, "children": [],
        }
    ])

    locator = versions[0].source_locator
    assert locator["page"] == 3
    assert locator["page_end"] == 5


def test_article_sans_page_ne_porte_ni_page_ni_page_end():
    versions = _ingest([
        {"type": "ARTICLE", "number": "1", "title": "", "content": "Contenu.", "page": None, "children": []}
    ])

    locator = versions[0].source_locator
    assert "page" not in locator
    assert "page_end" not in locator
