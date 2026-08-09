"""mibeko-python#5 — le mobilier de page du Journal officiel (numéro de page,
bandeau, numéro d'édition, date) s'incrustait en pleine phrase dans le contenu
des articles à cheval sur deux pages. 554 articles publiés dans 302 documents,
mesuré en production le 09/08/2026.

Les cas ci-dessous sont tous tirés du corpus réel : les motifs à retirer viennent
d'un inventaire des lignes jouxtant les 26 845 bandeaux des 1 437 markdowns de
`data/pipeline/md/`, et les faux positifs protégés sont des défauts que le filtre
a effectivement produits avant d'être resserré.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.page_furniture import strip_page_furniture  # noqa: E402
from src.extractor.parser import LegalDocumentParser  # noqa: E402


def _lignes(texte):
    return [l for l in texte.split("\n") if l.strip()]


# --------------------------------------------------------------------------
# Ce qui doit être retiré
# --------------------------------------------------------------------------

def test_bloc_complet_au_saut_de_page():
    """Cas nominal du ticket : arrêté n° 4602, JO 42-2025, article 12."""
    texte = (
        "Le bureau du protocole est dirigé et animé par un chef de bureau.\n"
        "1438\n"
        "Journal officiel de la République du Congo\n"
        "N° 42-2025\n"
        "Il est chargé, notamment, de :"
    )
    assert _lignes(strip_page_furniture(texte)) == [
        "Le bureau du protocole est dirigé et animé par un chef de bureau.",
        "Il est chargé, notamment, de :",
    ]


def test_marqueur_de_page_traverse_le_bloc_sans_etre_retire():
    """Forme réelle de `congo-jo-2022-35.md` : le marqueur sépare le texte du
    numéro de page. Il doit être traversé (sinon le numéro survit) mais jamais
    retiré — la citabilité par page en dépend."""
    texte = (
        "Ci-dessus dénommé « Impôt Rwandais »\n"
        "[[MIBEKO_PAGE:4]]\n"
        "1364\n"
        "Journal officiel de la République du Congo\n"
        "N° 35-2022\n"
        "b) Au Congo :"
    )
    resultat = _lignes(strip_page_furniture(texte))
    assert resultat == [
        "Ci-dessus dénommé « Impôt Rwandais »",
        "[[MIBEKO_PAGE:4]]",
        "b) Au Congo :",
    ]


def test_variantes_de_bandeau_et_de_date():
    """Casse libre, ligature « ﬁ » décomposée en « offi ciel », date en plage
    (JO hebdomadaire de 2005), édition spéciale, volume, millésime."""
    for bandeau in (
        "Journal officiel de la République du Congo",
        "Journal Officiel de la République du Congo",
        "JOURNAL OFFICIEL DE LA RÉPUBLIQUE DU CONGO",
        "Journal offi ciel de la République du Congo",
        "Journal officiel de la République du Congo.",
    ):
        assert strip_page_furniture(f"Texte.\n{bandeau}\nSuite.") == "Texte.\nSuite.", bandeau

    for date in (
        "Du jeudi 1er septembre 2022",
        "Du 8 au 14 Avril 2005",
        "Du 23 septembre 2025",
        "De mai 2012",
        "D’octobre 2016",
    ):
        texte = f"Texte.\nJournal officiel de la République du Congo\n{date}\nSuite."
        assert strip_page_furniture(texte) == "Texte.\nSuite.", date

    for mobilier in ("VOLUME XV", "Hors texte", "65e ANNEE - EDITION SPECIALE N° 5",
                     "Edition spéciale N° 5-2025", "N° 35 - 2022"):
        texte = f"Texte.\nJournal officiel de la République du Congo\n{mobilier}\nSuite."
        assert strip_page_furniture(texte) == "Texte.\nSuite.", mobilier


# --------------------------------------------------------------------------
# Ce qui ne doit JAMAIS être touché
# --------------------------------------------------------------------------

def test_formule_d_execution_legitime_intacte():
    """Le faux positif nommé par le ticket. Attesté 2 454 fois dans le corpus
    local — le filtre doit en laisser passer exactement autant."""
    texte = (
        "Art. 5. — Le présent décret sera enregistré, publié au Journal "
        "officiel de la République du Congo et communiqué partout où besoin sera."
    )
    assert strip_page_furniture(texte) == texte


def test_bandeau_prolonge_par_la_phrase_n_est_pas_une_ancre():
    """95 lignes du corpus commencent par le bandeau mais le prolongent : c'est
    la formule d'exécution coupée par la mise en page, pas un en-tête. Si elle
    servait d'ancre, le nombre voisin serait détruit."""
    texte = (
        "sera enregistré, publié au\n"
        "Journal officiel de la République du Congo et commu-\n"
        "niqué partout où besoin sera.\n"
        "1438"
    )
    assert strip_page_furniture(texte) == texte


def test_formule_coupee_avant_son_complement_preservee():
    """Décret n° 2025-273 du 25 juin 2025, art. 2, publié en production : la
    formule est coupée juste avant son complément d'objet, si bien que DEUX
    bandeaux se suivent — le complément puis l'en-tête. Seul le second doit
    partir ; le premier, retiré, laissait l'article sur « …et publié au »."""
    texte = (
        "Le présent décret sera enregistré et publié au\n"
        "Journal officiel de la République du Congo.\n"
        "Journal officiel de la République du Congo\n"
        "Du jeudi 26 juin 2025"
    )
    assert _lignes(strip_page_furniture(texte)) == [
        "Le présent décret sera enregistré et publié au",
        "Journal officiel de la République du Congo.",
    ]


def test_bandeau_apres_phrase_en_cours_reste_du_mobilier():
    """Contre-épreuve : une phrase coupée par un saut de page (sans formule de
    publication) laisse bien l'en-tête au statut de mobilier. 569 lignes du
    corpus local sont dans ce cas — les confondre avec le cas précédent
    ferait survivre le bandeau dans la moitié des documents."""
    texte = (
        "des matières précieuses en vue de participer aux transactions visées à\n"
        "Journal Officiel de la République du Congo\n"
        "Du 8 au 14 Avril 2005\n"
        "l’article 12 du présent code."
    )
    assert _lignes(strip_page_furniture(texte)) == [
        "des matières précieuses en vue de participer aux transactions visées à",
        "l’article 12 du présent code.",
    ]


def test_nombres_seuls_hors_bloc_ancre_preserves():
    """`arrete-n-1606-du-19-juin-2025-m-ikounga` liste des numéros d'arrêtés un
    par ligne. Sans la condition d'ancrage, 26 lignes de contenu réel de ce
    document publié disparaissaient."""
    texte = "Vu l’arrêté\nn°\n1607\ndu 19 juin 2025 ;\nn°\n1608\ndu 19 juin 2025 ;"
    assert strip_page_furniture(texte) == texte


def test_fin_de_phrase_en_minuscule_preservee():
    """« …indice 1270 pour compter » / « du 1er avril 1998. » : le mobilier porte
    la majuscule, la continuation de phrase jamais. Onze occurrences du corpus
    étaient emportées avant que le motif ne devienne sensible à la casse."""
    texte = (
        "Est promu à deux ans, indice 1270 pour compter\n"
        "du 1er avril 1998.\n"
        "Journal Officiel de la République du Congo\n"
        "Catégorie II, échelle 1"
    )
    assert _lignes(strip_page_furniture(texte)) == [
        "Est promu à deux ans, indice 1270 pour compter",
        "du 1er avril 1998.",
        "Catégorie II, échelle 1",
    ]


def test_texte_legitime_colle_au_saut_de_page_preserve():
    """Autour d'un saut de page cohabitent mobilier et contenu : signataires,
    visas, puces. L'expansion s'arrête à la première ligne de texte."""
    texte = (
        "Vu la Constitution ;\n"
        "1438\n"
        "Journal officiel de la République du Congo\n"
        "N° 42-2025\n"
        "Pierre OBA"
    )
    assert _lignes(strip_page_furniture(texte)) == ["Vu la Constitution ;", "Pierre OBA"]


def test_document_sans_bandeau_inchange():
    """Un texte qui n'est pas un Journal officiel traverse le filtre intact,
    numéros isolés compris."""
    texte = "Article 1er : Le taux est fixé à\n1500\nfrancs CFA.\nArticle 2 : Abrogation."
    assert strip_page_furniture(texte) == texte


def test_idempotence_et_texte_vide():
    texte = "Texte.\n1438\nJournal officiel de la République du Congo\nSuite."
    une_passe = strip_page_furniture(texte)
    assert strip_page_furniture(une_passe) == une_passe
    assert strip_page_furniture("") == ""


# --------------------------------------------------------------------------
# Intégration : le filtre agit avant la pose des frontières d'articles
# --------------------------------------------------------------------------

def test_le_parseur_ne_verse_plus_le_bandeau_dans_l_article():
    texte = (
        "Article 12 : Le bureau du protocole est dirigé par un chef de bureau.\n"
        "1438\n"
        "Journal officiel de la République du Congo\n"
        "N° 42-2025\n"
        "Il est chargé, notamment, de la correspondance.\n"
        "Article 13 : Le présent arrêté prend effet à compter de sa signature."
    )
    articles = [n for n in LegalDocumentParser(text_content=texte).parse_hierarchy()
                if n["type"] == "ARTICLE"]
    assert [a["number"] for a in articles] == ["12", "13"]
    assert "Journal officiel" not in articles[0]["content"]
    assert "1438" not in articles[0]["content"]
    # Le contenu utile des deux côtés du saut de page est conservé.
    assert "chef de bureau" in articles[0]["content"]
    assert "correspondance" in articles[0]["content"]
