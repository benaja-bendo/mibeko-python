"""Comptage du nombre de pages d'un PDF.

Le dossier de travail laisse une IA exterieure poser un repere de page sur
chaque article. Sans nombre de pages en base, rien ne distingue « page 12 » de
« page 412 » sur un PDF qui en compte 64 : c'est l'erreur la plus banale d'une
correction automatique, et la plus silencieuse. La colonne `media_files.page_count`
alimente le controle cote Laravel (migration 2026_08_29_140000).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compter_pages_pdf(chemin: str) -> Optional[int]:
    """Retourne le nombre de pages, ou None si le fichier est illisible.

    Ne leve jamais : un PDF corrompu ou une dependance absente ne doit pas faire
    echouer une ingestion qui, par ailleurs, reussit. La valeur reste alors
    inconnue et le controle aval se tait — c'est le comportement voulu.
    """
    try:
        import fitz  # PyMuPDF

        with fitz.open(chemin) as doc:
            pages = doc.page_count

        return pages if pages > 0 else None
    except Exception as exc:  # noqa: BLE001 — voir docstring
        logger.warning("Nombre de pages illisible pour %s : %s", chemin, exc)

        return None
