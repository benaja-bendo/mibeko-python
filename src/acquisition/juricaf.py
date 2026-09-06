"""Acquisition de la jurisprudence CCJA sur juricaf.org (mibeko-python#19).

ohada.org lui-même ne donne pas accès gratuitement au texte intégral des
arrêts CCJA (catalogue biblio.ohada.org gated, accès « officiel » par
formulaire manuel). juricaf.org, projet de l'AHJUCAF (association des cours
suprêmes francophones, soutenue par l'OIF), republie ces mêmes arrêts en
texte intégral, gratuitement, sans compte, déjà anonymisé RGPD — vérifié en
direct le 06/09/2026 (1 325 arrêts CCJA indexés à cette date).

Architecture pensée multi-source dès le départ : `SOURCE = "juricaf"` est
inscrit dans chaque `ManifestEntry.type_source` pour qu'un futur connecteur
ohada.org authentifié (compte personnel du fondateur, phase 2 — voir le
ticket) s'ajoute sans redécouper ce module.

Deux étapes, comme sgg.py : lister (pagination déterministe `?page=N`) puis
récupérer le HTML d'un arrêt (texte intégral, pas de PDF à parser). La
structuration (extraction du texte + des citations d'articles) est un
module séparé, en aval.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel

from .manifest import Manifest, ManifestEntry, known_checksums, utc_now_iso
from .politeness import AcquisitionError, PoliteClient

SOURCE = "jurisprudence_ccja"

BASE_URL = "https://juricaf.org"
SEARCH_PATH = (
    "/recherche/+/facet_juridiction:Cour_commune_de_justice_et_d'arbitrage,facet_pays:OHADA"
)

# <a class="a-unstyled" href="/arret/SLUG">OHADA, Cour commune de justice et
# d'arbitrage (ohada), 13 juillet 2023, 163/2023</a>
RESULT_LINK_RE = re.compile(
    r"""<a\s+class="a-unstyled"\s+href="(?P<href>/arret/[^"]+)">(?P<titre>[^<]+)</a>""",
)
# Le numéro de décision termine toujours le titre : "..., 163/2023".
NUMERO_RE = re.compile(r"(?P<numero>\d+/\d{4})\s*$")
# La date encodée dans le slug lui-même (YYYYMMDD) est plus fiable que de
# reparser la date en toutes lettres du titre (locale, abréviations).
SLUG_DATE_RE = re.compile(r"-(?P<annee>\d{4})(?P<mois>\d{2})(?P<jour>\d{2})-")
# L'esperluette de la pagination est encodée en entité HTML (&amp;page=2) :
# capturer tout le href puis décoder, plutôt que d'ancrer sur un caractère
# `&` littéral qui n'apparaît jamais tel quel dans ce balisage.
NEXT_PAGE_RE = re.compile(r"""<a href="(?P<href>[^"]+)">\s*Suivant""")


class ArretRef(BaseModel):
    """Une entrée de la liste des arrêts CCJA, avant récupération du texte."""

    slug: str
    url: str
    titre: str
    numero: Optional[str] = None
    date_decision: Optional[str] = None  # ISO 8601 (YYYY-MM-DD)


def _absolute(href: str) -> str:
    return href if href.startswith("http") else BASE_URL + href


def parse_listing_page(html: str) -> list[ArretRef]:
    """Extrait les arrêts référencés sur une page de résultats de recherche."""
    results: list[ArretRef] = []
    for match in RESULT_LINK_RE.finditer(html):
        href = match.group("href")
        slug = href.removeprefix("/arret/")
        titre = " ".join(match.group("titre").split())

        numero_match = NUMERO_RE.search(titre)
        numero = numero_match.group("numero") if numero_match else None

        date_decision = None
        date_match = SLUG_DATE_RE.search(slug)
        if date_match:
            date_decision = f"{date_match['annee']}-{date_match['mois']}-{date_match['jour']}"

        results.append(
            ArretRef(
                slug=slug,
                url=_absolute(href),
                titre=titre,
                numero=numero,
                date_decision=date_decision,
            )
        )
    return results


def next_page_url(html: str) -> Optional[str]:
    """URL de la page suivante (lien « Suivant »), ou None en fin de liste."""
    match = NEXT_PAGE_RE.search(html)
    if not match:
        return None
    href = match.group("href").replace("&amp;", "&")
    return _absolute(href)


def discover_arrets(
    client: PoliteClient,
    start_page: int = 1,
    max_pages: Optional[int] = None,
) -> Iterator[ArretRef]:
    """Parcourt les pages de résultats à partir de `start_page`, une à une.

    Politesse déjà assurée par `PoliteClient` (délai + backoff) : chaque page
    est une requête. `max_pages` borne un run (reprise à `start_page` la fois
    suivante) — un crawl complet des ~133 pages ne doit jamais se faire en un
    seul run sans supervision.
    """
    url = f"{BASE_URL}{SEARCH_PATH}?tri=DESC&pays=OHADA&page={start_page}"
    pages_vues = 0
    while url is not None:
        response = client.get(url)
        if response.status_code != 200:
            return
        yield from parse_listing_page(response.text)
        pages_vues += 1
        if max_pages is not None and pages_vues >= max_pages:
            return
        url = next_page_url(response.text)


def fetch_arret_html(client: PoliteClient, arret: ArretRef, dest_dir: Path) -> tuple[str, int, Path]:
    """Télécharge le HTML intégral d'un arrêt (texte intégral, pas de PDF).

    Réutilise `PoliteClient.download` (streaming, écriture atomique, SHA-256)
    à l'identique de l'acquisition PDF — seule l'extension change.
    """
    dest = dest_dir / f"{arret.slug}.html"
    sha256, size_bytes = client.download(arret.url, dest)
    return sha256, size_bytes, dest


def run_juricaf_acquire(
    data_dir: Path,
    start_page: int = 1,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
    client: Optional[PoliteClient] = None,
) -> dict:
    """Découvre puis télécharge des arrêts CCJA, en évitant tout doublon.

    Idempotent comme `run_acquire` : un arrêt déjà au manifeste (par id ou
    par URL source) n'est jamais retéléchargé, et le manifeste est sauvegardé
    après CHAQUE arrêt — un run interrompu ne perd que ce qu'il n'a pas
    encore écrit. `limit` borne les téléchargements RÉELS de ce run (les
    doublons rencontrés en chemin ne le décomptent pas), `max_pages` borne le
    nombre de pages de résultats parcourues.
    """
    manifest_path = data_dir / "manifests" / "juricaf.jsonl"
    manifest = Manifest(manifest_path)
    known_shas = known_checksums(data_dir / "manifests")
    dest_dir = data_dir / "sources" / "juricaf"

    owns_client = client is None
    client = client or PoliteClient()
    report: dict = {"telecharges": [], "doublons": [], "erreurs": [], "deja_connus": 0}
    try:
        for arret in discover_arrets(client, start_page=start_page, max_pages=max_pages):
            entry_id = f"juricaf/{arret.slug}"
            if entry_id in manifest or manifest.by_source_url(arret.url):
                report["deja_connus"] += 1
                continue
            if limit is not None and len(report["telecharges"]) >= limit:
                break

            try:
                sha256, size_bytes, dest = fetch_arret_html(client, arret, dest_dir)
            except AcquisitionError as exc:
                report["erreurs"].append({"url": arret.url, "erreur": str(exc)})
                continue

            duplicate_of = known_shas.get(sha256)
            if duplicate_of:
                dest.unlink(missing_ok=True)
                report["doublons"].append({"url": arret.url, "doublon_de": duplicate_of})
                continue

            entry = ManifestEntry(
                id=entry_id,
                fichier=str(dest.relative_to(data_dir)),
                sha256=sha256,
                size_bytes=size_bytes,
                type_source=SOURCE,
                source_url=arret.url,
                fetched_at=utc_now_iso(),
                statut="telecharge",
            )
            entry.add_event("telecharge", "MibekoBot/juricaf-acquire", detail=arret.url)
            manifest.upsert(entry)
            known_shas[sha256] = entry_id
            manifest.save()
            report["telecharges"].append(arret.url)
    finally:
        if owns_client:
            client.close()
    return report
