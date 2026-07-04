"""Acquisition des Journaux officiels sur sgg.cg.

Grammaire des noms de fichiers, relevée sur les 68 JO déjà acquis
(data/sources/sgg/JO/, Phase 0) :

    congo-jo-{AAAA}-{NN}(-sp)?(-volume-{romain}(-{suffixe})?)?(-2)?.pdf

    - AAAA : année (1958-…)
    - NN : numéro du JO (1-2 chiffres observés, 3 acceptés)
    - -sp : édition spéciale
    - -volume-{romain} : JO fractionné en volumes (observé sur 2025-5 uniquement)
    - -2 : second fichier pour un même numéro

L'URL est reconstructible : https://www.sgg.cg/JO/{AAAA}/{nom_de_fichier}.

Deux stratégies de découverte, toutes deux pilotées par le carnet :
- index : scan de pages d'index HTML (ex. « journaux spéciaux ») listant des
  liens vers /JO/{AAAA}/….pdf ;
- énumération : sonde HEAD sur congo-jo-{AAAA}-{n}.pdf, n croissant, arrêt
  après `stop_after` absences consécutives (filet quand aucune page d'index
  ne liste les numéros ordinaires).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .manifest import Manifest, ManifestEntry, utc_now_iso
from .politeness import AcquisitionError, PoliteClient

SGG_BASE = "https://www.sgg.cg"

JO_FILENAME_RE = re.compile(
    r"^congo-jo-(?P<annee>\d{4})-(?P<numero>\d{1,3})"
    r"(?P<suffixe>(?:-sp)?(?:-volume-[ivxlcdm]+(?:-[a-z0-9-]+)?)?(?:-2)?)"
    r"\.pdf$",
    re.IGNORECASE,
)

# Liens /JO/{AAAA}/xxx.pdf dans une page d'index HTML.
JO_HREF_RE = re.compile(r"""href=["'](?P<href>[^"']*/JO/\d{4}/[^"']+\.pdf)["']""", re.IGNORECASE)


def parse_jo_filename(filename: str) -> Optional[dict]:
    """Décompose un nom de fichier JO ; None si hors grammaire."""
    match = JO_FILENAME_RE.match(filename)
    if not match:
        return None
    return {
        "annee": int(match.group("annee")),
        "numero": match.group("numero").lstrip("0") or "0",
        "special": "-sp" in match.group("suffixe").lower(),
        "filename": filename,
    }


def build_jo_url(filename: str) -> Optional[str]:
    parsed = parse_jo_filename(filename)
    if not parsed:
        return None
    return f"{SGG_BASE}/JO/{parsed['annee']}/{filename}"


def extract_jo_links(html: str) -> list[str]:
    """URLs absolues des PDF de JO trouvés dans une page d'index."""
    urls = []
    for match in JO_HREF_RE.finditer(html):
        href = match.group("href")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = SGG_BASE + href
        elif not href.startswith("http"):
            href = f"{SGG_BASE}/{href.lstrip('./')}"
        if href not in urls:
            urls.append(href)
    return urls


def discover_from_index_pages(client: PoliteClient, index_pages: Iterable[str]) -> list[str]:
    urls: list[str] = []
    for page_url in index_pages:
        response = client.get(page_url)
        if response.status_code != 200:
            continue
        for url in extract_jo_links(response.text):
            if url not in urls:
                urls.append(url)
    return urls


def discover_by_enumeration(
    client: PoliteClient,
    annee: int,
    start: int = 1,
    max_numero: int = 60,
    stop_after: int = 5,
) -> list[str]:
    """Sonde congo-jo-{annee}-{n}.pdf jusqu'à `stop_after` 404 consécutifs."""
    found: list[str] = []
    misses = 0
    for numero in range(start, max_numero + 1):
        url = f"{SGG_BASE}/JO/{annee}/congo-jo-{annee}-{numero}.pdf"
        try:
            response = client.head(url)
        except AcquisitionError:
            break
        if response.status_code == 200:
            found.append(url)
            misses = 0
        else:
            misses += 1
            if misses >= stop_after:
                break
    return found


def acquire_jo_urls(
    client: PoliteClient,
    manifest: Manifest,
    urls: Iterable[str],
    sources_dir: Path,
    known_shas: dict[str, str],
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Télécharge les URLs de JO absentes du manifeste. Idempotent.

    Retourne un résumé {telecharges, ignores_connus, doublons, erreurs, prevus}.
    """
    summary = {"telecharges": 0, "ignores_connus": 0, "doublons": 0, "erreurs": 0, "prevus": []}
    for url in urls:
        filename = url.rsplit("/", 1)[-1]
        parsed = parse_jo_filename(filename)
        if not parsed:
            continue  # lien hors grammaire JO : ignoré (loggable par l'appelant)
        entry_id = f"sgg-jo/{filename.rsplit('.', 1)[0]}"
        if entry_id in manifest or manifest.by_source_url(url):
            summary["ignores_connus"] += 1
            continue
        if limit is not None and summary["telecharges"] + len(summary["prevus"]) >= limit:
            break
        if dry_run:
            summary["prevus"].append(url)
            continue

        dest = sources_dir / "sgg" / "JO" / filename
        try:
            sha, size = client.download(url, dest)
        except AcquisitionError as exc:
            summary["erreurs"] += 1
            summary.setdefault("erreurs_detail", []).append(f"{url} : {exc}")
            continue

        duplicate_of = known_shas.get(sha)
        if duplicate_of:
            # Checksum déjà connu sous un autre id : on ne crée pas de doublon.
            dest.unlink(missing_ok=True)
            existing = manifest.get(duplicate_of)
            if existing:
                existing.add_event(
                    "doublon_checksum", "MibekoBot/acquire", detail=f"aussi disponible : {url}"
                )
            summary["doublons"] += 1
            manifest.save()
            continue

        entry = ManifestEntry(
            id=entry_id,
            fichier=str(dest.relative_to(sources_dir.parent)),
            sha256=sha,
            size_bytes=size,
            type_source="journal_officiel",
            source_url=url,
            fetched_at=utc_now_iso(),
            jo_annee=parsed["annee"],
            jo_numero=parsed["numero"],
            statut="telecharge",
        )
        entry.add_event("telecharge", "MibekoBot/acquire", detail=url)
        manifest.upsert(entry)
        known_shas[sha] = entry_id
        manifest.save()  # sauvegarde après chaque fichier : reprise sur crash
        summary["telecharges"] += 1
    return summary
