"""Triage pdftotext natif vs MinerU (étage 2 de l'usine à textes).

Extraction native rapide (PyMuPDF, SANS OCR) pour décider si un PDF est déjà
exploitable tel quel ou doit passer par MinerU. Deux signaux combinés :

- `chars_per_page` : un PDF scanné (image pure, sans couche texte) a ~0
  caractère natif par page, quel que soit le score de lisibilité —
  `compute_ocr_quality` renvoie un score NEUTRE de 1.0 sur un texte vide (« pas
  de dégradation mesurable »), ce qui serait trompeur pris seul ici.
- `compute_ocr_quality(score)` : le MÊME indicateur que celui utilisé après
  extraction MinerU côté API (réutilisé tel quel — un seul calcul de qualité
  dans tout le pipeline, pas une seconde heuristique qui pourrait diverger).

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
from typing import Any, Dict, List, Optional

from src.extractor.text_quality import OCR_QUALITY_WARN_THRESHOLD, compute_ocr_quality

logger = logging.getLogger(__name__)

# En dessous de ce nombre de caractères natifs par page, on considère qu'il n'y
# a pas de couche texte exploitable (PDF scanné/image) — quel que soit le score
# de lisibilité (neutre à 1.0 sur un texte vide, donc trompeur pris seul).
MIN_CHARS_PER_PAGE = 50


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

    return TriageResult(
        method="native",
        reason=f"texte natif exploitable (score {quality['score']}, {chars_per_page:.0f} car./page)",
        quality=quality,
        num_pages=num_pages,
        chars_per_page=chars_per_page,
        text=render_native_markdown(pages),
    )
