"""Tests de `flag_structure_coverage_gaps` (mibeko-python#24, Mesure 2 § 3.5
du plan « boîte de réception ») — même convention DB fake que
`test_flag_page_coverage.py`.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ingestion import flag_structure_coverage_gaps  # noqa: E402


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


def _markdown(pages: dict[int, str]) -> str:
    lignes = []
    for numero in sorted(pages):
        lignes.append(f"[[MIBEKO_PAGE:{numero}]]")
        lignes.append(pages[numero])
    return "\n".join(lignes)


def test_signale_une_page_avec_contenu_reel_non_couverte_par_un_article():
    db = FakeSession()
    markdown = _markdown({
        1: "Contenu de l'article 1, substantiel et complet en tous points.",
        2: "Contenu perdu entre l'extraction et la structuration du texte.",
    })
    # Seule la page 1 est couverte par un article — page 2 a du contenu réel
    # dans le markdown mais n'apparaît dans aucun nœud de la hiérarchie.
    hierarchy = [{"type": "ARTICLE", "number": "1", "page": 1, "content": markdown, "children": []}]

    a_flague = flag_structure_coverage_gaps(db, uuid.uuid4(), markdown, hierarchy)

    assert a_flague is True
    assert len(db.added) == 1
    flag = db.added[0]
    assert flag.type_probleme == "bloc_non_structure"
    assert flag.severity == "warning"
    assert "2" in flag.description


def test_ne_signale_rien_quand_toutes_les_pages_avec_contenu_sont_couvertes():
    db = FakeSession()
    markdown = _markdown({
        1: "Contenu de l'article 1, substantiel et complet en tous points.",
        2: "Contenu de l'article 2, également substantiel et complet.",
    })
    hierarchy = [
        {"type": "ARTICLE", "number": "1", "page": 1, "content": "x", "children": []},
        {"type": "ARTICLE", "number": "2", "page": 2, "content": "x", "children": []},
    ]

    a_flague = flag_structure_coverage_gaps(db, uuid.uuid4(), markdown, hierarchy)

    assert a_flague is False
    assert db.added == []


def test_respecte_une_plage_page_page_end_pas_seulement_la_page_de_depart():
    """Un article qui s'étend sur plusieurs pages (`page_end`) doit couvrir
    TOUTE sa plage, pas seulement sa première page — sinon ses pages
    suivantes ressortiraient à tort comme « non structurées »."""
    db = FakeSession()
    markdown = _markdown({
        1: "Début de l'article, qui continue sur la page suivante ici.",
        2: "Suite et fin de l'article, toujours le même article long.",
    })
    hierarchy = [{"type": "ARTICLE", "number": "1", "page": 1, "page_end": 2, "content": "x", "children": []}]

    a_flague = flag_structure_coverage_gaps(db, uuid.uuid4(), markdown, hierarchy)

    assert a_flague is False
    assert db.added == []


def test_parcourt_les_enfants_de_la_hierarchie_recursivement():
    db = FakeSession()
    markdown = _markdown({1: "Contenu d'un article niché sous un chapitre, substantiel."})
    hierarchy = [{
        "type": "STRUCTURE", "number": "Chapitre I", "page": None, "content": "",
        "children": [{"type": "ARTICLE", "number": "1", "page": 1, "content": "x", "children": []}],
    }]

    a_flague = flag_structure_coverage_gaps(db, uuid.uuid4(), markdown, hierarchy)

    assert a_flague is False
    assert db.added == []


def test_ne_compare_jamais_a_un_total_de_pages_hors_de_la_plage_du_document():
    """Même garde que la Mesure 1 : un acte de 2 pages extrait d'un JO de 50
    ne doit jamais être jugé sur des pages qui appartiennent à d'autres
    actes du même journal."""
    db = FakeSession()
    markdown = _markdown({
        8: "Corps du décret, page 8, substantiel et complet en tous points.",
        9: "Suite et signature du décret, page 9, également substantielle.",
    })
    hierarchy = [{"type": "ARTICLE", "number": "1", "page": 8, "page_end": 9, "content": "x", "children": []}]

    a_flague = flag_structure_coverage_gaps(db, uuid.uuid4(), markdown, hierarchy)

    assert a_flague is False
    assert db.added == []
