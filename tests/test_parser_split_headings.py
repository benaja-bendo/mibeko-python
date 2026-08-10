"""Pilote de redécoupage d'un JO spécial (2010-02) en prod, 10/08/2026 : MinerU
a rendu le titre de l'article 61 un mot par ligne —

    Article
    61
    :
    Rémunération
    perçue
    par
    le
    Concessionnaire

— alors que le même document rend l'article 62 sur une seule ligne quelques
dizaines de lignes plus loin (« Article 62 : Rémunération due au Concédant »).
`ARTICLE_PATTERN` exige un numéro sur la même ligne que « Article » : le mot
seul ne matche jamais. Avant ce correctif, tout le contenu de l'article 61
(sous-clauses 61.1 à 61.8, ~5000 caractères sur les redevances aéroportuaires)
restait rattaché à l'article 60 précédent — pas perdu, juste mal étiqueté — et
le détecteur de curation signalait « article 61 manquant » en blocking.

Cas réel exact : document dev id 48706146-1354-47ba-a544-7c20b078c9b4 (Décret
n° 2010-523 du 14 juillet 2010, corpus prod).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.parser import (  # noqa: E402
    LegalDocumentParser,
    _rejoin_split_article_headings,
)


def _articles(hierarchy):
    return [n for n in hierarchy if n["type"] == "ARTICLE"]


# --- _rejoin_split_article_headings, cas réel --------------------------------

def test_recolle_titre_eclate_un_mot_par_ligne():
    """Cas réel exact (Décret n° 2010-523, prod)."""
    texte = (
        "60.2. deuxième phrase de l'article précédent.\n"
        "Article \n61 \n: \nRémunération \nperçue \npar \nle\nConcessionnaire\n"
        "Le Concessionnaire perçoit des redevances, dites Redevances "
        "Aéroportuaires liées au fonctionnement.\n"
    )
    recolle = _rejoin_split_article_headings(texte)
    lignes = recolle.split("\n")

    assert "Article 61 : Rémunération perçue par le Concessionnaire" in lignes
    # La phrase de corps qui suit n'est pas avalée par la fusion.
    assert any(l.startswith("Le Concessionnaire perçoit des redevances") for l in lignes)


def test_recolle_titre_court_deux_fragments():
    """Cas minimal : deux fragments seulement (numéro puis rien d'autre)."""
    texte = "Article \n42 \nLe titulaire doit garantir l'installation.\n"
    recolle = _rejoin_split_article_headings(texte)
    assert "Article 42" in recolle.split("\n")[0]


# --- Garde-fou : pas de sur-fusion sur un « Article » isolé sans suite valide -

def test_ne_fusionne_pas_si_aucune_combinaison_ne_matche():
    """« Article » seul suivi de prose normale (pas de numéro reconnaissable
    dans les fragments courts qui suivent) : les lignes d'origine restent
    intactes, rien n'est perdu ni inventé."""
    texte = "Article \nscientifique publié dans une revue à comité de lecture.\n"
    recolle = _rejoin_split_article_headings(texte)
    assert recolle == texte


def test_ne_fusionne_pas_au_dela_du_plafond_de_fragments():
    """Une ligne « Article » isolée suivie d'une longue série de mots courts
    qui ne forme jamais un en-tête valide (aucun numéro) : abandon propre,
    pas de fusion sauvage sur des dizaines de lignes."""
    fragments = ["Article"] + [f"mot{i}" for i in range(30)]
    texte = "\n".join(fragments) + "\n"
    recolle = _rejoin_split_article_headings(texte)
    assert recolle == texte


def test_ne_touche_pas_un_texte_sans_titre_eclate():
    texte = "Article 60 : Equilibre Financier et Equilibre Economique.\n60.1. Contenu normal.\n"
    assert _rejoin_split_article_headings(texte) == texte


def test_texte_vide():
    assert _rejoin_split_article_headings("") == ""


# --- Bout en bout : parse_hierarchy sépare bien 60/61/62 au lieu de fusionner 61 dans 60 --

def test_parse_hierarchy_separe_article_a_titre_eclate():
    texte = (
        "Article 60 : Equilibre Financier et Equilibre Economique de la Concession\n"
        "60.1. Le Concessionnaire doit gérer la Concession de façon à en assurer l'équilibre.\n"
        "Article \n61 \n: \nRémunération \nperçue \npar \nle\nConcessionnaire\n"
        "Le Concessionnaire perçoit des redevances, dites Redevances Aéroportuaires.\n"
        "61.1. Dispositions particulières applicables aux Redevances Aéroportuaires.\n"
        "Article 62 : Rémunération due au Concédant\n"
        "62.1. Le Concessionnaire occupe le domaine de l'Etat.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]

    assert numeros == ["60", "61", "62"]
    art_61 = articles[numeros.index("61")]
    assert "Redevances Aéroportuaires" in art_61["content"]
    assert "61.1" in art_61["content"]
    # Le contenu de 61 ne doit plus traîner dans celui de 60.
    art_60 = articles[numeros.index("60")]
    assert "Rémunération perçue par le Concessionnaire" not in art_60["content"]
