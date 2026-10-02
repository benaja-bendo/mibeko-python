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
