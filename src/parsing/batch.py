"""Orchestration du parsing en lot (étage 2), pilotée par le manifeste.

Une commande unique (`python main.py process-batch`) traite tout ce qui est au
statut `telecharge` (ou `erreur`, pour retenter) dans les manifestes de
`data/manifests/` : triage natif d'abord (`src.parsing.triage`), MinerU
(local ou cloud selon `MINERU_BACKEND`) seulement si le natif ne suffit pas.

Idempotent : une entrée dont l'artefact + les métriques existent déjà pour le
même SHA source est sautée (sauf `force=True`). Reprend après interruption :
le manifeste est sauvegardé après CHAQUE document traité, jamais en un seul
batch final — un crash à mi-lot ne perd que le document en cours.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.acquisition.manifest import Manifest, ManifestEntry, utc_now_iso
from src.parsing.triage import triage_pdf

MineruRunner = Callable[[Path], Awaitable[Tuple[str, str]]]

# Décision §9-10 du plan (docs/pipeline/01-plan.md) : le carnet v1 exclut les
# traités internationaux/CEMAC (hors périmètre produit v1, provenance conservée
# au manifeste seulement) et les lots privés (avocat_alban : pas une source
# officielle, cf. règle mission n°5). Par défaut, process-batch ne les touche
# PAS — sinon la première exécution sans argument engloutirait ~325 documents
# hors scope avant que quiconque ne s'en aperçoive.
DEFAULT_EXCLUDED_TYPE_SOURCES = frozenset({"traite_international", "lot_prive"})


class ParsingError(RuntimeError):
    """Échec de traitement d'un document (PDF manquant, MinerU en erreur…)."""


def artefact_paths(data_dir: Path, entry_id: str) -> Dict[str, Path]:
    """Chemins des artefacts d'une entrée. `entry_id` contient déjà un `/`
    (ex. "sgg-jo/congo-jo-2026-13") : pathlib le résout en sous-dossier.
    """
    return {
        "md": data_dir / "pipeline" / "md" / f"{entry_id}.md",
        "json": data_dir / "pipeline" / "json" / f"{entry_id}.json",
        "metrics": data_dir / "pipeline" / "metrics" / f"{entry_id}.json",
    }


def is_already_processed(data_dir: Path, entry: ManifestEntry) -> bool:
    """Idempotence : métriques présentes, SHA source inchangé, artefacts au
    complet pour la méthode retenue. Une entrée en échec (`methode='erreur'`
    ou fichiers manquants) n'est PAS considérée comme traitée → retentée.
    """
    paths = artefact_paths(data_dir, entry.id)
    if not paths["metrics"].is_file():
        return False
    try:
        metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if metrics.get("source_sha256") != entry.sha256:
        return False  # source_sha256 change en pratique seulement si data/sources/ a été altéré
    methode = metrics.get("methode")
    if methode not in ("native", "mineru_local", "mineru_cloud"):
        return False
    if not paths["md"].is_file():
        return False
    if methode != "native" and not paths["json"].is_file():
        return False
    return True


async def run_mineru(pdf_path: Path) -> Tuple[str, str]:
    """Soumet le PDF à MinerU (cloud ou local selon `MINERU_BACKEND`) et
    retourne (markdown, json_brut). Lève ParsingError sur échec.
    """
    from src.services.mineru_service import mineru_service

    task_id = await mineru_service.submit_pdf(str(pdf_path))
    result = await mineru_service.get_results(task_id)
    if result.get("status") != "success":
        raise ParsingError(result.get("error") or f"MinerU : statut {result.get('status')!r}")

    md_text = ""
    json_text = ""
    if result.get("md_url"):
        md_bytes = await mineru_service.download_result(result["md_url"])
        md_text = md_bytes.decode("utf-8", errors="replace")
    if result.get("json_url"):
        json_bytes = await mineru_service.download_result(result["json_url"])
        json_text = json_bytes.decode("utf-8", errors="replace")
    if not md_text:
        raise ParsingError("MinerU n'a renvoyé aucun markdown.")
    return md_text, json_text


def _mineru_backend_label() -> str:
    from src.services.mineru_service import mineru_service

    # getattr défensif : certains tests neutralisent temporairement
    # src.services.mineru_service (cf. tests/conftest.py::stub_service_modules,
    # qui restaure sys.modules en sortie de bloc). Le service réel a toujours
    # .backend ; ce repli reste une ceinture-bretelles si un test venait à ne
    # pas restaurer correctement, jamais exercé en production.
    return f"mineru_{getattr(mineru_service, 'backend', 'cloud')}"


def process_entry(
    data_dir: Path,
    entry: ManifestEntry,
    force: bool = False,
    mineru_runner: Optional[MineruRunner] = None,
) -> Dict[str, Any]:
    """Traite une entrée : triage puis, si besoin, MinerU. Écrit les artefacts
    et les métriques sur disque. Retourne le résumé (dict) ; ne lève PAS sur
    échec MinerU — l'échec est encodé dans le résultat (`methode: 'erreur'`),
    c'est l'appelant (`run_batch`) qui décide de la transition de statut.
    """
    paths = artefact_paths(data_dir, entry.id)
    if not force and is_already_processed(data_dir, entry):
        return {"id": entry.id, "skipped": True, "raison": "déjà traité (SHA source inchangé)"}

    pdf_path = data_dir / entry.fichier
    if not pdf_path.is_file():
        raise ParsingError(f"PDF introuvable : {pdf_path}")

    started = time.monotonic()
    triage = triage_pdf(pdf_path)

    metrics: Dict[str, Any] = {
        "manifest_id": entry.id,
        "source_sha256": entry.sha256,
        "traite_le": utc_now_iso(),
        "num_pages": triage.num_pages,
        "chars_per_page": round(triage.chars_per_page, 1),
        "triage_reason": triage.reason,
        "quality": triage.quality,
    }

    if triage.method == "native":
        paths["md"].parent.mkdir(parents=True, exist_ok=True)
        paths["md"].write_text(triage.text or "", encoding="utf-8")
        metrics["methode"] = "native"
        metrics["duree_secondes"] = round(time.monotonic() - started, 2)
        paths["metrics"].parent.mkdir(parents=True, exist_ok=True)
        paths["metrics"].write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"id": entry.id, "skipped": False, **metrics}

    runner = mineru_runner or run_mineru
    try:
        md_text, json_text = asyncio.run(runner(pdf_path))
    except Exception as exc:  # MinerU HTTP, timeout, ParsingError… : tout est un échec de cette entrée
        metrics["methode"] = "erreur"
        # str(exc) est vide pour certaines exceptions sans message (ex.
        # httpx.ReadTimeout()) — vérifié en pratique (04/07/2026) : ne jamais
        # écrire une erreur illisible dans les métriques/logs.
        metrics["erreur"] = str(exc) or f"{type(exc).__name__} (sans message)"
        metrics["duree_secondes"] = round(time.monotonic() - started, 2)
        paths["metrics"].parent.mkdir(parents=True, exist_ok=True)
        paths["metrics"].write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"id": entry.id, "skipped": False, **metrics}

    paths["md"].parent.mkdir(parents=True, exist_ok=True)
    paths["md"].write_text(md_text, encoding="utf-8")
    if json_text:
        paths["json"].parent.mkdir(parents=True, exist_ok=True)
        paths["json"].write_text(json_text, encoding="utf-8")

    metrics["methode"] = _mineru_backend_label()
    metrics["duree_secondes"] = round(time.monotonic() - started, 2)
    paths["metrics"].parent.mkdir(parents=True, exist_ok=True)
    paths["metrics"].write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": entry.id, "skipped": False, **metrics}


def _iter_manifests(data_dir: Path, source_key: Optional[str] = None):
    manifests_dir = data_dir / "manifests"
    if not manifests_dir.is_dir():
        return
    for manifest_path in sorted(manifests_dir.glob("*.jsonl")):
        if source_key and manifest_path.stem != source_key:
            continue
        yield manifest_path


def dry_run_report(
    data_dir: Path,
    source_key: Optional[str] = None,
    limit: Optional[int] = None,
    include_hors_perimetre: bool = False,
) -> List[Dict[str, Any]]:
    """Triage seul (aucune écriture, aucun appel MinerU) : prévisualise la
    méthode qui serait retenue pour chaque document éligible du carnet.
    """
    report: List[Dict[str, Any]] = []
    for manifest_path in _iter_manifests(data_dir, source_key):
        manifest = Manifest(manifest_path)
        for entry in manifest.iter_entries():
            if entry.statut not in ("telecharge", "erreur"):
                continue
            if not include_hors_perimetre and entry.type_source in DEFAULT_EXCLUDED_TYPE_SOURCES:
                continue
            if limit is not None and len(report) >= limit:
                return report
            pdf_path = data_dir / entry.fichier
            if not pdf_path.is_file():
                report.append({"id": entry.id, "erreur": "PDF introuvable"})
                continue
            triage = triage_pdf(pdf_path)
            report.append(
                {"id": entry.id, "methode_prevue": triage.method, "raison": triage.reason}
            )
    return report


def run_batch(
    data_dir: Path,
    source_key: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    mineru_runner: Optional[MineruRunner] = None,
    include_hors_perimetre: bool = False,
) -> Dict[str, Any]:
    """Traite les entrées éligibles de tous les manifestes (ou d'un seul).
    Séquentiel — un lot nocturne, pas de parallélisme MinerU CPU. Idempotent
    et reprenable : chaque manifeste est sauvegardé dès qu'une de ses entrées
    change (pas seulement à la toute fin). Par défaut, exclut les traités
    internationaux/CEMAC et les lots privés (hors périmètre v1, cf.
    DEFAULT_EXCLUDED_TYPE_SOURCES) — comptés séparément, jamais silencieusement.
    """
    summary: Dict[str, Any] = {
        "traites": 0, "sautes": 0, "hors_perimetre": 0, "erreurs": [], "par_methode": {}
    }
    processed = 0

    for manifest_path in _iter_manifests(data_dir, source_key):
        manifest = Manifest(manifest_path)
        for entry in manifest.iter_entries():
            if entry.statut not in ("telecharge", "erreur"):
                continue
            if not include_hors_perimetre and entry.type_source in DEFAULT_EXCLUDED_TYPE_SOURCES:
                summary["hors_perimetre"] += 1
                continue
            if limit is not None and processed >= limit:
                break

            result = process_entry(data_dir, entry, force=force, mineru_runner=mineru_runner)
            processed += 1
            entry_changed = False

            if result.get("skipped"):
                summary["sautes"] += 1
                # Bookkeeping : une entrée sautée est par définition déjà
                # traitée (artefacts + métriques à jour) — resynchroniser le
                # statut évite qu'un reset manuel vers 'telecharge' reste
                # bloqué indéfiniment sur un statut qui ne reflète plus la
                # réalité disque.
                if entry.statut != "parse":
                    entry.statut = "parse"
                    entry_changed = True
            else:
                methode = result.get("methode")
                if methode == "erreur":
                    entry.statut = "erreur"
                    entry.add_event("erreur_parsing", "MibekoBot/process-batch", detail=result.get("erreur"))
                    summary["erreurs"].append({"id": entry.id, "erreur": result.get("erreur")})
                else:
                    entry.statut = "parse"
                    entry.add_event("parse", "MibekoBot/process-batch", detail=methode)
                    summary["traites"] += 1
                    summary["par_methode"][methode] = summary["par_methode"].get(methode, 0) + 1
                entry_changed = True

            # Sauvegarde APRÈS CHAQUE document (pas seulement en fin de
            # manifeste) : un crash à mi-lot ne laisse aucun document déjà
            # traité avec un statut périmé sur disque.
            if entry_changed:
                manifest.save()

        if limit is not None and processed >= limit:
            break

    return summary
