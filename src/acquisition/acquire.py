"""Orchestration de l'acquisition pilotée par le carnet (corpus-v1.yaml).

L'usine ne traite que ce qui figure au carnet (règle mission). Le carnet
décrit des `series` (lots, ex. Journaux officiels par années) et des `textes`
(documents unitaires avec URL explicite). Ce module lit le carnet, découvre
les URLs, et délègue le téléchargement idempotent à sgg.acquire_jo_urls /
au téléchargement unitaire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .manifest import Manifest, ManifestEntry, known_checksums, utc_now_iso
from .politeness import AcquisitionError, PoliteClient
from .sgg import (
    acquire_jo_urls,
    discover_by_enumeration,
    discover_from_index_pages,
    parse_jo_filename,
)


def load_carnet(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        carnet = yaml.safe_load(handle) or {}
    if not isinstance(carnet, dict):
        raise ValueError(f"Carnet invalide (dictionnaire YAML attendu) : {path}")
    return carnet


def _acquire_jo_series(
    client: PoliteClient,
    series: dict,
    data_dir: Path,
    known_shas: dict[str, str],
    dry_run: bool,
    limit: Optional[int],
) -> dict:
    annees = set(int(a) for a in series.get("annees", []))
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")

    urls: list[str] = []
    index_pages = series.get("index_pages", [])
    if index_pages:
        for url in discover_from_index_pages(client, index_pages):
            parsed = parse_jo_filename(url.rsplit("/", 1)[-1])
            if parsed and (not annees or parsed["annee"] in annees):
                urls.append(url)
    enumeration = series.get("enumeration", {})
    if enumeration.get("actif", False):
        for annee in sorted(annees):
            for url in discover_by_enumeration(
                client,
                annee,
                max_numero=int(enumeration.get("max_numero", 60)),
                stop_after=int(enumeration.get("stop_apres_absents", 5)),
            ):
                if url not in urls:
                    urls.append(url)

    result = acquire_jo_urls(
        client,
        manifest,
        urls,
        sources_dir=data_dir / "sources",
        known_shas=known_shas,
        dry_run=dry_run,
        limit=limit,
    )
    result["urls_decouvertes"] = len(urls)
    return result


def _acquire_texte(
    client: PoliteClient,
    texte: dict,
    data_dir: Path,
    known_shas: dict[str, str],
    dry_run: bool,
) -> dict:
    source = texte.get("source") or {}
    url = source.get("url")
    if not url or str(url).startswith("<"):
        return {"ignore": "pas d'URL (à compléter dans le carnet)"}

    manifest_key = texte.get("manifeste", "divers")
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    entry_id = f"{manifest_key}/{texte['id']}"
    if entry_id in manifest or manifest.by_source_url(url):
        return {"ignore": "déjà au manifeste"}
    if dry_run:
        return {"prevu": url}

    filename = url.rsplit("/", 1)[-1] or f"{texte['id']}.pdf"
    dest = data_dir / "sources" / texte.get("dest", "divers") / filename
    try:
        sha, size = client.download(url, dest)
    except AcquisitionError as exc:
        return {"erreur": str(exc)}

    duplicate_of = known_shas.get(sha)
    if duplicate_of:
        dest.unlink(missing_ok=True)
        return {"doublon_de": duplicate_of}

    entry = ManifestEntry(
        id=entry_id,
        fichier=str(dest.relative_to(data_dir)),
        sha256=sha,
        size_bytes=size,
        type_source=texte.get("type_source", "inconnu"),
        source_url=url,
        fetched_at=utc_now_iso(),
        statut="telecharge",
    )
    entry.add_event("telecharge", "MibekoBot/acquire", detail=url)
    manifest.upsert(entry)
    known_shas[sha] = entry_id
    manifest.save()
    return {"telecharge": url}


def run_acquire(
    carnet_path: Path,
    data_dir: Path,
    source_key: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    client: Optional[PoliteClient] = None,
) -> dict:
    """Exécute l'acquisition du carnet. Idempotent, relançable à volonté."""
    carnet = load_carnet(carnet_path)
    known_shas = known_checksums(data_dir / "manifests")
    owns_client = client is None
    client = client or PoliteClient()
    report: dict = {"series": {}, "textes": {}}
    try:
        for series in carnet.get("series", []) or []:
            if source_key and series.get("id") != source_key:
                continue
            if series.get("type") == "journal_officiel":
                report["series"][series["id"]] = _acquire_jo_series(
                    client, series, data_dir, known_shas, dry_run, limit
                )
            else:
                report["series"][series["id"]] = {"ignore": f"type non géré : {series.get('type')}"}
        if not source_key or source_key == "textes":
            for texte in carnet.get("textes", []) or []:
                report["textes"][texte["id"]] = _acquire_texte(
                    client, texte, data_dir, known_shas, dry_run
                )
    finally:
        if owns_client:
            client.close()
    return report
