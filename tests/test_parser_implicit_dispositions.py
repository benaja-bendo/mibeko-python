"""Régressions : un acte structuré peut ne contenir aucun en-tête « Article ».

Le corps sous TITRE/SECTION doit alors devenir des feuilles DISPOSITION_N ;
une signature par délégation et ses notes restent des feuilles distinctes.
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


def test_structured_notice_without_articles_keeps_all_content():
    text = "\n".join([
        "[[MIBEKO_PAGE:41]]",
        "Avis n° 344 relatif au règlement financier des marchandises importées.",
        "Tel est l'objet du présent avis, qui abroge l'Avis n° 197.",
        "TITRE PREMIER",
        "PROCÉDURE DE LA LICENCE D'IMPORTATION",
        "Section I. — Régime général.",
        "[[MIBEKO_PAGE:42]]",
        "I. — Opérations financières autorisées avant l'expédition des marchandises.",
        "1° Après visa de la licence, une couverture de change peut être constituée.",
        "II. — Opérations financières autorisées à partir de l'expédition.",
        "Section II. — Régime particulier.",
        "[[MIBEKO_PAGE:43]]",
        "Les couvertures de change peuvent être effectuées dans les conditions précisées.",
        "TITRE II : DISPOSITIONS PARTICULIÈRES",
        "La période maximum est portée à six mois.",
        "Pour le Directeur général :",
        "Le Directeur, A. SALPHATI.",
        "(1) La justification résulte des derniers titres de transport.",
        "Une lettre de voiture peut être présentée.",
    ])

    hierarchy = LegalDocumentParser(text_content=text).parse_hierarchy()
    nodes = _flatten(hierarchy)
    dispositions = [node for node in nodes if node["type"] == "DISPOSITION"]

    assert [node["number"] for node in dispositions] == [
        "DISPOSITION_1",
        "DISPOSITION_2",
        "DISPOSITION_3",
    ]
    assert dispositions[0]["page"] == 42
    assert dispositions[0]["page_end"] == 42
    assert dispositions[1]["page"] == 43
    assert dispositions[2]["content"] == "La période maximum est portée à six mois."
    assert "Après visa de la licence" in dispositions[0]["content"]
    assert "Les couvertures de change" in dispositions[1]["content"]

    signature = next(node for node in nodes if node["type"] == "SIGNATURE")
    assert signature["content"] == "Pour le Directeur général :\nLe Directeur, A. SALPHATI."

    note = next(node for node in nodes if node["type"] == "NOTE")
    assert note["number"] == "NOTE_1"
    assert "derniers titres de transport" in note["content"]
    assert "lettre de voiture" in note["content"]

    retained = "\n".join(node.get("content", "") for node in nodes)
    for fragment in [
        "abroge l'Avis n° 197",
        "Après visa de la licence",
        "Les couvertures de change",
        "La période maximum est portée à six mois",
        "A. SALPHATI",
        "derniers titres de transport",
    ]:
        assert fragment in retained


def test_footnote_marker_inside_article_is_not_reclassified():
    hierarchy = LegalDocumentParser(
        text_content="Article 1er : Une condition s'applique.\n(1) Cette précision appartient à l'article."
    ).parse_hierarchy()

    assert [node["type"] for node in hierarchy] == ["ARTICLE"]
    assert "(1) Cette précision" in hierarchy[0]["content"]
