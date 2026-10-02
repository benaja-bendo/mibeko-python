"""Régressions du parseur relevées sur la loi n° 1-2026 (code minier), le 02/10/2026.

Le brouillon chargé en production comptait 383 lignes pour 317 articles : 67
feuilles DISPOSITION_N (suites d'intitulés prises pour des articles), les articles
114 et 116 absents, un faux article sans numéro. Chaque test reprend un extrait
réel du markdown (mibeko-python#45). Rejoué sur les 1 437 markdown du corpus, le
parseur conserve 97,1 % du texte contre 89,3 % avant ces correctifs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.parser import LegalDocumentParser  # noqa: E402


def _flatten(nodes):
    flattened = []
    for node in nodes:
        flattened.append(node)
        flattened.extend(_flatten(node.get("children", [])))
    return flattened


def _parse(*lines):
    return _flatten(LegalDocumentParser(text_content="\n".join(lines)).parse_hierarchy())


def _numbers(nodes, kind):
    return [n["number"] for n in nodes if n["type"] == kind]


def _titles(nodes, kind):
    return [n["title"] for n in nodes if n["type"] == kind]


# ---------------------------------------------------------------------------
# Espace de largeur nulle devant « Article N » (articles 114 et 116)
# ---------------------------------------------------------------------------


def test_un_espace_de_largeur_nulle_n_empeche_pas_de_reconnaitre_un_article():
    nodes = _parse(
        "Article 113 : La production minière est répartie entre l’Etat et le contractant.",
        "​Article 114 : Le contrat de partage de production fixe les modalités.",
        "Article 115 : Les règles sont fixées par décret.",
    )

    assert _numbers(nodes, "ARTICLE") == ["113", "114", "115"]


def test_les_caracteres_invisibles_ne_restent_pas_dans_le_contenu():
    nodes = _parse(
        "Article 116 : Pour chaque permis, le profit-mine est partagé.",
        "​Toutefois, la part dans le profit-mine pour une année est fixée.",
    )

    contenu = next(n["content"] for n in nodes if n["type"] == "ARTICLE")
    assert "​" not in contenu
    assert "Toutefois" in contenu


# ---------------------------------------------------------------------------
# Intitulé de division écrit sur plusieurs lignes
# ---------------------------------------------------------------------------


def test_la_suite_en_minuscule_d_un_intitule_le_prolonge():
    nodes = _parse(
        "TITRE I : DISPOSITIONS GENERALES",
        "Chapitre 1 : De l’objet, du champ",
        "d’application et des définitions",
        "Article premier : La présente loi régit les activités minières.",
    )

    assert _titles(nodes, "CHAPITRE") == ["De l’objet, du champ d’application et des définitions"]
    assert _numbers(nodes, "DISPOSITION") == []


def test_un_intitule_en_capitales_se_prolonge_et_recolle_les_mots_coupes():
    """Quatre lignes réelles du TITRE II, dont deux mots coupés par un tiret
    conditionnel : « PRIN » + « CIPES », « AUTORISA » + « TIONS »."""
    nodes = _parse(
        "TITRE II\xa0:  DES PERIMETRES MINIERS, DES PRIN\xad",
        "CIPES, DES TITRES MINIERS, DES AUTORISA\xad",
        "TIONS DIVERSES ET DU CONTRAT DE PARTAGE ",
        "DE PRODUCTION",
        "Chapitre 1 : Des périmètres miniers",
        "Article 20 : Texte.",
    )

    assert _titles(nodes, "TITRE") == [
        "DES PERIMETRES MINIERS, DES PRINCIPES, DES TITRES MINIERS, "
        "DES AUTORISATIONS DIVERSES ET DU CONTRAT DE PARTAGE DE PRODUCTION"
    ]
    assert _numbers(nodes, "DISPOSITION") == []


def test_un_intitule_dont_le_titre_est_sur_la_ligne_suivante_se_prolonge_aussi():
    nodes = _parse(
        "TITRE II.",
        "DU CONTRAT DE",
        "TRAVAIL ET DE L’EMPLOI",
        "Article 5 : Texte.",
    )

    assert _titles(nodes, "TITRE") == ["DU CONTRAT DE TRAVAIL ET DE L’EMPLOI"]


def test_un_corps_de_texte_sous_un_intitule_n_est_jamais_avale():
    """Un paragraphe qui commence par une majuscule et une phrase en casse mixte
    reste un corps de texte (feuille DISPOSITION), pas une suite d'intitulé."""
    nodes = _parse(
        "TITRE I : CHAMP D’APPLICATION",
        "Les dispositions du présent avis s’appliquent à tous les opérateurs.",
    )

    assert _titles(nodes, "TITRE") == ["CHAMP D’APPLICATION"]
    assert _numbers(nodes, "DISPOSITION") == ["DISPOSITION_1"]


def test_une_ligne_en_capitales_ne_prolonge_pas_un_intitule_en_casse_mixte():
    nodes = _parse(
        "Chapitre 1 : De l’objet",
        "DES ORGANES DE CONTROLE",
        "Article 1 : Texte.",
    )

    assert _titles(nodes, "CHAPITRE") == ["De l’objet"]
    assert _numbers(nodes, "DISPOSITION") == ["DISPOSITION_1"]


def test_le_prolongement_s_arrete_apres_quatre_lignes():
    nodes = _parse(
        "Chapitre 1 : De l’objet",
        "a",
        "b",
        "c",
        "d",
        "e",
        "Article 1 : Texte.",
    )

    assert _titles(nodes, "CHAPITRE") == ["De l’objet a b c d"]
    assert _numbers(nodes, "DISPOSITION") == ["DISPOSITION_1"]


def test_un_intitule_deja_long_n_est_pas_prolonge_par_une_phrase():
    """Un paragraphe numéroté lu comme un intitulé (accord de prêt) ne reçoit pas
    ses lignes suivantes : le titre recollé ne dépasse jamais 160 caractères."""
    premiere = "Section 2.01. " + "L’Emprunteur exécute le Projet avec la diligence nécessaire, " * 2
    nodes = _parse(premiere, "et selon les méthodes administratives appropriées.", "Article 1 : Texte.")

    assert len(_titles(nodes, "SECTION")[0]) < 160
    assert "méthodes administratives" not in _titles(nodes, "SECTION")[0]


def test_la_suite_d_un_intitule_n_entre_pas_dans_le_contenu_d_un_article_ouvert():
    nodes = _parse(
        "Chapitre 1 : Des définitions",
        "Article 1 : Au sens de la présente loi, on entend par",
        "substance minérale toute substance naturelle.",
    )

    assert _titles(nodes, "CHAPITRE") == ["Des définitions"]
    assert "substance minérale" in next(n["content"] for n in nodes if n["type"] == "ARTICLE")


# ---------------------------------------------------------------------------
# « article. » replié en début de ligne n'est pas un en-tête d'article
# ---------------------------------------------------------------------------


def test_le_mot_article_en_fin_de_phrase_n_ouvre_pas_un_faux_article():
    nodes = _parse(
        "Article 80 : La stabilisation est garantie dans les conditions prévues par le présent",
        "article.",
        "Pendant la durée de validité d’un permis minier, les taux sont stables.",
    )

    assert _numbers(nodes, "ARTICLE") == ["80"]
    contenu = nodes[0]["content"]
    assert "présent\narticle." in contenu or "présent article." in contenu
    assert "Pendant la durée de validité" in contenu


def test_un_en_tete_sans_numero_a_majuscule_reste_un_article():
    """« Article : Le titulaire… » (numéro perdu à l'OCR) reste reconnu."""
    nodes = _parse(
        "Article 3 : Premier texte.",
        "Article : Le titulaire du permis est tenu de déclarer.",
    )

    assert len(_numbers(nodes, "ARTICLE")) == 2


# ---------------------------------------------------------------------------
# Un mot de prose n'est pas un intitulé : « partiel », « titre d'exploitation »
# ---------------------------------------------------------------------------


def test_partiel_et_titre_d_exploitation_en_debut_de_ligne_n_ouvrent_aucune_division():
    nodes = _parse(
        "Article 74 : Le requérant cessionnaire",
        "partiel",
        "d’un permis d’exploitation doit présenter l’acte de cession.",
        "Article 132 : L’acte d’hypothèque portant sur le",
        "titre d’exploitation doit indiquer la durée de validité.",
        "Article 133 : Suite.",
    )

    assert [n for n in nodes if n["type"] not in ("ARTICLE",)] == []
    assert _numbers(nodes, "ARTICLE") == ["74", "132", "133"]
    assert "titre d’exploitation doit indiquer" in nodes[1]["content"]


def test_un_vrai_intitule_en_minuscules_avec_titre_en_capitales_est_conserve():
    nodes = _parse(
        "section 1 OPERATION PREVOL ET DEPART",
        "Article 1 : Texte.",
    )

    assert _numbers(nodes, "SECTION") == ["1"]
    assert _titles(nodes, "SECTION") == ["OPERATION PREVOL ET DEPART"]


def test_un_renvoi_en_minuscules_n_ouvre_pas_de_division():
    nodes = _parse(
        "Article 12 : Les éléments sont précisés par",
        "section 8103, nature 6651, type 1.",
        "Article 13 : Suite.",
    )

    assert [n for n in nodes if n["type"] == "SECTION"] == []


# ---------------------------------------------------------------------------
# Aucune ligne n'est jetée en silence
# ---------------------------------------------------------------------------


def test_un_texte_revenu_apres_un_tableau_sans_division_n_est_pas_perdu():
    """Sans division ouverte, une ligne hors article n'avait aucune branche pour
    la recevoir : elle disparaissait (6 545 lignes d'une annexe du JO 5-2025)."""
    nodes = _parse(
        "Article 1 : Premier texte.",
        "<table><tr><td>x</td></tr></table>",
        "Texte revenu après le tableau.",
        "Article 2 : Suite.",
    )

    feuilles = [n for n in nodes if n["type"] == "DISPOSITION"]
    assert len(feuilles) == 1
    assert "Texte revenu après le tableau." in feuilles[0]["content"]
    assert _numbers(nodes, "ARTICLE") == ["1", "2"]
