"""Triage pdftotext natif vs OCR (étage 2 de l'usine à textes).

Extraction native rapide (PyMuPDF, SANS OCR) pour décider si un PDF est déjà
exploitable tel quel ou doit passer par l'OCR (Mistral OCR par défaut, ou
MinerU en repli — cf. `src/parsing/batch.py`, `OCR_BACKEND`). Trois signaux
combinés :

- `chars_per_page` : un PDF scanné (image pure, sans couche texte) a ~0
  caractère natif par page, quel que soit le score de lisibilité —
  `compute_ocr_quality` renvoie un score NEUTRE de 1.0 sur un texte vide (« pas
  de dégradation mesurable »), ce qui serait trompeur pris seul ici.
- `compute_ocr_quality(score)` : le MÊME indicateur que celui utilisé après
  extraction OCR côté API (réutilisé tel quel — un seul calcul de qualité
  dans tout le pipeline, pas une seconde heuristique qui pourrait diverger).
- Ratio de mots d'une lettre PAGE PAR PAGE (`MAX_SINGLE_LETTER_WORD_RATIO`,
  `MIN_DEGRADED_PAGES`) : détecte un OCR pré-existant dégradé qui passe les
  deux signaux ci-dessus (texte natif présent, score global correct) mais
  dont plusieurs pages contiennent des mots fragmentés en lettres isolées.
  Absorbe mibeko-python#6.

Note : `LegalDocumentParser.extract_text()` (src/extractor/parser.py) tente
`page.get_textpage_ocr(...)` avant de retomber sur `get_text("text")` — c'est
approprié pour LE parsing final, mais inadapté au triage : on a justement
besoin de savoir si le PDF a DÉJÀ une couche texte, pas d'en produire une à la
volée (get_textpage_ocr nécessite Tesseract et coûte cher sur un lot entier).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.extractor.text_quality import OCR_QUALITY_WARN_THRESHOLD, compute_ocr_quality

logger = logging.getLogger(__name__)

# En dessous de ce nombre de caractères natifs par page, on considère qu'il n'y
# a pas de couche texte exploitable (PDF scanné/image) — quel que soit le score
# de lisibilité (neutre à 1.0 sur un texte vide, donc trompeur pris seul).
#
# Relevé de 50 à 300 le 14/09/2026 (mibeko-python#22, absorbe #6) : le seuil de
# 50 laissait passer des PDF dont les seuls caractères natifs étaient les
# numéros de page imprimés — un JO à 69 car./page avait été classé « texte
# natif exploitable » alors qu'aucun contenu réel n'était présent (constat
# docs/pipeline/protocole-validation.md, § « ce qui a disparu avant même
# d'arriver au texte »).
MIN_CHARS_PER_PAGE = 300

# Ratio de « mots » réduits à une seule lettre (hors mots légitimes comme "à",
# "y" — cf. compute_ocr_quality) au-delà duquel une PAGE est jugée dégradée.
#
# Calibré empiriquement le 14/09/2026 en trois passes sur les 1349 documents
# déjà triés « native » du corpus local — chaque piège rencontré est resté en
# commentaire pour ne pas le retrouver à la prochaine recalibration :
#   1. Un seuil de 2 % sur le texte AGRÉGÉ (document entier) reproduisait bien
#      les 22 % déjà cités par mibeko-python#6 (301/1347) — mais un document
#      où seules quelques pages sur 76 sont dégradées voit son ratio DILUÉ
#      sous tout seuil raisonnable une fois agrégé (cas nommé par #6,
#      `code-penal/Penal-Code-1836.pdf` : ratio agrégé 0,8 % alors que ses
#      pages 2/4/6 sont à 13,3/4,2/3,6 %). D'où le calcul PAGE PAR PAGE
#      ci-dessous plutôt que sur le texte concaténé.
#   2. Mais un seuil de 2 % appliqué À CHAQUE PAGE (une seule page suffisant à
#      router tout le document) fait basculer 89,9 % du corpus : le ratio
#      d'une page isolée est un échantillon petit et bruyant — une poignée de
#      mots d'une lettre incidents (sigles, énumérations) suffit à dépasser
#      2 % sur une page sans rien dire de la qualité OCR.
#   3. Un seuil à 6 % avec `MIN_DEGRADED_PAGES` = 2 retombe à 22,6 % du corpus
#      (305/1349), très proche du chiffre #6 — mais RATE `Penal-Code-1836.pdf`
#      lui-même : sa page 2 (13,3 %) tombe sous `MIN_WORDS_FOR_PAGE_RATIO` (45
#      mots < 50) et ses pages 4/6 (4,2 % / 3,6 %) sous 6 % ne totalisent
#      qu'UNE page qualifiante, pas deux. Faire correspondre le pourcentage
#      global sans capturer le cas nommé qui a motivé le ticket ne suffit pas.
#   4. Seuil à 3,5 % (garde `MIN_DEGRADED_PAGES` = 2, `MIN_WORDS_FOR_PAGE_RATIO`
#      = 50) : capture ses pages 4 ET 6 (4,2 % et 3,6 %, toutes deux > 3,5 %),
#      ainsi que les JO 2005-2012 cités par #6 comme les pires du corpus —
#      au prix d'un corpus plus large basculé vers l'OCR (34,8 %, ~470
#      documents). Assumé : à 4 $/1000 pages, le re-OCR d'un document qui se
#      révèle déjà propre coûte quelques centimes ; ne pas re-OCRiser un
#      document réellement dégradé coûte un texte juridique faux en
#      production. Dans le doute, réOCRiser — d'autant que Mistral OCR
#      restitue mieux les tableaux même sur du contenu déjà propre (vérifié
#      sur congo-jo-2007-17, rapport de viabilité du 14/09/2026).
MAX_SINGLE_LETTER_WORD_RATIO = 0.035

# Nombre minimal de pages significatives (cf. MIN_WORDS_FOR_PAGE_RATIO) devant
# dépasser MAX_SINGLE_LETTER_WORD_RATIO avant de router tout le document vers
# l'OCR — une seule page dégradée peut être un tableau isolé (colonnes M/F,
# sigles pointés) plutôt qu'un vrai symptôme de mauvais OCR sur le document.
MIN_DEGRADED_PAGES = 2


@dataclass
class TriageResult:
    method: str                      # "native" ou "mineru"
    reason: str
    quality: Dict[str, Any]
    num_pages: int
    chars_per_page: float
    text: Optional[str] = None       # markdown avec marqueurs de page, si method == "native"


def extract_native_text_by_page(pdf_path: Path) -> List[str]:
    """Texte natif par page, SANS OCR. Une page sans couche texte renvoie ''.

    Un PDF illisible (0 octet, tronqué, non-PDF) renvoie une liste vide plutôt
    que de lever : sgg.cg sert réellement de tels fichiers (congo-jo-2026-17.pdf
    fait 0 octet à la source, congo-jo-2025-45.pdf 822 octets), et sans ce
    garde-fou un seul d'entre eux interrompait tout un lot de plusieurs
    centaines de documents. L'appelant voit alors 0 page / 0 car. et route le
    document vers MinerU, qui échouera proprement et sera signalé.
    """
    import fitz  # PyMuPDF — import différé (convention déjà suivie par parser.py)

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # fitz lève EmptyFileError / FileDataError selon le cas
        logger.warning("PDF illisible, ignoré au triage : %s", pdf_path)
        return []
    try:
        return [page.get_text("text") for page in doc]
    except Exception:
        logger.warning("PDF partiellement illisible, ignoré au triage : %s", pdf_path)
        return []
    finally:
        doc.close()


def render_native_markdown(pages: List[str]) -> str:
    """Assemble les pages natives avec des marqueurs `[[MIBEKO_PAGE:N]]`
    (même convention que la sortie MinerU annotée, 1-based) : le parser
    existant (PAGE_MARKER_PATTERN) offre ainsi la citabilité par page même
    sur le chemin rapide 'native', sans traitement supplémentaire en aval.
    """
    lines: List[str] = []
    for index, page_text in enumerate(pages, start=1):
        lines.append(f"[[MIBEKO_PAGE:{index}]]")
        lines.append(page_text.strip())
    return "\n".join(lines)


# Nombre minimal de "mots" sur une page pour que son ratio de mots d'une
# lettre soit un signal, pas du bruit d'échantillon (une page de titre à 9
# caractères peut afficher un ratio de 60 % sans rien dire de la qualité OCR).
MIN_WORDS_FOR_PAGE_RATIO = 50


def _degraded_pages(pages: List[str]) -> List[Tuple[int, float]]:
    """Pages dont le ratio de mots d'une lettre dépasse
    `MAX_SINGLE_LETTER_WORD_RATIO`, parmi celles ayant assez de mots pour être
    significatives. Renvoie [(numéro de page 1-based, ratio), …], triées par
    ratio décroissant.

    Calculé PAGE PAR PAGE, jamais sur le texte concaténé : un document de 76
    pages où seules quelques-unes sont dégradées dilue un ratio agrégé sous
    tout seuil raisonnable (constaté sur `code-penal/Penal-Code-1836.pdf`,
    cas nommé par mibeko-python#6 — ratio agrégé 0,8 %, mais ses pages 2 et 4
    à 13,3 % et 4,2 % : « modiflés », « Ia loi », « (iénCral de l,'A.l,.F. »).
    """
    degraded: List[Tuple[int, float]] = []
    for index, page_text in enumerate(pages, start=1):
        page_quality = compute_ocr_quality(page_text)
        total_words = page_quality["signals"]["total_words"]
        if total_words < MIN_WORDS_FOR_PAGE_RATIO:
            continue
        single_letter = page_quality["signals"]["single_letter_word_count"]
        ratio = single_letter / total_words
        if ratio > MAX_SINGLE_LETTER_WORD_RATIO:
            degraded.append((index, ratio))
    degraded.sort(key=lambda item: item[1], reverse=True)
    return degraded


def triage_pdf(pdf_path: Path, threshold: float = OCR_QUALITY_WARN_THRESHOLD) -> TriageResult:
    """Décide 'native' ou 'mineru' pour un PDF donné. Ne modifie rien sur disque."""
    pages = extract_native_text_by_page(pdf_path)
    num_pages = len(pages) or 1
    raw_text = "\n".join(page.strip() for page in pages if page.strip())
    quality = compute_ocr_quality(raw_text)
    chars_per_page = quality["total_chars"] / num_pages

    if chars_per_page < MIN_CHARS_PER_PAGE:
        return TriageResult(
            method="mineru",
            reason=(
                f"texte natif quasi nul ({chars_per_page:.0f} car./page "
                f"< {MIN_CHARS_PER_PAGE}) — probable scan sans couche texte"
            ),
            quality=quality,
            num_pages=num_pages,
            chars_per_page=chars_per_page,
        )

    if quality["score"] < threshold:
        return TriageResult(
            method="mineru",
            reason=f"qualité native insuffisante (score {quality['score']} < seuil {threshold})",
            quality=quality,
            num_pages=num_pages,
            chars_per_page=chars_per_page,
        )

    degraded = _degraded_pages(pages)
    if len(degraded) >= MIN_DEGRADED_PAGES:
        worst_page, worst_ratio = degraded[0]
        pages_listees = ", ".join(f"{p} ({r:.1%})" for p, r in degraded[:5])
        reste = f" (+{len(degraded) - 5} autres)" if len(degraded) > 5 else ""
        return TriageResult(
            method="mineru",
            reason=(
                f"{len(degraded)} pages avec un ratio de mots d'une lettre "
                f"> {MAX_SINGLE_LETTER_WORD_RATIO:.1%} (pire : page {worst_page}, "
                f"{worst_ratio:.1%}) — {pages_listees}{reste} — OCR pré-existant "
                "dégradé ou tableau/colonnes mal linéarisés probables"
            ),
            quality=quality,
            num_pages=num_pages,
            chars_per_page=chars_per_page,
        )

    return TriageResult(
        method="native",
        reason=f"texte natif exploitable (score {quality['score']}, {chars_per_page:.0f} car./page)",
        quality=quality,
        num_pages=num_pages,
        chars_per_page=chars_per_page,
        text=render_native_markdown(pages),
    )
