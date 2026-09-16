"""Export périodique `ingestion_provenances` (Postgres) → `data/manifests/*.jsonl`.

mibeko-python#28 (reliquat #23, § 3.7 du plan « boîte de réception ») :
Postgres porte désormais l'état de provenance sans le perdre sous écriture
concurrente (dashboard#140), mais les commandes CLI existantes
(`acquire`, `backfill-manifest`, `push-corpus`…) lisent encore le JSONL —
cet export les tient à jour sans les réécrire.

**Fusion, jamais un remplacement** : une exécution charge le JSONL cible
existant (`Manifest.__init__`), met à jour seulement les entrées connues de
Postgres (`Manifest.upsert`), puis sauvegarde. Les entrées posées par
`acquire`/`backfill-manifest` — qui n'écrivent jamais dans
`ingestion_provenances` — traversent donc l'export intactes. Un export qui
regénérerait le fichier à partir de Postgres seul effacerait silencieusement
tout ce corpus (plus de 1300 entrées `sgg-jo` à ce jour).

Le fichier de destination se déduit du premier segment de `manifest_id`
(« sgg-jo/congo-jo-2026-13 » → `sgg-jo.jsonl`), la même convention que les
manifestes déjà versionnés dans `data/manifests/`.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Dict, List

from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
from src.db.models import IngestionProvenance


def _vers_manifest_entry(provenance: IngestionProvenance) -> ManifestEntry:
    fetched_at = None
    if provenance.fetched_at is not None:
        fetched_at = provenance.fetched_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")

    return ManifestEntry(
        id=provenance.manifest_id,
        fichier=provenance.fichier,
        sha256=provenance.sha256,
        size_bytes=provenance.size_bytes,
        type_source=provenance.type_source,
        source_url=provenance.source_url,
        fetched_at=fetched_at,
        jo_annee=provenance.jo_annee,
        jo_numero=provenance.jo_numero,
        jo_date=provenance.jo_date.isoformat() if provenance.jo_date else None,
        statut=provenance.statut or "telecharge",
        retroactif=provenance.retroactif,
        variantes_multiples=provenance.variantes_multiples,
        titre=provenance.titre,
        evenements=provenance.evenements or [],
    )


def export_provenances_to_jsonl(db: Session, manifests_directory: Path) -> Dict[str, Dict[str, List[str]]]:
    """Fusionne `ingestion_provenances` dans les JSONL de `manifests_directory`.

    Une ligne Postgres sans `fichier` ni `size_bytes` (écrite avant
    mibeko-python#28, ou dont le fichier a depuis été purgé) est ignorée —
    l'exporter produirait une entrée de manifeste que les commandes CLI
    existantes ne sauraient pas exploiter (`entry.fichier` pointe alors
    nulle part). Renvoie, par namespace, les ids exportés et ceux ignorés.
    """
    par_namespace: Dict[str, List[IngestionProvenance]] = {}
    ignores: Dict[str, List[str]] = {}

    for provenance in db.query(IngestionProvenance).all():
        namespace = provenance.manifest_id.split("/", 1)[0]
        if not provenance.fichier or provenance.size_bytes is None:
            ignores.setdefault(namespace, []).append(provenance.manifest_id)
            continue
        par_namespace.setdefault(namespace, []).append(provenance)

    exportes: Dict[str, List[str]] = {}
    for namespace, lignes in par_namespace.items():
        manifest = Manifest(manifests_directory / f"{namespace}.jsonl")
        for provenance in lignes:
            manifest.upsert(_vers_manifest_entry(provenance))
        manifest.save()
        exportes[namespace] = [p.manifest_id for p in lignes]

    return {"exportes": exportes, "ignores": ignores}
