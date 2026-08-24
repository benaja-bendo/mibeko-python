"""Retrait des échappements LaTeX laissés par MinerU dans le texte extrait.

Jumeau EXACT de `app/Services/LatexArtifactCleaner.php` (mibeko-tableau-de-bord).
Les deux implémentations doivent rester alignées : Laravel nettoie ce qu'il
ingère lui-même (`LegalTextParser::sanitizeContent()`), ce module nettoie le
chemin d'ingestion réel, qui passe par ici et écrit en SQL direct.

MinerU rend en mode mathématique tout ce qu'il prend pour un exposant : « le
1er juin » ressort en ``le $1^{\\text{er}}$ juin``, « 4° échelon » en
``$4^{\\circ}$ échelon``, « n° 342 » en ``$\\mathfrak{n}^{\\circ}$ 342``. Rien
de tout cela n'est une formule : c'est de la typographie ordinaire, et elle
s'affiche telle quelle dans les exports PDF utilisateur (le gabarit
`documents/pro_article_pdf.blade.php` imprime `contenu_texte` brut).

La conversion est volontairement TIMIDE : on DÉSÉCHAPPE, on ne RÉINTERPRÈTE
jamais. ``$1^{\\text{st}}$`` devient « 1st », pas « 1er », même si le corpus
est francophone et que l'OCR a manifestement fauté — corriger la faute serait
une décision éditoriale, hors du périmètre d'un nettoyage mécanique. Toute
portion qui ne se réduit pas intégralement à du texte par les règles ci-dessous
est laissée INTACTE : mieux vaut un ``$\\frac{a}{b}$`` visible dans un PDF
qu'un texte juridique dégradé en silence.

Trois garde-fous ferment les cas dangereux :
  1. une portion doit être sur UNE seule ligne — les ``$`` dépareillés d'un
     tableau (``$100.000.000\\n$``) ne s'apparient donc jamais ;
  2. elle doit contenir un marqueur LaTeX (``\\``, ``^{``, ``_{``) — sans quoi
     le ``$`` est presque toujours le symbole monétaire (« moins de 60 $ ») et
     le convertir l'effacerait. Le corpus contient des conventions minières
     libellées en dollars ;
  3. le résultat ne doit contenir ni reste de syntaxe LaTeX, ni opérateur, ni
     parenthèse — ce qui ressemble encore à une formule est refusé.

Volontairement HORS PÉRIMÈTRE (décidé le 25/08/2026, mibeko-dashboard#25) : le
numéro de loi écrit sans son préfixe, ``$15 - 62$`` pour « 15-62 ». Le garde-fou
n° 2 le refuse déjà faute de marqueur LaTeX, et c'est bien ainsi : le convertir
imposerait de relâcher ce garde-fou pour toute portion sans marqueur, c'est-à-dire
de rendre convertibles les montants monétaires (« 60 $ ») et les vraies
soustractions. La lecture correcte n'est ici accessible qu'au contexte — une
occurrence sœur en clair dans le même article — donc à un humain ou à une passe
de curation, pas à un nettoyage mécanique qui ne voit qu'une portion à la fois.
"""

import re
from typing import List, NamedTuple, Optional

# Une portion `$…$` plus longue que ça n'est pas un artefact typographique mais
# une vraie expression : refusée quoi qu'il arrive.
LONGUEUR_MAX = 40

# Commandes de mise en forme : pure décoration, leur contenu est du texte.
# `\mathfrak` n'y figure pas — il est traité à part, cf. `_decoder`.
POLICES = (
    "text",
    "textrm",
    "textbf",
    "textit",
    "textsf",
    "mathrm",
    "mathbf",
    "mathit",
    "mathsf",
    "pmb",
    "operatorname",
)

# Une portion en mode mathématique, avec l'espace horizontal qui l'entoure.
# `[^$\r\n]` interdit d'enjamber une fin de ligne : c'est ce qui protège les
# `$` dépareillés des tableaux de chiffres.
#
# `(?P<base>\d)?` capture, quand il est présent, le chiffre collé immédiatement
# avant la portion — nécessaire pour distinguer, dans `remplacer()`, un
# exposant nu recollé à son chiffre (« 1 $^{er}$ ») du cas général déjà géré
# (base ET exposant tous deux à l'intérieur du `$`). Le chiffre capturé est
# réémis tel quel : rien n'est perdu, il change seulement de groupe.
_PORTION_RE = re.compile(
    r"(?P<base>\d)?"
    # Mot collé devant un indice NU (`Le $_{S}$`) : même accident que le
    # chiffre devant un exposant nu, côté indice. Le lookahead exige que la
    # portion commence par `_{`, sinon un mot quelconque serait aspiré ici.
    r"(?:(?P<base_mot>[^\W\d_]{2,})(?=[^\S\r\n]*\$[^\S\r\n]*_\{))?"
    r"(?P<avant>[^\S\r\n]*)\$(?P<inner>[^$\r\n]*)\$(?P<apres>[^\S\r\n]*)"
)

_MARQUEUR_RE = re.compile(r"\\|\^\{|_\{")
_MATHFRAK_N_RE = re.compile(r"\\mathfrak\{([nN])\}")
_POLICE_RE = re.compile(r"\\(?:" + "|".join(POLICES) + r")\{([^{}]*)\}")
# `[o0]` : l'OCR confond régulièrement la lettre o et le chiffre zéro dans
# l'exposant de « n° ». Les deux graphies désignent la même abréviation, et
# un même article publié porte parfois les deux côte à côte.
_EXPOSANT_O_RE = re.compile(r"([nN])\^\{[o0]\}")
_EXPOSANT_OS_RE = re.compile(r"([nN])\^\{[o0]s\}")
_EXPOSANT_DEGRE_RE = re.compile(r"\^\{°\}")
# `[^\W\d_]` = une lettre Unicode (ni chiffre, ni souligné) : l'équivalent
# Python de `\pL`, que le module `re` standard ne connaît pas.
_EXPOSANT_TEXTE_RE = re.compile(r"(?<![^\W\d_])\^\{([^\W\d_]{1,4})\}")
# Indices `_{…}`, symétriques des exposants : MinerU rend en indice la lettre
# finale d'un mot (« Les » → ``Le $_{S}$``, « ses » → ``$se_{S}$``). Deux formes
# seulement sont admises, l'une et l'autre choisies pour ne JAMAIS attraper une
# notation mathématique :
#   - l'indice NU (la portion ne contient que `_{X}`) : la base est le mot
#     resté hors du `$`, cas impossible pour une variable indicée ;
#   - l'indice porté par une base d'AU MOINS DEUX lettres (`Se_{S}`), donc un
#     début de mot — une variable mathématique s'écrit sur une seule lettre,
#     et `u_{n}` reste ainsi refusé comme avant.
_INDICE_NU_RE = re.compile(r"^_\{([^\W\d_]{1,4})\}$")
_INDICE_MOT_RE = re.compile(r"([^\W\d_]{2,})_\{([^\W\d_]{1,4})\}")

_RESTE_LATEX_RE = re.compile(r"[\\{}^_$]")

# Ponctuation admise dans le texte final. Tout le reste (parenthèse, opérateur,
# astérisque) fait échouer la conversion : `$1^{*}$`, bruit OCR pour « 1er »,
# est ainsi signalé plutôt que rendu « 1* ».
_PONCTUATION_ADMISE = frozenset(" °%.,;:'’«»-–—/")

_SUBSTITUTIONS_SIMPLES = (("\\circ", "°"), ("\\%", "%"), ("\\,", " "), ("\\;", " "), ("\\ ", " "))


class Analyse(NamedTuple):
    """Texte nettoyé, conversions appliquées, fragments laissés intacts."""

    texte: str
    convertis: List[tuple]
    refuses: List[str]


def strip_latex_artifacts(texte: str) -> str:
    """Texte nettoyé de ce qui pouvait l'être sûrement ; le reste est conservé."""

    return analyser(texte).texte


def analyser(texte: str) -> Analyse:
    """Nettoie et rend compte : ce qui a été converti, ce qui a été refusé."""

    convertis: List[tuple] = []
    refuses: List[str] = []

    def remplacer(match: "re.Match") -> str:
        base = match.group("base") or ""
        base_mot = match.group("base_mot") or ""
        avant, inner, apres = match.group("avant"), match.group("inner"), match.group("apres")
        brut = f"{base}{base_mot}{avant}${inner}${apres}"
        clair = _decoder(inner, base_mot[-1:] or None)

        if clair is None:
            refuses.append(brut.strip())
            return brut

        # L'espace autour de la portion est celui que MinerU a inséré en
        # ouvrant le mode mathématique : on en garde un seul, sans jamais
        # coller deux mots qui étaient séparés. EXCEPTION : un chiffre collé
        # juste avant un exposant nu (rien d'autre dans le `$`, cf. `base`
        # ci-dessus) — MinerU rend parfois « 1er » en laissant le « 1 » en
        # texte normal et en n'ouvrant le mode mathématique que pour
        # l'exposant seul (« 1 $^{er}$ »), contrairement au cas normal où
        # toute la portion, base comprise, est à l'intérieur du `$`
        # (« $1^{\text{er}}$ »). Sans cette exception, le blanc que MinerU
        # insère à l'ouverture du mode mathématique était reproduit tel quel,
        # donnant « 1 er » au lieu de « 1er ».
        base_collee = base != "" and inner.lstrip().startswith("^{")
        # Symétrique côté indice : le mot resté hors du `$` se recolle à la
        # lettre que MinerU en avait détachée (« Le $_{S}$ » → « Les »).
        indice_colle = base_mot != "" and inner.lstrip().startswith("_{")
        colle = base_collee or indice_colle
        prefixe = "" if colle else (" " if avant or clair[:1].isspace() else "")
        suffixe = " " if apres or clair[-1:].isspace() else ""
        remplacement = f"{base}{base_mot}{prefixe}{clair.strip()}{suffixe}"

        convertis.append((brut.strip(), remplacement.strip()))
        return remplacement

    resultat = _PORTION_RE.sub(remplacer, texte)

    return Analyse(resultat, convertis, refuses + _marqueurs_hors_portion(texte))


def _accorder_casse(lettre: str, precedente: Optional[str]) -> str:
    """Abaisse la casse d'une lettre mise en indice au milieu d'un mot.

    MinerU rend l'indice en capitale quelle que soit la casse d'origine : « Les »
    ressort en ``Le $_{S}$``. Une majuscule qui suit une minuscule À L'INTÉRIEUR
    d'un mot n'existe pas en français — c'est un artefact de rendu, pas une
    information. On ne change donc jamais la lettre elle-même, seulement sa
    casse, et seulement dans cette position-là.
    """

    if precedente and precedente.islower() and lettre.isupper():
        return lettre.lower()

    return lettre


def _decoder(inner: str, precedente: Optional[str] = None) -> Optional[str]:
    """Réduit l'intérieur d'une portion à du texte brut, ou ``None`` si la
    moindre chose y résiste.

    ``precedente`` est la dernière lettre du mot resté hors du ``$`` quand la
    portion est un indice nu : elle sert à accorder la casse, rien d'autre.
    """

    # Sans marqueur LaTeX, ce n'est pas un artefact d'extraction : le `$` est un
    # symbole monétaire (« le US $ », « moins de 60 $ »).
    if not _MARQUEUR_RE.search(inner):
        return None

    # MinerU lit régulièrement le « n » de « n° » en gothique. Toute autre
    # lettre en `\mathfrak` relève d'une vraie notation mathématique.
    texte = _MATHFRAK_N_RE.sub(r"\1", inner)

    # Polices : on garde le contenu, jamais imbriqué (`[^{}]`) — une
    # imbrication laisse un `\` qui fera échouer la validation finale.
    for _ in range(4):
        etape = _POLICE_RE.sub(r"\1", texte)
        if etape == texte:
            break
        texte = etape

    for commande, remplacement in _SUBSTITUTIONS_SIMPLES:
        texte = texte.replace(commande, remplacement)

    # « n^{o} » et « n^{os} » : l'exposant o du français « n° » et de son
    # pluriel « nos ». Traités avant l'exposant générique, qui rendrait « no »
    # — un mot, pas une abréviation de numéro.
    texte = _EXPOSANT_O_RE.sub(r"\1°", texte)
    texte = _EXPOSANT_OS_RE.sub(r"\1os", texte)
    texte = _EXPOSANT_DEGRE_RE.sub("°", texte)

    # Exposant textuel court : ordinal (« 1^{er} », « 3^{e} », « 4^{th} »).
    # Jamais après une lettre : `x^{n}` est une puissance, pas un ordinal, et
    # l'aplatir en « xn » détruirait la formule.
    texte = _EXPOSANT_TEXTE_RE.sub(r"\1", texte)

    # Indice nu : la base est le mot laissé hors du `$` par MinerU.
    texte = _INDICE_NU_RE.sub(lambda m: _accorder_casse(m.group(1), precedente), texte)
    # Indice porté par un début de mot : la base est dans la portion.
    texte = _INDICE_MOT_RE.sub(
        lambda m: m.group(1) + _accorder_casse(m.group(2), m.group(1)[-1:]),
        texte,
    )

    # Une commande non reconnue (\frac, \mathbb, \rightarrow, \prime…) a laissé
    # sa barre oblique, un exposant complexe ses accolades : refus.
    if _RESTE_LATEX_RE.search(texte):
        return None

    if not all(ch.isalnum() or ch in _PONCTUATION_ADMISE for ch in texte):
        return None

    if not texte.strip() or len(texte) > LONGUEUR_MAX:
        return None

    return texte


def _marqueurs_hors_portion(texte: str) -> List[str]:
    """Marqueurs LaTeX qui traînent HORS de toute portion ``$…$`` : ``$``
    orphelin d'un tableau, ``\\frac`` nu, exposant sans délimiteur. Jamais
    touchés — on ne sait pas où commence ni où finit l'expression — seulement
    signalés."""

    reste = _PORTION_RE.sub(" ", texte)

    return [
        trouve.strip()
        for trouve in re.findall(r".{0,30}(?:\\frac|\\math|\\pmb|\\text|\^\{|_\{|\$).{0,30}", reste)
    ]
