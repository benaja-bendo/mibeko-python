"""Date de parution d'un JO lue dans son ours (mibeko-python#39).

Extraits réels, relevés le 01/10/2026 dans `data/pipeline/md/sgg-jo/` : Mistral
n'avait donné aucune date de parution pour ces numéros, l'ours la porte.
"""

import datetime

import pytest

from src.structuration.journals import date_parution_depuis_ours


@pytest.mark.parametrize(
    "ours, attendu",
    [
        ("68e ANNEE - EDITION SPECIALE N°6 Du lundi 20 Avril 2026", datetime.date(2026, 4, 20)),
        ("68e ANNEE-EDITION SPECIALE N° 4 Du Jeudi 26 février 2026", datetime.date(2026, 2, 26)),
        ("68e ANNEE - EDITION SPECIALE N°1 Du 5 janvier 2026", datetime.date(2026, 1, 5)),
        ("65e ANNEE - EDITION SPECIALE N° 1 Du vendredi 26 janvier 2024", datetime.date(2024, 1, 26)),
        ("64e ANNEE - EDITION SPECIALE N° 12 Du 26 décembre 2022", datetime.date(2022, 12, 26)),
        ("57e ANNEE - EDITION SPECIALE N° 2 Du 25 février 2015", datetime.date(2015, 2, 25)),
        # Numéro ordinaire : le n° 23 du 4 juin 2026 (dashboard#218).
        ("SOMMAIRE RÉPUBLIQUE DU CONGO Unité - Travail - Progrès 68e ANNEE - N° 23 Jeudi 4 juin 2026",
         datetime.date(2026, 6, 4)),
        ("51e ANNEE - EDITION SPECIALE N° 4 Du 30 décémbre 2009", datetime.date(2009, 12, 30)),
    ],
)
def test_lit_la_date_de_l_ours(ours, attendu):
    texte = f"[[MIBEKO_PAGE:1]]\n# JOURNAL OFFICIEL\n{ours}\n\nPARTIE OFFICIELLE"
    assert date_parution_depuis_ours(texte) == attendu


def test_le_numero_dans_l_ours_n_est_pas_pris_pour_le_jour():
    """« N° 23 Jeudi 4 juin » : le 23 est le numéro, pas le jour."""
    assert date_parution_depuis_ours("68e ANNEE - N° 23 Jeudi 4 juin 2026") == datetime.date(2026, 6, 4)


def test_refuse_une_annee_incoherente_avec_le_rang():
    """La 68e année est 2026 : une date de 2019 sous « 68e ANNEE » est un faux
    appariement, pas une date de parution."""
    assert date_parution_depuis_ours("68e ANNEE - N° 3 … loi du 12 mars 2019") is None


def test_sans_ours_aucune_date_n_est_inventee():
    assert date_parution_depuis_ours("Loi n° 1-2026 du 18 avril 2026 portant code minier") is None
    assert date_parution_depuis_ours("") is None


def test_ne_lit_que_le_debut_du_numero():
    """L'ours est en tête : une mention lointaine (citation d'un autre JO dans
    le corps du texte) ne doit pas servir de date de parution."""
    loin = "x" * 9000 + " 68e ANNEE - N° 5 Du 31 mars 2026"
    assert date_parution_depuis_ours(loin) is None
