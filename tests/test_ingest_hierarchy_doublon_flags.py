"""Tests du signalement des collisions de numérotation d'article à
l'insertion (`ingest_hierarchy`) — audit docs/audit-ingestion-2026-08-02.md,
phase 2 : `unique_article_number` ne renomme plus jamais EN SILENCE, chaque
collision (consécutive ou non) devient un `CurationFlag` individuel
`article_doublon`, numéro d'origine conservé. DB fake (aucun réseau, aucune
Postgres réelle) — même convention que `test_structuration_structurer.py`,
mais ici `ingest_hierarchy` tourne pour de vrai (pas mocké) : c'est la seule
façon d'exercer `unique_article_number`, closure privée de la fonction.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from conftest import stub_service_modules  # noqa: E402

from src.db.models import Article, CurationFlag, LegalDocument  # noqa: E402

with stub_service_modules():
    import src.api.main as main_module  # noqa: E402
    from src.api.main import ingest_hierarchy  # noqa: E402


class FakeQuery:
    """`.filter(...).delete(...)` no-op — utilisé par
    `flag_article_sequence_anomalies` pour purger les anciens flags
    heuristiques non résolus avant de recalculer (documents toujours neufs
    dans ces tests : rien à purger)."""

    def filter(self, *args, **kwargs):
        return self

    def delete(self, synchronize_session=False):
        return 0


class FakeSession:
    """`clear_document_structure` (purge la structure existante avant
    réimport) est neutralisée par le fixture `_noop_clear` ci-dessous : hors
    sujet pour ces tests (documents toujours neufs, jamais réimportés), et sa
    sous-requête `.in_(db.query(Article.id)...)` exige un vrai objet
    SQLAlchemy que ce fake ne fournit pas — même principe que le mock total
    d'`ingest_hierarchy` dans le reste de la suite, restreint ici à la seule
    partie non pertinente."""

    def __init__(self):
        self.added = []
        self.flushed_ids = set()
        self._pending = []

    def query(self, model):
        return FakeQuery()

    def add(self, obj):
        self.added.append(obj)
        self._pending.append(obj)

    def flush(self):
        for obj in self._pending:
            if getattr(obj, "id", None) is not None:
                self.flushed_ids.add(obj.id)
        self._pending = []


@pytest.fixture(autouse=True)
def _noop_clear_document_structure(monkeypatch):
    monkeypatch.setattr(main_module, "clear_document_structure", lambda db, document_id: None)


def _document() -> LegalDocument:
    return LegalDocument(id=uuid.uuid4(), titre_officiel="Document de test", document_role="FLUX")


def _article_node(number: str, content: str = "contenu") -> dict:
    return {"type": "ARTICLE", "number": number, "title": "", "content": content, "page": None, "children": []}


def _articles(db):
    return [obj for obj in db.added if isinstance(obj, Article)]


def _doublon_flags(db):
    return [obj for obj in db.added if isinstance(obj, CurationFlag) and obj.type_probleme == "article_doublon"]


def test_collision_courte_reste_blocking():
    """Une collision qui ne relance pas une série soutenue derrière elle
    reste une vraie anomalie : sévérité inchangée ('blocking'), description
    inchangée. Non-régression explicite de la remédiation phase 4 (avant, ce
    test ne vérifiait pas la sévérité)."""
    document = _document()
    db = FakeSession()
    hierarchy = [_article_node("1"), _article_node("2"), _article_node("1")]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    flags = _doublon_flags(db)
    assert len(flags) == 1
    assert flags[0].severity == "blocking"
    assert "Nécessite une revue humaine" in flags[0].description


def test_serie_annexe_confirmee_declasse_en_warning():
    """Cas réel (Arrêté n° 3277 du 28 août 2025, prod) : acte principal à 2
    articles + annexe (cahier des charges) qui redémarre à 1 et reprend une
    série soutenue. Avant la remédiation phase 4 : 2 flags `article_doublon`
    `blocking` (« Nécessite une revue humaine… ») — identique à une vraie
    collision non résolue, alors que ce n'en est probablement pas une.
    Après : toujours 2 flags (jamais de renommage silencieux, cf.
    `test_zero_renommage_sans_flag`), mais en `severity='warning'` — n'empêche
    plus la publication côté Laravel (seul `blocking` bloque)."""
    document = _document()
    db = FakeSession()
    hierarchy = [
        _article_node("1"),
        _article_node("2"),
        {"type": "SIGNATURE", "content": "Fait à Brazzaville", "page": None, "children": []},
        _article_node("1"),  # annexe : redémarre
        _article_node("2"),
        _article_node("3"),
        _article_node("4"),
        _article_node("5"),
    ]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    flags = _doublon_flags(db)
    assert len(flags) == 2  # toujours un flag par renommage — jamais silencieux
    assert all(f.severity == "warning" for f in flags)
    assert all("série secondaire" in f.description for f in flags)
    assert all("Nécessite une revue humaine" not in f.description for f in flags)


def test_collision_consecutive_et_non_consecutive_toutes_flagees():
    document = _document()
    db = FakeSession()
    hierarchy = [
        _article_node("1"),
        _article_node("2"),
        _article_node("2"),  # collision CONSÉCUTIVE
        _article_node("3"),
        _article_node("1"),  # collision NON CONSÉCUTIVE (avec le tout premier)
    ]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    articles = _articles(db)
    assert len(articles) == 5
    numeros = sorted(a.numero_article for a in articles)
    assert numeros == ["1", "1_doublon_1", "2", "2_doublon_1", "3"]

    flags = _doublon_flags(db)
    assert len(flags) == 2  # exactement les 2 renommages, pas plus
    descriptions = " | ".join(f.description for f in flags)
    assert "« 1 »" in descriptions and "« 1_doublon_1 »" in descriptions
    assert "« 2 »" in descriptions and "« 2_doublon_1 »" in descriptions
    for flag in flags:
        assert flag.source == "heuristic"
        assert flag.severity == "blocking"
        assert flag.document_id == document.id
        assert flag.article_id is not None


def test_preambule_et_signature_multiples_flagues_comme_les_articles():
    """`unique_article_number` s'applique aussi aux feuilles PREAMBULE/
    SIGNATURE multiples (acte compilé) — même garde-fou, même flag."""
    document = _document()
    db = FakeSession()
    hierarchy = [
        {"type": "PREAMBULE", "content": "premier préambule", "page": None, "children": []},
        _article_node("1"),
        {"type": "PREAMBULE", "content": "second préambule (recueil)", "page": None, "children": []},
        {"type": "SIGNATURE", "content": "Fait à Brazzaville", "page": None, "children": []},
        {"type": "SIGNATURE", "content": "Fait à Pointe-Noire", "page": None, "children": []},
    ]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    articles = _articles(db)
    numeros = sorted(a.numero_article for a in articles)
    assert numeros == ["1", "PREAMBULE", "PREAMBULE_doublon_1", "SIGNATURE", "SIGNATURE_doublon_1"]
    flags = _doublon_flags(db)
    assert len(flags) == 2


def test_zero_renommage_sans_flag():
    """Invariant central de la phase 2 : tout article dont le numéro porte
    `_doublon_` a EXACTEMENT un `CurationFlag` `article_doublon` qui le cible
    — ni renommage muet, ni flag orphelin/dupliqué."""
    document = _document()
    db = FakeSession()
    # Motif avec beaucoup de collisions variées (immédiates et espacées).
    hierarchy = [_article_node(str((n % 3) + 1)) for n in range(12)]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    articles = _articles(db)
    renamed_ids = {a.id for a in articles if "_doublon_" in a.numero_article}
    flags = _doublon_flags(db)
    flagged_ids = {f.article_id for f in flags}

    assert renamed_ids  # le fixture provoque bien des renommages
    assert renamed_ids == flagged_ids
    assert len(flags) == len(renamed_ids)  # bijection stricte : pas de doublon de flag


def test_articles_finaux_flushes_avant_les_flags_de_collision():
    """Régression (Code pénal 1836, 2026-08-03) : la session réelle est
    `autoflush=False` (`src/db/database.py`) — seul le passage d'un
    `StructureNode` flush les articles en attente (cf. `insert_nodes`). Les
    derniers articles de l'arbre, après le dernier `StructureNode`, restaient
    seulement `add()`-és — jamais flush()-és — au moment où la boucle de
    collision leur attachait un `CurationFlag`. En Postgres réel ceci lève une
    `ForeignKeyViolation` au commit (12 articles jamais flush sur ce document,
    dont 5 référencés par un flag). `FakeSession.flushed_ids` simule cette
    contrainte FK sans base réelle."""
    document = _document()
    db = FakeSession()
    hierarchy = [
        {"type": "TITRE", "number": "I", "title": "Titre I", "children": [_article_node("1")]},
        # Après ce seul StructureNode (qui flush à son passage), plus aucun
        # jusqu'à la fin de l'arbre : ces deux articles — dont la collision —
        # ne sont couverts par AUCUN flush() sans le correctif.
        _article_node("2"),
        _article_node("2"),
    ]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    flags = _doublon_flags(db)
    assert flags  # la collision tardive est bien détectée
    for flag in flags:
        assert flag.article_id in db.flushed_ids, (
            "CurationFlag référence un article jamais flush() — "
            "violerait la contrainte de clé étrangère en Postgres réel"
        )


def test_aucune_collision_aucun_flag():
    document = _document()
    db = FakeSession()
    hierarchy = [_article_node(str(n)) for n in range(1, 6)]

    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")

    assert _doublon_flags(db) == []
    assert all("_doublon_" not in a.numero_article for a in _articles(db))
