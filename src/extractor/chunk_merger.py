"""
Fusion de chunks MinerU en un document logique unique.

Contexte
--------
Les très gros PDF (Code Bleu OHADA ~1071 p., Encyclopédie ~2191 p.) dépassent
les limites d'extraction de MinerU. On découpe donc le PDF en tranches de pages
*avant* extraction, ce qui produit plusieurs fichiers ``.md`` / ``.json`` nommés
``chunk_{debut}_a_{fin}.ext``. Or le pipeline d'ingestion n'accepte qu'un seul
fichier : ce module recompose un ``.json`` (et un ``.md`` de secours) unique et
cohérent.

Deux points d'entrée :

- **CLI / dossier** : ``merge_folder`` lit ``chunk_*.json`` / ``chunk_*.md``.
- **Upload web (en mémoire)** : ``merge_json_chunks`` / ``merge_markdown_chunks``
  reçoivent une liste ``[(nom_fichier, contenu)]`` (les fichiers envoyés depuis
  ``/editor/ingestion``).

Particularité vérifiée sur le Code Bleu
---------------------------------------
MinerU numérote les pages **localement** par chunk : ``page_idx`` repart de 0
dans chaque tranche. À la fusion on **ré-offsette** ``page_idx`` avec la page de
départ du chunk afin de conserver la pagination globale, indispensable à la
citabilité (« page 1023 ») via ``article_versions.source_locator``.

Ordre des morceaux
------------------
On s'appuie d'abord sur le nom de fichier ``chunk_{debut}_a_{fin}`` (offset exact
= ``debut - 1``). Si un nom ne respecte pas ce format, on bascule en mode
« ordre d'upload » : les morceaux sont concaténés dans l'ordre reçu et les pages
sont **cumulées** (offset = nombre de pages déjà fusionnées), avec un
avertissement car la pagination peut alors être imprécise.

Ce module ne touche pas à la base de données : il produit des données fusionnées.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple, Union

Content = Union[str, bytes, bytearray]

# chunk_1_a_200.json / chunk_1001_a_1071.md ...
CHUNK_RE = re.compile(r"chunk_(\d+)_a_(\d+)\.(json|md)$", re.IGNORECASE)


def parse_chunk_range(filename: str) -> Optional[Tuple[int, int]]:
    """Extrait (page_debut, page_fin) du nom de fichier, sinon None."""
    match = CHUNK_RE.search(os.path.basename(filename or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def check_contiguity(ranges: List[Tuple[int, int]]) -> List[str]:
    """Signale trous et chevauchements entre tranches successives (triées)."""
    warnings: List[str] = []
    for (prev_start, prev_end), (cur_start, cur_end) in zip(ranges, ranges[1:]):
        if cur_start == prev_end + 1:
            continue
        if cur_start <= prev_end:
            warnings.append(
                f"Chevauchement : pages {prev_start}-{prev_end} et {cur_start}-{cur_end}"
            )
        else:
            warnings.append(f"Trou : rien entre la page {prev_end} et la page {cur_start}")
    return warnings


def _as_text(content: Content) -> str:
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="ignore")
    return content


def _order_items(
    items: List[Tuple[str, Content]],
) -> Tuple[List[Tuple[Optional[Tuple[int, int]], str, Content]], str, List[str]]:
    """Ordonne les morceaux. Renvoie (items_ordonnés, mode, warnings).

    mode == 'filename' si tous les noms respectent ``chunk_{a}_a_{b}`` → tri par
    page de début. Sinon mode == 'order' (ordre d'upload, pagination cumulée).
    """
    parsed = [(parse_chunk_range(name), name, content) for name, content in items]
    all_named = all(rng is not None for rng, _name, _c in parsed)
    warnings: List[str] = []

    if all_named and len(parsed) > 1:
        parsed.sort(key=lambda triple: triple[0][0])  # type: ignore[index]
        warnings = check_contiguity([rng for rng, _n, _c in parsed])  # type: ignore[misc]
        return parsed, "filename", warnings

    if len(parsed) > 1:
        warnings.append(
            "Noms de fichiers non conformes à « chunk_{début}_a_{fin} » : fusion dans "
            "l'ordre d'upload avec pagination cumulée (peut être imprécise)."
        )
    return parsed, "order", warnings


# ---------------------------------------------------------------------------
# Fusion en mémoire (utilisée par l'upload web)
# ---------------------------------------------------------------------------

def merge_json_chunks(items: List[Tuple[str, Content]]) -> Tuple[Dict, List[str]]:
    """Fusionne des JSON MinerU (en mémoire) en un seul, à pagination globale.

    ``items`` : liste ``[(nom_fichier, contenu_json)]``. Renvoie (json_fusionné,
    avertissements).
    """
    parsed, mode, warnings = _order_items(items)
    merged: Optional[Dict] = None
    cumulative = 0

    for rng, _name, content in parsed:
        data = json.loads(_as_text(content))
        pages = data.get("pdf_info", []) or []
        offset = (rng[0] - 1) if (mode == "filename" and rng is not None) else cumulative
        for page in pages:
            if isinstance(page.get("page_idx"), int):
                page["page_idx"] += offset
        if merged is None:
            merged = {key: value for key, value in data.items() if key != "pdf_info"}
            merged["pdf_info"] = []
        merged["pdf_info"].extend(pages)
        cumulative += len(pages)

    if merged is None:
        merged = {"pdf_info": []}
    merged["_mibeko_merge"] = {
        "mode": mode,
        "source_chunks": [name for _r, name, _c in parsed],
        "total_pages": cumulative,
        "warnings": warnings,
    }
    return merged, warnings


def merge_markdown_chunks(items: List[Tuple[str, Content]]) -> Tuple[str, List[str]]:
    """Concatène des markdown MinerU (en mémoire) dans l'ordre des pages."""
    parsed, _mode, warnings = _order_items(items)
    parts: List[str] = []
    for _rng, name, content in parsed:
        parts.append(f"<!-- chunk {os.path.basename(name)} -->\n{_as_text(content).strip()}")
    return "\n\n".join(parts), warnings


# ---------------------------------------------------------------------------
# Fusion depuis un dossier (utilisée par la CLI `merge-chunks`)
# ---------------------------------------------------------------------------

def _read_chunk_items(folder: str, ext: str) -> List[Tuple[str, bytes]]:
    items: List[Tuple[str, bytes]] = []
    for name in os.listdir(folder):
        if not name.lower().endswith("." + ext.lower()):
            continue
        if parse_chunk_range(name) is None:
            continue
        with open(os.path.join(folder, name), "rb") as handle:
            items.append((name, handle.read()))
    return items


def merge_mineru_json(folder: str) -> Dict:
    """Fusionne les ``chunk_*.json`` d'un dossier en un JSON MinerU unique."""
    items = _read_chunk_items(folder, "json")
    if not items:
        raise FileNotFoundError(f"Aucun fichier chunk_*.json dans {folder}")
    merged, _warnings = merge_json_chunks(items)
    return merged


def merge_markdown(folder: str) -> str:
    """Concatène les ``chunk_*.md`` d'un dossier dans l'ordre des pages."""
    items = _read_chunk_items(folder, "md")
    merged, _warnings = merge_markdown_chunks(items)
    return merged


def _block_text(block: Dict) -> str:
    """Texte concaténé des spans d'un bloc MinerU (ignore les spans table/html)."""
    parts: List[str] = []
    for line in block.get("lines", []) or []:
        parts.append(
            " ".join(
                span.get("content", "")
                for span in line.get("spans", []) or []
                if span.get("content")
            )
        )
    return " ".join(parts).strip()


def detect_acte_uniforme_boundaries(merged_json: Dict) -> List[Dict]:
    """Repère les débuts d'Actes uniformes pour le découpage en sous-documents (G1).

    Heuristique conservatrice : un bloc de type ``title`` dont le texte commence
    par « ACTE UNIFORME ». C'est une aide à la curation (frontières + page de
    début), pas une vérité : un juriste valide ensuite le découpage.
    """
    boundaries: List[Dict] = []
    for page in merged_json.get("pdf_info", []) or []:
        page_idx = page.get("page_idx")
        for block in page.get("preproc_blocks", []) or []:
            if block.get("type") != "title":
                continue
            text = _block_text(block)
            if text.upper().startswith("ACTE UNIFORME"):
                if boundaries and boundaries[-1]["title"] == text[:200]:
                    continue
                boundaries.append({"page": page_idx, "title": text[:200]})
    return boundaries


def merge_folder(folder: str, output_basename: Optional[str] = None,
                 detect_actes: bool = True) -> Dict:
    """Orchestre la fusion d'un dossier : écrit ``{base}.merged.json`` et ``.merged.md``."""
    base = output_basename or os.path.basename(os.path.normpath(folder))
    merged_json = merge_mineru_json(folder)
    merged_md = merge_markdown(folder)

    json_path = os.path.join(folder, f"{base}.merged.json")
    md_path = os.path.join(folder, f"{base}.merged.md")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(merged_json, handle, ensure_ascii=False)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(merged_md)

    boundaries = detect_acte_uniforme_boundaries(merged_json) if detect_actes else []
    meta = merged_json.get("_mibeko_merge", {})

    return {
        "json_path": json_path,
        "md_path": md_path,
        "total_pages": meta.get("total_pages", 0),
        "warnings": meta.get("warnings", []),
        "acte_boundaries": boundaries,
    }
