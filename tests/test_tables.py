"""Tests de la normalisation des tableaux MinerU (`src/extractor/tables.py`).

Les fragments HTML viennent du corpus réel — décret budgétaire n° 59-183 du
21 août 1959 et arrêtés miniers de 2026, tels que produits par MinerU. Aucun
n'est inventé.

Ce que ces tests protègent, dans l'ordre d'importance :
  1. **rien ne se perd** : un balisage tronqué ou un tableau vide laisse le
     texte d'origine intact plutôt que de l'escamoter ;
  2. **rien ne sort avec des balises** : c'est l'invariant du corpus ;
  3. **rien n'est deviné en silence** : une géométrie incertaine ou une
     arithmétique fausse produit une anomalie, jamais une correction.

Jumeaux d'affichage (mêmes cas de parsing) :
`mibeko-front/src/shared/lib/tables.test.ts`.

Exécutable sans base :  python3 tests/test_tables.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.tables import (  # noqa: E402
    contains_table_markup,
    linearize_table,
    looks_like_subscription_grid,
    normalize_content,
    parse_html_table,
)

BUDGET_HTML = (
    "<table><tr><td>Chapitres et articles</td><td>NOMENCLATURE</td>"
    "<td>Crédits primitifs</td><td>Crédits supplément.</td><td>Crédits nouveaux</td></tr>"
    "<tr><td>3-2-1</td><td>Assemblée législative (personnel)</td>"
    "<td>37.000.000</td><td>13.000.000</td><td>50.000.000</td></tr>"
    "<tr><td>3-4-1</td><td>Ministères (personnel)</td>"
    "<td>55.805.000</td><td>32.000.000</td><td>87.805.000</td></tr>"
    "<tr><td>4-1-1</td><td>Assemblée législative (matériel)</td>"
    "<td>6.015.000</td><td>700.000</td><td>6.715.000</td></tr></table>"
)

COORDONNEES_HTML = (
    "<table><tr><td>Sommets</td><td>Longitudes</td><td>Latitudes</td></tr>"
    "<tr><td>A</td><td>11° 22&#x27;22, 40&#x27; E</td><td>03° 39&#x27;7, 20&quot; S</td></tr>"
    "<tr><td>B</td><td>11° 32&#x27;45, 60&#x27; E</td><td>03° 39&#x27;7, 20&quot; S</td></tr></table>"
)


def test_parse_reconnait_entete_et_rangees():
    table, anomalies = parse_html_table(BUDGET_HTML)
    assert table is not None
    assert table.headers == [
        "Chapitres et articles",
        "NOMENCLATURE",
        "Crédits primitifs",
        "Crédits supplément.",
        "Crédits nouveaux",
    ]
    assert len(table.rows) == 3
    assert table.rows[0] == [
        "3-2-1",
        "Assemblée législative (personnel)",
        "37.000.000",
        "13.000.000",
        "50.000.000",
    ]
    assert anomalies == []
    print("✓ en-tête et rangées reconnus")


def test_parse_decode_les_entites():
    table, _ = parse_html_table(COORDONNEES_HTML)
    assert table is not None
    assert table.rows[0] == ["A", "11° 22'22, 40' E", "03° 39'7, 20\" S"]
    print("✓ entités HTML décodées")


def test_parse_naplatit_pas_une_premiere_rangee_numerique_en_entete():
    table, _ = parse_html_table(
        "<table><tr><td>3-2-1</td><td>37.000.000</td></tr>"
        "<tr><td>3-4-1</td><td>55.805.000</td></tr></table>"
    )
    assert table is not None
    assert table.headers == []
    assert len(table.rows) == 2
    print("✓ rangée de données non promue en en-tête")


def test_colspan_aplati_en_cellules_vides():
    table, _ = parse_html_table(
        '<table><tr><td>A</td><td>B</td><td>C</td></tr>'
        '<tr><td colspan="2">Total</td><td>9</td></tr></table>'
    )
    assert table is not None
    assert table.rows == [["Total", "", "9"]]
    print("✓ colspan aplati")


def test_colspan_aberrant_compte_pour_un_et_se_signale():
    table, anomalies = parse_html_table('<table><tr><td colspan="9999">X</td></tr></table>')
    assert table is not None
    assert table.rows == [["X"]]
    assert "tableau_colspan_aberrant" in {anomaly.code for anomaly in anomalies}
    print("✓ colspan aberrant compté pour un et signalé")


def test_rowspan_signale_jamais_devine():
    table, anomalies = parse_html_table(
        '<table><tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td></tr></table>'
    )
    assert table is not None
    codes = {anomaly.code for anomaly in anomalies}
    assert "tableau_rowspan" in codes
    print("✓ rowspan signalé sans être deviné")


def test_largeur_irreguliere_signalee():
    _, anomalies = parse_html_table(
        "<table><tr><td>A</td><td>B</td><td>C</td></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr>"
        "<tr><td>4</td><td>5</td></tr></table>"
    )
    assert "tableau_largeur_irreguliere" in {anomaly.code for anomaly in anomalies}
    print("✓ largeur irrégulière signalée")


def test_tableau_sans_rangee_ne_perd_pas_le_texte():
    table, anomalies = parse_html_table("<table></table>")
    assert table is None
    assert {anomaly.code for anomaly in anomalies} == {"tableau_vide"}

    normalise, tables, anomalies = normalize_content("Avant <table></table> après")
    assert "Avant" in normalise and "après" in normalise
    assert tables == []
    print("✓ tableau vide : texte conservé")


def test_balise_non_fermee_ne_perd_pas_le_texte():
    contenu = "<table><tr><td>Rangée sans fermeture"
    normalise, tables, anomalies = normalize_content(contenu)
    assert "Rangée sans fermeture" in normalise
    assert tables == []
    assert "tableau_non_ferme" in {anomaly.code for anomaly in anomalies}
    print("✓ balise non fermée : texte conservé et signalé")


def test_normalize_ne_laisse_aucune_balise():
    normalise, tables, _ = normalize_content(
        f"La zone est définie comme suit :\n{COORDONNEES_HTML}\nFait à Brazzaville."
    )
    assert not contains_table_markup(normalise)
    assert "<td>" not in normalise and "&#x27;" not in normalise
    assert "La zone est définie comme suit :" in normalise
    assert "Fait à Brazzaville." in normalise
    assert "Sommets | Longitudes | Latitudes" in normalise
    assert len(tables) == 1
    print("✓ aucune balise en sortie, texte encadrant conservé")


def test_ancrage_des_lignes():
    normalise, tables, _ = normalize_content(f"Introduction.\n{COORDONNEES_HTML}\nFin.")
    table = tables[0]
    lignes = normalise.split("\n")
    assert lignes[table.line_start : table.line_end] == linearize_table(table).split("\n")
    assert lignes[table.line_start - 1] == "Introduction."
    assert lignes[table.line_end] == "Fin."
    print("✓ bornes de lignes exactes")


def test_ancrage_de_deux_tableaux_identiques():
    normalise, tables, _ = normalize_content(
        f"{COORDONNEES_HTML}\nIntercalaire\n{COORDONNEES_HTML}"
    )
    assert len(tables) == 2
    assert tables[0].line_end <= tables[1].line_start
    assert tables[0].line_start != tables[1].line_start
    print("✓ deux tableaux identiques restent distincts")


def test_normalize_est_idempotent():
    une_passe, tables, _ = normalize_content(f"Texte.\n{BUDGET_HTML}")
    deux_passes, tables_bis, anomalies = normalize_content(une_passe)
    assert deux_passes == une_passe
    assert tables_bis == []
    assert anomalies == []
    assert tables  # la première passe a bien produit un tableau
    print("✓ idempotent : rejouable sans double conversion")


def test_caption_portee_par_le_premier_tableau():
    _, tables, _ = normalize_content(BUDGET_HTML, caption="Crédits ouverts")
    assert tables[0].caption == "Crédits ouverts"
    assert linearize_table(tables[0]).startswith("Crédits ouverts\n")
    print("✓ intitulé rattaché au tableau")


def test_locator_pret_pour_source_locator():
    _, tables, _ = normalize_content(COORDONNEES_HTML)
    locator = tables[0].to_locator()
    assert locator["headers"] == ["Sommets", "Longitudes", "Latitudes"]
    assert locator["line_start"] == 0
    assert locator["html_source"].startswith("<table>")
    print("✓ forme stockée conforme au contrat d'API")


# ---------------------------------------------------------------------------
# Contrôles arithmétiques — le cœur du contrôle qualité
# ---------------------------------------------------------------------------


def _budget_reel():
    """Le tableau du décret n° 59-183, dans son état publié en production.

    Rangée « Agriculture » : 4.335.000 + 255.000 = 4.590.000, or la colonne
    « crédits nouveaux » annonce 4.600.000. La dernière rangée totalise les
    précédentes et en porte le contrecoup.
    """
    lignes = [
        ("3-2-1", "Assemblée législative (personnel)", "37.000.000", "13.000.000", "50.000.000"),
        ("3-4-1", "Ministères (personnel)", "55.805.000", "32.000.000", "87.805.000"),
        ("4-1-1", "Assemblée législative (matériel)", "6.015.000", "700.000", "6.715.000"),
        ("4-2-1", "Ministères (matériel)", "20.862.000", "6.500.000", "27.362.000"),
        ("10-1-1", "Agriculture", "4.335.000", "255.000", "4.600.000"),  # ← fautive
        ("10-4-1", "Élevage", "13.615.000", "110.000", "13.725.000"),
        ("12-4-1", "Affaires économiques", "690.000", "85.000", "775.000"),
        ("13-4-1", "Service santé A.M.A.", "115.730.000", "7.500.000", "123.230.000"),
    ]
    entete = (
        "<tr><td>Chapitres et articles</td><td>NOMENCLATURE</td><td>Crédits primitifs</td>"
        "<td>Crédits supplément.</td><td>Crédits nouveaux</td></tr>"
    )
    corps = "".join(
        "<tr>" + "".join(f"<td>{cellule}</td>" for cellule in ligne) + "</tr>" for ligne in lignes
    )
    return f"<table>{entete}{corps}</table>"


def test_somme_en_ligne_designe_la_rangee_fautive():
    _, anomalies = parse_html_table(_budget_reel())
    somme = [a for a in anomalies if a.code == "tableau_somme_incoherente"]
    assert len(somme) == 1, [a.code for a in anomalies]
    # La rangée « Agriculture » est la 5e du corps : le message doit la nommer
    # et donner le montant attendu, sinon l'éditeur relit tout le tableau.
    assert "rangée 5" in somme[0].message
    assert "4 590 000" in somme[0].message
    assert "4 600 000" in somme[0].message
    print("✓ rangée fautive désignée avec le montant attendu")


def test_somme_en_ligne_se_tait_sur_un_tableau_juste():
    _, anomalies = parse_html_table(BUDGET_HTML)
    assert [a for a in anomalies if a.code.startswith("tableau_somme")] == []
    print("✓ aucun faux positif sur un tableau juste")


def test_rangee_de_totaux_incoherente_signalee():
    corps = "".join(
        f"<tr><td>Chapitre {i}</td><td>{i * 1000}</td><td>{i * 2000}</td></tr>"
        for i in range(1, 6)
    )
    # Totaux attendus : 15 000 et 30 000. On en fausse un de 10 (0,07 %).
    totaux = "<tr><td>Totaux</td><td>15000</td><td>30010</td></tr>"
    _, anomalies = parse_html_table(f"<table>{corps}{totaux}</table>")
    total = [a for a in anomalies if a.code == "tableau_total_incoherent"]
    assert len(total) == 1, [a.code for a in anomalies]
    assert "30 010" in total[0].message and "30 000" in total[0].message
    print("✓ rangée de totaux fausse signalée avec l'écart")


def test_derniere_rangee_ordinaire_nest_pas_prise_pour_un_total():
    corps = "".join(
        f"<tr><td>Chapitre {i}</td><td>{i * 1000}</td><td>{i * 2000}</td></tr>"
        for i in range(1, 7)
    )
    _, anomalies = parse_html_table(f"<table>{corps}</table>")
    assert [a for a in anomalies if a.code == "tableau_total_incoherent"] == []
    print("✓ dernière rangée ordinaire non prise pour un total")


def test_pas_de_controle_sur_un_tableau_non_numerique():
    _, anomalies = parse_html_table(COORDONNEES_HTML)
    assert [a for a in anomalies if a.code.startswith("tableau_somme")] == []
    assert [a for a in anomalies if a.code.startswith("tableau_total")] == []
    print("✓ tableau de coordonnées épargné par l'arithmétique")


def test_grille_dabonnement_reperee_comme_faux_article():
    grille, _ = parse_html_table(
        "<table><tr><td>DESTINATIONS</td><td>ABONNEMENTS</td><td>1 AN</td><td>6 MOIS</td></tr>"
        "<tr><td>Journal officiel — Congo</td><td>Annonces</td><td>24.000</td><td>12.000</td></tr>"
        "<tr><td>Étranger</td><td>Tarif numéro</td><td>38.000</td><td>19.000</td></tr></table>"
    )
    assert grille is not None
    assert looks_like_subscription_grid(grille) is True

    juridique, _ = parse_html_table(BUDGET_HTML)
    assert looks_like_subscription_grid(juridique) is False

    # Cas réel du JO n° 29-1963 : l'OCR a éclaté « ABONNEMENTS » en
    # « ABON NÉM EN T S ». Un motif littéral le manquerait.
    abime, _ = parse_html_table(
        "<table><tr><td>DESTINATION</td><td>ABON NÉM EN T S</td><td>NUMERO</td></tr>"
        "<tr><td>1 AN</td><td>6 MOIS</td><td></td></tr>"
        "<tr><td>Etats de l'ex-A.E.F.</td><td>5.065</td><td>215</td></tr></table>"
    )
    assert looks_like_subscription_grid(abime) is True

    # Un tableau juridique peut parler de destinations et de tarifs sans être
    # un ours : sans durée d'abonnement, on ne conclut pas.
    douane, _ = parse_html_table(
        "<table><tr><td>Destination</td><td>Tarif</td><td>Numéro</td></tr>"
        "<tr><td>Pointe-Noire</td><td>12.000</td><td>4</td></tr>"
        "<tr><td>Ouesso</td><td>18.000</td><td>5</td></tr></table>"
    )
    assert looks_like_subscription_grid(douane) is False
    print("✓ ours du JO distingué d'un tableau juridique, OCR abîmé compris")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(tests)} tests passés.")
