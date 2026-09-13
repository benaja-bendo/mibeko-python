"""Un passage de veille : acquisition → parsing → structuration, en séquence.

Périmètre fixé par la décision du ticket (mibeko-python#21) : uniquement les
nouveaux Journaux officiels — série `jo-recents` du carnet, manifeste
`sgg-jo`. Pas de paramètre pour élargir le périmètre à l'exécution : un
périmètre plus large est un changement de code, pas un flag.

Écrit directement dans la base que `main.py` cible (donc en production quand
ce module tourne dans le conteneur déployé) : même chemin que l'upload manuel
existant, qui écrit déjà en `draft` dans la même base. `staging ≠ publié`
reste le seul garde-fou nécessaire — voir `docs/pipeline/README.md` §2.2 et
la décision datée dans `docs/decisions.md`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.acquisition.acquire import run_acquire
from src.acquisition.config import corpus_file, data_dir
from src.veille.healthcheck import ping

logger = logging.getLogger("veille.runner")

SOURCE_SERIE_CARNET = "jo-recents"
SOURCE_MANIFESTE = "sgg-jo"


def run_once(dry_run: bool = False) -> Dict[str, Any]:
    """Un passage complet. Ne lève pas : les échecs sont dans `report["echec"]`."""
    report: Dict[str, Any] = {"echec": None, "acquisition": None, "parsing": None, "structuration": None}
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

    try:
        from src.parsing.batch import dry_run_report as parsing_dry_run_report
        from src.parsing.batch import run_batch as run_parsing_batch

        if dry_run:
            report["parsing"] = parsing_dry_run_report(data_dir(), source_key=SOURCE_MANIFESTE, limit=None)
        else:
            report["parsing"] = run_parsing_batch(data_dir(), source_key=SOURCE_MANIFESTE, limit=None, force=False)
    except Exception as exc:
        logger.exception("veille : échec du parsing")
        report["echec"] = f"parsing : {exc}"
        ping("/fail")
        return report

    from src.db.database import SessionLocal
    from src.structuration.batch import dry_run_report as structuration_dry_run_report
    from src.structuration.batch import run_batch as run_structuration_batch

    db = SessionLocal()
    try:
        if dry_run:
            report["structuration"] = structuration_dry_run_report(
                db, data_dir(), source_key=SOURCE_MANIFESTE, limit=None
            )
        else:
            report["structuration"] = run_structuration_batch(db, data_dir(), source_key=SOURCE_MANIFESTE, limit=None)
    except Exception as exc:
        logger.exception("veille : échec de la structuration")
        report["echec"] = f"structuration : {exc}"
        ping("/fail")
        return report
    finally:
        db.close()

    ping("")
    logger.info("veille : passage terminé sans échec — %s", report)
    return report
