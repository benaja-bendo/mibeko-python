import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.extractor.latex_artifacts import strip_latex_artifacts
from src.extractor.page_furniture import strip_page_furniture

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grammaire des unités structurelles
# ---------------------------------------------------------------------------
# Niveaux du plus englobant au plus fin. Un niveau N ferme tous les niveaux >= N.
# L'ARTICLE est une feuille : il s'attache au dernier niveau ouvert.
STRUCTURE_LEVELS: List[str] = [
    "PARTIE",
    "LIVRE",
    "TITRE",
    "CHAPITRE",
    "SECTION",
    "SOUS_SECTION",
    "PARAGRAPHE",
]

_ROMAN = r"[IVXLCDM]+"
_ORDINAL_WORD = r"PREMI(?:ER|[EÈ]RE)|UNIQUE|LIMINAIRE|PR[ÉE]LIMINAIRE|\w+I[EÈ]ME"
_NUMBER = rf"(?:{_ROMAN}|\d+(?:er|[eè]re?|[eè]me)?|{_ORDINAL_WORD})"
# Séparateur toléré entre le numéro et le libellé : ":", ".", "-", "–", "—".
_SEP = r"[\s:.\-–—]*"

_LEVEL_KEYWORDS = {
    "PARTIE": r"PARTIE",
    "LIVRE": r"LIVRE",
    "TITRE": r"TITRE",
    "CHAPITRE": r"CHAPITRE",
    "SECTION": r"SECTION",
    "SOUS_SECTION": r"SOUS[\s\-]SECTION",
    "PARAGRAPHE": r"PARAGRAPHE|§",
}

# Le séparateur avant le numéro tolère les artefacts OCR : "TITRE : VI", "TITREX".
STRUCTURE_PATTERNS: Dict[str, re.Pattern] = {
    level: re.compile(
        rf"^(?:{keyword}){_SEP}({_NUMBER})\b{_SEP}(.*)$",
        re.IGNORECASE,
    )
    for level, keyword in _LEVEL_KEYWORDS.items()
}

# MinerU omet parfois l'espace entre un numéro romain et l'intitulé :
# « CHAPITRE IDISPOSITIONS GENERALES ». Ce filet n'est consulté qu'APRÈS
# l'échec des motifs normaux, afin qu'un titre sain comme « CHAPITRE III : ... »
# reste analysé par la grammaire principale sans ambiguïté romaine.
_CANONICAL_ROMAN = (
    r"(?=[IVXLCDM])M{0,4}(?:CM|CD|D?C{0,3})"
    r"(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
)
ATTACHED_ROMAN_STRUCTURE_PATTERN = re.compile(
    rf"^(?P<level>(?i:PARTIE|LIVRE|TITRE|CHAPITRE|SECTION))\s+"
    rf"(?P<number>{_CANONICAL_ROMAN})(?P<title>[A-ZÀ-ÖØ-Þ]{{2}}.*)$"
)

# "ARTICLE 1er : contenu...", "Art. 12.-", "Article L.122-4", "ARTICLE PREMIER",
# "Art.4.‐ ..." (sans espace, tiret Unicode ‐ fréquent dans les Actes OHADA),
# "Articles 194 :" (pluriel, en-tête d'un seul article numéroté malgré le
# pluriel — coquille source ou OCR). Le qualificatif "nouveau/nouvelle" est
# capturé DANS le numéro ("Article 2 nouveau" -> "2 nouveau") : dans un acte
# modificatif il désigne le texte de remplacement d'un article cité, à ne pas
# confondre avec l'article d'exécution de même numéro (sinon doublon + fausse
# alerte de curation).
#
# Second groupe alternatif (numéro ABSENT) : « Article : Le titulaire... » —
# le chiffre a été perdu à l'OCR (rendu vide plutôt que par un caractère
# reconnaissable), mais le mot « Article » lui-même est net et suivi d'un
# séparateur explicite. Le texte de l'article suivant se retrouvait sinon
# collé en fin de l'article précédent (contenu jamais perdu, juste caché —
# audit remédiation 2026-08-02 phase 5). Le séparateur y est rendu
# OBLIGATOIRE (pas optionnel) : sans lui, un simple mot de prose commençant
# par « Article » (rare mais possible) serait pris à tort pour un en-tête.
# `open_article` retombe déjà sur un identifiant `SANS_NUM_xxxxx` quand
# `number` est vide (cf. `ingest_hierarchy`, main.py) — aucun autre correctif
# nécessaire en aval.
#
# Corps du numéro, partagé par les branches singulier/pluriel ci-dessous :
# - `\d+(?:\.\d+)*` : chiffres, avec continuations décimales optionnelles
#   (« 1.1.2 » — règlements CEMAC/OHADA à numérotation Titre.Chapitre.Article ;
#   sans ce `(?:\.\d+)*`, seul le « 1 » de tête était capturé, le reste
#   (« .1.2 Directives... ») versé à tort dans le contenu de l'article).
# - Le suffixe optionnel couvre aussi `[a-z]\)?` (« 14 a) », « 150 a »… — Code
#   Pénal congolais, sous-paragraphes lettrés d'un même article : la lettre
#   fait partie du numéro réel, pas un doublon du numéro de base. Le texte
#   source l'écrit tantôt avec parenthèse fermante, tantôt sans — d'où le `?`.
#   Le lookahead `(?![a-zA-ZÀ-ÿ])` qui suit est nécessaire dès que la
#   parenthèse est optionnelle : `re.IGNORECASE` fait matcher `[a-z]` sur une
#   lettre MAJUSCULE aussi, donc sans lui, une seule lettre d'un mot bien plus
#   long ("D" de « Directives », "D" de « Dérogation ») était absorbée à tort
#   dans le numéro, tronquant le début du contenu de l'article. Plage
#   accentuée incluse (« Dérogation ») sinon le "D" passait déjà le lookahead
#   ASCII-only avant le "é" suivant.
# - Tout le segment compact est atomique et un tiret n'est accepté dans le
#   numéro que s'il est suivi de chiffres. Sans la première
#   règle, une citation en plage (« articles 5-11, ... ») reculait jusqu'à « 5 »
#   et recyclait le tiret comme séparateur. Sans la seconde, la ponctuation
#   source « ARTICLE 3- L'acte... » devenait le faux numéro « 3- L » et avalait
#   l'initiale du contenu.
_NUMERO_BODY = (
    r"(?>\d+(?:\.\d+)*(?:-\d+)*(?:(?:er|[eè]re?|[eè]me|bis|ter|quater|quinquies|sexies|septies|nouveau|nouvelle)|[a-z](?![a-zA-ZÀ-ÿ]))?)"
    r"(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|nouveau|nouvelle|nouveaux|nouvelles|[a-z]\)?(?![a-zA-ZÀ-ÿ])))?"
)

# Branche PLURIEL séparée (« Articles 194 : ... ») : un séparateur après le
# numéro y est TOUJOURS obligatoire, contrairement au singulier. Sans cette
# distinction, une citation en prose (« Articles 74 et 75 de la présente loi
# doivent être respectés ») était prise à tort pour un nouvel article « 74 »
# de contenu « et 75 de la présente loi… » — régression découverte en
# rejouant la structuration du Code Pénal/Loi 28-2016 en prod (remédiation
# 2026-08-02 phase 5, suite).
#
# Côté singulier, un numéro suivi d'une virgule (`(?!\s*,)`) est aussi rejeté :
# une citation à la chaîne comme « article 182, 183 et 184. » (repli d'un
# retour à la ligne OCR en tête de ligne) était prise pour un nouvel article
# « 182 » de contenu « , 183 et 184. » — même famille de faux positif que la
# citation plurielle ci-dessus, mais avec « article » au singulier donc non
# couverte par son garde-fou. Trouvé en rejouant le Code Pénal en prod.
ARTICLE_PATTERN = re.compile(
    r"^\.?\s*(?:"
    rf"ARTICLES\.?\s*(?:[:.\-–—‐]\s*)?(?P<num_pl>PREMIER|[LDRA]?\.?\s*{_NUMERO_BODY})\s*[:.\-–—‐]+\s*(?P<content_pl>.*)"
    r"|"
    r"(?:ARTICLE|ART)\.?\s*(?:"
    rf"(?:[:.\-–—‐]\s*)?(?P<num>PREMIER|[LDRA]?\.?\s*{_NUMERO_BODY})(?!\s*,)"
    r"\s*[:.\-–—‐]*"
    r"|[:.\-–—‐]+"
    r")\s*(?P<content>.*)"
    r")$",
    re.IGNORECASE,
)


def _article_match_groups(match: "re.Match") -> Tuple[Optional[str], str]:
    """Numéro et contenu capturés par `ARTICLE_PATTERN`, quelle que soit la
    branche (singulier/pluriel) qui a matché."""
    num = match.group("num_pl") if match.group("num_pl") is not None else match.group("num")
    content = match.group("content_pl") if match.group("content_pl") is not None else match.group("content")
    return num, content


# Amorce d'un titre d'article éclaté un mot par ligne par MinerU — trouvé en
# pilotant le redécoupage d'un JO spécial (2010-02) en prod le 10/08/2026 :
# « Article \n61 \n: \n Rémunération \n perçue \n par \n le \n Concessionnaire »,
# alors que le même document rend « Article 62 : Rémunération due au
# Concédant » sur une seule ligne quelques dizaines de lignes plus loin. Le
# mot « Article » seul sur sa ligne ne matche jamais ARTICLE_PATTERN (qui
# exige un numéro) : le contenu qui suit restait rattaché à l'article
# précédent jusqu'au prochain en-tête reconnu — pas perdu, juste mal étiqueté,
# et signalé en aval comme un « article manquant ».
_SPLIT_HEADING_START_PATTERN = re.compile(r"^(?:ARTICLES?|ART)\.?$", re.IGNORECASE)
# Fragments plausibles d'un numéro/titre éclaté : peu de mots par ligne. Une
# vraie phrase de corps de texte en fait toujours davantage — le seuil sert de
# garde-fou, pas de règle grammaticale.
_SPLIT_HEADING_MAX_WORDS_PER_LINE = 3
# Nombre de lignes fragmentées tolérées après l'amorce avant d'abandonner : un
# titre d'article, même très éclaté, ne s'étend jamais sur des dizaines de
# lignes — au-delà, ce n'est plus ce motif.
_SPLIT_HEADING_MAX_FRAGMENTS = 20

_TABLE_OF_CONTENTS_START_PATTERN = re.compile(
    r"^(?:SOMMAIRE|TABLE\s+DES\s+MATI[ÈE]RES)\b",
    re.IGNORECASE,
)
_TABLE_OF_CONTENTS_END_PATTERN = re.compile(
    r"^(?:LE\s+CONSEIL\b|L['’]ASSEMBL[ÉE]E\b|LE\s+PR[ÉE]SIDENT\b|VU\b|"
    r"APR[ÈE]S\s+EN\s+AVOIR\b|(?:ARTICLE|ART)\.?\s+(?:PREMIER|1(?:ER)?|\d+)\b)",
    re.IGNORECASE,
)


def _strip_leading_table_of_contents(texte: str) -> str:
    """Retire un sommaire placé avant le premier article du texte juridique.

    Les recueils OHADA commencent souvent par plusieurs pages de sommaire dont
    les lignes « LIVRE / TITRE / CHAPITRE » ressemblent exactement à la vraie
    structure. Les conserver fait ouvrir la hiérarchie trop tôt, transforme les
    fragments de pagination en ``DISPOSITION_N`` et classe ensuite les visas de
    l'acte comme une disposition au lieu d'un préambule.

    Le retrait est volontairement conservateur : uniquement le PREMIER
    « SOMMAIRE / TABLE DES MATIÈRES », uniquement s'il précède tout article, et
    uniquement si une formule juridique de reprise est trouvée. À défaut de
    borne sûre, le texte est rendu intact.
    """
    if not texte:
        return texte

    lines = texte.split("\n")
    contents_start: Optional[int] = None
    article_seen = False

    for index, line in enumerate(lines):
        match_line = _clean_for_matching(line)
        if ARTICLE_PATTERN.match(match_line):
            article_seen = True
        if not article_seen and _TABLE_OF_CONTENTS_START_PATTERN.match(match_line):
            contents_start = index
            break

    if contents_start is None:
        return texte

    for index in range(contents_start + 1, len(lines)):
        if _TABLE_OF_CONTENTS_END_PATTERN.match(_clean_for_matching(lines[index])):
            return "\n".join(lines[:contents_start] + lines[index:])

    return texte


def _rejoin_split_article_headings(texte: str) -> str:
    """Recolle un titre d'article que MinerU a rendu un mot par ligne.

    N'agit que sur les lignes qui, seules, ne contiennent QUE le mot « Article »
    (ou « Art »/« Articles ») — jamais reconnu comme un en-tête valide par
    ARTICLE_PATTERN puisqu'il exige un numéro. Les lignes suivantes, tant
    qu'elles restent courtes, sont fusionnées avec des espaces jusqu'à ce que
    le résultat matche ARTICLE_PATTERN (numéro reconnu). Dès qu'une ligne est
    trop longue pour être un fragment de titre, ou que le plafond de lignes est
    atteint sans qu'aucune fusion ne matche, les lignes d'origine sont laissées
    intactes : un faux déclenchement (un document où « Article » apparaît
    seul sur sa ligne pour une tout autre raison) ne peut donc rien casser,
    il ne fait juste rien.
    """
    if not texte:
        return texte

    lignes = texte.split("\n")
    resultat: List[str] = []
    i = 0
    n = len(lignes)
    while i < n:
        ligne = lignes[i]
        if not _SPLIT_HEADING_START_PATTERN.match(_clean_for_matching(ligne)):
            resultat.append(ligne)
            i += 1
            continue

        # Fusion GLOUTONNE d'abord (toutes les lignes courtes consécutives),
        # test ARTICLE_PATTERN ensuite, une seule fois sur le résultat complet.
        # Tester à chaque fragment ajouté s'arrêterait dès « Article 61 » seul
        # (numéro reconnu, contenu vide déjà valide pour ARTICLE_PATTERN) sans
        # jamais absorber le reste du titre qui suit.
        fragments = [ligne.strip()]
        j = i + 1
        while j < n and (j - i) <= _SPLIT_HEADING_MAX_FRAGMENTS:
            candidate_line = lignes[j].strip()
            if not candidate_line or len(candidate_line.split()) > _SPLIT_HEADING_MAX_WORDS_PER_LINE:
                break
            fragments.append(candidate_line)
            j += 1

        candidate = " ".join(fragments)
        if len(fragments) > 1 and ARTICLE_PATTERN.match(_clean_for_matching(candidate)):
            resultat.append(candidate)
            i = j
        else:
            resultat.append(ligne)
            i += 1

    return "\n".join(resultat)

# Formule finale d'un acte : « Fait à Brazzaville, le 18 avril 2026 » suivie du
# nom du ou des signataires. Isolée en feuille SIGNATURE plutôt que collée au
# contenu du dernier article. On exige « le <jour> » après le lieu pour ne pas
# confondre avec une ligne d'article qui débuterait par « Fait à … » (le « l »
# de « le » est parfois OCRisé en « I » majuscule).
SIGNATURE_PATTERN = re.compile(
    r"^(?:Fait\s+à\b.*\b[lI]e\s+\d{1,2}(?:er)?\b.*|Pour\s+le\s+Directeur\s+général\s*:)$",
    re.IGNORECASE,
)

# Note de bas de page placée après la signature (« (1) La justification… »).
# On ne l'interprète comme une feuille NOTE que lorsque la signature est déjà
# ouverte : le même motif peut parfaitement appartenir au corps d'un article.
FOOTNOTE_PATTERN = re.compile(r"^\((\d+)\)\s*(.*)$")

# En-têtes de rubrique d'un Journal Officiel (ministère, partie, sous-section…)
# qui suivent parfois la signature du dernier acte d'une section. Servent
# UNIQUEMENT, en mode signature, à clore la feuille SIGNATURE sans la polluer.
_SECTION_NOISE_PATTERN = re.compile(
    r"^(?:MINIST[EÈ]RE|PR[EÉ]SIDENCE|PRIMATURE|PARTIE\b|ANNONCES|ACTE\s+EN\s+ABR[EÉ]G[EÉ]|"
    r"[AB]\s*[-–—]\s|[-–—]\s*(?:DECRET|TEXTES|ANNONCES)|AVIS\s+N[°ºo])",
    re.IGNORECASE,
)

# Forme inversée : "PREMIÈRE PARTIE — ...", "DEUXIÈME PARTIE : ..."
PARTIE_INVERTED_PATTERN = re.compile(
    rf"^({_NUMBER})\s+PARTIE{_SEP}(.*)$",
    re.IGNORECASE,
)

# Bandeau de page imprimé (numéro de page courant d'un ouvrage relié, ex.
# « p.11 », parfois suivi du Titre/Chapitre courant réimprimé en en-tête).
# Remédiation 2026-08-02 phase 5 : confirmé sur le Code civil en prod — un
# « article » fantôme, contenant SEULEMENT ce bandeau, précède
# systématiquement le VRAI article de même numéro (le bandeau de la page
# suivante répète la référence de l'article avant que son texte ne
# commence). Sans fusion, ce fantôme devient un doublon de chaîne
# individuellement flagué (144 cas sur ce seul document).
_PAGE_BANNER_LINE_PATTERN = re.compile(r"^p\.?\s*\d+\.?$", re.IGNORECASE)

# Rappel de Titre/Chapitre courant en bandeau de page (« Titre Ier : Des
# droits civils »). Volontairement plus permissif que STRUCTURE_PATTERNS
# (qui n'a pas besoin, ici, d'extraire un numéro exploitable — seulement de
# reconnaître qu'une ligne EST un bandeau structurel, pas du texte d'article).
_STRUCTURAL_BANNER_START_PATTERN = re.compile(
    r"^(?:PARTIE|LIVRE|TITRE|CHAPITRE|SECTION|SOUS[\s\-]SECTION|PARAGRAPHE|§)\b",
    re.IGNORECASE,
)

# Marqueur de page injecté par l'extraction JSON MinerU (cf.
# extract_text_from_mineru_json) : permet de tamponner chaque nœud avec sa page
# d'origine (1-based) pour la citabilité « page N ». Absent des entrées markdown.
PAGE_MARKER_PATTERN = re.compile(r"^\[\[MIBEKO_PAGE:(\d+)\]\]$")

# Tableau HTML émis par MinerU (grilles salariales, annexes budgétaires…),
# routé vers un nœud feuille TABLEAU plutôt que noyé dans l'article courant.
#
# Le plus souvent sur une seule ligne, mais PAS toujours : MinerU coupe la ligne
# quand une cellule est longue (136 tableaux sur 1323 dans le corpus local au
# 09/08/2026). Ne lire que la ligne d'ouverture tronquait le tableau ET versait
# ses lignes suivantes dans le flux d'articles — d'où l'accumulation jusqu'à la
# fermeture, plafonnée par `_TABLE_MAX_LINES` pour qu'une balise jamais refermée
# n'avale pas le document.
TABLE_HTML_PATTERN = re.compile(r"^<table[\s>]", re.IGNORECASE)
TABLE_HTML_CLOSE = re.compile(r"</table\s*>", re.IGNORECASE)
_TABLE_MAX_LINES = 200

# Lignes de bruit à ignorer : images markdown, filets, numéros de page isolés
# et marqueurs internes ajoutés par `merge-chunks`. Ces derniers décrivent le
# traitement technique, jamais le texte juridique, et ne doivent donc entrer
# ni dans un préambule ni dans le corps d'un article.
_NOISE_PATTERN = re.compile(
    r"^(?:!\[.*|[-_*=]{3,}|\d{1,3}|[o0]{3,}|<!--\s*chunk\b.*-->)$",
    re.IGNORECASE,
)

# Préfixes markdown à retirer pour la détection (mais pas du contenu).
_MD_PREFIX = re.compile(r"^[#>\s]*[*_]{0,3}\s*")
_MD_SUFFIX = re.compile(r"\s*[*_]{1,3}$")


def _clean_for_matching(line: str) -> str:
    """Retire les décorations markdown (titres, gras) pour tester les regex."""

    cleaned = _MD_PREFIX.sub("", line.strip())
    cleaned = _MD_SUFFIX.sub("", cleaned)
    return cleaned.strip()


def _is_page_banner_noise(text: str) -> bool:
    """Le contenu ne contient RIEN d'autre qu'un bandeau de page imprimé
    (« p.11 ») et/ou un rappel de Titre/Chapitre courant — aucun vrai texte
    juridique. Cf. `_PAGE_BANNER_LINE_PATTERN`. Une chaîne vide est considérée
    comme du bruit (rien à perdre en la fusionnant)."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return True
    for line in lines:
        cleaned = _clean_for_matching(line)
        if _PAGE_BANNER_LINE_PATTERN.match(cleaned):
            continue
        if _STRUCTURAL_BANNER_START_PATTERN.match(cleaned):
            continue
        return False
    return True


class LegalDocumentParser:
    """
    Parseur de structure hiérarchique d'un texte juridique (code, loi, décret)
    depuis un texte brut ou un markdown OCRisé (sortie MinerU).

    Produit une liste de noeuds racines de la forme :
    {"type": "TITRE", "number": "I", "title": "...", "content": "", "children": [...]}
    Les ARTICLEs portent leur texte dans "content" (retours à la ligne préservés).
    Le corps placé sous une division mais dépourvu d'en-tête « Article » est
    conservé dans des feuilles DISPOSITION_N au lieu d'être ignoré.

    Le texte qui précède le premier élément structurel d'un acte (qualité du
    signataire, visas « Vu … », considérants) est émis comme feuille PREAMBULE
    en tête des roots — mais uniquement si un ARTICLE/structure suit.
    """

    def __init__(self, pdf_path: Optional[str] = None, text_content: Optional[str] = None):
        self.pdf_path = pdf_path
        self.text_content = text_content

    def extract_text(self) -> str:
        """Retourne le texte fourni ou l'extrait du PDF via PyMuPDF (OCR si dispo).

        Les échappements LaTeX de MinerU sont retirés ICI, en amont de tout :
        c'est le seul point par lequel passent les contenus d'articles, de
        préambules, de signatures et les intitulés de nœuds, quel que soit
        l'appelant (pipeline live, upload manuel, scripts de rattrapage). En
        prime, les regex de détection ci-dessous voient « Article 1er » là où
        elles lisaient ``Article $1^{\\text{er}}$``.

        Le mobilier de page du Journal officiel (numéro de page, bandeau, date
        d'édition réimprimés à chaque saut de page) est retiré au même endroit et
        pour la même raison : les quatre appelants de ce parseur convergent tous
        ici, avant que `parse_hierarchy` ne pose les frontières d'articles. Sans
        ce retrait, l'en-tête s'incruste en pleine phrase dans le contenu de
        l'article à cheval sur deux pages (554 articles publiés concernés,
        mesuré en production le 09/08/2026). Les marqueurs `[[MIBEKO_PAGE:N]]`
        traversent le filtre intacts — la citabilité par page en dépend.

        Un titre d'article que MinerU a rendu un mot par ligne (« Article \\n61
        \\n: \\n Rémunération… ») est recollé en dernier, une fois le mobilier de
        page retiré : sinon un bandeau intercalé entre deux fragments du titre
        empêcherait la fusion. Sans ce recollage, ARTICLE_PATTERN n'y voit
        jamais un numéro, et tout le texte qui suit reste rattaché à l'article
        précédent — pas perdu, juste mal étiqueté (trouvé le 10/08/2026 en
        pilotant le redécoupage d'un JO spécial en prod).
        """

        if self.text_content:
            return _rejoin_split_article_headings(
                _strip_leading_table_of_contents(
                    strip_page_furniture(strip_latex_artifacts(self.text_content))
                )
            )

        if not self.pdf_path:
            return ""

        import fitz  # PyMuPDF — import différé : inutile pour le parsing de texte

        doc = fitz.open(self.pdf_path)
        full_text = []

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            try:
                tp = page.get_textpage_ocr(flags=0, dpi=300, full=True, language="fra")
                text = tp.extractText()
            except Exception:
                text = page.get_text("text")

            clean_lines = [line.strip() for line in text.split("\n") if line.strip()]
            full_text.append("\n".join(clean_lines))

        return _rejoin_split_article_headings(
            _strip_leading_table_of_contents(
                strip_page_furniture(strip_latex_artifacts("\n".join(full_text)))
            )
        )

    def parse_hierarchy(self) -> List[Dict[str, Any]]:
        """Analyse le texte et reconstruit l'arborescence du document."""

        text = self.extract_text()
        roots: List[Dict[str, Any]] = []
        # Pile des noeuds structurels ouverts : [(index_niveau, noeud), ...]
        open_nodes: List[Tuple[int, Dict[str, Any]]] = []
        current_article: Optional[Dict[str, Any]] = None
        content_buffer: List[str] = []
        # Page d'origine courante (1-based), alimentée par les marqueurs MinerU.
        # Reste None pour les entrées sans pagination (markdown brut).
        current_page: Optional[int] = None
        # Texte antérieur au premier élément structurel (qualité du signataire,
        # visas, considérants). Émis comme feuille PREAMBULE en tête, mais
        # SEULEMENT si un ARTICLE/structure suit : un texte sans dispositif
        # (proclamation, discours) n'est pas un préambule et reste géré par le
        # fallback « texte intégral » de l'appelant.
        preamble_buffer: List[str] = []
        preamble_page: Optional[int] = None
        structure_opened = False
        # Formule finale (« Fait à … » + signataire) isolée en feuille SIGNATURE
        # plutôt que collée au contenu du dernier article.
        current_signature: Optional[Dict[str, Any]] = None
        signature_buffer: List[str] = []
        # Certains avis/instructions sont structurés en titres et sections sans
        # jamais employer le mot « Article ». Leur corps est une vraie unité de
        # contenu : on le bufferise puis on l'attache à la division courante sous
        # une feuille technique DISPOSITION_N.
        disposition_buffer: List[str] = []
        disposition_page: Optional[int] = None
        disposition_end_page: Optional[int] = None
        disposition_counter = 0
        # Les notes après signature sont des feuilles racines NOTE_N, séparées
        # du signataire et du dispositif tout en restant citables.
        current_note: Optional[Dict[str, Any]] = None
        note_buffer: List[str] = []

        def close_article() -> None:
            """Finalise l'article courant — ou le retire silencieusement de
            l'arbre si son contenu ne s'avère être QUE du bruit de bandeau de
            page (cf. `_is_page_banner_noise`) : un numéro d'article répété en
            en-tête de page (avec parfois le Titre/Chapitre courant) juste
            avant que le VRAI article de même numéro ne commence, capté à tort
            comme un article fantôme. Remédiation 2026-08-02 phase 5 — confirmé
            sur le Code civil en prod (144 cas), toujours suivi d'un rappel de
            Titre/Chapitre/Section dupliqué qui referme l'article via cette
            fonction (`open_structure`), jamais directement par `open_article`."""
            nonlocal current_article
            if current_article is not None:
                inline = current_article.get("content", "").strip()
                block = "\n".join(content_buffer).strip()
                full_content = f"{inline}\n{block}".strip() if inline and block else (inline or block)
                if _is_page_banner_noise(full_content):
                    container = open_nodes[-1][1]["children"] if open_nodes else roots
                    if container and container[-1] is current_article:
                        container.pop()
                else:
                    current_article["content"] = full_content
            content_buffer.clear()
            current_article = None

        def attach_to_parent(node: Dict[str, Any]) -> None:
            if open_nodes:
                open_nodes[-1][1]["children"].append(node)
            else:
                roots.append(node)

        def close_disposition() -> None:
            """Attache le texte implicite à la division ouverte la plus fine."""
            nonlocal disposition_counter, disposition_page, disposition_end_page
            text = "\n".join(disposition_buffer).strip()
            disposition_buffer.clear()
            if not text:
                disposition_page = None
                disposition_end_page = None
                return

            disposition_counter += 1
            node = {
                "type": "DISPOSITION",
                "number": f"DISPOSITION_{disposition_counter}",
                "title": "",
                "content": text,
                "page": disposition_page,
                "page_end": disposition_end_page,
                "children": [],
            }
            attach_to_parent(node)
            disposition_page = None
            disposition_end_page = None

        def flush_preamble() -> None:
            """Émet le préambule bufferisé comme feuille de tête, une seule fois.

            Appelé à l'ouverture du premier élément structurel : le texte qui le
            précède (visas, considérants) devient un nœud PREAMBULE prepend en
            tête des roots. Si aucun élément structurel n'ouvre jamais, le buffer
            est ignoré (l'appelant retombe sur son fallback « texte intégral »).
            """
            nonlocal structure_opened
            if structure_opened:
                return
            structure_opened = True
            text = "\n".join(preamble_buffer).strip()
            preamble_buffer.clear()
            if text:
                roots.insert(0, {
                    "type": "PREAMBULE",
                    "number": "",
                    "title": "",
                    "content": text,
                    "page": preamble_page,
                    "children": [],
                })

        def close_signature() -> None:
            nonlocal current_signature
            if current_signature is not None:
                current_signature["content"] = "\n".join(signature_buffer).strip()
            signature_buffer.clear()
            current_signature = None

        def close_note() -> None:
            nonlocal current_note
            if current_note is not None:
                current_note["content"] = "\n".join(note_buffer).strip()
            note_buffer.clear()
            current_note = None

        def open_note(number: str, first_line: str) -> None:
            nonlocal current_note
            close_note()
            node = {
                "type": "NOTE",
                "number": f"NOTE_{number}",
                "title": "",
                "content": "",
                "page": current_page,
                "children": [],
            }
            roots.append(node)
            current_note = node
            if first_line:
                note_buffer.append(first_line)

        def open_signature(first_line: str) -> None:
            nonlocal current_signature
            # Une signature (« Fait à … ») marque la fin du dispositif : le texte
            # qui précède (qualité du signataire, visas « Vu … », considérants) est
            # un vrai préambule, même si AUCUN article/structure n'a été détecté
            # avant (acte court : nomination, décision, ou « Article » mal OCRisé).
            # Sans ce flush — contrairement à open_structure/open_article/open_table
            # — ce préambule était silencieusement perdu (roots = [SIGNATURE] seul).
            flush_preamble()
            close_note()
            close_disposition()
            close_signature()
            close_article()
            node = {
                "type": "SIGNATURE",
                "number": "",
                "title": "",
                "content": "",
                "page": current_page,
                "children": [],
            }
            # Toujours à la racine de l'acte (le signataire engage l'acte entier),
            # pas rattachée au dernier chapitre/section ouvert.
            roots.append(node)
            current_signature = node
            signature_buffer.append(first_line)

        def open_structure(level: str, number: str, title: str) -> None:
            nonlocal current_article
            flush_preamble()
            close_note()
            close_signature()
            close_article()
            # À faire AVANT de dépiler la structure : le bloc appartient à la
            # division qui se ferme, pas à celle que l'on va ouvrir.
            close_disposition()
            level_index = STRUCTURE_LEVELS.index(level)
            while open_nodes and open_nodes[-1][0] >= level_index:
                open_nodes.pop()

            node = {
                "type": level,
                "number": (number or "").strip(),
                "title": (title or "").strip(),
                "content": "",
                "page": current_page,
                "children": [],
            }
            attach_to_parent(node)
            open_nodes.append((level_index, node))

        def open_article(number: str, inline_content: str) -> None:
            nonlocal current_article
            flush_preamble()
            close_note()
            close_signature()
            close_disposition()
            close_article()
            node = {
                "type": "ARTICLE",
                "number": (number or "").strip(),
                "title": "",
                "content": (inline_content or "").strip(),
                "page": current_page,
                "children": [],
            }
            attach_to_parent(node)
            current_article = node

        def open_table(html: str) -> None:
            # Feuille autonome rattachée à la section courante (sœur des articles),
            # pas au contenu de l'article précédent.
            flush_preamble()
            close_note()
            close_signature()
            close_disposition()
            close_article()
            node = {
                "type": "TABLEAU",
                "number": "",
                "title": "",
                "content": (html or "").strip(),
                "page": current_page,
                "children": [],
            }
            attach_to_parent(node)

        lines = text.split("\n")
        index = 0
        while index < len(lines):
            stripped = lines[index].strip()
            index += 1
            if not stripped:
                continue

            page_marker = PAGE_MARKER_PATTERN.match(stripped)
            if page_marker:
                current_page = int(page_marker.group(1))
                continue

            if TABLE_HTML_PATTERN.match(stripped):
                fragment = [stripped]
                while (
                    not TABLE_HTML_CLOSE.search(fragment[-1])
                    and index < len(lines)
                    and len(fragment) < _TABLE_MAX_LINES
                ):
                    fragment.append(lines[index].strip())
                    index += 1
                open_table(" ".join(part for part in fragment if part))
                continue

            match_line = _clean_for_matching(stripped)
            if not match_line or _NOISE_PATTERN.match(match_line):
                continue

            if SIGNATURE_PATTERN.match(match_line):
                # « Fait à … » : clôt le dispositif et ouvre la feuille SIGNATURE.
                open_signature(match_line)
                continue

            footnote_match = FOOTNOTE_PATTERN.match(match_line)
            if footnote_match and (current_signature is not None or current_note is not None):
                close_signature()
                open_note(footnote_match.group(1), footnote_match.group(2).strip())
                continue

            article_match = ARTICLE_PATTERN.match(match_line)
            if article_match:
                article_num, article_content = _article_match_groups(article_match)
                open_article(article_num, article_content)
                continue

            structure_match = None
            inverted = PARTIE_INVERTED_PATTERN.match(match_line)
            if inverted:
                structure_match = ("PARTIE", inverted.group(1), inverted.group(2))
            else:
                for level in STRUCTURE_LEVELS:
                    m = STRUCTURE_PATTERNS[level].match(match_line)
                    if m:
                        structure_match = (level, m.group(1), m.group(2))
                        break
                if structure_match is None:
                    attached = ATTACHED_ROMAN_STRUCTURE_PATTERN.match(match_line)
                    if attached:
                        structure_match = (
                            attached.group("level").upper(),
                            attached.group("number"),
                            attached.group("title"),
                        )

            if structure_match:
                open_structure(*structure_match)
                continue

            if current_note is not None:
                if _SECTION_NOISE_PATTERN.match(match_line):
                    close_note()
                else:
                    note_buffer.append(match_line)
            elif current_signature is not None:
                # Contenu de la formule finale (nom du signataire…). Un en-tête de
                # rubrique du JO (ministère, partie…) clôt la signature et est ignoré
                # pour ne pas la polluer avec le début de l'acte suivant.
                if _SECTION_NOISE_PATTERN.match(match_line):
                    close_signature()
                else:
                    signature_buffer.append(match_line)
            elif current_article is not None:
                # Contenu d'article : on préserve les retours à la ligne
                # (listes à puces, alinéas) au lieu d'aplatir le texte.
                content_buffer.append(stripped)
            elif open_nodes and not open_nodes[-1][1]["title"]:
                # Titre d'unité écrit sur la ligne suivante (ex: "# TITRE II." puis
                # "DU CONTRAT DE TRAVAIL").
                open_nodes[-1][1]["title"] = match_line
            elif not structure_opened:
                # Préambule de l'acte : qualité du signataire, visas « Vu … »,
                # considérants — tout ce qui précède le premier article/structure.
                if preamble_page is None:
                    preamble_page = current_page
                preamble_buffer.append(match_line)
            elif open_nodes:
                # Corps d'une division sans en-tête « Article ». `match_line`
                # retire seulement les décorations Markdown de MinerU ; le texte
                # juridique et ses retours à la ligne restent intacts.
                if disposition_page is None:
                    disposition_page = current_page
                disposition_end_page = current_page
                disposition_buffer.append(match_line)

        close_note()
        close_signature()
        close_article()
        close_disposition()
        return roots
