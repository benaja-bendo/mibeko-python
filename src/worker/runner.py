"""Réservation, bail et jeton de tentative pour la file `ingestion_jobs`
(mibeko-python#23).

Trois opérations, chacune sa propre transaction courte — jamais une
transaction ouverte pendant l'OCR ou l'appel LLM (§ 3.6/§ L1 du plan) :

1. `reserve_job` : verrouille et prend en charge UN travail éligible
   (`pending`, ou `running` dont le bail a expiré — reprise après incident
   worker), jamais deux fois le même par deux workers concurrents
   (`FOR UPDATE SKIP LOCKED`).
2. `renew_lease` : prolonge le bail pendant un traitement long, sans jamais
   écrire si un autre worker a entre-temps repris le travail.
3. `finalize_job` : écrit le résultat final — vérifie le jeton juste avant,
   abandonne sans rien écrire s'il a changé (un autre worker a la main).
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.db.models import IngestionJob

logger = logging.getLogger("mibeko.worker")

# Durée du bail avant qu'un travail "running" redevienne éligible à la
# réservation par un AUTRE worker (incident : le worker qui le détenait a
# planté sans jamais atteindre finalize_job). Doit rester nettement plus
# grande que l'intervalle de renouvellement (cf. WORKER_LEASE_RENEWAL_SECONDS
# dans le module appelant) pour qu'un worker vivant ne se fasse jamais
# déposséder de son propre travail.
WORKER_LEASE_MINUTES = int(os.getenv("WORKER_LEASE_MINUTES", "30"))

# hôte:pid — jamais un identifiant opaque : un incident doit pouvoir remonter
# au processus exact qui détenait (ou détient) le bail.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class FencingTokenExpiredError(RuntimeError):
    """Le jeton de bail a changé depuis la réservation : un autre worker a
    repris ce travail. Ne jamais écrire dans ce cas — c'est précisément ce
    que le jeton existe pour empêcher."""


def reserve_job(db: Session) -> Optional[IngestionJob]:
    """Réserve un travail éligible : `pending`, ou `running` dont le bail a
    expiré. Transaction courte : verrouille, marque `running`, incrémente le
    jeton, commit immédiatement — ne reste jamais ouverte pendant le
    traitement (cf. module docstring).

    `order_by(created_at)` : les travaux les plus anciens d'abord, pour
    qu'un dépôt ne double jamais un travail plus récent en attente.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=WORKER_LEASE_MINUTES)

    job = (
        db.query(IngestionJob)
        .filter(
            or_(
                IngestionJob.status == IngestionJob.STATUS_PENDING,
                and_(
                    IngestionJob.status == IngestionJob.STATUS_RUNNING,
                    IngestionJob.locked_at.isnot(None),
                    IngestionJob.locked_at < cutoff,
                ),
            )
        )
        .order_by(IngestionJob.created_at)
        # `populate_existing()` : sans lui, un objet déjà chargé dans la
        # session (même identité, non expiré) garde ses valeurs EN MÉMOIRE au
        # lieu d'être rafraîchi par cette requête — un autre worker/processus
        # a pu modifier la ligne entre-temps, il faut la relire pour de vrai.
        .populate_existing()
        .with_for_update(skip_locked=True)
        .first()
    )
    if job is None:
        return None

    if job.status == IngestionJob.STATUS_RUNNING:
        logger.warning(
            "job %s : bail expiré (dernier détenteur %s, verrouillé le %s) — repris par %s",
            job.id, job.locked_by, job.locked_at, WORKER_ID,
        )

    job.status = IngestionJob.STATUS_RUNNING
    job.locked_at = datetime.utcnow()
    job.locked_by = WORKER_ID
    job.fencing_token = (job.fencing_token or 0) + 1
    db.commit()
    db.refresh(job)
    return job


def renew_lease(db: Session, job: IngestionJob, fencing_token: int) -> bool:
    """Prolonge le bail d'un travail en cours de traitement. Renvoie `False`
    (sans rien écrire) si le jeton actuel en base diffère de celui détenu par
    l'appelant : un autre worker a déjà repris ce travail, il ne faut plus y
    toucher.
    """
    current = (
        db.query(IngestionJob)
        .filter(IngestionJob.id == job.id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if current is None or current.fencing_token != fencing_token:
        db.rollback()
        return False

    current.locked_at = datetime.utcnow()
    db.commit()
    return True


def finalize_job(
    db: Session,
    job_id,
    fencing_token: int,
    *,
    status: str,
    step: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    last_error: Optional[str] = None,
    error_class: Optional[str] = None,
    increment_attempts: bool = False,
) -> bool:
    """Écrit l'état final d'un travail — vérifie le jeton juste AVANT
    d'écrire (pas seulement à la réservation) : abandonne sans rien écrire si
    un autre worker a repris le travail entre-temps (bail expiré pendant un
    traitement anormalement long). Renvoie `True` si l'écriture a eu lieu.

    `last_error` jamais vide sur un échec (piège déjà rencontré avec
    `httpx.ReadTimeout` sans message) — c'est la responsabilité de
    l'appelant, cf. `classify_error`.
    """
    current = (
        db.query(IngestionJob)
        .filter(IngestionJob.id == job_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if current is None or current.fencing_token != fencing_token:
        db.rollback()
        logger.warning(
            "job %s : jeton expiré à la finalisation (attendu %s) — écriture abandonnée, "
            "un autre worker a la main",
            job_id, fencing_token,
        )
        return False

    current.status = status
    if step is not None:
        current.step = step
    if result is not None:
        current.result = result
    current.last_error = last_error
    current.error_class = error_class
    if increment_attempts:
        current.attempts = (current.attempts or 0) + 1
    db.commit()
    return True


# Erreurs réseau/quota transitoires : à réessayer avec un backoff exponentiel,
# jamais classées "definitive" — une panne réseau passagère ne dit rien sur
# la validité du document.
_TRANSIENT_EXCEPTION_NAMES = frozenset({
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutError",
    "TimeoutException",
    "RemoteProtocolError",
    "PoolTimeout",
})


def classify_error(exc: BaseException) -> str:
    """Classe une exception en `transitoire` (réseau/quota — réessayer) ou
    `definitive` (échec de validation ou autre — inutile de réessayer à
    l'identique). `information_manquante` (donnée absente que le LLM ne peut
    pas deviner) n'est jamais déduite d'une exception : c'est un résultat
    NORMAL de la structuration (ex. nature introuvable), posé explicitement
    par l'appelant, jamais levé.

    Comparaison par NOM de classe plutôt que `isinstance` : évite d'importer
    httpx ici pour un module qui doit rester utilisable sans dépendance
    réseau directe, et couvre les exceptions enveloppées par des clients tiers
    (MinIO, Mistral) qui réutilisent ces mêmes noms.
    """
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSIENT_EXCEPTION_NAMES:
            return IngestionJob.ERROR_TRANSITOIRE
    return IngestionJob.ERROR_DEFINITIVE


def backoff_seconds(attempts: int, base: float = 30.0, cap: float = 1800.0) -> float:
    """Palier de backoff exponentiel pour une erreur `transitoire` : 30 s,
    60 s, 120 s… plafonné à 30 min. `attempts` = tentatives déjà faites
    (1 après le premier échec)."""
    return min(cap, base * (2 ** max(0, attempts - 1)))
