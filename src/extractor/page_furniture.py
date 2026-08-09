"""Retrait du mobilier de page d'un Journal officiel (en-tête et pied de page
réimprimés à chaque saut de page) avant la pose des frontières d'articles.

POURQUOI — audit du 08/08/2026 : au point exact où le PDF change de page, la
chaîne d'extraction verse dans le texte de l'article le pied de page et l'en-tête
du JO, en pleine phrase :

    Le bureau du protocole est dirigé et animé par un chef de bureau.
    1438
    Journal officiel de la République du Congo
    N° 42-2025
    Il est chargé, notamment, de : …

Mesuré en production le 09/08/2026 : 554 articles publiés dans 302 documents.

PRINCIPE — deux conditions cumulatives, jamais une seule
--------------------------------------------------------
Une ligne n'est retirée que si (1) elle appartient au vocabulaire du mobilier de
page ET (2) elle est rattachée à un **bloc ancré** par un bandeau certain. La
seconde condition n'est pas de la prudence décorative, elle est ce qui rend le
filtre sûr — vérifié sur la production :

- `arrete-n-1606-du-19-juin-2025-m-ikounga` liste des numéros d'arrêtés un par
  ligne (« n° / 1607 / du »). Un filtre sur le seul motif « nombre seul »
  détruirait 26 lignes de contenu réel dans ce document.
- Autour des sauts de page cohabitent du mobilier et du texte légitime :
  « Pierre OBA », « Denis SASSOU-N'GUESSO », « Vu la Constitution ; », des puces
  « - ». Un filtre sur la seule proximité du bandeau les emporterait.

L'ancre elle-même est STRICTE : la ligne ne doit rien porter après « du Congo ».
Sur les 1 437 markdowns du corpus local, 95 lignes commencent par le bandeau mais
le prolongent (« …du Congo et commu- », « …du Congo selon la ») : c'est la formule
d'exécution légitime coupée par la mise en page, jamais un en-tête. Et sur les
26 845 bandeaux stricts du même corpus, **un seul** est suivi d'une continuation
de phrase — le cas « formule légitime seule sur sa ligne » n'existe pratiquement
pas, alors que le mobilier, lui, est massif.

Les marqueurs `[[MIBEKO_PAGE:N]]` sont TRANSPARENTS : traversés lors de
l'expansion du bloc, jamais retirés (la citabilité par page en dépend).
"""

import re
from typing import List, Set

# --- Ancre : bandeau du JO, seul sur sa ligne -------------------------------
# Tolérances tirées du corpus réel : casse libre (« Journal Officiel »,
# « JOURNAL OFFICIEL » — 11 297 occurrences), ligature « ﬁ » décomposée en
# « offi ciel » (constatée en production sur l'édition spéciale n° 5-2025), et
# doublement « offiiciel ». Le `$` après « du Congo » est le garde-fou central :
# sans lui, la formule « publié au Journal officiel de la République du Congo et
# communiqué… » deviendrait une ancre.
BANDEAU_JO = re.compile(
    r"^Journal\s+off\s*[iﬁ]{0,2}\s*ciel\s+de\s+la\s+R[ée]publique\s+du\s+Congo\s*[.,]?$",
    re.IGNORECASE,
)

_MOIS = (
    r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[ûu]t|"
    r"septembre|octobre|novembre|d[ée]cembre"
)
_JOUR_SEMAINE = r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"

# --- Vocabulaire du mobilier (hors ancre) -----------------------------------
# Établi par inventaire des lignes jouxtant les 26 845 bandeaux du corpus local,
# pas par supposition. Chaque motif est ancré sur la ligne ENTIÈRE.
_MOBILIER = (
    # Numéro de page seul. Le plus fréquent (28 127 occurrences autour d'un
    # bandeau) et le plus dangereux hors bloc ancré — d'où la double condition.
    re.compile(r"^\d{1,5}$"),
    # Numéro d'édition : « N° 35-2022 », « N° 35 - 2022 ». Le « ° » ressort
    # parfois en « o » ou « 0 » à l'OCR.
    re.compile(r"^N\s*[°ºo0]\s*\d+\s*[-–—]\s*\d{4}\s*[.,]?$", re.IGNORECASE),
    # « Edition spéciale N° 5-2025 », « Edition Spéciale N° 8-2016 ».
    re.compile(r"^[ÉE]dition\s+sp[ée]ciale\s+N\s*[°ºo0]?\s*\d+.*$", re.IGNORECASE),
    # « 65e ANNEE - EDITION SPECIALE N° 5 » : millésime de la publication.
    re.compile(r"^\d{1,3}\s*(?:e|[èe]me)\s+ANN[EÉ]E\b.*$", re.IGNORECASE),
    # « VOLUME XV », « VOLUME XIX » (éditions spéciales reliées).
    re.compile(r"^VOLUME\s+[IVXLCDM]+\s*$", re.IGNORECASE),
    # Mention de tomaison des éditions spéciales.
    re.compile(r"^Hors\s+texte\s*$", re.IGNORECASE),
    # Date d'édition, quatre gabarits attestés :
    #   « Du jeudi 1er septembre 2022 »  (JO hebdomadaire actuel)
    #   « Du 8 au 14 Avril 2005 »        (JO hebdomadaire de 2005, plage)
    #   « Du 23 septembre 2025 »         (sans jour de la semaine)
    #   « De mai 2012 », « D'octobre 2016 » (éditions mensuelles)
    #
    # Le « D » initial est CASSE SENSIBLE (`(?-i:…)`) alors que le reste du motif
    # ne l'est pas. Sans cette dissymétrie, le filtre emportait onze fins de
    # phrase du corpus, coupées par la mise en page juste avant un saut de page :
    # « …indice 1270 pour compter » / « du 1er avril 1998. ». Le mobilier de page
    # porte toujours la majuscule, la continuation de phrase jamais.
    re.compile(
        rf"^(?-i:Du)\s+(?:{_JOUR_SEMAINE})\s+\d{{1,2}}\s*(?:er)?\s+(?:{_MOIS})\s+\d{{4}}\s*[.,]?$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?-i:Du)\s+\d{{1,2}}\s*(?:er)?\s+au\s+\d{{1,2}}\s+(?:{_MOIS})\s+\d{{4}}\s*[.,]?$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?-i:Du)\s+\d{{1,2}}\s*(?:er)?\s+(?:{_MOIS})\s+\d{{4}}\s*[.,]?$",
        re.IGNORECASE,
    ),
    re.compile(rf"^(?-i:D)[e’'`]\s*(?:{_MOIS})\s+\d{{4}}\s*[.,]?$", re.IGNORECASE),
)

# Traversées sans être retirées : le marqueur de page (citabilité) et les lignes
# vides. Sans cette transparence, un bloc « [[MIBEKO_PAGE:4]] / 1364 / bandeau »
# se briserait au marqueur et le numéro de page survivrait.
_TRANSPARENT = re.compile(r"^(?:\[\[MIBEKO_PAGE:\d+\]\])?$")

# La formule d'exécution coupée par la mise en page JUSTE AVANT son complément :
#
#     Le présent décret sera enregistré et publié au
#     Journal officiel de la République du Congo.      <- complément, pas un en-tête
#     Journal officiel de la République du Congo       <- l'en-tête, lui
#     Du jeudi 26 juin 2025
#
# Le bandeau de la 2e ligne est alors indiscernable d'un en-tête sur sa seule
# forme : c'est la ligne PRÉCÉDENTE qui tranche. Sans ce garde-fou, le décret
# n° 2025-273 du 25 juin 2025 (art. 2, publié en production) perdait son
# complément d'objet et se terminait sur « …sera enregistré et publié au ».
#
# Le contrôle de non-régression qui mesurait « la formule est-elle intacte ? »
# ne cherchait la formule que sur UNE ligne : il était aveugle à sa forme coupée.
_FORMULE_EN_ATTENTE_DE_COMPLEMENT = re.compile(
    r"(?:publi[ée]s?|ins[ée]r[ée]s?|enregistr[ée]s?|communiqu[ée]s?|transmis(?:es?)?|"
    r"paru[es]?|reproduit[es]?)\s+(?:au|aux)\s*$",
    re.IGNORECASE,
)


def _est_mobilier(ligne: str) -> bool:
    return any(motif.match(ligne) for motif in _MOBILIER)


def _est_bandeau_de_page(lignes: List[str], i: int) -> bool:
    """Vrai si la ligne `i` est l'en-tête réimprimé du JO, et non le complément
    d'objet d'une formule d'exécution coupée à la ligne précédente."""
    if not BANDEAU_JO.match(lignes[i]):
        return False
    for j in range(i - 1, -1, -1):
        if not lignes[j]:
            continue
        return not _FORMULE_EN_ATTENTE_DE_COMPLEMENT.search(lignes[j])
    return True


def reperer_mobilier(lignes: List[str]) -> Set[int]:
    """Indices des lignes à retirer : les bandeaux, et le mobilier qui leur est
    contigu de proche en proche.

    L'expansion part de chaque bandeau et progresse vers le haut puis vers le
    bas tant qu'elle rencontre du mobilier ou une ligne transparente. Elle
    s'arrête à la première ligne de texte — c'est ce qui laisse intacts
    « Vu la Constitution ; » ou « Pierre OBA » collés à un saut de page.
    """
    nettoyees = [ligne.strip() for ligne in lignes]
    a_retirer: Set[int] = set()

    for i, ligne in enumerate(nettoyees):
        if not _est_bandeau_de_page(nettoyees, i):
            continue
        a_retirer.add(i)
        for pas in (-1, 1):
            j = i + pas
            while 0 <= j < len(nettoyees):
                courante = nettoyees[j]
                if _TRANSPARENT.match(courante):
                    # Traversée : ni retirée, ni bloquante.
                    j += pas
                    continue
                # `_est_bandeau_de_page` est réévalué ici, pas seulement à
                # l'ancrage : l'expansion vers le haut passerait sinon par-dessus
                # le complément de la formule d'exécution pour l'emporter.
                if _est_bandeau_de_page(nettoyees, j) or _est_mobilier(courante):
                    a_retirer.add(j)
                    j += pas
                    continue
                break

    return a_retirer


def strip_page_furniture(texte: str) -> str:
    """Retire le mobilier de page d'un texte de JO. Idempotent.

    Sans bandeau, rien n'est retiré : un document qui n'est pas un Journal
    officiel traverse la fonction sans être modifié.
    """
    if not texte:
        return texte

    lignes = texte.split("\n")
    a_retirer = reperer_mobilier(lignes)
    if not a_retirer:
        return texte

    return "\n".join(ligne for i, ligne in enumerate(lignes) if i not in a_retirer)
