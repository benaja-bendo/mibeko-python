"""Déduction déterministe du type_code — le rempart contre les documents invisibles.

La recherche publique fait un INNER JOIN sur document_types : un document publié
sans type_code n'apparaît jamais. La déduction ne doit donc jamais renvoyer None,
et rester rejouable à l'identique (aucun LLM).
"""

import pytest

from src.structuration.typage import deduire_type_code, planifier_backfill


@pytest.mark.parametrize(
    ("nature", "attendu"),
    [
        ("Journal officiel", "JO"),
        ("Code", "CODE"),
        ("Loi", "LOI"),
        ("Décret", "DEC"),
        ("Arrêté", "ARR"),
        ("Ordonnance", "ORD"),
    ],
)
def test_la_nature_du_manifeste_prime(nature, attendu):
    assert deduire_type_code(nature, "titre sans rapport") == attendu


@pytest.mark.parametrize(
    ("titre", "attendu"),
    [
        ("Journal officiel de la République du Congo n° 23", "JO"),
        ("journal officiel n° 26-2025", "JO"),
        ("Loi n° 16 - 2017 du 30 mars 2017", "LOI"),
        ("Décret n° 2014 - 243 du 28 mai 2014", "DEC"),
        ("Décret n° 2017-41 portant forme des statuts", "DEC"),
        ("Arrêté N°3268 Portant création du bureau", "ARR"),
        ("Ordonnance n° 1-2020", "ORD"),
        ("Code du travail", "CODE"),
        ("code de la famille de 1984", "CODE"),
        ("Republique du Congo Constitution 2015", "CONST"),
        ("Acte uniforme portant droit commercial général", "AU"),
        ("Convention collective du commerce", "CONV"),
    ],
)
def test_le_titre_sert_de_repli(titre, attendu):
    assert deduire_type_code(None, titre) == attendu


@pytest.mark.parametrize(
    "titre",
    [
        "Compte rendu du Conseil des Ministres — compte-rendu-cmd-2024-07-03",
        "cahier des charges n° 38-2025",
        "Règlement n° 07/12-UEAC-066-CM-23",
    ],
)
def test_texte_est_le_filet_jamais_none(titre):
    """Un contenu inclassable reste visible : TEXTE, jamais None."""
    assert deduire_type_code(None, titre) == "TEXTE"


def test_sans_rien_le_filet_tient():
    assert deduire_type_code(None, None) == "TEXTE"
    assert deduire_type_code("nature inconnue", "") == "TEXTE"


def test_un_decret_modifiant_une_loi_reste_un_decret():
    """Les motifs sont ancrés en début de titre : le texte cité ne compte pas."""
    assert deduire_type_code(None, "Décret modifiant la loi n° 4-2020") == "DEC"


def test_planifier_backfill_repartit_par_code():
    plan = planifier_backfill(
        [
            ("id-1", "Loi n° 1-2026"),
            ("id-2", "Loi n° 2-2026"),
            ("id-3", "Journal officiel n° 5"),
            ("id-4", "Compte rendu du Conseil des Ministres"),
        ]
    )

    assert sorted(plan) == ["JO", "LOI", "TEXTE"]
    assert [i for i, _ in plan["LOI"]] == ["id-1", "id-2"]
    assert [i for i, _ in plan["JO"]] == ["id-3"]
    assert [i for i, _ in plan["TEXTE"]] == ["id-4"]
