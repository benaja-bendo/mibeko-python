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
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
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


class ManifestEntryIntrouvable(RuntimeError):
    """`ingestion_jobs.manifest_id` ne correspond à aucune entrée existante
    (manifeste renommé/supprimé entre le dépôt du job et son traitement)."""


def _find_entry(data_dir: Path, manifest_id: str) -> Tuple[Manifest, ManifestEntry]:
    """Localise l'entrée de manifeste d'un travail à partir de `manifest_id`
    (= `ManifestEntry.id`, ex. "sgg-jo/congo-jo-2026-13") : le segment avant
    le premier "/" est le nom du fichier manifeste
    (`data/manifests/<segment>.jsonl`) — convention déjà en vigueur côté
    veille (`src/veille/runner.py::SOURCE_MANIFESTE`) et
    `src/parsing/batch.py::artefact_paths`, jamais réinventée ici.
    """
    prefix = manifest_id.split("/", 1)[0]
    manifest_path = data_dir / "manifests" / f"{prefix}.jsonl"
    manifest = Manifest(manifest_path)
    entry = manifest.get(manifest_id)
    if entry is None:
        raise ManifestEntryIntrouvable(
            f"entrée de manifeste introuvable pour le job : {manifest_id!r} ({manifest_path})"
        )
    return manifest, entry


class _StepFailure(Exception):
    """Échec d'une étape du pipeline, déjà classifié — jamais une exception
    brute au-delà de `_do_parse_step`/`_do_structure_step`."""

    def __init__(self, message: str, error_class: str):
        super().__init__(message)
        self.error_class = error_class


def _classify_structuration_motif(motif: str) -> str:
    """Classe l'échec de `structure_document` à partir de son `motif` — la
    fonction ne renvoie qu'une chaîne, jamais l'exception d'origine (ses
    blocs `except Exception as exc` la stringifient déjà). Trois familles
    observées dans le code actuel de `src/structuration/structurer.py` :

    - un appel Mistral qui a échoué (réseau/quota, message "appel Mistral en
      échec") → transitoire, une relance peut réussir sans rien changer
      d'autre ;
    - une réponse Mistral reçue mais invalide pour le schéma (message
      "validation du schéma en échec", ex. nature introuvable), ou une date
      de consolidation introuvable pour un STOCK (message "date de
      consolidation introuvable") → information_manquante — le signalement
      `blocking` est déjà posé par `structure_document` lui-même, jamais un
      troisième appel identique (§ 3.6/L1 du plan « boîte de réception ») ;
    - tout le reste (markdown introuvable, échec d'insertion DB, parseur en
      échec) → definitive, une donnée ou un bug, pas un incident réseau.
    """
    if "appel Mistral en échec" in motif:
        return IngestionJob.ERROR_TRANSITOIRE
    if "validation du schéma en échec" in motif or "date de consolidation introuvable" in motif:
        return IngestionJob.ERROR_INFORMATION_MANQUANTE
    return IngestionJob.ERROR_DEFINITIVE


def _do_parse_step(
    data_dir: Path,
    manifest: Manifest,
    entry: ManifestEntry,
    process_entry_fn: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    """Exécute l'étape `parse` (triage natif → OCR) pour `entry`, resynchronise
    son statut dans le manifeste, et renvoie le fragment à fusionner dans
    `ingestion_jobs.result`. Lève `_StepFailure` (déjà classifiée) sur échec —
    jamais d'exception brute hors de cette fonction.
    """
    try:
        result = process_entry_fn(data_dir, entry, force=False)
    except Exception as exc:  # PDF manquant (ParsingError), etc.
        raise _StepFailure(
            str(exc) or f"{type(exc).__name__} (sans message)", classify_error(exc)
        ) from exc

    if result.get("methode") == "erreur":
        message = result.get("erreur") or "échec du triage/OCR (sans message)"
        entry.statut = "erreur"
        entry.add_event("erreur_parsing", "MibekoBot/worker", detail=message)
        manifest.save()
        # process_entry avale l'exception d'origine dans une chaîne (cf. son
        # propre bloc except dans src/parsing/batch.py) : impossible de la
        # reclassifier précisément depuis ce point. Le module la documente
        # déjà comme presque toujours réseau/quota (moteur OCR distant).
        raise _StepFailure(message, IngestionJob.ERROR_TRANSITOIRE)

    # Resynchronise le manifeste même sur une entrée "sautée" (déjà traitée) :
    # même logique que src/parsing/batch.py::run_batch, pour qu'un statut
    # manifeste périmé (ex. reset manuel vers "telecharge") ne reste jamais
    # bloqué alors que les artefacts disque sont à jour.
    if entry.statut != "parse":
        entry.statut = "parse"
        entry.add_event("parse", "MibekoBot/worker", detail=result.get("methode") or "déjà traité")
        manifest.save()

    return {"parse": result}


def _do_structure_step(
    db: Session,
    data_dir: Path,
    manifest: Manifest,
    entry: ManifestEntry,
    structure_document_fn: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    """Exécute l'étape `structure` (parseur + Mistral + insertion DB) pour
    `entry`. Même contrat que `_do_parse_step` : lève `_StepFailure`
    (classifiée) sur échec, ne renvoie que sur succès (`structure` ou
    `deja_existant`).
    """
    try:
        result = structure_document_fn(db, data_dir, entry, dry_run=False)
    except Exception as exc:
        raise _StepFailure(
            str(exc) or f"{type(exc).__name__} (sans message)", classify_error(exc)
        ) from exc

    statut = result.get("statut")
    if statut == "erreur":
        motif = result.get("motif") or "échec de structuration (sans motif)"
        raise _StepFailure(motif, _classify_structuration_motif(motif))

    # `document_ids` (JO scindé en actes) ou `document_id` seul (acte isolé,
    # ou "déjà existant") — jamais confondus (§ 3.6, identité n°2 : ce que le
    # job a réellement écrit, pour qu'une reprise sache quoi compléter).
    document_ids = result.get("document_ids") or (
        [str(result["document_id"])] if result.get("document_id") else []
    )
    entry.statut = "structure"
    entry.add_event(
        "structure", "MibekoBot/worker",
        detail="déjà existant" if statut == "deja_existant" else (",".join(document_ids) or None),
    )
    manifest.save()

    return {"structure": {"statut": statut, "document_ids": document_ids}}


# Intervalle de renouvellement du bail pendant un traitement long (OCR, appel
# LLM) — § 3.3/L1 du plan « boîte de réception » : "toutes les 5 minutes sur
# un bail de 30". Doit rester nettement plus court que WORKER_LEASE_MINUTES
# pour qu'un job simplement lent (pas mort) ne se fasse jamais déposséder par
# un autre worker pendant qu'il travaille encore dessus.
WORKER_LEASE_RENEWAL_SECONDS = int(os.getenv("WORKER_LEASE_RENEWAL_SECONDS", "300"))


def _start_lease_renewal(job_id, fencing_token: int, stop: threading.Event) -> threading.Thread:
    """Démarre le thread de renouvellement périodique du bail. Session
    Postgres dédiée à chaque tour : une session SQLAlchemy n'est pas
    thread-safe, jamais partagée avec le thread principal qui exécute
    `process_entry`/`structure_document`. `SimpleNamespace(id=job_id)` évite
    une requête de lecture superflue — `renew_lease` n'utilise que `.id`.
    """

    def _loop() -> None:
        from src.db.database import SessionLocal

        while not stop.wait(WORKER_LEASE_RENEWAL_SECONDS):
            session = SessionLocal()
            try:
                if not renew_lease(session, SimpleNamespace(id=job_id), fencing_token):
                    logger.warning(
                        "job %s : bail non renouvelé (jeton %s expiré) — un autre worker a repris la main",
                        job_id, fencing_token,
                    )
            except Exception:
                logger.exception("job %s : échec du renouvellement de bail (retenté au prochain tour)", job_id)
            finally:
                session.close()

    thread = threading.Thread(target=_loop, name=f"lease-renewal-{job_id}", daemon=True)
    thread.start()
    return thread


def _finalize_failure(
    db: Session,
    job_id,
    fencing_token: int,
    failure: _StepFailure,
    attempts_before: int,
    max_attempts: int,
    result: Dict[str, Any],
) -> bool:
    """Écrit l'échec d'une étape. Une erreur `transitoire` dont il reste des
    tentatives repasse en `pending` (éligible à une nouvelle réservation) —
    `definitive`/`information_manquante` vont directement à `failed`, jamais
    de réessai (§ L1 du plan : "definitive → failed direct, pas de réessai
    inutile" ; "information_manquante → ne redemande jamais la même chose au
    LLM sans signal nouveau").

    Limite assumée : le schéma `ingestion_jobs` (migration Laravel
    dashboard#140) n'a pas de colonne de planification de relance
    (`locked_at`/`locked_by` ne portent que le bail, pas un "pas avant").
    Un job repassé `pending` est donc IMMÉDIATEMENT éligible à
    `reserve_job` — sans colonne dédiée, le backoff exponentiel
    (`backoff_seconds`) est appliqué comme un délai avant la PROCHAINE
    réservation du worker (cf. la boucle `main.py::worker`), pas comme un
    filtre par job. Correct dans le régime visé par le plan (§ 3.3 : "quelques
    travaux par jour, un seul worker") ; passer à une vraie planification par
    job demanderait une colonne dédiée, hors périmètre de ce ticket.
    """
    attempts_after = attempts_before + 1
    if failure.error_class == IngestionJob.ERROR_TRANSITOIRE and attempts_after < max_attempts:
        status = IngestionJob.STATUS_PENDING
    else:
        status = IngestionJob.STATUS_FAILED
    return finalize_job(
        db, job_id, fencing_token,
        status=status, last_error=str(failure), error_class=failure.error_class,
        increment_attempts=True, result=result,
    )


def process_job(
    db: Session,
    data_dir: Path,
    job: IngestionJob,
    *,
    process_entry_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    structure_document_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> bool:
    """Traite un travail déjà réservé (`job.status == running`, tel que rendu
    par `reserve_job`) jusqu'à son terme (`done`/`failed`/`pending` pour
    retentative) ou jusqu'à ce qu'un autre worker reprenne la main (jeton
    expiré en cours de route).

    Reprend depuis `job.step` : un travail dont l'étape `parse` a déjà réussi
    (coupure avant `structure`) ne rejoue jamais le triage/OCR — `job.result`
    (§ 3.6, identité n°2) est la source de vérité de ce qui a déjà été écrit,
    jamais une clé recalculée depuis une sortie LLM. L'étape `controle`
    (L2/L4 du plan, non livrées) n'existe pas encore : un job dont la
    structuration a réussi est considéré terminé pour ce lot.

    Renvoie `True` si CE worker a mené le travail à son terme (jeton encore
    valide à la dernière écriture), `False` si le jeton a expiré en cours de
    route — plus rien à faire, un autre worker a la main (cf. `finalize_job`).

    `process_entry_fn`/`structure_document_fn` : injection pour les tests,
    mêmes signatures que `src.parsing.batch.process_entry` et
    `src.structuration.structurer.structure_document`. `None` = implémentations
    réelles, importées ici (pas au niveau module) pour ne payer le couplage
    MinIO/FastAPI de `structurer.py` (cf. commit 712133c) qu'à l'exécution,
    jamais à la seule importation de ce module.
    """
    if process_entry_fn is None:
        from src.parsing.batch import process_entry as process_entry_fn
    if structure_document_fn is None:
        from src.structuration.structurer import structure_document as structure_document_fn

    job_id = job.id
    fencing_token = job.fencing_token
    step = job.step
    max_attempts = job.max_attempts
    job_result: Dict[str, Any] = dict(job.result or {})

    stop_renewal = threading.Event()
    renewal_thread = _start_lease_renewal(job_id, fencing_token, stop_renewal)
    try:
        try:
            manifest, entry = _find_entry(data_dir, job.manifest_id)
        except ManifestEntryIntrouvable as exc:
            # Jamais transitoire : un manifeste absent ne réapparaît pas tout
            # seul au prochain essai.
            return finalize_job(
                db, job_id, fencing_token, status=IngestionJob.STATUS_FAILED,
                last_error=str(exc), error_class=IngestionJob.ERROR_DEFINITIVE,
                increment_attempts=True, result=job_result,
            )

        if step == IngestionJob.STEP_RECU:
            try:
                job_result.update(_do_parse_step(data_dir, manifest, entry, process_entry_fn))
            except _StepFailure as failure:
                return _finalize_failure(db, job_id, fencing_token, failure, job.attempts, max_attempts, job_result)

            ok = finalize_job(
                db, job_id, fencing_token, status=IngestionJob.STATUS_RUNNING,
                step=IngestionJob.STEP_PARSE, result=job_result,
            )
            if not ok:
                return False
            step = IngestionJob.STEP_PARSE

        if step == IngestionJob.STEP_PARSE:
            try:
                job_result.update(_do_structure_step(db, data_dir, manifest, entry, structure_document_fn))
            except _StepFailure as failure:
                return _finalize_failure(db, job_id, fencing_token, failure, job.attempts, max_attempts, job_result)

            return finalize_job(
                db, job_id, fencing_token, status=IngestionJob.STATUS_DONE,
                step=IngestionJob.STEP_TERMINE, result=job_result,
            )

        # step déjà structure/controle/termine (ex. reprise après incident
        # pendant une étape de contrôle qui n'existe pas encore, L2/L4 hors
        # périmètre) : rien de plus à faire pour ce lot.
        return finalize_job(
            db, job_id, fencing_token, status=IngestionJob.STATUS_DONE,
            step=IngestionJob.STEP_TERMINE, result=job_result,
        )
    finally:
        stop_renewal.set()
        renewal_thread.join(timeout=5)
