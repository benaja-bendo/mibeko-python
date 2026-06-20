"""
Tests du parser pour la capture du préambule (qualité du signataire, visas,
considérants) — feuille PREAMBULE émise en tête, uniquement si un dispositif suit.

Exécutable sans dépendance externe :  python3 tests/test_parser_preamble.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.parser import LegalDocumentParser  # noqa: E402


def parse(text: str):
    return LegalDocumentParser(text_content=text).parse_hierarchy()


def test_preambule_capture_avec_articles():
    """Un arrêté de JO : visas avant Article 1er → feuille PREAMBULE en tête."""
    text = "\n".join([
        "Arrêté n° 974 du 18 avril 2026 portant modification de l'article 2",
        "La ministre des transports, de l'aviation civile et de la marine marchande,",
        "Vu la Constitution ;",
        "Vu l'ordonnance n° 8-2000 du 23 février 2000 portant création du conseil ;",
        "Vu le décret n° 2013-487 du 26 septembre 2013 portant approbation des statuts ;",
        "Arrête :",
        "Article 1er : L'article 2 de l'arrêté n° 5696 susvisé est modifié comme suit.",
        "Article 2 : Le présent arrêté sera enregistré et publié.",
    ])
    roots = parse(text)

    assert roots[0]["type"] == "PREAMBULE", f"attendu PREAMBULE en tête, obtenu {roots[0]['type']}"
    assert "Vu la Constitution" in roots[0]["content"]
    assert "La ministre des transports" in roots[0]["content"]
    # Les articles suivent, intacts.
    articles = [n for n in roots if n["type"] == "ARTICLE"]
    assert len(articles) == 2, f"attendu 2 articles, obtenu {len(articles)}"
    assert articles[0]["number"].lower().startswith("1")
    # Le préambule est bien le 1er nœud (s'affichera en tête via ordre_affichage).
    assert roots[0]["type"] == "PREAMBULE" and roots[1]["type"] == "ARTICLE"


def test_pas_de_preambule_sans_dispositif():
    """Texte sans article ni structure (proclamation) : AUCUN PREAMBULE.

    On laisse le fallback « texte intégral » de l'appelant gérer ce cas — sinon
    on étiquetterait à tort tout le corps comme un préambule.
    """
    text = "\n".join([
        "Proclamation",
        "La Cour constitutionnelle,",
        "Vu la Constitution ;",
        "Proclame les résultats définitifs de l'élection.",
    ])
    roots = parse(text)

    assert roots == [], f"attendu aucun nœud (fallback appelant), obtenu {roots}"


def test_preambule_avant_structure():
    """Visas avant un TITRE : PREAMBULE en tête, structure préservée dessous."""
    text = "\n".join([
        "Décret n° 2026-1 portant code de la route",
        "Le Président de la République,",
        "Vu la Constitution ;",
        "TITRE I : DISPOSITIONS GÉNÉRALES",
        "Article 1er : Champ d'application du présent code.",
    ])
    roots = parse(text)

    assert roots[0]["type"] == "PREAMBULE"
    assert "Vu la Constitution" in roots[0]["content"]
    assert roots[1]["type"] == "TITRE"
    assert roots[1]["children"] and roots[1]["children"][0]["type"] == "ARTICLE"


def test_aucun_preambule_si_commence_par_article():
    """Document démarrant directement par un article : pas de feuille PREAMBULE."""
    text = "\n".join([
        "Article 1er : Premier contenu.",
        "Article 2 : Second contenu.",
    ])
    roots = parse(text)

    assert all(n["type"] != "PREAMBULE" for n in roots), "aucun PREAMBULE attendu"
    assert [n["type"] for n in roots] == ["ARTICLE", "ARTICLE"]


def test_article_nouveau_distinct_de_execution():
    """Acte modificatif : « Article 2 nouveau » ≠ « Article 2 » → pas de doublon.

    Le qualificatif « nouveau » est capturé dans le numéro pour ne pas entrer en
    collision avec l'article d'exécution de même numéro.
    """
    text = "\n".join([
        "Article premier : L'article 2 de l'arrêté n° 5696 susvisé est modifié ainsi qu'il suit :",
        "Article 2 nouveau : L'antenne d'Oyo assure les missions du conseil.",
        "Article 2 : Le présent arrêté sera enregistré et publié.",
    ])
    roots = parse(text)

    numbers = [n["number"] for n in roots if n["type"] == "ARTICLE"]
    assert numbers == ["premier", "2 nouveau", "2"], f"obtenu {numbers}"
    assert len(numbers) == len(set(numbers)), "aucun doublon de numéro attendu"
    # Le « nouveau : » ne fuit pas dans le contenu de l'article de remplacement.
    nouveau = next(n for n in roots if n["number"] == "2 nouveau")
    assert nouveau["content"].startswith("L'antenne d'Oyo")


def test_signature_isolee_du_dernier_article():
    """La formule « Fait à … » + signataire est isolée en feuille SIGNATURE."""
    text = "\n".join([
        "Article premier : Il est créé une antenne à Mossaka.",
        "Article 2 : Le présent arrêté sera enregistré et publié.",
        "Fait à Brazzaville, le 18 avril 2026",
        "Ingrid Olga Ghislaine EBOUKA-BABACKAS",
    ])
    roots = parse(text)

    assert roots[-1]["type"] == "SIGNATURE", f"dernier nœud = {roots[-1]['type']}"
    assert "Fait à Brazzaville" in roots[-1]["content"]
    assert "EBOUKA-BABACKAS" in roots[-1]["content"]
    # Le dernier article ne contient plus la signature.
    last_article = [n for n in roots if n["type"] == "ARTICLE"][-1]
    assert "Fait à" not in last_article["content"]
    assert "EBOUKA-BABACKAS" not in last_article["content"]


def test_signature_a_la_racine_meme_avec_structure():
    """Acte structuré : la SIGNATURE est à la racine, pas enfant du dernier chapitre."""
    text = "\n".join([
        "Le ministre,",
        "Vu la Constitution ;",
        "Chapitre 1 : Dispositions générales",
        "Article premier : Objet du présent arrêté.",
        "Chapitre 2 : Dispositions finales",
        "Article 2 : Le présent arrêté sera publié.",
        "Fait à Brazzaville, le 18 avril 2026",
        "Pierre OBA",
    ])
    roots = parse(text)

    types = [n["type"] for n in roots]
    assert types == ["PREAMBULE", "CHAPITRE", "CHAPITRE", "SIGNATURE"], f"obtenu {types}"
    # Pas de signature enfant d'un chapitre.
    for node in roots:
        assert all(c["type"] != "SIGNATURE" for c in node.get("children", []))


def test_signature_close_sur_entete_rubrique():
    """Un en-tête de rubrique du JO après la signature ne la pollue pas."""
    text = "\n".join([
        "Article premier : Disposition.",
        "Fait à Brazzaville, le 18 avril 2026",
        "Pierre OBA",
        "MINISTERE DES AFFAIRES FONCIERES ET DU DOMAINE PUBLIC",
        "RECONNAISSANCE DE TERRES",
    ])
    roots = parse(text)

    signature = next(n for n in roots if n["type"] == "SIGNATURE")
    assert "Pierre OBA" in signature["content"]
    assert "MINISTERE" not in signature["content"], "l'en-tête suivant ne doit pas être happé"


def test_page_du_preambule_renseignee():
    """La page d'origine du préambule est tamponnée (citabilité « page N »)."""
    text = "\n".join([
        "[[MIBEKO_PAGE:7]]",
        "Le ministre,",
        "Vu la Constitution ;",
        "Article 1er : Contenu.",
    ])
    roots = parse(text)

    assert roots[0]["type"] == "PREAMBULE"
    assert roots[0]["page"] == 7, f"attendu page 7, obtenu {roots[0]['page']}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {test.__name__} : {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    sys.exit(1 if failures else 0)
