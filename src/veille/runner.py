"""Un passage de veille : acquisition, puis dépôt de travaux dans la file
`ingestion_jobs` — plus de parsing ni de structuration ici (mibeko-python#23,
§ 3.4 : « elle acquiert et dépose un travail ; le worker fait le reste »,
modifiant mibeko-python#21). Deux boucles concurrentes sur la même base et
les mêmes artefacts (`api` + `veille` + `worker`) seraient une source
d'incidents inutile — un seul daemon (`python main.py worker`) traite tout.

Périmètre fixé par la décision du ticket #21 : uniquement les nouveaux
Journaux officiels — série `jo-recents` du carnet, manifeste `sgg-jo`. Pas de
paramètre pour élargir le périmètre à l'exécution : un périmètre plus large
est un changement de code, pas un flag.

Écrit directement dans la base que `main.py` cible (donc en production quand
ce module tourne dans le conteneur déployé) : même chemin que l'upload manuel
existant, qui écrit déjà en `draft` dans la même base. `staging ≠ publié`
reste le seul garde-fou nécessaire — voir `docs/pipeline/README.md` §2.2 et
la décision datée dans `docs/decisions.md`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.acquisition.acquire import run_acquire
from src.acquisition.config import corpus_file, data_dir
from src.acquisition.manifest import Manifest
from src.veille.healthcheck import ping

logger = logging.getLogger("veille.runner")

SOURCE_SERIE_CARNET = "jo-recents"
SOURCE_MANIFESTE = "sgg-jo"


def _deposer_jobs_veille(db, manifest: Manifest, dry_run: bool = False) -> Dict[str, List[str]]:
    """Dépose un job `kind=veille` pour chaque entrée éligible (`telecharge`
    ou `erreur`) du manifeste qui n'a pas déjà un travail `pending`/`running`
    en file. Sans cette déduplication, deux passages successifs (ou un
    passage relancé) avant que le worker n'ait traité le premier job
    fabriqueraient un doublon — incident (a) du plan « boîte de réception »,
    § L1. `dry_run` liste ce qui serait déposé sans rien écrire.
    """
    from src.db.models import IngestionJob

    deposes: List[str] = []
    deja_en_file: List[str] = []
    for entry in manifest.iter_entries():
        if entry.statut not in ("telecharge", "erreur"):
            continue
        existant = (
            db.query(IngestionJob)
            .filter(
                IngestionJob.manifest_id == entry.id,
                IngestionJob.status.in_([IngestionJob.STATUS_PENDING, IngestionJob.STATUS_RUNNING]),
            )
            .first()
        )
        if existant is not None:
            deja_en_file.append(entry.id)
            continue
        if dry_run:
            deposes.append(entry.id)
            continue
        job = IngestionJob(kind=IngestionJob.KIND_VEILLE, manifest_id=entry.id, requested_by="veille-corpus")
        db.add(job)
        db.commit()
        deposes.append(entry.id)

    return {"deposes": deposes, "deja_en_file": deja_en_file}


def run_once(dry_run: bool = False) -> Dict[str, Any]:
    """Un passage complet : acquisition puis dépôt de travaux. Ne lève pas :
    les échecs sont dans `report["echec"]`."""
    report: Dict[str, Any] = {"echec": None, "acquisition": None, "depots": None}
    ping("/start")

    try:
        report["acquisition"] = run_acquire(
            corpus_file(), data_dir(), source_key=SOURCE_SERIE_CARNET, dry_run=dry_run, limit=None
        )
    except Exception as exc:  # acquisition indisponible (sgg.cg down, etc.) : rien à traiter ensuite
        logger.exception("veille : échec de l'acquisition")
        report["echec"] = f"acquisition : {exc}"
        ping("/fail")
        return report

    from src.db.database import SessionLocal

    manifest = Manifest(data_dir() / "manifests" / f"{SOURCE_MANIFESTE}.jsonl")
    db = SessionLocal()
    try:
        report["depots"] = _deposer_jobs_veille(db, manifest, dry_run=dry_run)
    except Exception as exc:
        logger.exception("veille : échec du dépôt des travaux")
        report["echec"] = f"depots : {exc}"
        ping("/fail")
        return report
    finally:
        db.close()

    ping("")
    logger.info("veille : passage terminé sans échec — %s", report)
    return report
