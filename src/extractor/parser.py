import logging
import re
from typing import Any, Dict, List, Optional, Tuple

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

# "ARTICLE 1er : contenu...", "Art. 12.-", "Article L.122-4", "ARTICLE PREMIER",
# "Art.4.‐ ..." (sans espace, tiret Unicode ‐ fréquent dans les Actes OHADA).
# Le qualificatif "nouveau/nouvelle" est capturé DANS le numéro ("Article 2
# nouveau" -> "2 nouveau") : dans un acte modificatif il désigne le texte de
# remplacement d'un article cité, à ne pas confondre avec l'article d'exécution
# de même numéro (sinon doublon + fausse alerte de curation).
ARTICLE_PATTERN = re.compile(
    r"^(?:ARTICLE|ART)\.?\s*"
    r"(PREMIER|[LDRA]?\.?\s*\d+[a-zA-Z0-9\-]*(?:\s+(?:bis|ter|quater|quinquies|nouveau|nouvelle|nouveaux|nouvelles))?)"
    r"\s*[:.\-–—‐]*\s*(.*)$",
    re.IGNORECASE,
)

# Formule finale d'un acte : « Fait à Brazzaville, le 18 avril 2026 » suivie du
# nom du ou des signataires. Isolée en feuille SIGNATURE plutôt que collée au
# contenu du dernier article. On exige « le <jour> » après le lieu pour ne pas
# confondre avec une ligne d'article qui débuterait par « Fait à … » (le « l »
# de « le » est parfois OCRisé en « I » majuscule).
SIGNATURE_PATTERN = re.compile(r"^Fait\s+à\b.*\b[lI]e\s+\d", re.IGNORECASE)

# En-têtes de rubrique d'un Journal Officiel (ministère, partie, sous-section…)
# qui suivent parfois la signature du dernier acte d'une section. Servent
# UNIQUEMENT, en mode signature, à clore la feuille SIGNATURE sans la polluer.
_SECTION_NOISE_PATTERN = re.compile(
    r"^(?:MINIST[EÈ]RE|PR[EÉ]SIDENCE|PRIMATURE|PARTIE\b|ANNONCES|ACTE\s+EN\s+ABR[EÉ]G[EÉ]|"
    r"[AB]\s*[-–—]\s|[-–—]\s*(?:DECRET|TEXTES|ANNONCES))",
    re.IGNORECASE,
)

# Forme inversée : "PREMIÈRE PARTIE — ...", "DEUXIÈME PARTIE : ..."
PARTIE_INVERTED_PATTERN = re.compile(
    rf"^({_NUMBER})\s+PARTIE{_SEP}(.*)$",
    re.IGNORECASE,
)

# Marqueur de page injecté par l'extraction JSON MinerU (cf.
# extract_text_from_mineru_json) : permet de tamponner chaque nœud avec sa page
# d'origine (1-based) pour la citabilité « page N ». Absent des entrées markdown.
PAGE_MARKER_PATTERN = re.compile(r"^\[\[MIBEKO_PAGE:(\d+)\]\]$")

# Tableau HTML émis par l'extraction JSON MinerU (grilles salariales, etc.) sur
# une seule ligne. Routé vers un nœud feuille TABLEAU plutôt que noyé dans
# l'article courant.
TABLE_HTML_PATTERN = re.compile(r"^<table[\s>]", re.IGNORECASE)

# Lignes de bruit à ignorer : images markdown, filets, numéros de page isolés.
_NOISE_PATTERN = re.compile(r"^(?:!\[.*|[-_*=]{3,}|\d{1,3}|[o0]{3,})$")

# Préfixes markdown à retirer pour la détection (mais pas du contenu).
_MD_PREFIX = re.compile(r"^[#>\s]*[*_]{0,3}\s*")
_MD_SUFFIX = re.compile(r"\s*[*_]{1,3}$")


def _clean_for_matching(line: str) -> str:
    """Retire les décorations markdown (titres, gras) pour tester les regex."""

    cleaned = _MD_PREFIX.sub("", line.strip())
    cleaned = _MD_SUFFIX.sub("", cleaned)
    return cleaned.strip()


class LegalDocumentParser:
    """
    Parseur de structure hiérarchique d'un texte juridique (code, loi, décret)
    depuis un texte brut ou un markdown OCRisé (sortie MinerU).

    Produit une liste de noeuds racines de la forme :
    {"type": "TITRE", "number": "I", "title": "...", "content": "", "children": [...]}
    Les ARTICLEs portent leur texte dans "content" (retours à la ligne préservés).

    Le texte qui précède le premier élément structurel d'un acte (qualité du
    signataire, visas « Vu … », considérants) est émis comme feuille PREAMBULE
    en tête des roots — mais uniquement si un ARTICLE/structure suit.
    """

    def __init__(self, pdf_path: Optional[str] = None, text_content: Optional[str] = None):
        self.pdf_path = pdf_path
        self.text_content = text_content

    def extract_text(self) -> str:
        """Retourne le texte fourni ou l'extrait du PDF via PyMuPDF (OCR si dispo)."""

        if self.text_content:
            return self.text_content

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

        return "\n".join(full_text)

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

        def close_article() -> None:
            nonlocal current_article
            if current_article is not None:
                inline = current_article.get("content", "").strip()
                block = "\n".join(content_buffer).strip()
                current_article["content"] = f"{inline}\n{block}".strip() if inline and block else (inline or block)
            content_buffer.clear()
            current_article = None

        def attach_to_parent(node: Dict[str, Any]) -> None:
            if open_nodes:
                open_nodes[-1][1]["children"].append(node)
            else:
                roots.append(node)

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

        def open_signature(first_line: str) -> None:
            nonlocal current_signature
            # Une signature (« Fait à … ») marque la fin du dispositif : le texte
            # qui précède (qualité du signataire, visas « Vu … », considérants) est
            # un vrai préambule, même si AUCUN article/structure n'a été détecté
            # avant (acte court : nomination, décision, ou « Article » mal OCRisé).
            # Sans ce flush — contrairement à open_structure/open_article/open_table
            # — ce préambule était silencieusement perdu (roots = [SIGNATURE] seul).
            flush_preamble()
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
            close_signature()
            close_article()
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
            close_signature()
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
            close_signature()
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

        for raw_line in text.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue

            page_marker = PAGE_MARKER_PATTERN.match(stripped)
            if page_marker:
                current_page = int(page_marker.group(1))
                continue

            if TABLE_HTML_PATTERN.match(stripped):
                open_table(stripped)
                continue

            match_line = _clean_for_matching(stripped)
            if not match_line or _NOISE_PATTERN.match(match_line):
                continue

            if SIGNATURE_PATTERN.match(match_line):
                # « Fait à … » : clôt le dispositif et ouvre la feuille SIGNATURE.
                open_signature(match_line)
                continue

            article_match = ARTICLE_PATTERN.match(match_line)
            if article_match:
                open_article(article_match.group(1), article_match.group(2))
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

            if structure_match:
                open_structure(*structure_match)
                continue

            if current_signature is not None:
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

        close_signature()
        close_article()
        return roots
