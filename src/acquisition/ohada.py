"""Reconnaissance des Actes uniformes sur ohada.org.

Règle mission (étage 1) : identifier les URLs exactes et les faire VALIDER
humainement avant toute collecte. Ce module ne télécharge donc aucun PDF :
il produit un rapport markdown (data/manifests/ohada-recon.md) listant les
liens candidats trouvés sur les pages d'index, à relire avant d'inscrire les
URLs validées dans le carnet (corpus-v1.yaml).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .manifest import utc_now_iso
from .politeness import PoliteClient

OHADA_INDEX_PAGES = [
    "https://www.ohada.org/droit-commercial-general/",
]

PDF_HREF_RE = re.compile(r"""href=["'](?P<href>[^"']+\.pdf)["']""", re.IGNORECASE)
PAGE_LINK_RE = re.compile(
    r"""<a[^>]+href=["'](?P<href>[^"']+)["'][^>]*>(?P<label>[^<]{0,200})</a>""",
    re.IGNORECASE,
)
ACTE_KEYWORD_RE = re.compile(r"acte\s+uniforme|droit\s+", re.IGNORECASE)


def _absolutize(href: str, base: str = "https://www.ohada.org") -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    if not href.startswith("http"):
        return f"{base}/{href.lstrip('./')}"
    return href


def scan_index_page(html: str) -> dict:
    """Extrait les liens PDF et les pages candidates « Acte uniforme »."""
    pdfs = []
    for match in PDF_HREF_RE.finditer(html):
        url = _absolutize(match.group("href"))
        if url not in pdfs:
            pdfs.append(url)
    pages = []
    for match in PAGE_LINK_RE.finditer(html):
        label = " ".join(match.group("label").split())
        href = _absolutize(match.group("href"))
        if not label or href.lower().endswith(".pdf"):
            continue
        if ACTE_KEYWORD_RE.search(label) and "ohada.org" in href:
            candidate = {"url": href, "label": label}
            if candidate not in pages:
                pages.append(candidate)
    return {"pdfs": pdfs, "pages": pages}


def recon(client: PoliteClient, out_path: Path, index_pages: Iterable[str] | None = None) -> dict:
    """Scanne les pages d'index OHADA et écrit le rapport à valider."""
    index_pages = list(index_pages or OHADA_INDEX_PAGES)
    report: dict = {"quand": utc_now_iso(), "pages_scannees": [], "pdfs": [], "pages": []}
    for page_url in index_pages:
        response = client.get(page_url)
        if response.status_code != 200:
            report["pages_scannees"].append({"url": page_url, "http": response.status_code})
            continue
        found = scan_index_page(response.text)
        report["pages_scannees"].append({"url": page_url, "http": 200})
        for url in found["pdfs"]:
            if url not in report["pdfs"]:
                report["pdfs"].append(url)
        for page in found["pages"]:
            if page not in report["pages"]:
                report["pages"].append(page)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Reconnaissance OHADA — rapport à valider",
        "",
        f"> Généré le {report['quand']} par `python main.py ohada-recon`. "
        "Aucun téléchargement effectué : relire, puis reporter les URLs "
        "validées dans `data/corpus/corpus-v1.yaml`.",
        "",
        "## Pages scannées",
        "",
    ]
    for page in report["pages_scannees"]:
        lines.append(f"- {page['url']} (HTTP {page['http']})")
    lines += ["", f"## Liens PDF trouvés ({len(report['pdfs'])})", ""]
    for url in report["pdfs"]:
        lines.append(f"- [ ] {url}")
    lines += ["", f"## Pages candidates « Acte uniforme » ({len(report['pages'])})", ""]
    for page in report["pages"]:
        lines.append(f"- [ ] [{page['label']}]({page['url']})")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return report
