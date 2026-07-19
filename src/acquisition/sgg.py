"""Acquisition des Journaux officiels sur sgg.cg.

Grammaire des noms de fichiers, relevée sur les 68 JO déjà acquis
(data/sources/sgg/JO/, Phase 0) puis élargie par la recon du 04/07/2026
(72 répertoires-années, autoindex Apache ouvert) :

    congo-jo-{AAAA}-{NN}{suffixe}.pdf

    - AAAA : année (1946-…)
    - NN : numéro du JO (1-2 chiffres observés, 3 acceptés)
    - suffixe : libre (-sp, -volume-{romain}(-…)?, -2, -3…, -interactif,
      -interactif-2…) — la recon a établi que ces suffixes de révision sont
      imprévisibles et NE DOIVENT PAS être reconstruits ; `suffixe` n'est
      décomposé que pour repérer -sp (édition spéciale).

L'URL est reconstructible à partir d'un nom de fichier connu :
https://www.sgg.cg/JO/{AAAA}/{nom_de_fichier}. Mais construire un nom de
fichier à partir du numéro seul n'est PAS fiable (suffixes imprévisibles) :
c'est pourquoi la découverte par autoindex (ci-dessous) prime sur toute
reconstruction.

Trois stratégies de découverte, pilotées par le carnet :
- autoindex (par défaut) : liste le répertoire Apache ouvert /JO/{AAAA}/ et
  retient tous les liens .pdf réellement présents — ne rate aucune variante ;
- index : scan de pages d'index HTML (ex. « journaux spéciaux ») listant des
  liens vers /JO/{AAAA}/….pdf ;
- énumération (filet) : sonde HEAD sur congo-jo-{AAAA}-{n}.pdf, n croissant,
  arrêt après `stop_after` absences consécutives — utilisée seulement si
  l'autoindex est fermé pour l'année considérée.

Plusieurs fichiers différents peuvent partager le même numéro nominal (re-
publications, versions interactives, corrections). On les télécharge TOUS
(chacun son entrée de manifeste) plutôt que d'en choisir un arbitrairement ;
`acquire_jo_urls` les signale via `ManifestEntry.variantes_multiples` +
l'événement `variantes_multiples_detectees`, pour arbitrage humain.
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
    r"(?P<suffixe>[a-z0-9-]*)"
    r"\.pdf$",
    re.IGNORECASE,
)

# Liens /JO/{AAAA}/xxx.pdf dans une page d'index HTML.
JO_HREF_RE = re.compile(r"""href=["'](?P<href>[^"']*/JO/\d{4}/[^"']+\.pdf)["']""", re.IGNORECASE)

# Liens .pdf dans un autoindex Apache classique (mod_autoindex) : relatifs au
# répertoire courant, ex. <a href="congo-jo-2026-13.pdf">congo-jo-2026-13.pdf</a>.
AUTOINDEX_HREF_RE = re.compile(r"""href=["'](?P<href>[^"']+\.pdf)["']""", re.IGNORECASE)


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


def discover_by_autoindex(client: PoliteClient, annee: int) -> list[str]:
    """Liste /JO/{annee}/ via l'autoindex Apache ouvert (recon du 04/07/2026).

    Contrairement à `discover_by_enumeration`, ne devine aucun nom de fichier :
    restitue tous les PDF réellement présents, suffixes imprévisibles compris
    (-2, -13-5, -interactif, -interactif-2, -volume-xxii-3…). Retourne une
    liste vide (jamais d'exception) si le répertoire est fermé ou absent, pour
    permettre à l'appelant de replier silencieusement sur l'énumération HEAD.
    """
    dir_url = f"{SGG_BASE}/JO/{annee}/"
    try:
        response = client.get(dir_url)
    except AcquisitionError:
        return []
    if response.status_code != 200:
        return []

    urls: list[str] = []
    for match in AUTOINDEX_HREF_RE.finditer(response.text):
        href = match.group("href")
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = SGG_BASE + href
        else:
            url = dir_url + href
        if url not in urls:
            urls.append(url)
    return urls


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

    Retourne un résumé {telecharges, ignores_connus, doublons, erreurs,
    variantes_multiples, prevus}.
    """
    summary = {
        "telecharges": 0,
        "ignores_connus": 0,
        "doublons": 0,
        "erreurs": 0,
        "variantes_multiples": 0,
        "prevus": [],
    }
    # (annee, numero) -> ids déjà au manifeste, pour repérer les fichiers
    # multiples sous un même numéro nominal (suffixes imprévisibles).
    numero_index: dict[tuple[int, str], list[str]] = {}
    for existing in manifest.entries.values():
        if existing.jo_annee is not None and existing.jo_numero is not None:
            numero_index.setdefault((existing.jo_annee, existing.jo_numero), []).append(existing.id)

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

        numero_key = (parsed["annee"], parsed["numero"])
        siblings = numero_index.get(numero_key, [])
        if siblings:
            # Autre(s) fichier(s) déjà connus sous ce même numéro nominal :
            # on garde les deux (contenus différents, checksum déjà écarté
            # ci-dessus), et on flague pour arbitrage humain plutôt que de
            # choisir silencieusement lequel garder.
            groupe = sorted(siblings + [entry_id])
            detail = f"variantes du JO {parsed['annee']}-{parsed['numero']} : {', '.join(groupe)}"
            entry.variantes_multiples = groupe
            entry.add_event("variantes_multiples_detectees", "MibekoBot/acquire", detail=detail)
            for sibling_id in siblings:
                sibling = manifest.get(sibling_id)
                if sibling is None:
                    continue
                sibling.variantes_multiples = groupe
                if not any(e.quoi == "variantes_multiples_detectees" for e in sibling.evenements):
                    sibling.add_event(
                        "variantes_multiples_detectees", "MibekoBot/acquire", detail=detail
                    )
            summary["variantes_multiples"] += 1
        numero_index.setdefault(numero_key, []).append(entry_id)

        manifest.upsert(entry)
        known_shas[sha] = entry_id
        manifest.save()  # sauvegarde après chaque fichier : reprise sur crash
        summary["telecharges"] += 1
    return summary
