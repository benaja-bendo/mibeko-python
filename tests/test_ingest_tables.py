"""Tests du branchement de la normalisation des tableaux dans `ingest_hierarchy`.

`tests/test_tables.py` couvre le module de conversion lui-même ; ici on vérifie
ce que l'ingestion **écrit en base** : un `contenu_texte` sans balisage, la forme
canonique dans `source_locator`, et les signalements qui vont avec.

L'enjeu est l'invariant du corpus (`docs/decisions.md`, 09/08/2026) : aucun
balisage ne doit atteindre `article_versions.contenu_texte`, quelle que soit la
feuille — un tableau incrusté dans un article ordinaire compte autant qu'un
nœud TABLEAU (la production porte les deux : arrêtés miniers d'un côté, annexes
budgétaires de l'autre).

DB fake (aucun réseau, aucune Postgres réelle) — même convention que
`test_ingest_hierarchy_doublon_flags.py`.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from conftest import stub_service_modules  # noqa: E402

from src.db.models import Article, ArticleVersion, CurationFlag, LegalDocument  # noqa: E402

with stub_service_modules():
    import src.api.main as main_module  # noqa: E402
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
    monkeypatch.setattr(main_module, "clear_document_structure", lambda db, document_id: None)


def _document() -> LegalDocument:
    return LegalDocument(id=uuid.uuid4(), titre_officiel="Document de test", document_role="FLUX")


COORDONNEES_HTML = (
    "<table><tr><td>Sommets</td><td>Longitudes</td><td>Latitudes</td></tr>"
    "<tr><td>A</td><td>11° 22&#x27;22, 40&#x27; E</td><td>03° 39&#x27;7, 20&quot; S</td></tr>"
    "<tr><td>B</td><td>11° 32&#x27;45, 60&#x27; E</td><td>03° 46&#x27;12, 00&quot; S</td></tr></table>"
)


def _ingest(hierarchy):
    document = _document()
    db = FakeSession()
    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=None, validation_status="pending")
    return db


def _versions(db):
    return [obj for obj in db.added if isinstance(obj, ArticleVersion)]


def _articles(db):
    return [obj for obj in db.added if isinstance(obj, Article)]


def _flags(db, type_probleme=None):
    return [
        obj
        for obj in db.added
        if isinstance(obj, CurationFlag)
        and (type_probleme is None or obj.type_probleme == type_probleme)
    ]


def test_noeud_tableau_stocke_du_texte_et_la_forme_canonique():
    db = _ingest([
        {"type": "TABLEAU", "number": "", "title": "", "content": COORDONNEES_HTML,
         "page": 4, "children": []}
    ])

    version = _versions(db)[0]
    assert "<table" not in version.contenu_texte
    assert "&#x27;" not in version.contenu_texte
    assert version.contenu_texte.startswith("Sommets | Longitudes | Latitudes")
    assert "A | 11° 22'22, 40' E | 03° 39'7, 20\" S" in version.contenu_texte

    locator = version.source_locator
    assert locator["content_format"] == "table"
    assert locator["page"] == 4
    assert len(locator["tables"]) == 1
    table = locator["tables"][0]
    assert table["headers"] == ["Sommets", "Longitudes", "Latitudes"]
    assert table["line_start"] == 0 and table["line_end"] == 3
    # Le HTML d'origine reste disponible pour un retraitement ultérieur.
    assert table["html_source"].startswith("<table")


def test_tableau_incruste_dans_un_article_ordinaire_est_normalise_aussi():
    """Cas de production : arrêtés miniers 2026, article 2 = phrase + tableau."""
    contenu = (
        "La superficie de la zone à prospecter, réputée égale à 253 km², est définie "
        f"par les limites géographiques suivantes :\n{COORDONNEES_HTML}"
    )
    db = _ingest([
        {"type": "ARTICLE", "number": "2", "title": "", "content": contenu,
         "page": 2, "children": []}
    ])

    version = _versions(db)[0]
    assert "<table" not in version.contenu_texte
    assert "La superficie de la zone à prospecter" in version.contenu_texte
    assert version.source_locator["tables"][0]["line_start"] == 1
    # Un article ordinaire ne devient pas une feuille spéciale pour autant.
    assert "content_format" not in version.source_locator


def test_article_sans_tableau_reste_intact_et_sans_champ_superflu():
    db = _ingest([
        {"type": "ARTICLE", "number": "1", "title": "", "content": "Le présent décret…",
         "page": 1, "children": []}
    ])

    version = _versions(db)[0]
    assert version.contenu_texte == "Le présent décret…"
    assert version.source_locator == {"page": 1}
    assert _flags(db) == []


def test_disposition_et_note_conservent_numero_format_et_pages():
    db = _ingest([
        {
            "type": "DISPOSITION",
            "number": "DISPOSITION_1",
            "title": "",
            "content": "La couverture de change peut être constituée.",
            "page": 41,
            "page_end": 42,
            "children": [],
        },
        {
            "type": "NOTE",
            "number": "NOTE_1",
            "title": "",
            "content": "La justification résulte des titres de transport.",
            "page": 43,
            "children": [],
        },
    ])

    assert [article.numero_article for article in _articles(db)] == ["DISPOSITION_1", "NOTE_1"]
    disposition, note = _versions(db)
    assert disposition.source_locator == {
        "content_format": "disposition",
        "page": 41,
        "page_end": 42,
    }
    assert note.source_locator == {"content_format": "note", "page": 43}


def test_arithmetique_fausse_devient_un_signalement_sans_bloquer_lingestion():
    lignes = [
        ("3-2-1", "Assemblée législative", "37.000.000", "13.000.000", "50.000.000"),
        ("3-4-1", "Ministères", "55.805.000", "32.000.000", "87.805.000"),
        ("4-1-1", "Assemblée (matériel)", "6.015.000", "700.000", "6.715.000"),
        ("10-1-1", "Agriculture", "4.335.000", "255.000", "4.600.000"),  # 4.590.000 attendu
    ]
    entete = (
        "<tr><td>Chapitres</td><td>NOMENCLATURE</td><td>Crédits primitifs</td>"
        "<td>Crédits supplément.</td><td>Crédits nouveaux</td></tr>"
    )
    corps = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in l) + "</tr>" for l in lignes)

    db = _ingest([
        {"type": "TABLEAU", "number": "", "title": "", "content": f"<table>{entete}{corps}</table>",
         "page": 7, "children": []}
    ])

    # Le tableau est bien ingéré : un signalement n'annule jamais le contenu.
    version = _versions(db)[0]
    assert "Agriculture" in version.contenu_texte
    assert len(version.source_locator["tables"]) == 1

    flags = _flags(db, "tableau_somme_incoherente")
    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert flags[0].article_id is not None
    assert "4 590 000" in flags[0].description


def test_grille_dabonnement_signalee_comme_faux_article():
    grille = (
        "<table><tr><td>DESTINATIONS</td><td>ABONNEMENTS</td><td>1 AN</td><td>6 MOIS</td></tr>"
        "<tr><td>Journal officiel — Congo</td><td>Annonces</td><td>24.000</td><td>12.000</td></tr>"
        "<tr><td>Étranger</td><td>Tarif numéro</td><td>38.400</td><td>19.200</td></tr></table>"
    )
    db = _ingest([
        {"type": "TABLEAU", "number": "", "title": "", "content": grille, "page": 1, "children": []}
    ])

    flags = _flags(db, "tableau_ours_journal_officiel")
    assert len(flags) == 1
    assert "retirer-articles-masthead" in flags[0].description


def test_tableau_illisible_ne_perd_pas_le_contenu():
    """Un balisage tronqué est signalé, jamais escamoté : le texte officiel
    reste en base, à charge d'un humain de le reprendre depuis le PDF."""
    db = _ingest([
        {"type": "TABLEAU", "number": "", "title": "",
         "content": "<table><tr><td>Rangée sans fermeture", "page": 3, "children": []}
    ])

    version = _versions(db)[0]
    assert "Rangée sans fermeture" in version.contenu_texte
    assert "tableau_non_ferme" in {flag.type_probleme for flag in _flags(db)}
