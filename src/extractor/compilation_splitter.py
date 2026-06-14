"""
Découpage d'une compilation en sous-documents (G1).

Un recueil comme le « Code Bleu OHADA » regroupe une dizaine d'Actes uniformes,
dont la numérotation d'articles repart à 1 pour chaque acte. L'ingérer comme un
seul document produit des doublons d'articles et des citations ambiguës. On le
découpe donc en N sous-documents (un par Acte, typé AU).

La détection automatique des frontières n'est pas fiable sur ces recueils annotés
(peu d'actes portent un titre « ACTE UNIFORME », et les sujets se mêlent aux
sous-titres). La démarche est donc ASSISTÉE :

1. ``suggest_compilation_boundaries`` propose des bornes candidates (page + libellé) ;
2. un humain les valide/corrige (fichier ``boundaries.json``) ;
3. ``slice_mineru_json_by_boundaries`` découpe le JSON MinerU fusionné en N
   sous-JSON, chacun réutilisable tel quel par le flux d'upload existant
   (auto-typé AU, parsé avec ses pages).

``page_idx`` est conservé (global au PDF d'origine) : les citations « page N »
renvoient toujours à la pagination du recueil source.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Sujets canoniques des Actes uniformes OHADA : aide à la suggestion de bornes.
_OHADA_SUBJECTS: List[Tuple[str, str]] = [
    (r"DROIT COMMERCIAL G[EÉ]N[EÉ]RAL", "AUDCG — Droit commercial général"),
    (r"SOCI[EÉ]T[EÉ]S COMMERCIALES", "AUSCGIE — Sociétés commerciales et GIE"),
    (r"S[UÛ]RET[EÉ]S", "AUS — Sûretés"),
    (r"VOIES D.EX[EÉ]CUTION|PROC[EÉ]DURES SIMPLIFI[EÉ]ES", "AUPSRVE — Voies d'exécution"),
    (r"PROC[EÉ]DURES COLLECTIVES", "AUPCAP — Procédures collectives"),
    (r"DROIT DE L.ARBITRAGE", "AUA — Arbitrage"),
    (r"COMPTABILIT|SYST[EÈ]ME COMPTABLE", "AUDCIF — Comptabilité"),
    (r"TRANSPORT DE MARCHANDISES", "AUCTMR — Transport de marchandises"),
    (r"SOCI[EÉ]T[EÉ]S COOP[EÉ]RATIVES", "AUSCOOP — Sociétés coopératives"),
    (r"M[EÉ]DIATION", "AUM — Médiation"),
]


def _block_text(block: Dict) -> str:
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


def suggest_compilation_boundaries(merged_json: Dict) -> List[Dict]:
    """Propose des bornes candidates à curer : titres courts repérés comme début
    d'acte (« ACTE UNIFORME … ») ou correspondant à un sujet OHADA canonique.

    Renvoie [{start_page (1-based), title, suggested, type_code}], à VALIDER par un
    humain (la détection est volontairement permissive, des faux positifs sont
    attendus).
    """
    subjects = [(re.compile(pattern, re.IGNORECASE), label) for pattern, label in _OHADA_SUBJECTS]
    boundaries: List[Dict] = []
    seen: set = set()

    for page in merged_json.get("pdf_info", []) or []:
        page_idx = page.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        for block in page.get("preproc_blocks", []) or []:
            if block.get("type") != "title":
                continue
            text = _block_text(block)
            if not text or len(text) > 90:
                continue

            label: Optional[str] = None
            if text.upper().startswith("ACTE UNIFORME"):
                label = text
            else:
                for regex, canonical in subjects:
                    if regex.search(text):
                        label = canonical
                        break
            if not label:
                continue

            start_page = page_idx + 1
            key = (start_page, label)
            if key in seen:
                continue
            seen.add(key)
            boundaries.append({
                "start_page": start_page,
                "title": text,
                "suggested": label,
                "type_code": "AU",
            })

    return boundaries


def slice_mineru_json_by_boundaries(
    merged_json: Dict,
    boundaries: List[Dict],
) -> List[Tuple[Dict, Dict]]:
    """Découpe le JSON MinerU en sous-JSON selon les bornes validées.

    Chaque acte va de sa ``start_page`` (incluse) à la page précédant la borne
    suivante. ``page_idx`` est conservé (pagination globale du recueil). Renvoie
    [(borne, sous_json)] dans l'ordre des pages.
    """
    ordered = sorted(
        (b for b in boundaries if b.get("start_page")),
        key=lambda b: b["start_page"],
    )
    pages = merged_json.get("pdf_info", []) or []
    metadata = {key: value for key, value in merged_json.items() if key != "pdf_info"}

    result: List[Tuple[Dict, Dict]] = []
    for index, boundary in enumerate(ordered):
        start = boundary["start_page"]
        end = ordered[index + 1]["start_page"] - 1 if index + 1 < len(ordered) else None

        sub_pages = [
            page for page in pages
            if isinstance(page.get("page_idx"), int)
            and (page["page_idx"] + 1) >= start
            and (end is None or (page["page_idx"] + 1) <= end)
        ]
        sub_json = {**metadata, "pdf_info": sub_pages}
        sub_json["_mibeko_split"] = {
            "title": boundary.get("title"),
            "type_code": boundary.get("type_code", "AU"),
            "page_start": start,
            "page_end": end,
            "page_count": len(sub_pages),
        }
        result.append((boundary, sub_json))

    return result
