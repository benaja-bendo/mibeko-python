"""Orchestration de la structuration en lot (étage 3), pilotée par le manifeste.

Une commande unique (`python main.py structure-batch`) traite tout ce qui est au
statut `parse` (ou `erreur`, pour retenter) dans les manifestes de
`data/manifests/` : parseur heuristique + LLM Mistral (`src.structuration.structurer`),
puis insertion en base (`curation_status=draft`).

Idempotent : `structure_document` détecte les documents déjà insérés via
`document_key` et retourne `deja_existant` sans double écriture. Reprend après
interruption : le manifeste est sauvegardé après CHAQUE document traité, jamais
en un seul batch final — un crash à mi-lot ne perd que le document en cours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
from src.structuration.structurer import structure_document

# Même périmètre v1 que parsing/batch.py (cf. DEFAULT_EXCLUDED_TYPE_SOURCES,
# docs/pipeline/01-plan.md §9-10) : traités internationaux/CEMAC et lots privés
# exclus par défaut, comptés à part, jamais silencieusement absorbés.
DEFAULT_EXCLUDED_TYPE_SOURCES = frozenset({"traite_international", "lot_prive"})


def _iter_manifests(data_dir: Path, source_key: Optional[str] = None):
    manifests_dir = data_dir / "manifests"
    if not manifests_dir.is_dir():
        return
    for manifest_path in sorted(manifests_dir.glob("*.jsonl")):
        if source_key and manifest_path.stem != source_key:
            continue
        yield manifest_path


def dry_run_report(
    db: Session,
    data_dir: Path,
    source_key: Optional[str] = None,
    limit: Optional[int] = None,
    include_hors_perimetre: bool = False,
) -> List[Dict[str, Any]]:
    """Parsing + appel LLM + validation pour chaque document éligible du carnet,
    sans AUCUNE écriture (DB ou fichier) : mesure le taux de validation réel.
    """
    report: List[Dict[str, Any]] = []
    for manifest_path in _iter_manifests(data_dir, source_key):
        manifest = Manifest(manifest_path)
        for entry in manifest.iter_entries():
            if entry.statut not in ("parse", "erreur"):
                continue
            if not include_hors_perimetre and entry.type_source in DEFAULT_EXCLUDED_TYPE_SOURCES:
                continue
            if limit is not None and len(report) >= limit:
                return report
            result = structure_document(db, data_dir, entry, dry_run=True)
            report.append({"id": entry.id, "statut_prevu": result["statut"], "motif": result["motif"]})
    return report


def run_batch(
    db: Session,
    data_dir: Path,
    source_key: Optional[str] = None,
    limit: Optional[int] = None,
    include_hors_perimetre: bool = False,
) -> Dict[str, Any]:
    """Structure les entrées éligibles de tous les manifestes (ou d'un seul).
    Séquentiel, idempotent et reprenable : chaque manifeste est sauvegardé dès
    qu'une de ses entrées change (pas seulement à la toute fin). Par défaut,
    exclut les traités internationaux/CEMAC et les lots privés (hors périmètre
    v1, cf. DEFAULT_EXCLUDED_TYPE_SOURCES) — comptés séparément.

    PAS d'option de retraitement forcé, et ce n'est pas un oubli : l'idempotence
    tient ici à `document_key`, qui identifie le document en base. Un « force »
    ne pourrait pas la contourner sans créer un second document pour le même
    texte — précisément ce que cette clé existe pour empêcher. Retraiter un
    document déjà structuré suppose donc de décider de son sort (le corriger, le
    remplacer, le supprimer), ce qui relève de la curation et non d'un drapeau de
    ligne de commande. Un flag `--force` a existé jusqu'au 25/08/2026 : il était
    accepté, transmis jusqu'ici, et ignoré — voir mibeko-python#4.
    """
    summary: Dict[str, Any] = {
        "traites": 0, "sautes": 0, "hors_perimetre": 0, "erreurs": [], "deja_existants": 0
    }
    processed = 0

    for manifest_path in _iter_manifests(data_dir, source_key):
        manifest = Manifest(manifest_path)
        for entry in manifest.iter_entries():
            if entry.statut not in ("parse", "erreur"):
                continue
            if not include_hors_perimetre and entry.type_source in DEFAULT_EXCLUDED_TYPE_SOURCES:
                summary["hors_perimetre"] += 1
                continue
            if limit is not None and processed >= limit:
                break

            result = structure_document(db, data_dir, entry, dry_run=False)
            processed += 1

            if result["statut"] == "deja_existant":
                summary["deja_existants"] += 1
                if entry.statut != "structure":
                    entry.statut = "structure"
                    entry.add_event("structure", "MibekoBot/structure-batch", detail="déjà existant")
                    manifest.save()
                continue

            if result["statut"] == "erreur":
                entry.statut = "erreur"
                entry.add_event("erreur", "MibekoBot/structure-batch", detail=result["motif"])
                summary["erreurs"].append({"id": entry.id, "erreur": result["motif"]})
            else:
                entry.statut = "structure"
                entry.add_event("structure", "MibekoBot/structure-batch", detail=str(result["document_id"]))
                summary["traites"] += 1

            # Sauvegarde APRÈS CHAQUE document (pas seulement en fin de
            # manifeste) : un crash à mi-lot ne laisse aucun document déjà
            # traité avec un statut périmé sur disque.
            manifest.save()

        if limit is not None and processed >= limit:
            break

    return summary
