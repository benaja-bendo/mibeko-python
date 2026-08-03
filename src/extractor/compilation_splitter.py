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

# Marqueurs de début d'acte encapsulé dans un recueil NON-OHADA (code pénal AEF,
# recueils de textes usuels…). OCR-tolérant : accents optionnels, « N° » sous ses
# variantes (N°, Nº, No, N0). Le mapping de type est volontairement grossier
# (5 familles) — la classification fine reste celle de detect_texte_type
# (src/api/main.py). La détection est PERMISSIVE : ces bornes sont des candidates
# à valider par un humain (cf. docstring du module).
_ACT_NUM = r"N\s*[°ºo0]?\s*\d"
_ACT_BOUNDARY_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"^LOI\s+(?:CONSTITUTIONNELLE|ORGANIQUE)\b", re.IGNORECASE), "LOI"),
    (re.compile(rf"^LOI\s+{_ACT_NUM}", re.IGNORECASE), "LOI"),
    (re.compile(rf"^ORDONNANCE\s+{_ACT_NUM}", re.IGNORECASE), "ORD"),
    (re.compile(rf"^D[ÉE]CRET(?:[-\s]LOI)?\s+{_ACT_NUM}", re.IGNORECASE), "DEC"),
    (re.compile(rf"^ARR[ÊE]T[ÉE]\s+{_ACT_NUM}", re.IGNORECASE), "ARR"),
    (re.compile(r"^CODE\s+[A-ZÀ-Ÿ]", re.IGNORECASE), "CODE"),
]


def _clean_heading_line(raw_line: str) -> str:
    """Retire les décorations markdown de titre (« # », « ## »…) et les espaces."""
    return re.sub(r"^#+\s*", "", raw_line.strip()).strip()


def _looks_like_act_title(raw_line: str) -> bool:
    """Vrai si la ligne ressemble à un TITRE d'acte (titre markdown, ligne en
    grande partie capitalisée, ou début reconnu par `_ACT_BOUNDARY_PATTERNS`)
    — par opposition à un renvoi inline « Vu la loi n° X » en minuscules,
    qu'on ne veut pas proposer comme borne.

    Le troisième cas (motif d'acte reconnu) couvre les JO où MinerU rend les
    en-têtes de décret en casse mixte, sans niveau de titre markdown — constaté
    sur plusieurs recueils 1959/1990 où « DECRET N° 90-042 du 17 F. : er 1990,
    portant nonina-tion de Mr. AWAH... » ne matchait ni `#` ni le seuil de
    majuscules, et ne produisait donc aucune borne. `_detect_act_type` est déjà
    insensible à la casse (`re.IGNORECASE`) : le réutiliser ici ne fait
    qu'aligner la porte d'entrée sur un motif déjà éprouvé, sans changer ce
    qu'un « type d'acte » signifie."""
    stripped = raw_line.strip()
    if stripped.startswith("#"):
        return True
    if _detect_act_type(_clean_heading_line(stripped)) is not None:
        return True
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 6:
        return False
    uppercase = sum(1 for c in letters if c == c.upper())
    return uppercase / len(letters) >= 0.8


def _detect_act_type(cleaned_line: str) -> Optional[str]:
    """Type_code grossier d'un début d'acte (LOI/ORD/DEC/ARR/CODE), ou None."""
    for pattern, type_code in _ACT_BOUNDARY_PATTERNS:
        if pattern.match(cleaned_line):
            return type_code
    return None


def suggest_markdown_boundaries(markdown_text: str) -> List[Dict]:
    """Propose des bornes d'actes encapsulés depuis un markdown (sortie MinerU).

    Pendant markdown de ``suggest_compilation_boundaries`` (qui, lui, travaille sur
    le JSON paginé). Renvoie [{start_line (1-based), title, suggested, type_code}],
    à VALIDER par un humain. La détection est permissive ; des faux positifs (ex.
    une citation « Loi n° X » mise en titre par l'OCR) sont attendus et curés.
    """
    boundaries: List[Dict] = []
    for index, raw_line in enumerate(markdown_text.splitlines()):
        if not _looks_like_act_title(raw_line):
            continue
        cleaned = _clean_heading_line(raw_line)
        if not cleaned or len(cleaned) > 160:
            continue
        type_code = _detect_act_type(cleaned)
        if not type_code:
            continue
        boundaries.append({
            "start_line": index + 1,
            "title": cleaned,
            "suggested": cleaned[:80],
            "type_code": type_code,
        })
    return boundaries


def slice_markdown_by_boundaries(
    markdown_text: str,
    boundaries: List[Dict],
) -> List[Tuple[Dict, str]]:
    """Découpe un markdown en sous-markdowns selon les bornes validées.

    Chaque acte va de sa ``start_line`` (incluse) à la ligne précédant la borne
    suivante. Le contenu AVANT la première borne (couverture du recueil) est
    ignoré — comme pour le découpage JSON. Renvoie [(borne enrichie, sous_markdown)]
    dans l'ordre des lignes ; chaque borne porte un ``_mibeko_split`` (plage).
    """
    lines = markdown_text.splitlines()
    total = len(lines)
    ordered = sorted(
        (b for b in boundaries if b.get("start_line")),
        key=lambda b: b["start_line"],
    )

    result: List[Tuple[Dict, str]] = []
    for index, boundary in enumerate(ordered):
        start = boundary["start_line"]
        end = ordered[index + 1]["start_line"] - 1 if index + 1 < len(ordered) else total

        sub_lines = lines[start - 1:end]
        sub_text = "\n".join(sub_lines).strip()
        enriched = {
            **boundary,
            "_mibeko_split": {
                "title": boundary.get("title"),
                "type_code": boundary.get("type_code", "TEXTE"),
                "line_start": start,
                "line_end": end,
                "line_count": len(sub_lines),
            },
        }
        result.append((enriched, sub_text))

    return result


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
            type_code = "AU"
            if text.upper().startswith("ACTE UNIFORME"):
                label = text
            else:
                for regex, canonical in subjects:
                    if regex.search(text):
                        label = canonical
                        break
            if not label:
                # Hors OHADA : acte encapsulé générique (LOI/ORDONNANCE/DÉCRET…).
                detected = _detect_act_type(text)
                if detected:
                    label = text
                    type_code = detected
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
                "type_code": type_code,
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
