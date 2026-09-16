"""Tests de `flag_page_coverage_gaps` (mibeko-python#24, Mesure 1 § 3.5 du
plan « boîte de réception ») — DB fake (aucun réseau, aucune Postgres
réelle), même convention que `test_ingest_hierarchy_doublon_flags.py` : la
fonction ne fait qu'un `.filter(...).delete(...)` (purge) puis un `.add(...)`
conditionnel, rien qui exige une vraie session SQLAlchemy.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.services.ingestion import flag_page_coverage_gaps  # noqa: E402


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
    """Construit un markdown à marqueurs de page à partir de {numéro: contenu}."""
    lignes = []
    for numero in sorted(pages):
        lignes.append(f"[[MIBEKO_PAGE:{numero}]]")
        lignes.append(pages[numero])
    return "\n".join(lignes)


def test_flag_page_coverage_gaps_signale_une_page_vide_dans_la_plage_du_document():
    db = FakeSession()
    markdown = _markdown({
        1: "Contenu substantiel de la première page, largement suffisant.",
        2: "",  # page vide : perdue à l'extraction
        3: "Contenu substantiel de la troisième page, largement suffisant.",
    })

    a_flague = flag_page_coverage_gaps(db, uuid.uuid4(), markdown)

    assert a_flague is True
    assert len(db.added) == 1
    flag = db.added[0]
    assert flag.type_probleme == "couverture_pages_incomplete"
    assert flag.severity == "warning"
    assert "page" in flag.description.lower() and "2" in flag.description


def test_flag_page_coverage_gaps_ne_signale_rien_sur_un_document_sain():
    db = FakeSession()
    markdown = _markdown({
        1: "Contenu substantiel de la première page, largement suffisant.",
        2: "Contenu substantiel de la deuxième page, largement suffisant.",
    })

    a_flague = flag_page_coverage_gaps(db, uuid.uuid4(), markdown)

    assert a_flague is False
    assert db.added == []


def test_flag_page_coverage_gaps_ignore_un_markdown_sans_aucun_marqueur_de_page():
    db = FakeSession()

    a_flague = flag_page_coverage_gaps(db, uuid.uuid4(), "Un texte quelconque, sans marqueur MIBEKO_PAGE.")

    assert a_flague is False
    assert db.added == []


def test_flag_page_coverage_gaps_ne_compare_jamais_a_un_total_de_pages_hors_de_la_plage_du_document():
    """Régression directe du défaut trouvé le 14/09/2026 (§ 3.5) : un acte de
    2 pages extrait d'un Journal officiel de 50 ne doit JAMAIS être comparé
    aux pages des AUTRES actes du même journal — seule la plage propre à ce
    document (ici pages 8-9) compte, jamais 1-50."""
    db = FakeSession()
    # Fragment d'acte : seules les pages 8 et 9 lui appartiennent, toutes deux
    # couvertes. Aucune connaissance ici des pages 1-7/10-50 du JO entier.
    markdown = _markdown({
        8: "Corps complet du décret, largement suffisant en substance.",
        9: "Suite et signature du décret, également substantielle.",
    })

    a_flague = flag_page_coverage_gaps(db, uuid.uuid4(), markdown)

    assert a_flague is False
    assert db.added == []


def test_flag_page_coverage_gaps_detecte_une_page_manquante_au_milieu_de_la_plage():
    """Une page dont le marqueur n'apparaît pas DU TOUT (pas seulement vide)
    entre deux marqueurs présents doit aussi être détectée."""
    db = FakeSession()
    markdown = _markdown({
        8: "Corps complet du décret, largement suffisant en substance.",
        # page 9 absente du tout
        10: "Signature du décret, également substantielle en contenu.",
    })

    a_flague = flag_page_coverage_gaps(db, uuid.uuid4(), markdown)

    assert a_flague is True
    assert "9" in db.added[0].description
