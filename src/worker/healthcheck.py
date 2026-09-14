"""Ping « dead man's switch » du worker de la file `ingestion_jobs`
(mibeko-python#23, § L1 : « rien ne surveillait sa liveness jusqu'ici »).

Même mécanisme que `src.veille.healthcheck` (ping `/start` puis succès ou
`/fail`), instance séparée : le worker et la veille sont deux daemons
distincts (§ 3.4 du plan « boîte de réception »), chacun son URL de
healthcheck — l'absence de ping de l'un ne doit jamais masquer une panne de
l'autre. Désactivé par défaut (`WORKER_HEALTHCHECK_URL` vide).
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("worker.healthcheck")


def _url() -> str:
    return os.getenv("WORKER_HEALTHCHECK_URL", "").strip()


def ping(event: str = "") -> None:
    """Ping `<url><event>` (event = "", "/start" ou "/fail"). No-op si non
    configuré. Ne lève jamais : un ping raté n'est pas une panne du worker.
    """
    base = _url()
    if not base:
        return
    try:
        httpx.get(f"{base}{event}", timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("ping healthcheck %s échoué : %s", event or "(succès)", exc)
