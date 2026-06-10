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

# "ARTICLE 1er : contenu...", "Art. 12.-", "Article L.122-4", "ARTICLE PREMIER"
ARTICLE_PATTERN = re.compile(
    r"^(?:ARTICLE|ART)\.?\s+"
    r"(PREMIER|[LDRA]?\.?\s*\d+[a-zA-Z0-9\-]*(?:\s+(?:bis|ter|quater|quinquies))?)"
    r"\s*[:.\-–—]*\s*(.*)$",
    re.IGNORECASE,
)

# Forme inversée : "PREMIÈRE PARTIE — ...", "DEUXIÈME PARTIE : ..."
PARTIE_INVERTED_PATTERN = re.compile(
    rf"^({_NUMBER})\s+PARTIE{_SEP}(.*)$",
    re.IGNORECASE,
)

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

        def open_structure(level: str, number: str, title: str) -> None:
            nonlocal current_article
            close_article()
            level_index = STRUCTURE_LEVELS.index(level)
            while open_nodes and open_nodes[-1][0] >= level_index:
                open_nodes.pop()

            node = {
                "type": level,
                "number": (number or "").strip(),
                "title": (title or "").strip(),
                "content": "",
                "children": [],
            }
            attach_to_parent(node)
            open_nodes.append((level_index, node))

        def open_article(number: str, inline_content: str) -> None:
            nonlocal current_article
            close_article()
            node = {
                "type": "ARTICLE",
                "number": (number or "").strip(),
                "title": "",
                "content": (inline_content or "").strip(),
                "children": [],
            }
            attach_to_parent(node)
            current_article = node

        for raw_line in text.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue

            match_line = _clean_for_matching(stripped)
            if not match_line or _NOISE_PATTERN.match(match_line):
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

            if current_article is not None:
                # Contenu d'article : on préserve les retours à la ligne
                # (listes à puces, alinéas) au lieu d'aplatir le texte.
                content_buffer.append(stripped)
            elif open_nodes and not open_nodes[-1][1]["title"]:
                # Titre d'unité écrit sur la ligne suivante (ex: "# TITRE II." puis
                # "DU CONTRAT DE TRAVAIL").
                open_nodes[-1][1]["title"] = match_line

        close_article()
        return roots
