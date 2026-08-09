"""Tests du routage des tableaux par le parseur (`src/extractor/parser.py`).

Le sujet ici n'est pas la conversion du tableau (cf. `test_tables.py`) mais son
**découpage** : MinerU coupe la ligne quand une cellule est longue, et le
parseur ne lisait que la ligne d'ouverture. Le tableau était alors tronqué *et*
ses lignes suivantes tombaient dans le flux d'articles — 136 tableaux sur 1323
concernés dans le corpus local au 09/08/2026.

Exécutable sans base :  python3 tests/test_parser_tables.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.parser import LegalDocumentParser  # noqa: E402


def _feuilles(noeuds, resultat=None):
    resultat = [] if resultat is None else resultat
    for noeud in noeuds:
        resultat.append(noeud)
        _feuilles(noeud.get("children") or [], resultat)
    return resultat


def _parse(markdown):
    return _feuilles(LegalDocumentParser(text_content=markdown).parse_hierarchy())


def test_tableau_sur_une_ligne():
    noeuds = _parse(
        "ARTICLE 1. — Barème applicable.\n\n"
        "<table><tr><td>Rubrique</td><td>Taux</td></tr><tr><td>A</td><td>5</td></tr></table>\n\n"
        "ARTICLE 2. — Entrée en vigueur.\n"
    )
    types = [n["type"] for n in noeuds]
    assert types.count("TABLEAU") == 1
    assert types.count("ARTICLE") == 2


def test_tableau_multiligne_reassemble_sans_deborder_sur_les_articles():
    markdown = (
        "ARTICLE 1. — Barème applicable.\n\n"
        "<table><tr><td>Rubrique</td><td>Taux\n"
        "applicable au 1er janvier</td></tr><tr><td>A</td>\n"
        "<td>5</td></tr></table>\n\n"
        "ARTICLE 2. — Entrée en vigueur.\n"
    )
    noeuds = _parse(markdown)
    tableaux = [n for n in noeuds if n["type"] == "TABLEAU"]
    articles = [n for n in noeuds if n["type"] == "ARTICLE"]

    assert len(tableaux) == 1
    assert tableaux[0]["content"].rstrip().endswith("</table>")
    assert "applicable au 1er janvier" in tableaux[0]["content"]

    # Le morceau de tableau ne doit pas avoir fui dans un article.
    assert len(articles) == 2
    assert all("<td>" not in (a["content"] or "") for a in articles)
    assert "Entrée en vigueur" in articles[1]["content"]


def test_balise_jamais_refermee_ne_devore_pas_le_document():
    """Garde-fou : sans plafond, une balise ouverte avalerait tout le reste du
    document dans un seul nœud TABLEAU."""
    lignes = "\n".join(f"ligne {i}" for i in range(300))
    noeuds = _parse(f"<table><tr><td>début\n{lignes}\n\nARTICLE 1. — Après le plafond.\n")

    tableaux = [n for n in noeuds if n["type"] == "TABLEAU"]
    assert len(tableaux) == 1
    assert tableaux[0]["content"].count("ligne ") <= 200
    # L'article situé au-delà du plafond reste atteignable.
    assert any(n["type"] == "ARTICLE" for n in noeuds)


def test_page_courante_conservee_pour_la_citabilite():
    noeuds = _parse(
        "[[MIBEKO_PAGE:12]]\n"
        "ARTICLE 1. — Barème.\n"
        "<table><tr><td>Rubrique</td><td>Taux\n"
        "détaillé</td></tr></table>\n"
    )
    tableau = next(n for n in noeuds if n["type"] == "TABLEAU")
    assert tableau["page"] == 12


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"\n{len(tests)} tests passés.")
