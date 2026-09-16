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
    § L1. `dry_run` liste ce qui serait déposé sans rien écrire (et ne pose
    donc jamais de verrou : sans écriture, rien à protéger d'une course).

    Verrou consultatif Postgres par entrée, portée TRANSACTION
    (`pg_advisory_xact_lock`, jamais la variante session
    `pg_advisory_lock`), autour du SELECT « pas déjà en file » + l'INSERT :
    les deux ne sont pas atomiques entre eux, et `ingestion_jobs.manifest_id`
    n'a délibérément pas de contrainte UNIQUE (une reprise légitime redépose
    sur la même entrée). Sans ce verrou, deux passages de veille qui se
    chevauchent (relance manuelle `--once` pendant que le conteneur tourne,
    redémarrage à cheval sur un cycle) peuvent tous deux passer le SELECT
    avant que l'un des deux ne commite — chacun déposant alors un job pour la
    même entrée (revue technique du 15/09, même famille que l'incident (a)
    que `POST /api/v1/depots` referme côté web via une contrainte UNIQUE).

    La variante session (`pg_advisory_lock` + `pg_advisory_unlock` explicite
    en `finally`) a été essayée puis abandonnée le 15/09 : un processus tué
    avant d'atteindre son `pg_advisory_unlock` laisse le verrou tenu jusqu'à
    ce que Postgres remarque la connexion morte — observé en pratique
    (backend resté `idle` après un `COMMIT`, verrou toujours `granted`,
    bloquant indéfiniment tout passage suivant sur la même entrée). La
    variante transaction n'a pas ce risque : elle est relâchée au
    commit/rollback qui suit dans TOUS les cas, y compris quand la connexion
    est coupée avant — Postgres nettoie la transaction ouverte (et le verrou
    avec elle) dès qu'il détecte la coupure, sans dépendre d'un appel
    explicite qui pourrait ne jamais arriver.
    """
    import datetime as _dt

    from sqlalchemy import text

    from src.db.models import IngestionJob, IngestionProvenance

    deposes: List[str] = []
    deja_en_file: List[str] = []
    for entry in manifest.iter_entries():
        if entry.statut not in ("telecharge", "erreur"):
            continue

        if dry_run:
            existant = (
                db.query(IngestionJob)
                .filter(
                    IngestionJob.manifest_id == entry.id,
                    IngestionJob.status.in_([IngestionJob.STATUS_PENDING, IngestionJob.STATUS_RUNNING]),
                )
                .first()
            )
            (deja_en_file if existant is not None else deposes).append(entry.id)
            continue

        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:cle))"), {"cle": entry.id})
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
            # Referme la transaction ouverte par le verrou avant l'entrée
            # suivante — sinon il resterait tenu jusqu'au prochain dépôt réel.
            db.commit()
            continue

        # Provenance Postgres (§ 3.7 du plan « boîte de réception ») : avant
        # ce correctif, seul POST /api/v1/depots l'écrivait — la veille ne
        # déposait qu'un IngestionJob, sans jamais renseigner d'où venait le
        # fichier. Idempotent par construction (une ligne par manifest_id,
        # contrainte UNIQUE) : une entrée "erreur" redéposée après un échec
        # définitif reste éligible indéfiniment (§ eligibilité ci-dessus),
        # donc revue par ce même passage plusieurs fois — sans ce garde, le
        # second passage violerait la contrainte.
        provenance_existante = (
            db.query(IngestionProvenance)
            .filter(IngestionProvenance.manifest_id == entry.id)
            .first()
        )
        if provenance_existante is None:
            fetched_at = None
            if entry.fetched_at:
                try:
                    fetched_at = _dt.datetime.fromisoformat(entry.fetched_at)
                except ValueError:
                    fetched_at = None
            jo_date = None
            if entry.jo_date:
                try:
                    jo_date = _dt.date.fromisoformat(entry.jo_date)
                except ValueError:
                    jo_date = None
            db.add(IngestionProvenance(
                manifest_id=entry.id,
                type_source=entry.type_source,
                fichier=entry.fichier,
                statut=entry.statut,
                size_bytes=entry.size_bytes,
                source_url=entry.source_url,
                jo_numero=entry.jo_numero,
                jo_date=jo_date,
                jo_annee=entry.jo_annee,
                titre=entry.titre,
                sha256=entry.sha256,
                fetched_at=fetched_at,
                retroactif=entry.retroactif,
                variantes_multiples=entry.variantes_multiples,
                evenements=[{"quand": entry.fetched_at, "quoi": "veille", "par": "veille-corpus"}],
            ))

        job = IngestionJob(kind=IngestionJob.KIND_VEILLE, manifest_id=entry.id, requested_by="veille-corpus")
        db.add(job)
        db.commit()  # relâche aussi le verrou (même transaction)
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
